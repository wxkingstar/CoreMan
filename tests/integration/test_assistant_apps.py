"""助理应用的凭证读取、向助理记录同步、与普通机器人互斥。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants.apps import (
    SECRET_AAD,
    app_credentials,
    assistant_app_ids,
    sync_bot_credentials,
)
from coreman.core.assistants.records import new_assistant_bot
from coreman.core.bots.platform_account import reserve_feishu_app
from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json
from coreman.core.crypto import Cipher
from coreman.core.db.models import PlatformApp, User
from coreman.core.errors import ApiError

CIPHER = Cipher(b"\x05" * 32)


async def _app(session: AsyncSession, secret: str = "s3cret") -> PlatformApp:
    app = PlatformApp(
        platform="feishu",
        name="个人助理",
        capabilities=["assistant"],
        app_id="cli_example",
        secret_enc=CIPHER.encrypt(secret, SECRET_AAD),
    )
    session.add(app)
    await session.flush()
    return app


async def _owner(session: AsyncSession) -> User:
    owner = User(login_name=f"u{uuid.uuid4().hex[:8]}", display_name="所有者")
    session.add(owner)
    await session.flush()
    return owner


async def test_app_credentials(db_session: AsyncSession) -> None:
    app = await _app(db_session)
    await db_session.refresh(app)
    assert app_credentials(CIPHER, app) == {"app_id": "cli_example", "app_secret": "s3cret"}
    app.capabilities = ["login"]
    with pytest.raises(ValueError, match="not_assistant_app"):
        app_credentials(CIPHER, app)


async def test_sync_rewrites_only_changed_rows(db_session: AsyncSession) -> None:
    app = await _app(db_session)
    await db_session.refresh(app)
    bots = []
    for _ in range(2):
        bot = new_assistant_bot(
            await _owner(db_session),
            app,
            relay_server_id=None,
            model="m",
            working_dir="/w",
            credentials_enc="",
        )
        db_session.add(bot)
        bots.append(bot)
    await db_session.flush()
    assert await sync_bot_credentials(db_session, CIPHER, app) == 2
    for bot in bots:
        assert decrypt_json(CIPHER, bot.credentials_enc, CREDENTIALS_AAD) == {
            "app_id": "cli_example",
            "app_secret": "s3cret",
        }
    assert await sync_bot_credentials(db_session, CIPHER, app) == 0
    app.secret_enc = CIPHER.encrypt("rotated", SECRET_AAD)
    assert await sync_bot_credentials(db_session, CIPHER, app) == 2


async def test_standard_bot_cannot_take_assistant_app(db_session: AsyncSession) -> None:
    await _app(db_session)
    assert await assistant_app_ids(db_session) == {"cli_example"}
    with pytest.raises(ApiError):
        await reserve_feishu_app(db_session, CIPHER, {"app_id": "cli_example", "app_secret": "x"})
    await reserve_feishu_app(db_session, CIPHER, {"app_id": "cli_other", "app_secret": "x"})
