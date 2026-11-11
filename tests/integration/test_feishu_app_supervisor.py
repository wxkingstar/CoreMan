"""飞书 supervisor：助理应用一个应用一个子进程，助理记录自己不起子进程。"""

from __future__ import annotations

import asyncio
import uuid

from coreman.core.assistants.records import new_assistant_bot
from coreman.core.db.models import AppLease, Bot, BotLease, PlatformApp, User
from coreman.runtime.gateway_feishu.service import GatewayFeishuService
from tests.integration.test_feishu_supervisor import FakeProcess


async def _seed(session):
    owner = User(login_name=f"u{uuid.uuid4().hex[:8]}", display_name="张三")
    app = PlatformApp(
        platform="feishu",
        name="个人助理",
        capabilities=["assistant"],
        app_id="cli_example",
        secret_enc="first",
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
    session.add(standard)
    await session.commit()
    return owner, app, standard


async def _started(service, *keys):
    for _ in range(200):
        if all(k in service.children and service.children[k].process for k in keys):
            return
        await asyncio.sleep(0.02)
    raise AssertionError("children did not start")


async def test_app_gets_one_child_even_without_assistants_and_assistants_get_none(
    db_session, runtime_settings, monkeypatch
):
    owner, app, standard = await _seed(db_session)
    service = GatewayFeishuService(port=0, interval=30)
    spawned: list[tuple[uuid.UUID, str, FakeProcess]] = []

    async def spawn(key, child):
        process = FakeProcess()
        spawned.append((key, child.kind, process))
        return process

    monkeypatch.setattr(service, "spawn", spawn)
    await service.on_start()
    try:
        # 应用下一条助理记录都没有，也要连上：第一个人私聊时才收得到事件。
        await _started(service, app.id, standard.id)
        assert {(key, kind) for key, kind, _ in spawned} == {
            (app.id, "app"),
            (standard.id, "bot"),
        }
        async with service.factory() as session:
            lease = await session.get(AppLease, app.id)
            assert lease.holder_instance == service.instance_id
        # 之后有了助理记录：不为它单独起子进程，也没有 bot 租约。
        assistant = new_assistant_bot(
            owner, app, relay_server_id=None, model="m", working_dir="/w", credentials_enc="x"
        )
        db_session.add(assistant)
        await db_session.commit()
        await service.reconcile()
        assert len(spawned) == 2 and assistant.id not in service.children
        async with service.factory() as session:
            assert await session.get(BotLease, assistant.id) is None
        # 密钥变了：停掉旧进程，起一个新的。
        app.secret_enc = "rotated"
        await db_session.commit()
        await service.reconcile()
        app_processes = [p for key, _, p in spawned if key == app.id]
        assert len(app_processes) == 2 and app_processes[0].terminated
        # 应用停用：停掉子进程并释放租约；普通机器人的子进程还在。
        app.enabled = False
        await db_session.commit()
        await service.reconcile()
        assert app.id not in service.children and app_processes[1].terminated
        assert standard.id in service.children
        async with service.factory() as session:
            lease = await session.get(AppLease, app.id)
            assert lease.holder_instance is None
    finally:
        await service.on_shutdown()


async def test_crashed_app_child_restarts_with_backoff_and_shutdown_releases(
    db_session, runtime_settings, monkeypatch
):
    _, app, standard = await _seed(db_session)
    service = GatewayFeishuService(port=0, interval=30)
    processes: dict[uuid.UUID, list[FakeProcess]] = {app.id: [], standard.id: []}

    async def spawn(key, child):
        process = FakeProcess()
        processes[key].append(process)
        return process

    monkeypatch.setattr(service, "spawn", spawn)
    await service.on_start()
    try:
        await _started(service, app.id, standard.id)
        child = service.children[app.id]
        processes[app.id][0].returncode = 1
        await service.reconcile()
        assert child.process is None and len(processes[app.id]) == 1
        async with service.factory() as session:
            assert (await session.get(AppLease, app.id)).connection_state == "disconnected"
        child.next_start = 0
        await service.reconcile()
        assert len(processes[app.id]) == 2
    finally:
        await service.on_shutdown()
    assert processes[app.id][-1].terminated and processes[standard.id][-1].terminated
    lease = await db_session.get(AppLease, app.id, populate_existing=True)
    assert lease.holder_instance is None


async def test_deleted_app_stops_its_child(db_session, runtime_settings, monkeypatch):
    _, app, standard = await _seed(db_session)
    service = GatewayFeishuService(port=0, interval=30)
    processes: dict[uuid.UUID, FakeProcess] = {}

    async def spawn(key, child):
        processes[key] = FakeProcess()
        return processes[key]

    monkeypatch.setattr(service, "spawn", spawn)
    await service.on_start()
    try:
        await _started(service, app.id, standard.id)
        await db_session.delete(app)
        await db_session.commit()
        await service.reconcile()
        assert app.id not in service.children and processes[app.id].terminated
        assert standard.id in service.children
    finally:
        await service.on_shutdown()
