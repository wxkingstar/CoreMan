"""飞书卡片交互：回复按钮的点击判定、置灰与身份核验，people 块的人员解析。"""

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update

from coreman.core.bots.secrets import CREDENTIALS_AAD, encrypt_json
from coreman.core.bus import instances, outbox, tasks
from coreman.core.chat import chat_logs
from coreman.core.chat.card_replies import enqueue_used
from coreman.core.chat.identity import resolve_feishu_event_speaker
from coreman.core.db.models import (
    ChatLog,
    FeishuDelivery,
    InboundEvent,
    OutboxItem,
    Task,
    TaskStream,
    User,
    UserIdentity,
    UserReached,
)
from coreman.core.db.session import make_session_factory
from coreman.core.feishu_cards.people import resolve_people
from coreman.core.feishu_personal import policy
from coreman.core.platforms.feishu import FeishuError
from coreman.runtime.gateway_feishu.inbound import normalize_event
from coreman.runtime.gateway_feishu.reply_buttons import admit
from coreman.runtime.gateway_feishu.transport import FeishuTransport
from tests.fakes.fake_relay import FakeRelay
from tests.integration.test_chat_handler import run, stream_of
from tests.integration.test_feishu_transport import FakeAPI, card_puts, finish, seed
from tests.integration.worker_helpers import seed_bot

ANSWER = (
    "退款率偏高，主要在两个品类。\n\n"
    '```card:people\n{"title": "跟进人", "users": ["Alice@Example.com", "bob", "nobody"]}\n```\n\n'
    '```card:actions\n{"buttons": [{"text": "打开后台", "url": "https://example.com/admin"}, '
    '{"text": "继续分析退款原因", "reply": true}, '
    '{"text": "按品类统计", "reply": "隐藏的另一句话"}]}\n```'
)
ASKER = "employee"


@pytest.fixture(autouse=True)
def _no_call_spacing(monkeypatch):
    monkeypatch.setattr("coreman.runtime.gateway_feishu.transport.CALL_SPACING_SECONDS", 0)


async def add_member(session, *, login, email=None, name=None, feishu_id, status="active", **kw):
    user = User(login_name=login, email=email, display_name=name or login, status=status)
    session.add(user)
    await session.flush()
    if feishu_id:
        session.add(
            UserIdentity(user_id=user.id, platform="feishu", platform_user_id=feishu_id, **kw)
        )
    await session.flush()
    return user


def walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


def reply_values(card):
    return [
        n["behaviors"][0]["value"]
        for n in walk(card)
        if n.get("tag") == "button" and n["behaviors"][0]["type"] == "callback"
    ]


def click_event(value, message_id, *, user_id=ASKER, event_id="ev1", chat_id="oc1"):
    return {
        "header": {
            "app_id": "cli_a",
            "event_type": "card.action.trigger",
            "event_id": event_id,
            "tenant_key": "tenant",
        },
        "event": {
            "operator": {
                "user_id": user_id,
                "open_id": f"ou_{user_id}",
                "union_id": f"on_{user_id}",
            },
            "context": {"open_chat_id": chat_id, "open_message_id": message_id},
            "action": {"tag": "button", "value": value},
        },
    }


def value_for(requester=ASKER, text="继续分析退款原因", row="cols_1"):
    from coreman.core.feishu_cards.reply_buttons import reply_value

    return reply_value(text, requester=requester, row=row, buttons=["btn_1", "btn_2"])


async def click(factory, bot, raw):
    """网关应答回调时做的事：归一化，判定提问人与会话类型，入站落库。"""
    message = normalize_event(
        raw,
        bot_id=bot.id,
        app_id="cli_a",
        bot_open_id="ou_bot",
        gateway_instance="gw",
        now=datetime.now(UTC),
    )
    assert message is not None
    async with factory() as session:
        outcome = await admit(session, bot, message, lease_generation=1)
        await session.commit()
    return outcome, message


async def feishu_worker_bot(session, **kw):
    bot, relay, cipher = await seed_bot(session, **kw)
    bot.platform = "feishu"
    bot.credentials_enc = encrypt_json(
        cipher, {"app_id": "cli_a", "app_secret": "secret"}, CREDENTIALS_AAD
    )
    await session.commit()
    return bot, cipher


async def claim(session):
    await instances.register(
        session, instance_id="worker-test", service="worker", version="dev", capacity=8
    )
    task = await tasks.claim(session, lane="normal", instance_id="worker-test")
    await session.commit()
    assert task is not None and task.kind == "chat"
    return task


# ---------------------------------------------------------------- 人员


async def test_people_resolve_by_email_login_or_unique_name(db_session):
    await add_member(
        db_session, login="alice", email="Alice@Example.com", name="Alice", feishu_id="u_alice"
    )
    await add_member(db_session, login="bob", name="Bob", feishu_id="u_bob")
    await add_member(db_session, login="lee1", name="Lee", feishu_id="u_lee1")
    await add_member(db_session, login="lee2", name="Lee", feishu_id="u_lee2")
    await add_member(db_session, login="gone", feishu_id="u_gone", status="disabled")
    await add_member(db_session, login="wecom", feishu_id=None)
    await db_session.commit()
    found = await resolve_people(
        db_session,
        [" alice@example.COM ", "bob", "Bob", "Lee", "gone", "wecom", "nobody", ""],
    )
    # 键是模型写的原文；同名的两个人不猜，停用的、没有飞书身份的不解析。
    assert found == {" alice@example.COM ": "u_alice", "bob": "u_bob", "Bob": "u_bob"}


# ---------------------------------------------------------------- 渲染


async def test_final_card_offers_reply_buttons_only_to_the_requester(db_session, db_engine):
    bot, row, generation = await seed(db_session)
    await add_member(
        db_session, login="alice", email="alice@example.com", name="Alice", feishu_id="u_alice"
    )
    await add_member(db_session, login="bob", name="Bob", feishu_id="u_bob")
    await db_session.execute(
        update(TaskStream)
        .where(TaskStream.task_id == row.task_id)
        .values(reply_context={"chat_id": "oc1", "message_id": "om_in", "requester_user_id": ASKER})
    )
    await db_session.commit()
    factory, api = make_session_factory(db_engine), FakeAPI()
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="old", generation=generation
    )
    await transport.round()
    await finish(factory, row, ANSWER)
    await transport.round()
    [final] = card_puts(api)
    [people] = [n for n in walk(final) if n.get("tag") == "person_list"]
    assert people["persons"] == [{"id": "u_alice"}, {"id": "u_bob"}]
    assert "nobody" in json.dumps(final, ensure_ascii=False)
    values = reply_values(final)
    # 发出去的就是按钮上的字；reply 里的字符串只是标记，不会出现在卡片里。
    assert [v["text"] for v in values] == ["继续分析退款原因", "按品类统计"]
    assert {v["requester"] for v in values} == {ASKER}
    assert "隐藏的另一句话" not in json.dumps(final, ensure_ascii=False)


async def test_cards_that_are_not_the_askers_reply_have_no_reply_behaviour(db_session, db_engine):
    """没有提问人的流（旧流、兜底回复）和出站 Markdown（定时任务结果等）：回复按钮只留文字。"""
    bot, row, generation = await seed(db_session)
    factory, api = make_session_factory(db_engine), FakeAPI()
    async with factory() as session:
        await outbox.add(
            session,
            bot_id=bot.id,
            platform="feishu",
            kind="send",
            dedupe_key="cron:1",
            target={"chat_id": "oc_private"},
            payload={"markdown": ANSWER},
        )
        await session.commit()
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="old", generation=generation
    )
    await transport.round()
    await finish(factory, row, ANSWER)
    await transport.round()
    [final] = card_puts(api)
    sent = [
        json.loads(b["content"])
        for _, p, b in api.calls
        if p == "/open-apis/im/v1/messages" and b.get("msg_type") == "interactive"
    ]
    cards = [final, *[c for c in sent if "schema" in c]]
    assert len(cards) >= 2
    for card in cards:
        body = json.dumps(card, ensure_ascii=False)
        assert reply_values(card) == [] and "callback" not in body
        assert "「继续分析退款原因」" in body
        assert "隐藏的另一句话" not in body


# ---------------------------------------------------------------- 应答回调


async def test_ack_rejects_other_clickers_and_settles_chat_type_from_private_chats(
    db_session, db_engine
):
    bot, _row, _generation = await seed(db_session)
    member = await add_member(db_session, login="alice", feishu_id=ASKER)
    db_session.add(UserReached(bot_id=bot.id, user_id=member.id, platform_chat_id="oc_private"))
    await db_session.commit()
    factory = make_session_factory(db_engine)

    # 别人点提问人的按钮：直接拒绝，什么都不落库。
    outcome, _ = await click(
        factory, bot, click_event(value_for(), "om_a", user_id="someone", chat_id="oc_group")
    )
    assert outcome == "not_requester"
    async with factory() as session:
        assert await session.scalar(select(InboundEvent.id)) is None

    # 提问人在自己与机器人的私聊里点：按私聊。
    outcome, private = await click(
        factory, bot, click_event(value_for(), "om_a", chat_id="oc_private")
    )
    assert outcome == "queued" and private.chat_type == "single"
    # 同一行再点（平台重推、连点、同一行的另一个按钮）：只算一次。
    again = click_event(value_for(text="按品类统计"), "om_a", chat_id="oc_private", event_id="e2")
    assert (await click(factory, bot, again))[0] == "duplicate"
    # 其余会话一律按群，即使 value 里写着私聊。
    forged = {**value_for(row="cols_2"), "chat": "single"}
    outcome, group = await click(factory, bot, click_event(forged, "om_b", chat_id="oc_other"))
    assert outcome == "queued" and group.chat_type == "group"

    async with factory() as session:
        events = list(await session.scalars(select(InboundEvent).order_by(InboundEvent.id)))
        assert [(e.chat_type, e.chat_id) for e in events] == [
            ("single", "oc_private"),
            ("group", "oc_other"),
        ]
        assert events[0].reply_context["chat_type"] == "single"
        # 应答窗口里不改卡片：置灰要等这一轮真正开始。
        assert await session.scalar(select(OutboxItem.id)) is None


# ---------------------------------------------------------------- worker


@pytest.mark.parametrize("chat_type", ["single", "group"])
async def test_accepted_click_opens_a_turn_and_greys_out_the_row(db_session, db_engine, chat_type):
    bot, _cipher = await feishu_worker_bot(db_session)
    member = await add_member(db_session, login="alice", feishu_id=ASKER)
    if chat_type == "single":
        db_session.add(UserReached(bot_id=bot.id, user_id=member.id, platform_chat_id="oc_chat"))
    await db_session.commit()
    factory = make_session_factory(db_engine)
    outcome, _ = await click(factory, bot, click_event(value_for(), "om_card", chat_id="oc_chat"))
    assert outcome == "queued"
    task = await claim(db_session)
    fake = FakeRelay("normal")
    await run(db_engine, task, fake)
    assert "继续分析退款原因" in json.dumps(fake.requests[0], ensure_ascii=False)
    stream = await stream_of(db_session, task.id)
    assert stream.reply_context["message_id"] == "om_card"
    # 这一轮的回复里若再有回复按钮，也只有同一个提问人能点。
    assert stream.reply_context["requester_user_id"] == ASKER
    log = (await db_session.scalars(select(ChatLog).where(ChatLog.task_id == task.id))).one()
    assert log.user_id == member.id and log.chat_type == chat_type
    # 点击的轮次不写私聊可达记录：私聊那条是原来就有的，群里的不会凭空多出来。
    reached = list(await db_session.scalars(select(UserReached)))
    assert [r.platform_chat_id for r in reached] == (["oc_chat"] if chat_type == "single" else [])
    [mark] = list(
        await db_session.scalars(select(OutboxItem).where(OutboxItem.kind == "card_update"))
    )
    assert mark.target == {"message_id": "om_card", "chat_id": "oc_chat"}
    assert mark.payload == {
        "_reply_used": {
            "row": "cols_1",
            "buttons": ["btn_1", "btn_2"],
            "label": "继续分析退款原因",
            "chat_type": chat_type,
        },
        "_operator": ASKER,
    }


async def test_denied_click_leaves_the_card_alone(db_session, db_engine):
    other = User(login_name="other", display_name="Other")
    db_session.add(other)
    await db_session.flush()
    bot, _cipher = await feishu_worker_bot(db_session, allowed_user_ids=[other.id])
    await add_member(db_session, login="alice", feishu_id=ASKER)
    await db_session.commit()
    factory = make_session_factory(db_engine)
    outcome, _ = await click(factory, bot, click_event(value_for(), "om_card", chat_id="oc_chat"))
    assert outcome == "queued"  # 应答时还不知道会被拒，所以只说「已收到」
    task = await claim(db_session)
    fake = FakeRelay("normal")
    await run(db_engine, task, fake)
    assert fake.requests == []
    marks = list(
        await db_session.scalars(select(OutboxItem).where(OutboxItem.kind == "card_update"))
    )
    assert marks == []


async def test_click_turns_never_record_private_reach(db_session):
    bot, _row, _generation = await seed(db_session)
    member = await add_member(db_session, login="alice", feishu_id=ASKER)
    await db_session.commit()
    entry = chat_logs.ChatLogEntry(
        bot_id=bot.id,
        bot_key=bot.bot_key,
        platform="feishu",
        chat_type="single",
        message_type="text",
        status="running",
        request_at=datetime.now(UTC),
        user_id=member.id,
        chat_id="oc_clicked",
        reach=False,
    )
    assert await chat_logs.open_turn(db_session, entry)
    await db_session.commit()
    assert await db_session.get(UserReached, (bot.id, member.id)) is None
    log = await db_session.scalar(select(ChatLog))
    assert log is not None and log.chat_type == "single"


# ---------------------------------------------------------------- 身份与个人工具


async def test_click_identity_is_checked_like_a_typed_message(db_session, db_engine):
    bot, cipher = await feishu_worker_bot(db_session)
    member = await add_member(db_session, login="alice", feishu_id=ASKER, union_id=f"on_{ASKER}")
    await db_session.commit()
    factory = make_session_factory(db_engine)
    await click(factory, bot, click_event(value_for(), "om_card", chat_id="oc_group"))
    event = await db_session.scalar(select(InboundEvent))
    assert event.payload["sender"]["union_id"] == f"on_{ASKER}"
    speaker = await resolve_feishu_event_speaker(db_session, bot=bot, event=event, cipher=cipher)
    assert speaker.user_id == member.id
    # 落库的列与原始回调对不上（open_id 被改过）：按未知身份处理。
    event.sender_open_id = "ou_forged"
    speaker = await resolve_feishu_event_speaker(db_session, bot=bot, event=event, cipher=cipher)
    assert speaker.user_id is None


async def test_click_in_the_askers_private_chat_gets_the_personal_origin(db_session, db_engine):
    bot, _cipher = await feishu_worker_bot(db_session)
    member = await add_member(db_session, login="alice", feishu_id=ASKER)
    db_session.add(UserReached(bot_id=bot.id, user_id=member.id, platform_chat_id="oc_private"))
    await db_session.commit()
    factory = make_session_factory(db_engine)
    await click(factory, bot, click_event(value_for(), "om_a", chat_id="oc_private"))
    await click(factory, bot, click_event(value_for(row="cols_2"), "om_b", chat_id="oc_group"))
    private, group = list(await db_session.scalars(select(Task).order_by(Task.id)))
    scope = await policy.verified_origin_scope(db_session, private, str(member.id))
    assert scope.chat_id == "oc_private" and scope.user_id == member.id
    with pytest.raises(ValueError, match="feishu_private_chat_required"):
        await policy.verified_origin_scope(db_session, group, str(member.id))
    # 落库的会话类型被改成私聊也不行：私聊只认本人与本机器人的真实私聊记录。
    event = await db_session.get(InboundEvent, group.inbound_event_id)
    event.chat_type = "single"
    await db_session.flush()
    with pytest.raises(ValueError, match="verified_private_origin_required"):
        await policy.verified_origin_scope(db_session, group, str(member.id))


# ---------------------------------------------------------------- 置灰


async def _accepted_click(factory, bot, value, mid):
    """点击被接受、这一轮开始：worker 在开流的事务里排出置灰。"""
    await click(factory, bot, click_event(value, mid))
    async with factory() as session:
        event = await session.scalar(select(InboundEvent).order_by(InboundEvent.id.desc()))
        await enqueue_used(session, bot.id, event)
        await session.commit()


async def _answered(db_session, db_engine, api, answer=ANSWER):
    bot, row, generation = await seed(db_session)
    await db_session.execute(
        update(TaskStream)
        .where(TaskStream.task_id == row.task_id)
        .values(reply_context={"chat_id": "oc1", "message_id": "om_in", "requester_user_id": ASKER})
    )
    await db_session.commit()
    factory = make_session_factory(db_engine)
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="old", generation=generation
    )
    await transport.round()
    await finish(factory, row, answer)
    await transport.round()
    return bot, row, factory, transport


async def test_streamed_card_row_is_greyed_out_through_cardkit(db_session, db_engine):
    api = FakeAPI()
    bot, row, factory, transport = await _answered(db_session, db_engine, api)
    [final] = card_puts(api)
    first = reply_values(final)[0]
    async with factory() as session:
        delivery = await session.get(FeishuDelivery, row.task_id)
        mid, before = delivery.message_id, delivery.sequence
    await _accepted_click(factory, bot, first, mid)
    await transport.round()
    [batch] = [b for _, p, b in api.calls if p == "/open-apis/cardkit/v1/cards/card1/batch_update"]
    assert batch["sequence"] > before
    actions = json.loads(batch["actions"])
    assert [a["params"]["element_id"] for a in actions[:-1]] == first["buttons"]
    assert actions[-1]["params"]["target_element_id"] == first["row"]
    note = actions[-1]["params"]["elements"][0]["content"]
    # seed 的会话没有私聊记录：按群，写明是谁选的。
    assert note == (
        f"<person id='{ASKER}' show_avatar=false></person> "
        "<font color='grey'>已选择：继续分析退款原因</font>"
    )
    async with factory() as session:
        mark = await session.scalar(select(OutboxItem).where(OutboxItem.kind == "card_update"))
        assert mark.status == "sent"


async def test_rejected_update_is_not_retried(db_session, db_engine):
    class RejectBatch(FakeAPI):
        async def call(self, method, path, **kwargs):
            if path.endswith("/batch_update"):
                self.calls.append((method, path, kwargs.get("json") or {}))
                raise FeishuError(300301)
            return await super().call(method, path, **kwargs)

    api = RejectBatch()
    bot, row, factory, transport = await _answered(db_session, db_engine, api)
    [final] = card_puts(api)
    async with factory() as session:
        mid = (await session.get(FeishuDelivery, row.task_id)).message_id
    await _accepted_click(factory, bot, reply_values(final)[0], mid)
    await transport.round()
    async with factory() as session:
        mark = await session.scalar(select(OutboxItem).where(OutboxItem.kind == "card_update"))
        assert mark.status == "sent"


async def test_continuation_card_is_patched_with_the_stored_json(db_session, db_engine):
    api = FakeAPI()
    long = "\n\n".join(f"## 第 {i} 节\n\n" + "很长的说明。" * 800 for i in range(6))
    bot, _row, factory, transport = await _answered(
        db_session, db_engine, api, answer=f"{long}\n\n{ANSWER}"
    )
    async with factory() as session:
        more = list(
            await session.scalars(
                select(OutboxItem).where(OutboxItem.kind == "send").order_by(OutboxItem.id)
            )
        )
    last = more[-1]
    values = reply_values(last.payload["card"])
    assert values, "按钮在最后一张续卡里"
    mid = last.payload["_feishu_message_id"]
    await _accepted_click(factory, bot, values[1], mid)
    await transport.round()
    [patch] = [
        b for m, p, b in api.calls if m == "PATCH" and p == f"/open-apis/im/v1/messages/{mid}"
    ]
    patched = json.loads(patch["content"])
    buttons = [n for n in walk(patched) if n.get("tag") == "button"]
    assert [b.get("disabled") for b in buttons] == [None, True, True]
    assert "已选择：按品类统计" in json.dumps(patched, ensure_ascii=False)
    # 改过的卡存回去：同一张卡上的下一次标记从改过的状态接着改。
    async with factory() as session:
        stored = (await session.get(OutboxItem, last.id)).payload["card"]
    assert stored == patched
