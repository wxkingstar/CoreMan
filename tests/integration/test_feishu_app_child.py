"""应用连接子进程每轮要看的状态、子进程入口，以及它装配出来的会话工厂带封存。"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from lark_oapi.ws import client as ws_client

from coreman.core.assistants import keys, sealed
from coreman.core.assistants.apps import connection_fingerprint
from coreman.core.assistants.records import new_assistant_bot
from coreman.core.assistants.session import SealedSession
from coreman.core.bus import app_leases, instances
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, PlatformApp, User, UserIdentity
from coreman.core.db.session import make_session_factory
from coreman.core.platforms.feishu import FeishuClient
from coreman.runtime.gateway_feishu import app_child
from tests.integration.assistant_helpers import raw
from tests.integration.test_assistant_gateway_inbound import APP, TEXT, message

CIPHER = Cipher(b"\x05" * 32)


def _app() -> PlatformApp:
    return PlatformApp(
        platform="feishu", name="个人助理", capabilities=["assistant"], app_id=APP, secret_enc="x"
    )


async def test_app_state_tracks_the_app_and_its_enabled_assistants(db_session, db_engine):
    app = _app()
    db_session.add(app)
    await db_session.flush()
    await db_session.refresh(app)
    bots = []
    for login in ("zhangsan", "lisi"):
        owner = User(login_name=login, display_name=login)
        db_session.add(owner)
        await db_session.flush()
        bot = new_assistant_bot(
            owner, app, relay_server_id=None, model="m", working_dir="/w", credentials_enc="x"
        )
        db_session.add(bot)
        bots.append(bot)
    db_session.add(
        Bot(
            bot_key="demo-bot",
            platform="feishu",
            name="demo-bot",
            created_by=bots[0].owner_id,
            model="m",
            working_dir="/d",
            credentials_enc="unused",
        )
    )
    await db_session.flush()
    bots[1].enabled = False
    await db_session.commit()
    factory = make_session_factory(db_engine)
    fingerprint, owned = await app_child.app_state(factory, CIPHER, app.id)
    assert fingerprint == connection_fingerprint(CIPHER, app) and owned == {str(bots[0].id)}
    app.secret_enc = "rotated"
    await db_session.commit()
    assert (await app_child.app_state(factory, CIPHER, app.id))[0] != fingerprint
    app.enabled = False
    await db_session.commit()
    assert await app_child.app_state(factory, CIPHER, app.id) == (None, set())
    assert await app_child.app_state(factory, CIPHER, uuid.uuid4()) == (None, set())


def test_run_process_gives_the_app_child_its_own_sdk_loop(monkeypatch):
    preloaded = ws_client.loop
    seen = []

    async def fake_run_app(app_id, instance_id, generation):
        seen.append(ws_client.loop)

    monkeypatch.setattr(ws_client, "loop", preloaded)
    monkeypatch.setattr(app_child, "run_app", fake_run_app)
    app_child.run_process(uuid.uuid4(), "instance", 1)
    assert seen and seen[0] is not preloaded and not seen[0].is_closed()


@pytest.mark.usefixtures("assistant_master")
async def test_private_message_through_the_child_wiring_is_stored_sealed(
    db_session, migrated_database
):
    app = _app()
    owner = User(login_name="zhangsan", display_name="张三")
    db_session.add_all([app, owner])
    await db_session.flush()
    await db_session.refresh(app)
    db_session.add(UserIdentity(user_id=owner.id, platform="feishu", platform_user_id="u_zhang"))
    # 开通时签发数据密钥，之后才有助理记录与它的内容行。
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    bot = new_assistant_bot(
        owner, app, relay_server_id=None, model="m", working_dir="/w", credentials_enc="x"
    )
    db_session.add(bot)
    await instances.register(
        db_session, instance_id="gw-a", service="gateway-feishu", version="dev", capacity=None
    )
    await app_leases.ensure_rows(db_session)
    generation = await app_leases.acquire(db_session, app.id, "gw-a")
    await db_session.commit()
    http = httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"code": 0})),
    )
    engine, guard_engine, factory = app_child.open_database(migrated_database)
    inbound = None
    try:
        # 子进程用的会话工厂就是带封存的那一个（与普通子进程、worker 同一个入口）。
        assert factory.class_ is SealedSession
        _, inbound = app_child.wire(
            factory,
            FeishuClient(APP, "synthetic", http=http),
            cipher=CIPHER,
            app_id=app.id,
            feishu_app_id=APP,
            instance_id="gw-a",
            generation=generation,
            guard=None,
        )
        assert await inbound.accept(message("u_zhang", "om1")) is None
    finally:
        if inbound:
            await inbound.aclose()
        await http.aclose()
        await engine.dispose()
        await guard_engine.dispose()
    stored = str(
        await raw(
            db_session, "SELECT payload::text FROM inbound_events WHERE bot_id = :b", b=bot.id
        )
    )
    assert json.loads(stored)["_sealed"].startswith("enc:v2:d1:")
    assert TEXT not in stored and json.dumps(TEXT)[1:-1] not in stored
