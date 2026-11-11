"""按应用与所有者找助理记录；应用是否可服务与连接指纹。"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants.apps import (
    SECRET_AAD,
    app_credentials,
    connection_fingerprint,
    is_assistant_app,
)
from coreman.core.assistants.records import new_assistant_bot
from coreman.core.assistants.routing import assistant_for
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, PlatformApp, User

CIPHER = Cipher(b"\x05" * 32)


async def _app(session: AsyncSession, app_id: str = "cli_example") -> PlatformApp:
    app = PlatformApp(
        platform="feishu",
        name="个人助理",
        capabilities=["assistant"],
        app_id=app_id,
        secret_enc=CIPHER.encrypt("s3cret", SECRET_AAD),
    )
    session.add(app)
    await session.flush()
    await session.refresh(app)
    return app


async def _owner(session: AsyncSession, name: str) -> User:
    owner = User(login_name=f"u{uuid.uuid4().hex[:8]}", display_name=name)
    session.add(owner)
    await session.flush()
    return owner


async def _assistant(session: AsyncSession, owner: User, app: PlatformApp) -> Bot:
    bot = new_assistant_bot(
        owner, app, relay_server_id=None, model="m", working_dir="/w", credentials_enc="unused"
    )
    session.add(bot)
    await session.flush()
    return bot


async def test_assistant_for_matches_app_owner_and_enabled(db_session: AsyncSession) -> None:
    app, other_app = await _app(db_session), await _app(db_session, "cli_other")
    zhang, li = await _owner(db_session, "张三"), await _owner(db_session, "李四")
    bot = await _assistant(db_session, zhang, app)
    found = await assistant_for(db_session, app.id, zhang.id)
    assert found is not None and found.id == bot.id
    assert await assistant_for(db_session, app.id, li.id) is None
    assert await assistant_for(db_session, other_app.id, zhang.id) is None
    bot.enabled = False
    await db_session.flush()
    assert await assistant_for(db_session, app.id, zhang.id) is None


async def test_standard_bot_is_never_routed(db_session: AsyncSession) -> None:
    app = await _app(db_session)
    zhang = await _owner(db_session, "张三")
    db_session.add(
        Bot(
            bot_key="demo-bot",
            platform="feishu",
            name="demo-bot",
            created_by=zhang.id,
            model="m",
            working_dir="/d",
            credentials_enc="unused",
        )
    )
    await db_session.flush()
    assert await assistant_for(db_session, app.id, zhang.id) is None


async def test_is_assistant_app_and_fingerprint(db_session: AsyncSession) -> None:
    app = await _app(db_session)
    assert is_assistant_app(app)
    assert app_credentials(CIPHER, app)["app_id"] == "cli_example"
    before = connection_fingerprint(CIPHER, app)
    app.name = "改个名字"
    app.secret_enc = CIPHER.encrypt("s3cret", SECRET_AAD)  # 保存时重新加密：密文变，密钥没变
    assert connection_fingerprint(CIPHER, app) == before
    app.secret_enc = CIPHER.encrypt("rotated", SECRET_AAD)
    assert connection_fingerprint(CIPHER, app) != before
    # 解不开的密文不抛错（supervisor 每轮都要算），退回按密文本身比较。
    app.secret_enc = "garbage-1"
    broken = connection_fingerprint(CIPHER, app)
    app.secret_enc = "garbage-2"
    assert connection_fingerprint(CIPHER, app) != broken
    for change in ({"enabled": False}, {"capabilities": ["login"]}, {"app_id": None}):
        fresh = PlatformApp(
            platform="feishu",
            name="x",
            capabilities=["assistant"],
            app_id="cli_example",
            secret_enc="x",
            enabled=True,
        )
        for field, value in change.items():
            setattr(fresh, field, value)
        assert not is_assistant_app(fresh)
