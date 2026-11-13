"""assistant_keys：每人一行。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import AssistantKey, User
from tests.conftest import BUSINESS_TABLES


async def _user(session: AsyncSession) -> User:
    user = User(login_name=f"u{uuid.uuid4().hex[:8]}", display_name="张三")
    session.add(user)
    await session.flush()
    return user


def test_table_is_truncated_between_tests() -> None:
    assert BUSINESS_TABLES.index("assistant_keys") < BUSINESS_TABLES.index("users")


async def test_one_key_per_user(db_session: AsyncSession) -> None:
    user = await _user(db_session)
    db_session.add(AssistantKey(user_id=user.id, dek_enc="enc:v2:k1:eA=="))
    await db_session.commit()
    # populate_existing：服务端默认值（created_at、generation）要重新读回来。
    row = await db_session.get(AssistantKey, user.id, populate_existing=True)
    assert row is not None and row.created_at is not None and row.destroyed_at is None
    assert row.generation == 1
    db_session.expunge(row)
    db_session.add(AssistantKey(user_id=user.id, dek_enc="enc:v2:k1:eQ=="))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_key_requires_existing_user(db_session: AsyncSession) -> None:
    db_session.add(AssistantKey(user_id=uuid.uuid4(), dek_enc="enc:v2:k1:eA=="))
    with pytest.raises(IntegrityError):
        await db_session.flush()
