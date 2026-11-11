"""助理应用的入站：私聊按人投递，群消息丢弃，进群即退，非所有者的点击无效。"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from coreman.core.assistants import keys, sealed
from coreman.core.assistants.records import new_assistant_bot
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    InboundEvent,
    PlatformApp,
    Task,
    User,
    UserIdentity,
    UserReached,
)
from coreman.core.db.session import make_session_factory
from coreman.core.platforms.feishu import FeishuClient
from coreman.runtime.gateway_feishu.assistant_inbound import AssistantInbound
from coreman.runtime.gateway_feishu.transport import LeaseLost
from tests.integration.assistant_helpers import raw as raw_sql

pytestmark = pytest.mark.usefixtures("assistant_master")
APP = "cli_example"
TEXT = "帮我看下今天的日程"


async def _member(session, login, name, feishu_id):
    user = User(login_name=login, display_name=name)
    session.add(user)
    await session.flush()
    session.add(UserIdentity(user_id=user.id, platform="feishu", platform_user_id=feishu_id))
    await session.flush()
    return user


async def _seed(session):
    app = PlatformApp(
        platform="feishu", name="个人助理", capabilities=["assistant"], app_id=APP, secret_enc="x"
    )
    session.add(app)
    await session.flush()
    await session.refresh(app)
    zhang = await _member(session, "zhangsan", "张三", "u_zhang")
    li = await _member(session, "lisi", "李四", "u_li")
    wang = await _member(session, "wangwu", "王五", "u_wang")  # 还没开通
    bots = {}
    for owner in (zhang, li):
        # 开通时签发数据密钥，之后才有助理记录与它的内容行。
        await keys.ensure_key(session, sealed.master(), owner.id)
        bot = new_assistant_bot(
            owner, app, relay_server_id=None, model="m", working_dir="/w", credentials_enc="x"
        )
        session.add(bot)
        bots[owner.id] = bot
    await session.commit()
    return app, zhang, li, wang, bots


def _inbound(db_engine, app, calls, *, fence=None):
    def handler(request):
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t", "expire": 7200})
        calls.append((request.method, request.url.path, json.loads(request.content or b"{}")))
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_notice"}})

    http = httpx.AsyncClient(
        base_url="https://open.feishu.cn", transport=httpx.MockTransport(handler)
    )

    async def no_fence():
        return None

    inbound = AssistantInbound(
        factory=make_session_factory(db_engine),
        client=FeishuClient(APP, "synthetic", http=http),
        app=app.id,
        feishu_app_id=APP,
        instance_id="gw-a",
        generation=1,
        cipher=Cipher(b"\x05" * 32),
        fence=fence or no_fence,
    )
    missing = []

    async def record(session, *, platform_user_id, user_id, raw):
        missing.append((platform_user_id, user_id))

    inbound.on_unprovisioned = record
    return inbound, http, missing


def message(user_id, mid, *, chat_type="p2p", chat_id=None):
    return {
        "header": {"app_id": APP, "event_type": "im.message.receive_v1", "event_id": f"ev_{mid}"},
        "event": {
            "sender": {
                "sender_type": "user",
                "sender_id": {"user_id": user_id, "open_id": f"ou_{user_id}"},
            },
            "message": {
                "message_id": mid,
                "chat_id": chat_id or f"oc_{user_id}",
                "chat_type": chat_type,
                "message_type": "text",
                "content": json.dumps({"text": TEXT}),
                "mentions": [],
            },
        },
    }


def click(user_id, chat_id):
    return {
        "header": {"app_id": APP, "event_type": "card.action.trigger", "event_id": "ev_click"},
        "event": {
            "operator": {"user_id": user_id, "open_id": f"ou_{user_id}"},
            "context": {"open_chat_id": chat_id, "open_message_id": "om_card"},
            "action": {"value": {"task_id": "personal:1"}, "form_value": {}},
        },
    }


async def test_private_messages_land_on_the_senders_own_assistant(db_session, db_engine):
    app, zhang, li, _, bots = await _seed(db_session)
    inbound, http, missing = _inbound(db_engine, app, [])
    try:
        assert await inbound.accept(message("u_zhang", "om1")) is None
        assert await inbound.accept(message("u_li", "om2")) is None
        assert await inbound.accept(message("u_zhang", "om1")) is None  # 平台重推
    finally:
        await inbound.aclose()
        await http.aclose()
    events = (await db_session.scalars(select(InboundEvent).order_by(InboundEvent.id))).all()
    assert [(e.bot_id, e.platform_msg_id, e.chat_type) for e in events] == [
        (bots[zhang.id].id, "om1", "single"),
        (bots[li.id].id, "om2", "single"),
    ]
    tasks = (await db_session.scalars(select(Task).order_by(Task.id))).all()
    assert [(t.bot_id, t.kind) for t in tasks] == [
        (bots[zhang.id].id, "chat"),
        (bots[li.id].id, "chat"),
    ]
    await sealed.open_rows(db_session, tasks)
    assert tasks[0].payload["bot_key"] == bots[zhang.id].bot_key
    assert missing == []


async def test_inbound_content_is_sealed_in_the_database(db_session, db_engine):
    app, zhang, _, _, bots = await _seed(db_session)
    inbound, http, _ = _inbound(db_engine, app, [])
    try:
        assert await inbound.accept(message("u_zhang", "om1")) is None
    finally:
        await inbound.aclose()
        await http.aclose()
    # 原生 SQL 看库里存的：入站事件与任务的载荷都是所有者数据密钥的密文，不含明文。
    # 载荷里的中文按 \u 转义与原样两种写法都查一遍。
    escaped = json.dumps(TEXT)[1:-1]
    for table in ("inbound_events", "tasks"):
        stored = str(
            await raw_sql(
                db_session,
                f"SELECT payload::text FROM {table} WHERE bot_id = :b",  # noqa: S608
                b=bots[zhang.id].id,
            )
        )
        assert json.loads(stored)["_sealed"].startswith("enc:v2:d1:")
        assert TEXT not in stored and escaped not in stored
    # 经会话取整行读到的是明文：worker 照常拿到消息正文。
    task = await db_session.scalar(select(Task).where(Task.bot_id == bots[zhang.id].id))
    assert TEXT in json.dumps(task.payload, ensure_ascii=False)


async def test_senders_without_an_assistant_only_reach_the_hook(db_session, db_engine):
    app, zhang, _, wang, bots = await _seed(db_session)
    bots[zhang.id].enabled = False  # 停用的助理等于没有
    await db_session.commit()
    inbound, http, missing = _inbound(db_engine, app, [])
    try:
        for user_id, mid in (("u_wang", "om1"), ("u_ghost", "om2"), ("u_zhang", "om3")):
            assert await inbound.accept(message(user_id, mid)) is None
    finally:
        await inbound.aclose()
        await http.aclose()
    assert missing == [("u_wang", wang.id), ("u_ghost", None), ("u_zhang", zhang.id)]
    assert (await db_session.scalars(select(InboundEvent))).all() == []
    assert (await db_session.scalars(select(Task))).all() == []


async def test_group_messages_are_dropped_without_a_trace(db_session, db_engine):
    app, *_ = await _seed(db_session)
    calls: list = []
    inbound, http, missing = _inbound(db_engine, app, calls)
    try:
        raw = message("u_zhang", "om1", chat_type="group", chat_id="oc_group")
        assert await inbound.accept(raw) is None
    finally:
        await inbound.aclose()
        await http.aclose()
    assert missing == [] and calls == []
    # 群消息在归一化之前丢弃：入站事件与任务都没有新增行。
    assert await raw_sql(db_session, "SELECT count(*) FROM inbound_events") == 0
    assert await raw_sql(db_session, "SELECT count(*) FROM tasks") == 0


async def test_added_to_a_group_leaves_and_tells_who_added_it(db_session, db_engine):
    app, *_ = await _seed(db_session)
    calls: list = []
    inbound, http, _ = _inbound(db_engine, app, calls)
    raw = {
        "header": {
            "app_id": APP,
            "event_type": "im.chat.member.bot.added_v1",
            "event_id": "ev_added",
        },
        "event": {"chat_id": "oc_group", "operator_id": {"user_id": "u_li"}},
    }
    try:
        assert await inbound.accept(raw) is None
    finally:
        await inbound.aclose()
        await http.aclose()
    assert calls[0] == ("DELETE", "/open-apis/im/v1/chats/oc_group/members", {"id_list": [APP]})
    method, path, body = calls[1]
    assert (method, path, body["receive_id"]) == ("POST", "/open-apis/im/v1/messages", "u_li")
    assert "私聊" in json.loads(body["content"])["text"]
    assert (await db_session.scalars(select(InboundEvent))).all() == []


async def test_card_clicks_count_only_in_the_owners_own_private_chat(db_session, db_engine):
    app, zhang, li, _, bots = await _seed(db_session)
    db_session.add(
        UserReached(bot_id=bots[zhang.id].id, user_id=zhang.id, platform_chat_id="oc_zhang")
    )
    await db_session.commit()
    inbound, http, _ = _inbound(db_engine, app, [])
    try:
        # 李四点了张三私聊里的卡片：按李四路由到李四的助理，但这不是李四与它的私聊。
        assert await inbound.accept(click("u_li", "oc_zhang")) == "not_requester"
        assert (await db_session.scalars(select(Task))).all() == []
        assert await inbound.accept(click("u_zhang", "oc_zhang")) is None
    finally:
        await inbound.aclose()
        await http.aclose()
    tasks = (await db_session.scalars(select(Task))).all()
    assert [(t.bot_id, t.kind, t.lane) for t in tasks] == [
        (bots[zhang.id].id, "card_action", "fast")
    ]


async def test_lost_lease_refuses_the_event(db_session, db_engine):
    app, *_ = await _seed(db_session)

    async def lost():
        raise LeaseLost

    inbound, http, _ = _inbound(db_engine, app, [], fence=lost)
    try:
        with pytest.raises(LeaseLost):
            await inbound.accept(message("u_zhang", "om1"))
    finally:
        await inbound.aclose()
        await http.aclose()
    assert (await db_session.scalars(select(InboundEvent))).all() == []


def test_hook_signature_is_stable_for_the_provisioning_plan():
    import inspect

    parameters = inspect.signature(AssistantInbound.on_unprovisioned).parameters
    assert list(parameters) == ["self", "session", "platform_user_id", "user_id", "raw"]
    assert uuid.UUID  # 类型见实现：user_id: uuid.UUID | None
