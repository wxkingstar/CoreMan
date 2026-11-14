"""顺带检查里要读飞书的两类：待批单该不该自动撤回；已交回的消息有没有被对方撤回。各自只做一次。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import delivery, followups, sent, settings
from coreman.core.assistants import delivery_checks as checks
from coreman.core.assistants.approvals import service
from coreman.core.db.models import AssistantApproval, AssistantWatch, Bot
from coreman.core.db.session import make_session_factory
from coreman.core.feishu_personal.service import PersonalError
from coreman.core.timeutils import utcnow
from tests.integration.delivery_helpers import boxed, inbox_rows, ready
from tests.integration.test_assistant_approval_result import _row

pytestmark = pytest.mark.usefixtures("assistant_master", "runtime_settings")
OWNER = "ou_owner"


def said(mid: str, sender: str = "ou_li", kind: str = "user", **extra: Any) -> dict[str, Any]:
    return {
        "message_id": mid,
        "chat_id": "oc_li",
        "sender": {"id": sender, "sender_type": kind},
        "msg_type": "text",
        "body": {"content": json.dumps({"text": "……"})},
        "deleted": False,
        **extra,
    }


class Feishu:
    """读接口的替身：按接口名给应答，记下调了什么。`history` 从新到旧。"""

    def __init__(
        self,
        *,
        deleted: tuple[str, ...] = (),
        history: list[dict[str, Any]] | None = None,
        failing: str = "",
        mode: str = "p2p",
    ) -> None:
        self.deleted, self.history, self.failing = set(deleted), history or [], failing
        self.mode = mode
        self.calls: list[str] = []

    async def __call__(
        self, key: str, *, path: Any = None, params: Any = None, json: Any = None
    ) -> dict[str, Any]:
        self.calls.append(key)
        if key == self.failing:
            raise PersonalError("authorization_required")
        if key == "im.mget":
            return {
                "items": [
                    {"message_id": mid, "deleted": mid in self.deleted}
                    for mid in params["message_ids"]
                ]
            }
        if key == "im.chat":
            return {"chat_mode": self.mode, "name": ""}
        if key == "im.history":
            return {"items": self.history}
        raise AssertionError(key)


def ms(at: datetime) -> int:
    return int(at.timestamp() * 1000)


async def pending(
    session: AsyncSession, bot: Bot, created: datetime, anchor: str = "om_anchor"
) -> AssistantApproval:
    row = _row(
        bot.id,
        "pending",
        chat_id="oc_li",
        anchor_message_id=anchor,
        session_key="oc_private",
        created_at=created,
    )
    session.add(row)
    await session.commit()
    return row


async def seen(session: AsyncSession, bot: Bot, *, owner: int = 0, latest: int = 0) -> None:
    """盯消息的游标：这个会话里所有者上次发言、读到的最新一条的毫秒时间。"""
    row = await session.get(AssistantWatch, bot.id)
    cursor = {"since": 0, "chats": {"oc_li": {"o": owner, "t": latest}}, "todo": []}
    if row is None:
        session.add(AssistantWatch(bot_id=bot.id, cursor_enc=cursor))
    else:
        row.cursor_enc = cursor
    await session.commit()


async def check(session: AsyncSession, bot: Bot, feishu: Feishu, now: datetime) -> int:
    prefs = await settings.get(session, bot.id)
    done = await checks.stale_approvals(session, bot, prefs, feishu, now, None)
    await session.commit()
    return done


async def status_of(session: AsyncSession, row: AssistantApproval) -> tuple[str, str | None]:
    found = await session.get(AssistantApproval, row.id, populate_existing=True)
    assert found is not None
    return found.status, found.error_code


async def test_nothing_changed_means_nothing_is_read_or_withdrawn(
    db_session: AsyncSession,
) -> None:
    bot, _ = await ready(db_session)
    now = utcnow()
    created = now - timedelta(minutes=10)
    row = await pending(db_session, bot, created)
    await seen(db_session, bot, owner=ms(created) - 5000, latest=ms(created) - 1000)
    feishu = Feishu()
    assert await check(db_session, bot, feishu, now) == 0
    # 只查了一次锚点有没有被撤回，没有读会话。
    assert feishu.calls == ["im.mget"]
    assert await status_of(db_session, row) == ("pending", None)
    assert await inbox_rows(db_session, bot) == []


async def test_the_owner_replying_himself_withdraws_the_draft_without_reading_the_chat(
    db_session: AsyncSession,
) -> None:
    bot, _ = await ready(db_session)
    now = utcnow()
    created = now - timedelta(minutes=10)
    row = await pending(db_session, bot, created)
    await seen(db_session, bot, owner=ms(created) + 60_000, latest=ms(created) + 60_000)
    feishu = Feishu()
    assert await check(db_session, bot, feishu, now) == 1
    assert feishu.calls == ["im.mget"]
    assert await status_of(db_session, row) == ("withdrawn", "owner_replied")
    (item,) = await inbox_rows(db_session, bot)
    assert (item.kind, item.level, item.chat_id) == ("reminder", "immediate", "oc_li")
    assert item.message_id == f"approval:{row.id}:withdrawn"
    assert item.summary_enc == {
        "reminder": "approval_withdrawn",
        "approval_id": str(row.id),
        "tool": "feishu_reply_message",
        "code": "owner_replied",
        "chat_id": "oc_li",
        "abstract": "私聊 → 李四",
    }
    # 再检查一次：单已不是待批，不会再撤、再入箱。
    assert await check(db_session, bot, Feishu(), now) == 0
    assert len(await inbox_rows(db_session, bot)) == 1


async def test_a_revised_draft_is_judged_from_the_revision_not_from_its_creation(
    db_session: AsyncSession,
) -> None:
    """改稿时平台重新读过会话、核对过锚点：那之前所有者说的话、会话里的新消息都已经算在新稿里。"""
    bot, _ = await ready(db_session)
    now = utcnow()
    created = now - timedelta(minutes=10)
    row = await pending(db_session, bot, created)
    # 提交一分钟后所有者亲自说了一句、对方也回了；五分钟后助理据此改了稿。
    await seen(db_session, bot, owner=ms(created) + 60_000, latest=ms(created) + 90_000)
    row.revised_at = created + timedelta(minutes=5)
    await db_session.commit()
    feishu = Feishu(history=[said("om_reply"), said("om_mine", OWNER), said("om_anchor")])
    assert await check(db_session, bot, feishu, now) == 0
    assert feishu.calls == ["im.mget"]
    assert await status_of(db_session, row) == ("pending", None)
    # 改稿之后所有者又亲自回了：照旧撤回。
    await seen(db_session, bot, owner=ms(created) + 6 * 60_000, latest=ms(created) + 6 * 60_000)
    assert await check(db_session, bot, Feishu(), now) == 1
    assert await status_of(db_session, row) == ("withdrawn", "owner_replied")


async def test_only_drafts_that_send_into_a_chat_are_checked(db_session: AsyncSession) -> None:
    """改、撤、置顶自己发过的消息的单，锚点就是那条消息本身：所有者之后又发言不让它过时。"""
    bot, _ = await ready(db_session)
    now = utcnow()
    created = now - timedelta(minutes=10)
    row = await pending(db_session, bot, created)
    row.tool = "feishu_edit_message"
    await seen(db_session, bot, owner=ms(created) + 60_000, latest=ms(created) + 60_000)
    feishu = Feishu(deleted=("om_anchor",))
    assert await check(db_session, bot, feishu, now) == 0
    assert feishu.calls == []
    assert await status_of(db_session, row) == ("pending", None)


async def test_a_recalled_anchor_withdraws_the_draft(db_session: AsyncSession) -> None:
    bot, _ = await ready(db_session)
    now = utcnow()
    row = await pending(db_session, bot, now - timedelta(minutes=10))
    feishu = Feishu(deleted=("om_anchor",))
    assert await check(db_session, bot, feishu, now) == 1
    assert feishu.calls == ["im.mget"]
    assert await status_of(db_session, row) == ("withdrawn", "anchor_recalled")


@pytest.mark.parametrize(
    ("history", "code"),
    [
        # 锚点之后对方又说了一句。
        ([said("om_new"), said("om_anchor")], "anchor_moved"),
        # 锚点之后所有者亲自回了（盯消息还没读到他这一句）。
        ([said("om_mine", sender=OWNER), said("om_anchor")], "owner_replied"),
        # 锚点已经不在最近的消息里。
        ([said("om_new")], "anchor_moved"),
        # 只有机器人的通知：不算会话动过。
        ([said("om_bot", kind="app"), said("om_anchor")], None),
        # 读会话时才看到锚点已被撤回（批量取消息那一步没认出来）：按对方撤回算。
        ([said("om_new"), said("om_anchor", deleted=True)], "anchor_recalled"),
    ],
)
async def test_new_activity_after_the_draft_is_checked_against_the_chat(
    db_session: AsyncSession, history: list[dict[str, Any]], code: str | None
) -> None:
    bot, _ = await ready(db_session)
    now = utcnow()
    created = now - timedelta(minutes=10)
    row = await pending(db_session, bot, created)
    await seen(db_session, bot, latest=ms(created) + 60_000)
    feishu = Feishu(history=history)
    assert await check(db_session, bot, feishu, now) == (1 if code else 0)
    assert feishu.calls == ["im.mget", "im.chat", "im.history"]
    assert await status_of(db_session, row) == (("withdrawn", code) if code else ("pending", None))
    # 所有者亲自回了的要立刻告诉助理；只是会话又有新消息的不急，跟下一批一起交回。
    levels = [item.level for item in await inbox_rows(db_session, bot)]
    assert levels == (
        {
            "owner_replied": ["immediate"],
            "anchor_moved": ["batch"],
            "anchor_recalled": ["immediate"],
        }[code]
        if code
        else []
    )


async def test_group_chatter_withdraws_nothing_but_a_reply_to_the_anchor_does(
    db_session: AsyncSession,
) -> None:
    bot, _ = await ready(db_session)
    now = utcnow()
    created = now - timedelta(minutes=10)
    row = await pending(db_session, bot, created)
    await seen(db_session, bot, latest=ms(created) + 60_000)
    chatter = [said("om_c2", "ou_wang"), said("om_c1", "ou_zhao"), said("om_anchor")]
    feishu = Feishu(history=chatter, mode="group")
    assert await check(db_session, bot, feishu, now) == 0
    assert feishu.calls == ["im.mget", "im.chat", "im.history"]
    assert await status_of(db_session, row) == ("pending", None)
    assert await inbox_rows(db_session, bot) == []
    # 有人回复了锚点那条：这个话头动过了。
    about = [said("om_c3", "ou_wang", parent_id="om_anchor"), *chatter]
    assert await check(db_session, bot, Feishu(history=about, mode="group"), now) == 1
    assert await status_of(db_session, row) == ("withdrawn", "anchor_moved")


async def test_a_busy_group_does_not_put_drafts_into_a_withdraw_and_redraft_loop(
    db_session: AsyncSession,
) -> None:
    """活跃的群里：助理起草 → 有人接话 → 平台撤回并提醒 → 助理重新起草 → 又有人接话……

    三道闸：群里的闲聊不算话头动过；同一个会话 30 分钟内平台至多因「会话动过」撤一次，
    之后的留着待批，交给执行前复查；这类提醒不急，跟下一批一起交回。
    """
    bot, _ = await ready(db_session)
    start = utcnow() - timedelta(hours=2)
    history = [said("om_anchor")]
    drafts: list[AssistantApproval] = []
    outcomes: list[tuple[str, str | None]] = []
    # 每 5 分钟一次检查，一共一小时；每次检查之前群里有人闲聊一句，另有人回复助理锚定的那条。
    for step in range(12):
        now = start + timedelta(minutes=5 * (step + 1))
        if not drafts or (await status_of(db_session, drafts[-1]))[0] != "pending":
            # 助理（重新）起草：锚点是它读到的最后一条。
            anchor = history[0]["message_id"]
            drafts.append(await pending(db_session, bot, now - timedelta(minutes=4), anchor))
        anchor = drafts[-1].anchor_message_id
        history.insert(0, said(f"om_chat_{step}", "ou_wang"))
        history.insert(0, said(f"om_about_{step}", "ou_zhao", parent_id=anchor))
        await seen(db_session, bot, latest=ms(now) - 1000)
        await check(db_session, bot, Feishu(history=list(history), mode="group"), now)
        outcomes.append(await status_of(db_session, drafts[-1]))
    # 一小时里有人接了 12 次话，平台只撤了 2 次（间隔 30 分钟），而不是每次都撤。
    withdrawn = [index for index, (status, _) in enumerate(outcomes) if status == "withdrawn"]
    assert withdrawn == [0, 6]
    assert len(drafts) == 3 and outcomes[-1] == ("pending", None)
    reminders = await inbox_rows(db_session, bot)
    assert [(item.kind, item.level) for item in reminders] == [("reminder", "batch")] * 2
    # 冷却只管「会话动过」：所有者亲自回了、对方撤回了被回复的消息，随时都撤。
    await seen(db_session, bot, owner=ms(now) + 1000, latest=ms(now) + 1000)
    assert await check(db_session, bot, Feishu(), now + timedelta(minutes=1)) == 1
    assert await status_of(db_session, drafts[-1]) == ("withdrawn", "owner_replied")


async def test_a_message_the_platform_sent_for_the_owner_withdraws_nothing(
    db_session: AsyncSession,
) -> None:
    """同会话的另一张单批准后由平台代发：发送人是所有者，但不算他亲自回复，这张单留着。"""
    bot, _ = await ready(db_session)
    now = utcnow()
    created = now - timedelta(minutes=10)
    row = await pending(db_session, bot, created)
    await seen(db_session, bot, latest=ms(created) + 60_000)
    await sent.record(db_session, bot.id, ["om_sent"])
    await db_session.commit()
    feishu = Feishu(history=[said("om_sent", OWNER), said("om_anchor")])
    assert await check(db_session, bot, feishu, now) == 0
    assert feishu.calls == ["im.mget", "im.chat", "im.history"]
    assert await status_of(db_session, row) == ("pending", None)
    # 所有者真的亲自回复了（没登记过的消息）：照旧撤回。
    feishu = Feishu(history=[said("om_own", OWNER), said("om_sent", OWNER), said("om_anchor")])
    assert await check(db_session, bot, feishu, now) == 1
    assert await status_of(db_session, row) == ("withdrawn", "owner_replied")


async def test_two_drafts_in_one_chat_read_it_once(db_session: AsyncSession) -> None:
    bot, _ = await ready(db_session)
    now = utcnow()
    created = now - timedelta(minutes=10)
    await pending(db_session, bot, created, "om_a")
    await pending(db_session, bot, created, "om_b")
    await seen(db_session, bot, latest=ms(created) + 60_000)
    feishu = Feishu(history=[said("om_new"), said("om_b"), said("om_a")])
    assert await check(db_session, bot, feishu, now) == 2
    assert feishu.calls == ["im.mget", "im.chat", "im.history"]


async def test_a_message_recalled_after_it_was_handed_over_is_noticed_once(
    db_session: AsyncSession,
) -> None:
    bot, _ = await ready(db_session)
    now = utcnow()
    await boxed(db_session, bot, "om_1")
    await boxed(db_session, bot, "om_2")
    task = await delivery.dispatch(db_session, bot, now)
    assert task is not None
    await delivery.settle(db_session, task, True)
    # 这一条还在箱里没交回。
    await boxed(db_session, bot, "om_later")
    opened = await followups.add(
        db_session, bot.id, category="promised", title="给李四回报价", origin_message_id="om_1"
    )
    await db_session.commit()
    feishu = Feishu(deleted=("om_1",))
    assert await checks.recalled_after_delivery(db_session, bot.id, feishu, now) == 1
    await db_session.commit()
    # 只查已经交回的消息：还在箱里的那条是盯消息自己查的。
    assert feishu.calls == ["im.mget"]
    rows = {row.message_id: row for row in await inbox_rows(db_session, bot)}
    assert (rows["om_1"].status, rows["om_1"].void_code) == ("void", "recalled")
    assert rows["om_2"].status == "delivered" and rows["om_later"].status == "pending"
    notice = rows["recalled:om_1"]
    assert (notice.kind, notice.level, notice.status) == ("reminder", "batch", "pending")
    assert notice.summary_enc == {
        "reminder": "message_recalled",
        "chat_id": "oc_li",
        "message_id": "om_1",
        "followups": ["F001"],
    }
    closed = await followups.get(db_session, bot.id, opened.seq)
    assert (closed.status, closed.close_code) == ("dropped", "origin_recalled")
    # 第二次：这条已经作废，不再查它，也不再提醒。
    again = Feishu(deleted=("om_1",))
    assert await checks.recalled_after_delivery(db_session, bot.id, again, now) == 0
    await db_session.commit()
    assert again.calls == ["im.mget"]
    assert len(await inbox_rows(db_session, bot)) == 4


async def test_messages_handed_over_long_ago_are_not_checked(db_session: AsyncSession) -> None:
    bot, _ = await ready(db_session)
    now = utcnow()
    await boxed(db_session, bot, "om_1")
    task = await delivery.dispatch(db_session, bot, now)
    assert task is not None
    await delivery.settle(db_session, task, True)
    await db_session.commit()
    feishu = Feishu(deleted=("om_1",))
    later = now + checks.RECALL_WINDOW + timedelta(minutes=1)
    assert await checks.recalled_after_delivery(db_session, bot.id, feishu, later) == 0
    assert feishu.calls == []


async def test_a_failed_read_keeps_what_was_done_and_only_logs_a_code(
    db_session: AsyncSession,
) -> None:
    bot, _ = await ready(db_session)
    now = utcnow()
    created = now - timedelta(minutes=10)
    row = await pending(db_session, bot, created)
    await seen(db_session, bot, owner=ms(created) + 60_000)
    prefs = await settings.get(db_session, bot.id)
    # 另有一条已经交回的消息：第二类检查会为它再调一次飞书。
    await boxed(db_session, bot, "om_1")
    task = await delivery.dispatch(db_session, bot, now)
    assert task is not None
    await delivery.settle(db_session, task, True)
    await db_session.commit()

    class Flaky(Feishu):
        """第一次调用（查锚点）成功，第二次（查已交回的消息）授权失效。"""

        async def __call__(self, key: str, **kw: Any) -> dict[str, Any]:
            if len(self.calls) == 1:
                self.calls.append(key)
                raise PersonalError("authorization_required")
            return await super().__call__(key, **kw)

    counts = await checks.feishu_checks(db_session, bot, prefs, Flaky(), now)
    await db_session.commit()
    assert counts == {
        "assistant_approvals_auto_withdrawn": 1,
        "assistant_recalls_noticed": 0,
        "assistant_marks_released": 0,
    }
    assert await status_of(db_session, row) == ("withdrawn", "owner_replied")


async def test_auto_withdraw_only_touches_pending_drafts(db_session: AsyncSession) -> None:
    bot, _ = await ready(db_session)
    row = _row(bot.id, "approved", chat_id="oc_li", anchor_message_id="om_a")
    db_session.add(row)
    await db_session.commit()
    assert await service.auto_withdraw(db_session, row.id, "owner_replied", cfg=None) is None
    assert await status_of(db_session, row) == ("approved", None)


async def test_a_draft_someone_else_is_working_on_is_left_for_the_next_sweep(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """执行、决定、改稿都是先锁审批单、再去取所有者的令牌；顺带检查反过来（先用过令牌、再来
    锁审批单）。所以这里不等锁：单被别人锁着就先不撤，下一次再看。"""
    bot, _ = await ready(db_session)
    now = utcnow()
    created = now - timedelta(minutes=10)
    row = await pending(db_session, bot, created)
    await seen(db_session, bot, owner=ms(created) + 60_000, latest=ms(created) + 60_000)
    async with make_session_factory(db_engine)() as other:
        await other.get(AssistantApproval, row.id, with_for_update=True)
        done = await asyncio.wait_for(check(db_session, bot, Feishu(), now), timeout=5)
        assert done == 0
        assert await inbox_rows(db_session, bot) == []
        await other.rollback()
    assert await status_of(db_session, row) == ("pending", None)
    # 锁放开之后的下一次检查照常撤回。
    assert await check(db_session, bot, Feishu(), now) == 1
    assert await status_of(db_session, row) == ("withdrawn", "owner_replied")
