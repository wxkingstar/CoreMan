"""盯消息的一轮：只记基线的第一轮、分级入箱、压着、去重、逐个探测、重点群与话题、撤回，

以及什么时候不轮询、每位助理一个短事务、日志里没有内容。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from structlog.testing import capture_logs

from coreman.core.assistants import followups, sent, settings, watch
from coreman.core.assistants.watch_grade import Context, Item, Level
from coreman.core.db.models import AssistantFollowup, Bot, FeishuPersonalGrant, RelayServer
from coreman.core.db.session import make_session_factory
from coreman.core.timeutils import utcnow
from tests.integration.assistant_helpers import SECRET, raw
from tests.integration.watch_helpers import (
    Hit,
    Watched,
    assistant,
    inbox_rows,
    install,
    ms,
    primed,
    tick,
    watch_row,
)

pytestmark = pytest.mark.usefixtures("assistant_master")
LI = {"chat_id": "oc_li", "chat_mode": "p2p"}
TEAM = {"chat_id": "oc_team", "name": "项目群", "chat_mode": "group"}
# 行被别的事务锁着时这两句会立刻报错。
GRANT_UNLOCKED = "SELECT 1 FROM feishu_personal_grants WHERE bot_id = :id FOR UPDATE NOWAIT"
WATCH_UNLOCKED = "SELECT 1 FROM assistant_watch WHERE bot_id = :id FOR UPDATE NOWAIT"


def base_time() -> datetime:
    return utcnow().replace(microsecond=0)


def after(base: datetime, seconds: int) -> datetime:
    return base + timedelta(seconds=seconds)


async def messages(session: AsyncSession, who: Watched) -> list[Any]:
    """别人发来的消息条目（不含所有者自己说的话）。"""
    return [row for row in await inbox_rows(session, who) if row.kind == "message"]


async def owner_said(session: AsyncSession, who: Watched) -> list[Any]:
    return [row for row in await inbox_rows(session, who) if row.kind == "owner_said"]


def test_grade_is_exported_and_polls_are_staggered() -> None:
    one = Item("oc_li", "p2p", "om_1", "ou_li", "user", 1)
    assert watch.grade(one, settings.AssistantSettings(open_id="ou_o"), Context(now=utcnow())) is (
        Level.IMMEDIATE
    )
    now = datetime(2026, 11, 16, 2, 0, 30, 500000, tzinfo=UTC)
    for offset in (0, 15, 30, 31, 59):
        at = watch.slot(now, 60, offset)
        assert now < at <= now + timedelta(seconds=60)
        assert int(at.timestamp()) % 60 == offset and at.microsecond == 0
    assert watch.phase(uuid.UUID(int=61)) == 1
    assert watch.phase(uuid.UUID(int=61), 120) == 61


async def test_new_assistants_are_enrolled_and_the_first_round_only_sets_a_baseline(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    factory = make_session_factory(db_engine)
    base = base_time()
    api.chats = [LI, TEAM]
    api.say("oc_li", "om_old", after(base, -3600))
    api.say("oc_li", "om_now", after(base, 5))
    counts = await tick(factory, base)
    assert counts["assistant_watch_enrolled"] == 1 and api.hits == []
    row = await watch_row(db_session, who)
    assert row.state == "idle" and row.cursor_enc == {} and row.head_enc is None
    assert base < row.next_poll_at <= after(base, 60)
    first = row.next_poll_at
    counts = await tick(factory, first)
    assert counts["assistant_watch_polled"] == 1 and counts["assistant_watch_items"] == 0
    # 第一轮只列一次会话：记下顺序与开始的时刻，不读任何历史。
    assert api.paths() == ["/im/v1/chats"]
    row = await watch_row(db_session, who)
    assert row.state == "polling" and row.last_ok_at == first and row.last_error is None
    assert row.cursor_enc == {"since": ms(first), "chats": {}, "todo": []}
    assert row.head_enc == ["oc_li", "oc_team"]
    assert first < row.next_poll_at <= after(first, 60)
    assert await inbox_rows(db_session, who) == []
    # 再登记一次不会多出一行。
    assert (await tick(factory, first))["assistant_watch_enrolled"] == 0


async def test_new_messages_are_graded_into_a_sealed_inbox(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    mine = {"chat_id": who.chat_id, "chat_mode": "p2p"}
    api.chats = [LI, TEAM, mine]
    api.say("oc_li", "om_1", after(base, 10), text=f"周五能给报价吗？{SECRET}")
    api.say("oc_team", "om_2", after(base, 11))
    api.say("oc_team", "om_3", after(base, 12), sender="cli_other", kind="app")
    api.say(who.chat_id, "om_4", after(base, 13), sender=who.open_id)
    now = after(base, 30)
    with capture_logs() as logs:
        counts = await tick(make_session_factory(db_engine), now)
    assert counts["assistant_watch_polled"] == 1 and counts["assistant_watch_items"] == 2
    # 列一次会话、读两个会话的历史、查一次待交回的条目有没有被撤回；所有者与助理自己的私聊不读。
    assert api.paths() == [
        "/im/v1/chats",
        "/im/v1/messages",
        "/im/v1/messages",
        "/im/v1/messages/mget",
    ]
    assert api.read(who.chat_id) == []
    private, group = await inbox_rows(db_session, who)
    assert (private.message_id, private.level, private.hold_until) == ("om_1", "immediate", None)
    assert private.chat_id == "oc_li" and private.status == "pending"
    assert SECRET in private.summary_enc["text"]  # type: ignore[index]
    assert (group.message_id, group.level) == ("om_2", "batch")
    assert group.hold_until == now + timedelta(minutes=5)
    assert group.summary_enc["chat_name"] == "项目群"  # type: ignore[index]
    # 库里与日志里都没有消息内容；会话顺序是密文。
    stored = str(
        await raw(db_session, "SELECT string_agg(summary_enc::text, '') FROM assistant_inbox")
    )
    head = str(await raw(db_session, "SELECT head_enc::text FROM assistant_watch"))
    assert SECRET not in stored and "报价" not in stored
    assert "oc_team" not in head and "enc:v2:d1:" in head
    # 游标（和谁有会话、读到哪条消息）同样是密文。
    cursor = str(await raw(db_session, "SELECT cursor_enc::text FROM assistant_watch"))
    assert "oc_li" not in cursor and "om_1" not in cursor and "enc:v2:d1:" in cursor
    assert SECRET not in json.dumps(logs, ensure_ascii=False, default=str)
    row = await watch_row(db_session, who)
    assert row.head_enc == ["oc_li", "oc_team", who.chat_id] and row.cursor_enc["todo"] == []
    assert row.cursor_enc["chats"]["oc_li"] == {
        "k": "p2p",
        "t": ms(after(base, 10)),
        "m": ["om_1"],
        "th": {},
    }
    assert row.cursor_enc["chats"]["oc_team"]["t"] == ms(after(base, 12))
    assert who.chat_id not in row.cursor_enc["chats"]
    assert (row.state, row.failures, row.last_ok_at) == ("polling", 0, now)


async def test_a_card_from_someone_is_boxed_as_readable_text_and_read_with_card_content(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [LI]
    card = {
        "schema": "2.0",
        "header": {"title": {"tag": "plain_text", "content": "报销提醒"}},
        "body": {
            "elements": [
                {"tag": "markdown", "content": "张三的差旅报销待处理"},
                {"tag": "img", "img_key": "img_v3_x"},
            ]
        },
    }
    api.say(
        "oc_li",
        "om_card",
        after(base, 10),
        msg_type="interactive",
        body={"content": json.dumps(card, ensure_ascii=False)},
    )
    counts = await tick(make_session_factory(db_engine), after(base, 30))
    assert counts["assistant_watch_items"] == 1
    (row,) = await messages(db_session, who)
    text_ = row.summary_enc["text"]  # type: ignore[index]
    assert text_.startswith("[卡片] ") and "张三的差旅报销待处理" in text_
    assert "含 1 张图" in text_
    (hit,) = api.read("oc_li")
    assert hit.params["card_msg_content_type"] == "user_card_content"


async def test_chat_kind_is_looked_up_once_when_the_list_does_not_say(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [{"chat_id": "oc_li"}]
    api.modes["oc_li"] = "p2p"
    api.say("oc_li", "om_1", after(base, 10))
    factory = make_session_factory(db_engine)
    await tick(factory, after(base, 30))
    api.say("oc_li", "om_2", after(base, 40))
    await tick(factory, after(base, 91))
    assert api.paths().count("/im/v1/chats/oc_li") == 1
    assert [row.level for row in await inbox_rows(db_session, who)] == ["immediate", "immediate"]


async def test_the_owners_own_message_is_not_boxed_and_holds_the_chat(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [LI]
    api.say("oc_li", "om_1", after(base, 10))
    api.say("oc_li", "om_2", after(base, 20), sender=who.open_id, text=f"我来回。{SECRET}")
    factory = make_session_factory(db_engine)
    await tick(factory, after(base, 30))
    release = after(base, 20) + timedelta(minutes=10)
    [held] = await messages(db_session, who)
    # 所有者自己发的不入 message 条目；先到的那条被压到他这次发言之后 10 分钟。
    assert (held.message_id, held.level, held.hold_until) == ("om_1", "hold", release)
    assert (await watch_row(db_session, who)).cursor_enc["chats"]["oc_li"]["o"] == ms(
        after(base, 20)
    )
    # 10 分钟内这个会话的新消息直接按「压着」入箱。
    api.say("oc_li", "om_3", after(base, 70))
    await tick(factory, after(base, 91))
    assert [
        (row.message_id, row.level, row.hold_until) for row in await messages(db_session, who)
    ] == [
        ("om_1", "hold", release),
        ("om_3", "hold", release),
    ]
    # 他停下满 10 分钟之后来的，照常分级。
    api.say("oc_li", "om_4", after(base, 700))
    await tick(factory, after(base, 760))
    last = (await messages(db_session, who))[-1]
    assert (last.message_id, last.level, last.hold_until) == ("om_4", "immediate", None)


async def test_the_same_message_is_boxed_once_across_reruns_and_overlap(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [LI]
    api.say("oc_li", "om_1", after(base, 10))
    factory = make_session_factory(db_engine)
    await tick(factory, after(base, 30))
    # 重跑：游标与顺序退回到这一轮之前（比如上一任 scheduler 的这一轮没来得及提交就没了）。
    await db_session.execute(
        text(
            "UPDATE assistant_watch SET cursor_enc = CAST(:cursor AS jsonb), head_enc = NULL,"
            " next_poll_at = :due"
        ),
        {"cursor": json.dumps({"since": ms(base), "chats": {}, "todo": []}), "due": base},
    )
    await db_session.commit()
    counts = await tick(factory, after(base, 31))
    assert counts["assistant_watch_polled"] == 1 and counts["assistant_watch_items"] == 0
    # 换主重叠：两个 scheduler 同时认领同一位助理，只有一个轮询。
    await db_session.execute(text("UPDATE assistant_watch SET next_poll_at = :due"), {"due": base})
    await db_session.commit()
    api.hits.clear()
    both = await asyncio.gather(tick(factory, after(base, 32)), tick(factory, after(base, 32)))
    assert sum(counts["assistant_watch_polled"] for counts in both) == 1
    assert api.paths().count("/im/v1/chats") == 1
    assert [row.message_id for row in await inbox_rows(db_session, who)] == ["om_1"]


async def test_an_unchanged_order_is_probed_from_the_top_until_a_quiet_chat(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    order = ["oc_a", "oc_b", "oc_c", "oc_d"]
    await primed(db_session, who, base, head=order)
    api.chats = [{"chat_id": chat, "chat_mode": "group"} for chat in order]
    factory = make_session_factory(db_engine)
    # 什么都没发生：只探排第一的会话。
    await tick(factory, after(base, 30))
    assert api.paths() == ["/im/v1/chats", "/im/v1/messages"] and len(api.read("oc_a")) == 1
    # b 来了消息、随后 a 也来了：顺序没变。从头探到第一个没有新消息的会话（c）为止。
    api.say("oc_b", "om_1", after(base, 40))
    api.say("oc_a", "om_2", after(base, 50))
    api.hits.clear()
    counts = await tick(factory, after(base, 91))
    assert counts["assistant_watch_items"] == 2
    assert [len(api.read(chat)) for chat in order] == [1, 1, 1, 0]
    # d 来了消息排到最前：读 d，再照例探一下 a。
    api.say("oc_d", "om_3", after(base, 100))
    api.chats = [api.chats[3], *api.chats[:3]]
    api.hits.clear()
    await tick(factory, after(base, 152))
    assert [len(api.read(chat)) for chat in order] == [1, 0, 0, 1]
    # 入箱的先后就是读的先后：先探到 a 的那条，再是 b 的。
    assert [row.message_id for row in await inbox_rows(db_session, who)] == ["om_2", "om_1", "om_3"]


async def test_focus_chats_are_read_every_round_with_their_threads_and_ignored_ones_never(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    prefs = await settings.get(db_session, who.bot.id)
    prefs.focus_chats_enc, prefs.ignored_chats_enc = ["oc_focus"], ["oc_noise"]
    await db_session.commit()
    base = base_time()
    order = ["oc_noise", "oc_a", "oc_focus"]
    await primed(db_session, who, base, head=order)
    api.chats = [{"chat_id": chat, "chat_mode": "group"} for chat in order]
    api.say("oc_noise", "om_n", after(base, 5))
    api.say("oc_focus", "om_root", after(base, 10), thread_id="omt_1")
    factory = make_session_factory(db_engine)
    await tick(factory, after(base, 30))
    # 忽略的会话不读，也不打断逐个探测（oc_a 照样被探）；重点群排在后面也读，并记下话题。
    assert api.read("oc_noise") == [] and len(api.read("oc_a")) == 1
    assert len(api.read("oc_focus")) == 1 and len(api.read("omt_1")) == 1
    [root] = await inbox_rows(db_session, who)
    assert (root.message_id, root.level, root.thread_id) == ("om_root", "immediate", "omt_1")
    # 话题里的回复不出现在主线、群的排序也没变：重点群每轮直接读，照样盯到。
    api.say("oc_focus", "om_reply", after(base, 60), thread_id="omt_1", _thread_only=True)
    api.hits.clear()
    await tick(factory, after(base, 91))
    assert len(api.read("oc_focus")) == 1 and len(api.read("omt_1")) == 1
    reply = (await inbox_rows(db_session, who))[-1]
    assert (reply.message_id, reply.level, reply.thread_id) == ("om_reply", "immediate", "omt_1")
    assert (await watch_row(db_session, who)).cursor_enc["chats"]["oc_focus"]["th"] == {
        "omt_1": ms(after(base, 60))
    }


async def test_a_recalled_message_voids_its_pending_item_and_closes_followups(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [LI]
    sent = api.say("oc_li", "om_1", after(base, 10))
    factory = make_session_factory(db_engine)
    await tick(factory, after(base, 30))
    followup = await followups.add(
        db_session, who.bot.id, category="promised", title="回李四报价", origin_message_id="om_1"
    )
    await db_session.commit()
    sent["deleted"] = True
    await tick(factory, after(base, 91))
    [row] = await inbox_rows(db_session, who)
    assert (row.status, row.void_code) == ("void", "recalled")
    closed = await db_session.get(AssistantFollowup, followup.id, populate_existing=True)
    assert closed is not None and (closed.status, closed.close_code) == (
        "dropped",
        "origin_recalled",
    )
    # 发出后在两轮之间就撤回的消息：读到时已经带撤回标记，不入箱。
    api.say("oc_li", "om_2", after(base, 100), deleted=True)
    await tick(factory, after(base, 152))
    assert [item.message_id for item in await inbox_rows(db_session, who)] == ["om_1"]


async def _off_shift(session: AsyncSession, who: Watched) -> None:
    (await settings.get(session, who.bot.id)).shift_override = "off"


async def _cold(session: AsyncSession, who: Watched) -> None:
    (await settings.get(session, who.bot.id)).cold_start_done_at = None


async def _paused(session: AsyncSession, who: Watched) -> None:
    relay = await session.get(RelayServer, who.bot.relay_server_id)
    assert relay is not None
    relay.assistant_pool = False


async def _disabled(session: AsyncSession, who: Watched) -> None:
    bot = await session.get(Bot, who.bot.id)
    assert bot is not None
    bot.enabled = False


async def _deprovisioning(session: AsyncSession, who: Watched) -> None:
    bot = await session.get(Bot, who.bot.id)
    assert bot is not None
    bot.assistant_state = "deprovisioning"


async def _no_identity(session: AsyncSession, who: Watched) -> None:
    (await settings.get(session, who.bot.id)).chat_id = ""


async def _revoked(session: AsyncSession, who: Watched) -> None:
    grant = await session.get(FeishuPersonalGrant, (who.bot.id, who.owner.id))
    assert grant is not None
    grant.status, grant.token_enc = "revoked", None


async def _other_identity(session: AsyncSession, who: Watched) -> None:
    (await settings.get(session, who.bot.id)).open_id = "ou_someone_else"


@pytest.mark.parametrize(
    ("change", "state", "code"),
    [
        (_off_shift, "idle", None),
        (_cold, "idle", "cold_start_pending"),
        (_paused, "paused", "runtime_not_allowed"),
        (_disabled, "idle", None),
        (_deprovisioning, "idle", None),
        (_no_identity, "idle", "identity_missing"),
        (_revoked, "backoff", "authorization_required"),
        (_other_identity, "backoff", "personal_grant_required"),
    ],
)
async def test_nothing_is_polled_when(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    change: Any,
    state: str,
    code: str | None,
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    await change(db_session, who)
    await db_session.commit()
    api.chats = [LI]
    api.say("oc_li", "om_1", after(base, 10))
    counts = await tick(make_session_factory(db_engine), after(base, 30))
    assert api.hits == [] and api.refreshes == 0
    assert counts["assistant_watch_polled"] == 0 and counts["assistant_watch_items"] == 0
    row = await watch_row(db_session, who)
    assert (row.state, row.last_error) == (state, code)
    assert row.next_poll_at > after(base, 30) and row.last_ok_at is None
    assert await inbox_rows(db_session, who) == []
    if change is _other_identity:
        # 记下的身份与授权行对不上：不调飞书，也不动授权行（`service._locked` 会清空令牌）。
        grant = await db_session.get(
            FeishuPersonalGrant, (who.bot.id, who.owner.id), populate_existing=True
        )
        assert grant is not None and grant.status == "connected" and grant.token_enc


async def test_each_assistant_is_polled_in_its_own_short_transaction(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """轮到第二位时，第一位的事务已经提交：他的授权行与盯消息状态都没有被锁着。"""
    api = install(monkeypatch)
    first, second = await assistant(db_session, "a"), await assistant(db_session, "b")
    base = base_time()
    await primed(db_session, first, base)
    await primed(db_session, second, base)
    api.chats = [LI]
    factory = make_session_factory(db_engine)
    monkeypatch.setattr(watch, "CONCURRENCY", 1)
    order: list[str] = []
    seen: list[str] = []

    async def before(hit: Hit) -> None:
        if hit.path != "/im/v1/chats":
            return
        order.append(hit.token)
        if len(order) != 2:
            return
        earlier = first if order[0] == first.token else second
        async with factory() as other:
            for locked in (GRANT_UNLOCKED, WATCH_UNLOCKED):
                await other.execute(text(locked), {"id": earlier.bot.id})
            seen.append(
                str(
                    await raw(
                        other,
                        "SELECT state FROM assistant_watch WHERE bot_id = :id",
                        id=earlier.bot.id,
                    )
                )
            )

    api.before = before
    counts = await tick(factory, after(base, 30))
    assert counts["assistant_watch_polled"] == 2 and counts["assistant_watch_failed"] == 0
    assert sorted(order) == [first.token, second.token] and seen == ["polling"]


async def test_an_unexpected_error_pushes_one_assistant_back_and_spares_the_rest(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    first, second = await assistant(db_session, "a"), await assistant(db_session, "b")
    base = base_time()
    await primed(db_session, first, base)
    await primed(db_session, second, base)
    api.chats = [LI]

    async def before(hit: Hit) -> None:
        if hit.token == first.token:
            raise RuntimeError(f"synthetic {SECRET}")

    api.before = before
    now = after(base, 30)
    with capture_logs() as logs:
        counts = await tick(make_session_factory(db_engine), now)
    assert counts["assistant_watch_failed"] == 1 and counts["assistant_watch_polled"] == 1
    broken = await watch_row(db_session, first)
    assert (broken.state, broken.last_error, broken.failures) == ("backoff", "RuntimeError", 1)
    assert broken.next_poll_at == now + timedelta(seconds=watch.ERROR_BACKOFF_MAX)
    assert (await watch_row(db_session, second)).state == "polling"
    assert SECRET not in json.dumps(logs, ensure_ascii=False, default=str)


async def test_the_owners_own_message_is_boxed_as_owner_said(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [TEAM]
    api.say("oc_team", "om_1", after(base, 10))
    api.say(
        "oc_team", "om_2", after(base, 20), sender=who.open_id, text=f"周四前我把报价发你{SECRET}"
    )
    factory = make_session_factory(db_engine)
    now = after(base, 30)
    await tick(factory, now)
    [said] = await owner_said(db_session, who)
    assert (said.message_id, said.level, said.chat_id) == ("om_2", "batch", "oc_team")
    assert said.hold_until == now + watch.OWNER_SAID_WINDOW
    assert said.summary_enc["text"] == f"周四前我把报价发你{SECRET}"  # type: ignore[index]
    stored = str(
        await raw(db_session, "SELECT string_agg(summary_enc::text, '') FROM assistant_inbox")
    )
    assert SECRET not in stored
    # 这个会话照旧被压着：别人先到的那条被压到他发言之后 10 分钟。
    [held] = await messages(db_session, who)
    assert (held.message_id, held.level) == ("om_1", "hold")


async def test_short_or_non_text_owner_messages_are_not_boxed(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [TEAM]
    api.say("oc_team", "om_1", after(base, 10), sender=who.open_id, text="好的")
    api.say("oc_team", "om_2", after(base, 11), sender=who.open_id, msg_type="image")
    factory = make_session_factory(db_engine)
    await tick(factory, after(base, 30))
    assert await owner_said(db_session, who) == []


async def test_messages_the_platform_sent_for_the_owner_are_not_boxed(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [TEAM]
    await sent.record(db_session, who.bot.id, ["om_1"])
    await db_session.commit()
    api.say("oc_team", "om_1", after(base, 10), sender=who.open_id, text="周四前我把报价发你")
    factory = make_session_factory(db_engine)
    await tick(factory, after(base, 30))
    assert await owner_said(db_session, who) == []


async def test_the_owners_chat_with_the_assistant_is_not_boxed(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [{"chat_id": who.chat_id, "chat_mode": "p2p"}]
    api.say(who.chat_id, "om_1", after(base, 10), sender=who.open_id, text="周四前我把报价发你")
    factory = make_session_factory(db_engine)
    await tick(factory, after(base, 30))
    assert await owner_said(db_session, who) == []


async def test_what_the_owner_says_to_a_bot_is_not_boxed_as_owner_said(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """私聊里读到过机器人（应用）发的消息，所有者在这个私聊里的话不入箱；标志随游标落库，
    下一轮读到的话同样不入箱。真人私聊与群聊不受影响。"""
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [
        {"chat_id": "oc_bot", "chat_mode": "p2p"},
        {"chat_id": "oc_li", "chat_mode": "p2p"},
        TEAM,
    ]
    api.say("oc_bot", "om_b1", after(base, 10), sender="cli_bot", kind="app", text="你好")
    api.say("oc_bot", "om_b2", after(base, 20), sender=who.open_id, text="帮我建个本周的周报文档吧")
    api.say("oc_li", "om_l1", after(base, 21), sender="ou_li", text="在吗")
    api.say("oc_li", "om_l2", after(base, 22), sender=who.open_id, text="周四前我把报价发你")
    api.say("oc_team", "om_t1", after(base, 23), sender="cli_bot", kind="app", text="收到")
    api.say("oc_team", "om_t2", after(base, 24), sender=who.open_id, text="周四前我把方案发你")
    factory = make_session_factory(db_engine)
    await tick(factory, after(base, 30))
    assert {row.message_id for row in await owner_said(db_session, who)} == {"om_l2", "om_t2"}
    # 下一轮：机器人这次没再说话，标志来自上一轮落库的游标。
    api.say("oc_bot", "om_b3", after(base, 40), sender=who.open_id, text="再帮我约一个会议室吧")
    await tick(factory, after(base, 60))
    assert {row.message_id for row in await owner_said(db_session, who)} == {"om_l2", "om_t2"}


async def test_a_system_message_does_not_mark_a_human_chat_as_a_bot_chat(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [{"chat_id": "oc_li", "chat_mode": "p2p"}]
    api.say(
        "oc_li", "om_s1", after(base, 10), sender="", kind="", msg_type="system", text="系统消息"
    )
    api.say("oc_li", "om_l2", after(base, 20), sender=who.open_id, text="周四前我把报价发你")
    await tick(make_session_factory(db_engine), after(base, 30))
    assert {row.message_id for row in await owner_said(db_session, who)} == {"om_l2"}


async def test_owner_said_items_share_one_batch(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [TEAM, LI]
    api.say("oc_team", "om_1", after(base, 10), sender=who.open_id, text="周四前我把报价发你")
    factory = make_session_factory(db_engine)
    await tick(factory, after(base, 30))
    api.chats = [LI, TEAM]
    api.say("oc_li", "om_2", after(base, 400), sender=who.open_id, text="发我一下合同")
    await tick(factory, after(base, 600))
    first, second = await owner_said(db_session, who)
    assert first.hold_until == second.hold_until == after(base, 30) + watch.OWNER_SAID_WINDOW


async def test_owner_said_rows_do_not_stretch_other_peoples_batch(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [TEAM, {"chat_id": "oc_other", "name": "另一个群", "chat_mode": "group"}]
    api.say("oc_team", "om_1", after(base, 10), sender=who.open_id, text="周四前我把报价发你")
    factory = make_session_factory(db_engine)
    await tick(factory, after(base, 30))
    # 另一个所有者没发过言的群里别人的话：照常攒 5 分钟，不跟着那条 30 分钟的批走。
    api.chats = [api.chats[1], api.chats[0]]
    api.say("oc_other", "om_2", after(base, 40), sender="ou_li")
    now = after(base, 91)
    await tick(factory, now)
    [other] = [row for row in await messages(db_session, who) if row.message_id == "om_2"]
    assert other.hold_until == now + timedelta(minutes=5)


async def test_hold_chat_does_not_push_owner_said_rows_back(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [TEAM]
    api.say("oc_team", "om_1", after(base, 10), sender=who.open_id, text="周四前我把报价发你")
    factory = make_session_factory(db_engine)
    await tick(factory, after(base, 30))
    # 所有者随后又在同一个会话里发言：压着的是别人的消息，他自己说过的话照旧 30 分钟那一批。
    api.say("oc_team", "om_2", after(base, 100), sender=who.open_id, text="我再确认一下")
    await tick(factory, after(base, 150))
    for row in await owner_said(db_session, who):
        assert (row.level, row.hold_until) == ("batch", after(base, 30) + watch.OWNER_SAID_WINDOW)


async def test_a_recalled_owner_message_voids_its_owner_said_item(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [TEAM]
    said = api.say(
        "oc_team", "om_1", after(base, 10), sender=who.open_id, text="周四前我把报价发你"
    )
    factory = make_session_factory(db_engine)
    await tick(factory, after(base, 30))
    said["deleted"] = True
    await tick(factory, after(base, 91))
    [row] = await owner_said(db_session, who)
    assert (row.status, row.void_code) == ("void", "recalled")


async def test_at_most_a_daily_quota_of_owner_said_items_per_local_day(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch, "OWNER_SAID_DAILY", 2)
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [TEAM]
    for index in (1, 2, 3):
        api.say(
            "oc_team",
            f"om_{index}",
            after(base, 10 + index),
            sender=who.open_id,
            text="周四前我把报价发你",
        )
    factory = make_session_factory(db_engine)
    await tick(factory, after(base, 30))
    assert [r.message_id for r in await owner_said(db_session, who)] == ["om_1", "om_2"]
    # 次日（所有者时区的下一个自然日）又能入箱。
    api.say("oc_team", "om_4", after(base, 86400 + 100), sender=who.open_id, text="发我一下合同")
    await tick(factory, after(base, 86400 + 120))
    assert [r.message_id for r in await owner_said(db_session, who)] == ["om_1", "om_2", "om_4"]
