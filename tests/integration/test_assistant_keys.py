"""数据密钥：生成一次、主密钥包裹、缓存 60 秒、销毁后不可用、主密钥轮换后仍可用。"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import keys
from coreman.core.crypto import Cipher, DecryptError, key_id_of
from coreman.core.db.models import AssistantKey, User

OLD = b"\x05" * 32
NEW = b"\x06" * 32
MASTER = Cipher(OLD)


@pytest.fixture(autouse=True)
def _clean_cache() -> Iterator[None]:
    keys.forget()
    yield
    keys.forget()


async def _owner(session: AsyncSession) -> uuid.UUID:
    user = User(login_name=f"u{uuid.uuid4().hex[:8]}", display_name="张三")
    session.add(user)
    await session.commit()
    return user.id


async def test_ensure_key_creates_once_and_wraps_with_master(db_session: AsyncSession) -> None:
    owner = await _owner(db_session)
    assert await keys.ensure_key(db_session, MASTER, owner) is True
    assert await keys.ensure_key(db_session, MASTER, owner) is False
    await db_session.commit()
    row = (await db_session.execute(select(AssistantKey))).scalar_one()
    assert row.dek_enc is not None and key_id_of(row.dek_enc) == "k1"
    data = await keys.data_cipher(db_session, MASTER, owner)
    token = data.encrypt("机密", "t.c:x")
    assert token.startswith("enc:v2:d1:") and data.decrypt(token, "t.c:x") == "机密"
    # 第二次命中缓存，是同一个对象。
    assert await keys.data_cipher(db_session, MASTER, owner) is data
    assert keys.cached(owner) is data


async def test_missing_key_is_reported(db_session: AsyncSession) -> None:
    owner = await _owner(db_session)
    with pytest.raises(keys.KeyMissing):
        await keys.data_cipher(db_session, MASTER, owner)


async def test_wrapped_key_is_bound_to_its_owner(db_session: AsyncSession) -> None:
    a, b = await _owner(db_session), await _owner(db_session)
    await keys.ensure_key(db_session, MASTER, a)
    await keys.ensure_key(db_session, MASTER, b)
    await db_session.execute(
        text(
            "UPDATE assistant_keys SET dek_enc ="
            " (SELECT dek_enc FROM assistant_keys WHERE user_id = :a) WHERE user_id = :b"
        ),
        {"a": a, "b": b},
    )
    await db_session.commit()
    with pytest.raises(DecryptError):
        await keys.data_cipher(db_session, MASTER, b)


async def test_destroy_makes_the_key_unavailable_for_good(db_session: AsyncSession) -> None:
    owner = await _owner(db_session)
    await keys.ensure_key(db_session, MASTER, owner)
    await db_session.commit()
    await keys.data_cipher(db_session, MASTER, owner)
    await keys.destroy_key(db_session, owner)
    await db_session.commit()
    row = (await db_session.execute(select(AssistantKey))).scalar_one()
    assert row.dek_enc is None and row.destroyed_at is not None
    with pytest.raises(keys.KeyDestroyed):
        await keys.data_cipher(db_session, MASTER, owner)
    # 写入路径的自动生成（reissue=False）不会把销毁撤销掉。
    assert await keys.ensure_key(db_session, MASTER, owner, reissue=False) is False
    with pytest.raises(keys.KeyDestroyed):
        await keys.data_cipher(db_session, MASTER, owner)


async def test_destroy_stamps_the_statement_time_not_the_transaction_start(
    db_session: AsyncSession,
) -> None:
    owner = await _owner(db_session)
    await keys.ensure_key(db_session, MASTER, owner)
    await db_session.commit()
    await db_session.execute(text("SELECT pg_sleep(0.2)"))
    inside = await db_session.scalar(text("SELECT clock_timestamp()"))
    await keys.destroy_key(db_session, owner)
    stamped = await db_session.scalar(select(AssistantKey.destroyed_at))
    await db_session.commit()
    assert stamped is not None and stamped >= inside


async def test_reissue_after_destroy_starts_a_new_generation(db_session: AsyncSession) -> None:
    owner = await _owner(db_session)
    await keys.ensure_key(db_session, MASTER, owner)
    await db_session.commit()
    first = await keys.data_cipher(db_session, MASTER, owner)
    old_token = first.encrypt("旧内容", "t.c:x")
    assert first.key_id == "d1" and keys.data_key_id(1) == "d1"
    await keys.destroy_key(db_session, owner)
    await db_session.commit()
    # 显式签发（开通流程）：代数加一，换一把新钥匙。
    assert await keys.ensure_key(db_session, MASTER, owner) is True
    await db_session.commit()
    row = (
        await db_session.execute(select(AssistantKey).execution_options(populate_existing=True))
    ).scalar_one()
    assert row.generation == 2 and row.dek_enc is not None and row.destroyed_at is None
    second = await keys.data_cipher(db_session, MASTER, owner)
    assert second.key_id == "d2"
    new_token = second.encrypt("新内容", "t.c:x")
    assert new_token.startswith("enc:v2:d2:") and second.decrypt(new_token, "t.c:x") == "新内容"
    # 新钥匙不认旧一代的密文：密钥标识对不上（按行读取时据此判为已锁定，见 Task 4）。
    with pytest.raises(DecryptError):
        second.decrypt(old_token, "t.c:x")
    # 已有可用密钥时再调不会又换一代。
    assert await keys.ensure_key(db_session, MASTER, owner) is False


async def test_destroy_elsewhere_takes_effect_within_one_ttl(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = await _owner(db_session)
    await keys.ensure_key(db_session, MASTER, owner)
    await db_session.commit()
    start = keys._now()
    data = await keys.data_cipher(db_session, MASTER, owner)
    # 别的进程销毁：本进程的缓存不知道。
    await db_session.execute(
        text("UPDATE assistant_keys SET dek_enc = NULL, destroyed_at = now() WHERE user_id = :u"),
        {"u": owner},
    )
    await db_session.commit()
    assert await keys.data_cipher(db_session, MASTER, owner) is data
    monkeypatch.setattr(keys, "_now", lambda: start + keys.KEY_TTL_SECONDS + 1)
    with pytest.raises(keys.KeyDestroyed):
        await keys.data_cipher(db_session, MASTER, owner)


async def test_uncommitted_key_is_not_cached(db_session: AsyncSession) -> None:
    owner = await _owner(db_session)
    await keys.ensure_key(db_session, MASTER, owner)
    await keys.data_cipher(db_session, MASTER, owner)
    await db_session.rollback()
    # 事务回滚后这把钥匙不存在；缓存里不能留着它，否则会用一把库里没有的钥匙加密。
    assert keys.cached(owner) is None
    with pytest.raises(keys.KeyMissing):
        await keys.data_cipher(db_session, MASTER, owner)


async def test_master_key_rotation_keeps_data_readable(db_session: AsyncSession) -> None:
    owner = await _owner(db_session)
    await keys.ensure_key(db_session, MASTER, owner)
    await db_session.commit()
    token = (await keys.data_cipher(db_session, MASTER, owner)).encrypt("机密", "t.c:x")
    keys.forget()
    rotated = Cipher(NEW, key_id="k2", previous={"k1": OLD})
    assert (await keys.data_cipher(db_session, rotated, owner)).decrypt(token, "t.c:x") == "机密"
    keys.forget()
    with pytest.raises(DecryptError):
        await keys.data_cipher(db_session, Cipher(NEW, key_id="k2"), owner)
