"""bots 的助理列与约束。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import BOT_KINDS, CAPABILITIES, Bot, PlatformApp, User


async def _owner_and_app(session: AsyncSession) -> tuple[User, PlatformApp]:
    owner = User(login_name=f"u{uuid.uuid4().hex[:8]}", display_name="所有者")
    app = PlatformApp(
        platform="feishu",
        name="个人助理",
        capabilities=["assistant"],
        app_id="cli_example",
        secret_enc="x",
    )
    session.add_all([owner, app])
    await session.flush()
    return owner, app


def _bot(owner: User, **over: object) -> Bot:
    values: dict[str, object] = {
        "bot_key": f"b-{uuid.uuid4().hex[:10]}",
        "platform": "feishu",
        "name": "demo-bot",
        "created_by": owner.id,
        "model": "m",
        "working_dir": "/d",
        "credentials_enc": "",
    }
    values.update(over)
    return Bot(**values)


def test_constants() -> None:
    assert BOT_KINDS == ("standard", "assistant")
    assert "assistant" in CAPABILITIES


async def test_standard_bot_defaults(db_session: AsyncSession) -> None:
    owner, _ = await _owner_and_app(db_session)
    bot = _bot(owner)
    db_session.add(bot)
    await db_session.flush()
    await db_session.refresh(bot)
    assert bot.kind == "standard" and bot.owner_id is None and bot.platform_app_id is None


async def test_assistant_requires_owner_and_app(db_session: AsyncSession) -> None:
    owner, app = await _owner_and_app(db_session)
    db_session.add(_bot(owner, kind="assistant", owner_id=owner.id))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
    owner, app = await _owner_and_app(db_session)
    db_session.add(_bot(owner, kind="assistant", owner_id=owner.id, platform_app_id=app.id))
    await db_session.flush()


async def test_standard_bot_cannot_carry_assistant_columns(db_session: AsyncSession) -> None:
    owner, app = await _owner_and_app(db_session)
    db_session.add(_bot(owner, platform_app_id=app.id))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_one_enabled_assistant_per_owner(db_session: AsyncSession) -> None:
    owner, app = await _owner_and_app(db_session)
    first = _bot(owner, kind="assistant", owner_id=owner.id, platform_app_id=app.id)
    db_session.add(first)
    await db_session.flush()
    first.enabled = False
    await db_session.flush()
    db_session.add(_bot(owner, kind="assistant", owner_id=owner.id, platform_app_id=app.id))
    await db_session.flush()  # 旧的已停用，可以再建一条
    db_session.add(_bot(owner, kind="assistant", owner_id=owner.id, platform_app_id=app.id))
    with pytest.raises(IntegrityError):
        await db_session.flush()
