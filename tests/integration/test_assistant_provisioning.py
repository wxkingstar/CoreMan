"""开通：登记 → 开通卡片 → 本人点开通 → 建记录与密钥、选节点、建目录 → 飞书授权 → 才可用。"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from coreman.core.assistants import keys, lifecycle, provisioning, runtime
from coreman.core.db.models import (
    AssistantKey,
    AssistantLifecycle,
    AssistantRetiredDir,
    Bot,
    FeishuPersonalGrant,
    Setting,
    UserIdentity,
)
from coreman.core.db.session import make_session_factory
from tests.integration import lifecycle_helpers
from tests.integration.lifecycle_helpers import (
    CHAT,
    CIPHER,
    OWNER_OPEN_ID,
    FakeFeishuAuth,
    FakeMessenger,
    FakeNode,
    assistant_app,
    click_event,
    flow_of,
    member,
    message_event,
    pool_relay,
    tick,
)

pytestmark = pytest.mark.usefixtures("assistant_master", "runtime_settings")


def install(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeNode, FakeFeishuAuth]:
    """公共的替身之外，开通对节点的调用也落到同一个节点替身上。"""
    node, auth = lifecycle_helpers.install(monkeypatch)
    monkeypatch.setattr(provisioning, "call_agent", node)
    return node, auth


class Door:
    """开通的两个入口（私聊登记、点开通卡片），按网关的做法调用：认人只看事件里的发送人或操作人。

    网关本身（`AssistantInbound.on_unprovisioned`）的用例见 test_assistant_gateway_provisioning。
    """

    def __init__(self, factory: async_sessionmaker[AsyncSession], app_id: uuid.UUID) -> None:
        self.factory, self.app_id = factory, app_id

    async def accept(self, raw: dict[str, Any]) -> str | None:
        event = raw["event"]
        click = raw["header"]["event_type"] == "card.action.trigger"
        pid = event["operator"]["user_id"] if click else event["sender"]["sender_id"]["user_id"]
        async with self.factory() as session:
            user_id = await session.scalar(
                select(UserIdentity.user_id).where(
                    UserIdentity.platform == "feishu", UserIdentity.platform_user_id == pid
                )
            )
            if user_id is None:
                return None
            outcome: str | None = None
            if click:
                outcome = await provisioning.accept_offer(
                    session, app_id=self.app_id, owner_id=user_id, raw=raw
                )
            else:
                await provisioning.request_offer(
                    session, app_id=self.app_id, owner_id=user_id, platform_user_id=pid
                )
            await session.commit()
        return outcome


async def poll_now(session: AsyncSession) -> None:
    """跳过飞书要求的查询间隔（授权查询按真实时间限速）。"""
    await session.execute(update(FeishuPersonalGrant).values(next_poll_at=None))
    await session.commit()


async def offered(db_engine: AsyncEngine, db_session: AsyncSession):  # type: ignore[no-untyped-def]
    """张三私聊了助理应用、收到了开通卡片。"""
    app = await assistant_app(db_session)
    owner = await member(db_session)
    await pool_relay(db_session)
    await db_session.commit()
    inbound = Door(make_session_factory(db_engine), app.id)
    assert await inbound.accept(message_event()) is None
    messenger = FakeMessenger()
    await tick(make_session_factory(db_engine), messenger)
    return app, owner, inbound, messenger


async def test_the_whole_way_from_a_message_to_a_usable_assistant(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    node, auth = install(monkeypatch)
    app, owner, inbound, messenger = await offered(db_engine, db_session)
    factory = make_session_factory(db_engine)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.step == "offered" and flow.offer_chat_id == CHAT
    token = messenger.token()
    # 令牌只存摘要。
    assert flow.offer_hash == lifecycle.token_hash(token) and token not in str(flow.offer_hash)
    assert await inbound.accept(click_event(token, flow.offer_message_id)) == "provision_started"
    await tick(factory, messenger)  # 建记录与密钥
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.step == "init_workspace" and flow.bot_id is not None
    bot = await db_session.get(Bot, flow.bot_id, populate_existing=True)
    assert bot is not None and bot.assistant_state == "provisioning" and bot.enabled
    assert bot.working_dir == f"/srv/pa/assistant-{bot.id.hex}"
    assert await runtime.turn_blocker(db_session, bot) == "provisioning"
    key = await db_session.get(AssistantKey, owner.id)
    assert key is not None and key.dek_enc and key.destroyed_at is None
    await tick(factory, messenger)  # 节点上建目录
    assert node.calls == [
        (
            "workspace-init",
            {
                "working_dir": bot.working_dir,
                "bot_id": str(bot.id),
                "content": node.calls[0][1]["content"],
            },
        )
    ]
    await tick(factory, messenger)  # 发起授权
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.step == "await_grant" and flow.issued == 1
    assert "一键授权" in str(messenger.cards[-1][1])
    await tick(factory, messenger, later=6)  # 还没点同意
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.step == "await_grant" and flow.finished_at is None
    auth.approve()
    await poll_now(db_session)
    await tick(factory, messenger, later=12)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.outcome == "done" and flow.finished_at is not None
    bot = await db_session.get(Bot, bot.id, populate_existing=True)
    assert bot is not None and bot.assistant_state == "active"
    grant = await db_session.get(FeishuPersonalGrant, (bot.id, owner.id), populate_existing=True)
    assert grant is not None and grant.status == "connected" and grant.authorization_level == "all"
    assert grant.open_id == OWNER_OPEN_ID
    assert messenger.texts[-1][1] == provisioning.welcome_text()


async def test_a_card_can_be_used_once_by_its_owner_in_its_own_chat(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    app, owner, inbound, messenger = await offered(db_engine, db_session)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None
    token, message_id = messenger.token(), flow.offer_message_id
    assert message_id is not None
    # 转发出去的卡片是另一条消息；换了会话也不行；令牌不对不行。
    assert await inbound.accept(click_event(token, "om_forwarded")) == "provision_expired"
    assert await inbound.accept(click_event(token, message_id, chat_id="oc_group")) == (
        "provision_expired"
    )
    assert await inbound.accept(click_event("guess", message_id)) == "provision_expired"
    # 别人点：按点的人找他自己的流程，没有，作废。
    other = await member(db_session, login="lisi", pid="lisi")
    await db_session.commit()
    assert await inbound.accept(click_event(token, message_id, pid="lisi")) == "provision_expired"
    assert await flow_of(db_session, other.id) is None
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.step == "offered"
    assert await inbound.accept(click_event(token, message_id)) == "provision_started"
    # 点过即作废：重放不会再开一次。
    assert await inbound.accept(click_event(token, message_id)) == "provision_started"
    count = await db_session.scalar(select(func.count()).select_from(AssistantLifecycle))
    assert count == 1


async def test_an_expired_card_does_nothing_and_a_new_message_brings_a_new_one(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    app, owner, inbound, messenger = await offered(db_engine, db_session)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.offer_message_id is not None
    token, message_id = messenger.token(), flow.offer_message_id
    later = provisioning.OFFER_TTL.total_seconds() + 1
    await tick(make_session_factory(db_engine), messenger, later=later)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.outcome == "expired" and flow.offer_hash is None
    assert await inbound.accept(click_event(token, message_id)) == "provision_expired"
    await inbound.accept(message_event())
    await tick(make_session_factory(db_engine), messenger, later=later)
    assert messenger.token() != token


async def test_messages_within_ten_minutes_do_not_resend_the_card(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    app, owner, inbound, messenger = await offered(db_engine, db_session)
    for _ in range(3):
        await inbound.accept(message_event())
        await tick(make_session_factory(db_engine), messenger)
    assert len(messenger.cards) == 1


async def test_people_outside_the_scope_are_told_once(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    app = await assistant_app(db_session)
    owner = await member(db_session)
    db_session.add(
        Setting(
            key="assistant_eligibility",
            value={"mode": "listed", "department_ids": [], "user_ids": []},
        )
    )
    await db_session.commit()
    inbound, messenger = Door(make_session_factory(db_engine), app.id), FakeMessenger()
    for _ in range(2):
        await inbound.accept(message_event())
        await tick(make_session_factory(db_engine), messenger)
    assert messenger.cards == []
    assert [text for _, text in messenger.texts] == [
        "你暂时不在个人助理的开通范围内，如需开通请联系管理员。"
    ]
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.outcome == "ineligible" and flow.error_code == "not_listed"


async def test_no_pool_runtime_retries_then_gives_up_without_leaving_anything(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    app = await assistant_app(db_session)
    owner = await member(db_session)
    await db_session.commit()
    inbound, messenger = Door(make_session_factory(db_engine), app.id), FakeMessenger()
    factory = make_session_factory(db_engine)
    await inbound.accept(message_event())
    await tick(factory, messenger)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.offer_message_id is not None
    await inbound.accept(click_event(messenger.token(), flow.offer_message_id))
    for attempt in range(lifecycle.PROVISION_MAX_ATTEMPTS):
        await tick(factory, messenger, later=700 * (attempt + 1))
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.outcome == "failed" and flow.error_code == "no_pool_runtime"
    assert await db_session.scalar(select(func.count()).select_from(Bot)) == 0
    assert await db_session.get(AssistantKey, owner.id) is None
    assert messenger.texts[-1][1] == provisioning.FAILURES["no_pool_runtime"]


async def test_a_node_that_stays_down_abandons_and_cleans_up(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    node, _ = install(monkeypatch)
    app, owner, inbound, messenger = await offered(db_engine, db_session)
    factory = make_session_factory(db_engine)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.offer_message_id is not None
    await inbound.accept(click_event(messenger.token(), flow.offer_message_id))
    await tick(factory, messenger)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.bot_id is not None
    bot_id = flow.bot_id
    node.down = True
    for attempt in range(lifecycle.PROVISION_MAX_ATTEMPTS):
        await tick(factory, messenger, later=700 * (attempt + 1))
    # 放弃开通 → 走一遍停用：记录停用、密钥销毁、目录登记待删。
    node.down = False
    for step in range(8):
        await tick(factory, messenger, later=700 * (lifecycle.PROVISION_MAX_ATTEMPTS + step + 1))
    bot = await db_session.get(Bot, bot_id, populate_existing=True)
    assert bot is not None and bot.assistant_state == "deprovisioned" and not bot.enabled
    key = await db_session.get(AssistantKey, owner.id, populate_existing=True)
    assert key is not None and key.dek_enc is None and key.destroyed_at is not None
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.action == "deprovision" and flow.trigger == "abandoned"
    assert flow.outcome == "done"
    assert "workspace-remove" in node.ops()
    assert await db_session.scalar(select(func.count()).select_from(AssistantRetiredDir)) == 0
    with pytest.raises(keys.KeyDestroyed):
        keys.forget()
        await keys.data_cipher(db_session, CIPHER, owner.id)


async def test_a_grant_that_is_never_completed_is_abandoned_after_three_links(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    app, owner, inbound, messenger = await offered(db_engine, db_session)
    factory = make_session_factory(db_engine)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.offer_message_id is not None
    await inbound.accept(click_event(messenger.token(), flow.offer_message_id))
    later = 0.0
    for _ in range(3 + 2 * provisioning.GRANT_ROUNDS):
        later += 700
        await tick(factory, messenger, later=later)
        # 本人一直没点：每发一次授权链接，就让它过期。
        await db_session.execute(
            update(FeishuPersonalGrant).values(
                pending_expires_at=func.now() - text("interval '1 second'")
            )
        )
        await db_session.commit()
    grants = [card for _, card in messenger.cards if "一键授权" in str(card)]
    assert len(grants) == provisioning.GRANT_ROUNDS
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.action == "deprovision" and flow.trigger == "abandoned"
    assert provisioning.FAILURES["grant_not_completed"] in [text for _, text in messenger.texts]
