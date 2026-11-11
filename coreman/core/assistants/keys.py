"""个人助理的数据密钥：每人一把 256 位密钥，用平台主密钥包裹后存 `assistant_keys`。

数据密钥只在平台进程内解开，不进任务载荷、环境变量、日志或异常消息。解开后的密钥在进程内
缓存 `KEY_TTL_SECONDS`：别的进程销毁之后，本进程最迟这么久就读不出来。
"""

from __future__ import annotations

import base64
import os
import time
import uuid
from typing import Any

from sqlalchemy import event, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from coreman.core.crypto import Cipher
from coreman.core.db.models import AssistantKey

KEY_TTL_SECONDS = 60.0
# 本会话里刚插入、可能还没提交的密钥：不进缓存，事务回滚时才不会留下一把库里没有的钥匙。
_FRESH = "assistant_keys_fresh"
_CACHE: dict[uuid.UUID, tuple[Cipher, float]] = {}


class KeyUnavailable(Exception):
    """所有者的数据密钥取不到：对应内容不可读。"""


class KeyMissing(KeyUnavailable):
    """还没有生成过数据密钥。"""


class KeyDestroyed(KeyUnavailable):
    """数据密钥已销毁。"""


def _now() -> float:
    return time.monotonic()


def wrap_aad(owner_id: uuid.UUID) -> str:
    return f"assistant_keys.dek_enc:{owner_id}"


def data_key_id(generation: int) -> str:
    """内容密文里的密钥标识：记下是第几代数据密钥加密的。与主密钥的 kid 无关。"""
    return f"d{generation}"


def forget(owner_id: uuid.UUID | None = None) -> None:
    """清本进程的缓存；不传就全清。"""
    if owner_id is None:
        _CACHE.clear()
    else:
        _CACHE.pop(owner_id, None)


def cached(owner_id: uuid.UUID) -> Cipher | None:
    hit = _CACHE.get(owner_id)
    return hit[0] if hit is not None and hit[1] > _now() else None


async def ensure_key(
    session: AsyncSession, cipher: Cipher, owner_id: uuid.UUID, *, reissue: bool = True
) -> bool:
    """保证所有者有一把可用的数据密钥，返回是否签发了新的。调用方负责提交。

    没有密钥行：生成第 1 代。密钥已销毁：`reissue` 为真时签发下一代（代数加一、清掉销毁时间），
    旧一代加密的内容从此按已锁定处理；为假时不动。已有可用密钥：不动。

    Args:
        reissue: 开通流程用默认的 True；写入路径首次用到时的自动生成必须传 False，
            否则停用之后一次迟到的写入就会把销毁撤销掉
    """
    token = cipher.encrypt(base64.b64encode(os.urandom(32)).decode("ascii"), wrap_aad(owner_id))
    stmt = insert(AssistantKey).values(user_id=owner_id, dek_enc=token)
    if reissue:
        stmt = stmt.on_conflict_do_update(
            index_elements=[AssistantKey.user_id],
            set_={
                "dek_enc": stmt.excluded.dek_enc,
                "generation": AssistantKey.generation + 1,
                "destroyed_at": None,
            },
            where=AssistantKey.destroyed_at.is_not(None),
        )
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=[AssistantKey.user_id])
    row = (await session.execute(stmt.returning(AssistantKey.user_id))).first()
    forget(owner_id)
    if row is None:
        return False
    session.info.setdefault(_FRESH, set()).add(owner_id)
    return True


_KEY_ROW = select(AssistantKey.dek_enc, AssistantKey.destroyed_at, AssistantKey.generation)


def _unwrap(info: dict[Any, Any], cipher: Cipher, owner_id: uuid.UUID, row: Any) -> Cipher:
    """把查到的密钥行解成 Cipher 并按需进缓存；`info` 是会话的 info 字典。"""
    if row is None:
        raise KeyMissing("所有者没有数据密钥")
    if row[1] is not None or not row[0]:
        raise KeyDestroyed("所有者的数据密钥已销毁")
    data = Cipher(
        base64.b64decode(cipher.decrypt(row[0], wrap_aad(owner_id))),
        key_id=data_key_id(int(row[2])),
    )
    if owner_id not in info.get(_FRESH, ()):
        _CACHE[owner_id] = (data, _now() + KEY_TTL_SECONDS)
    return data


async def data_cipher(session: AsyncSession, cipher: Cipher, owner_id: uuid.UUID) -> Cipher:
    """用所有者的数据密钥构造的 Cipher。

    Raises:
        KeyMissing: 没有密钥行
        KeyDestroyed: 密钥已销毁
        DecryptError: 主密钥解不开包裹（主密钥配错，或密钥行被挪动）
    """
    hit = cached(owner_id)
    if hit is not None:
        return hit
    _CACHE.pop(owner_id, None)
    row = (await session.execute(_KEY_ROW.where(AssistantKey.user_id == owner_id))).first()
    return _unwrap(session.info, cipher, owner_id, row)


def data_cipher_sync(session: Session, cipher: Cipher, owner_id: uuid.UUID) -> Cipher:
    """`data_cipher` 的同步版：给 flush 钩子用，走会话当前事务的同步连接。只取用，不签发。

    Raises:
        KeyMissing: 没有密钥行
        KeyDestroyed: 密钥已销毁
        DecryptError: 主密钥解不开包裹
    """
    hit = cached(owner_id)
    if hit is not None:
        return hit
    _CACHE.pop(owner_id, None)
    row = session.connection().execute(_KEY_ROW.where(AssistantKey.user_id == owner_id)).first()
    return _unwrap(session.info, cipher, owner_id, row)


async def destroy_key(session: AsyncSession, owner_id: uuid.UUID) -> None:
    """清空包裹后的密钥并记下时间；之后 `data_cipher` 抛 `KeyDestroyed`。调用方负责提交。

    本进程立即生效；其他进程最迟 `KEY_TTL_SECONDS` 后生效。
    """
    await session.execute(
        update(AssistantKey)
        .where(AssistantKey.user_id == owner_id, AssistantKey.destroyed_at.is_(None))
        .values(dek_enc=None, destroyed_at=func.now())
    )
    forget(owner_id)


@event.listens_for(Session, "after_commit")
@event.listens_for(Session, "after_rollback")
def _settle_fresh(session: Session) -> None:
    """事务结束：提交后的密钥已在库里，可以进缓存；回滚后的已不存在。两种情况都不再算「刚插入」。"""
    session.info.pop(_FRESH, None)
