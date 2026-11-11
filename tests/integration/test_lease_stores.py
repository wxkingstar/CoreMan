"""同一套租约扫描规则跑在两种存储上；助理记录不占 bot 租约。"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants.records import new_assistant_bot
from coreman.core.bus import app_leases, instances, leases
from coreman.core.db.models import AppLease, Bot, BotLease, PlatformApp, User
from coreman.core.db.session import make_session_factory
from coreman.runtime.gateway_common.lease_loop import LeaseCoordinator


class Hooks:
    def __init__(self) -> None:
        self.connected: dict[uuid.UUID, int] = {}
        self.maintained: list[uuid.UUID] = []

    def lease_connected(self) -> Collection[uuid.UUID]:
        return list(self.connected)

    async def lease_connect(self, bot_id: uuid.UUID, generation: int) -> bool:
        self.connected[bot_id] = generation
        return True

    async def lease_orphaned(self, bot_id: uuid.UUID) -> None:
        self.connected.pop(bot_id, None)

    async def lease_drain(self, bot_id: uuid.UUID) -> None:
        generation = self.connected.pop(bot_id)
        await self.coordinator.release(bot_id, generation)

    async def lease_maintain(self, held: list[Any]) -> None:
        self.maintained = [lease.key for lease in held]

    coordinator: LeaseCoordinator


async def _seed(session: AsyncSession) -> tuple[Bot, Bot, PlatformApp]:
    owner = User(login_name=f"u{uuid.uuid4().hex[:8]}", display_name="张三")
    app = PlatformApp(
        platform="feishu",
        name="个人助理",
        capabilities=["assistant"],
        app_id="cli_example",
        secret_enc="x",
    )
    session.add_all([owner, app])
    await session.flush()
    await session.refresh(app)
    standard = Bot(
        bot_key="demo-bot",
        platform="feishu",
        name="demo-bot",
        created_by=owner.id,
        model="m",
        working_dir="/d",
        credentials_enc="unused",
    )
    assistant = new_assistant_bot(
        owner, app, relay_server_id=None, model="m", working_dir="/w", credentials_enc="unused"
    )
    session.add_all([standard, assistant])
    await instances.register(
        session, instance_id="gw-a", service="gateway-feishu", version="dev", capacity=None
    )
    await session.commit()
    return standard, assistant, app


async def test_assistant_records_never_get_a_bot_lease(db_session: AsyncSession) -> None:
    standard, assistant, _ = await _seed(db_session)
    await leases.ensure_rows(db_session, "feishu")
    await db_session.commit()
    assert await db_session.get(BotLease, assistant.id) is None
    assert await leases.acquirable(db_session, "feishu") == [standard.id]


async def test_default_store_serves_bots_and_app_store_serves_apps(
    db_session: AsyncSession, db_engine: AsyncEngine
) -> None:
    standard, assistant, app = await _seed(db_session)
    factory = make_session_factory(db_engine)
    bot_hooks, app_hooks = Hooks(), Hooks()
    bots = LeaseCoordinator(
        platform="feishu", instance_id=lambda: "gw-a", factory=factory, hooks=bot_hooks
    )
    apps = LeaseCoordinator(
        platform="feishu",
        instance_id=lambda: "gw-a",
        factory=factory,
        hooks=app_hooks,
        store=app_leases,
    )
    bot_hooks.coordinator, app_hooks.coordinator = bots, apps
    await bots.round()
    await apps.round()
    assert bot_hooks.connected == {standard.id: 1} and bot_hooks.maintained == [standard.id]
    assert app_hooks.connected == {app.id: 1} and app_hooks.maintained == [app.id]
    # 应用停用：下一轮排空并按代次释放；bot 那一边不受影响。
    app.enabled = False
    await db_session.commit()
    await apps.round()
    await bots.round()
    assert app_hooks.connected == {} and bot_hooks.connected == {standard.id: 1}
    row = await db_session.get(AppLease, app.id, populate_existing=True)
    assert row is not None and row.holder_instance is None
    lease = await db_session.get(BotLease, standard.id, populate_existing=True)
    assert lease is not None and lease.holder_instance == "gw-a"
