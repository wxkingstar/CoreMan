"""网关接上开通：没有助理的人私聊（或打开私聊）就登记开通，点开通卡片得到应答；出错不挡应答。"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import provisioning
from coreman.core.db.models import AssistantLifecycle
from coreman.core.db.session import make_session_factory
from coreman.core.platforms.feishu import FeishuClient
from coreman.runtime.gateway_feishu.assistant_inbound import AssistantInbound
from coreman.runtime.gateway_feishu.channel import TOASTS
from tests.integration.lifecycle_helpers import (
    APP_ID,
    CIPHER,
    FakeMessenger,
    assistant_app,
    click_event,
    flow_of,
    install,
    member,
    message_event,
    pool_relay,
    tick,
)

pytestmark = pytest.mark.usefixtures("assistant_master", "runtime_settings")


def gateway(db_engine: AsyncEngine, app_id: uuid.UUID) -> AssistantInbound:
    """助理应用连接的入站一半；它自己的飞书客户端在开通里用不到。"""
    http = httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )

    async def no_fence() -> None:
        return None

    return AssistantInbound(
        factory=make_session_factory(db_engine),
        client=FeishuClient(APP_ID, "synthetic", http=http),
        app=app_id,
        feishu_app_id=APP_ID,
        instance_id="gw-a",
        generation=1,
        cipher=CIPHER,
        fence=no_fence,
    )


def entered(pid: str) -> dict:
    return {
        "header": {
            "event_id": f"ev-{uuid.uuid4().hex}",
            "event_type": "im.chat.access_event.bot_p2p_chat_entered_v1",
            "app_id": APP_ID,
        },
        "event": {"operator_id": {"user_id": pid}},
    }


async def count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(AssistantLifecycle)) or 0)


async def test_a_message_or_opening_the_chat_registers_an_offer(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    app = await assistant_app(db_session)
    owner = await member(db_session)
    other = await member(db_session, login="lisi", pid="lisi")
    await db_session.commit()
    inbound = gateway(db_engine, app.id)
    assert await inbound.accept(message_event()) is None
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.step == "offered" and flow.next_attempt_at is not None
    assert flow.platform_user_id == "zhangsan" and flow.platform_app_id == app.id
    assert await inbound.accept(entered("lisi")) is None
    assert (await flow_of(db_session, other.id)) is not None
    # 同一个人再发：仍只有一条进行中的流程。
    await inbound.accept(message_event())
    assert await count(db_session) == 2


async def test_unknown_people_and_group_messages_register_nothing(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    app = await assistant_app(db_session)
    await member(db_session)
    await db_session.commit()
    inbound = gateway(db_engine, app.id)
    assert await inbound.accept(message_event(pid="nobody")) is None
    group = message_event()
    group["event"]["message"]["chat_type"] = "group"
    assert await inbound.accept(group) is None
    assert await count(db_session) == 0


async def test_the_card_click_is_answered_through_the_gateway(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    app = await assistant_app(db_session)
    owner = await member(db_session)
    await pool_relay(db_session)
    await db_session.commit()
    inbound, messenger = gateway(db_engine, app.id), FakeMessenger()
    await inbound.accept(message_event())
    await tick(make_session_factory(db_engine), messenger)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.offer_message_id is not None
    token = messenger.token()
    assert await inbound.accept(click_event(token, "om_forwarded")) == "provision_expired"
    assert await inbound.accept(click_event(token, flow.offer_message_id)) == "provision_started"
    assert TOASTS["provision_started"]["type"] == "success"
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.step == "create"


async def test_a_failing_registration_never_blocks_the_gateway(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = await assistant_app(db_session)
    await member(db_session)
    await db_session.commit()

    async def broken(*_args: object, **_kw: object) -> None:
        raise RuntimeError("synthetic")

    monkeypatch.setattr(provisioning, "request_offer", broken)
    monkeypatch.setattr(provisioning, "accept_offer", broken)
    inbound = gateway(db_engine, app.id)
    assert await inbound.accept(message_event()) is None
    assert await inbound.accept(click_event("t", "om_1")) is None
    assert await count(db_session) == 0
