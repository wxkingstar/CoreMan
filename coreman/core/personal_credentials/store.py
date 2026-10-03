"""个人凭证的读写：逐条加密，AAD 绑定（AI 员工, 用户, 变量名）。只有开轮注入会解密明文。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.crypto import Cipher, DecryptError
from coreman.core.db.models import Bot, PersonalCredential
from coreman.core.logging import get_logger
from coreman.core.personal_credentials.policy import MIN_REDACT, key_problem, value_aad
from coreman.core.timeutils import utcnow

log = get_logger(__name__)
LAST_USED_EVERY = timedelta(hours=1)


@dataclass(frozen=True)
class Injected:
    """一轮注入的结果。`repr=False` 让任何 `repr`（日志、异常回溯、断言失败）都带不出明文。"""

    env: dict[str, str] = field(repr=False)
    secret_values: frozenset[str] = field(repr=False)
    names: tuple[str, ...]


async def save(
    session: AsyncSession,
    cipher: Cipher,
    *,
    bot_id: uuid.UUID,
    user_id: uuid.UUID,
    fields: list[dict[str, Any]],
    values: dict[str, str],
) -> list[str]:
    for spec in fields:
        key = str(spec["key"])
        enc = cipher.encrypt(values[key], value_aad(bot_id, user_id, key))
        label, secret = str(spec.get("label") or ""), bool(spec.get("secret", True))
        stmt = (
            insert(PersonalCredential)
            .values(
                bot_id=bot_id,
                user_id=user_id,
                env_key=key,
                label=label,
                secret=secret,
                value_enc=enc,
            )
            .on_conflict_do_update(
                index_elements=[
                    PersonalCredential.bot_id,
                    PersonalCredential.user_id,
                    PersonalCredential.env_key,
                ],
                set_={"label": label, "secret": secret, "value_enc": enc, "updated_at": func.now()},
            )
        )
        await session.execute(stmt)
    return sorted(str(f["key"]) for f in fields)


async def injected(
    session: AsyncSession, cipher: Cipher, *, bot_id: uuid.UUID, user_id: uuid.UUID
) -> Injected:
    rows = (
        await session.scalars(
            select(PersonalCredential)
            .where(PersonalCredential.bot_id == bot_id, PersonalCredential.user_id == user_id)
            .order_by(PersonalCredential.env_key)
            # expire_on_commit=False：同一会话里 Core UPDATE 改过的 last_used_at 不会回填到
            # identity map，必须强制按库里最新值刷新，节流才读得对。
            .execution_options(populate_existing=True)
        )
    ).all()
    now = utcnow()
    env: dict[str, str] = {}
    secrets: set[str] = set()
    used: list[uuid.UUID] = []
    for row in rows:
        # 策略收紧后的存量键名不越界。
        if key_problem(row.env_key):
            continue
        try:
            value = cipher.decrypt(row.value_enc, value_aad(bot_id, user_id, row.env_key))
        except DecryptError:
            log.warning("personal_credential_unreadable", env_key=row.env_key)
            continue
        env[row.env_key] = value
        if row.secret and len(value) >= MIN_REDACT:
            secrets.add(value)
        if row.last_used_at is None or row.last_used_at < now - LAST_USED_EVERY:
            used.append(row.id)
    if used:
        await session.execute(
            update(PersonalCredential)
            .where(PersonalCredential.id.in_(used))
            .values(last_used_at=now)
            .execution_options(synchronize_session=False)
        )
    return Injected(env, frozenset(secrets), tuple(env))


async def list_own(
    session: AsyncSession, user_id: uuid.UUID
) -> list[tuple[PersonalCredential, Bot]]:
    result = await session.execute(
        select(PersonalCredential, Bot)
        .join(Bot, Bot.id == PersonalCredential.bot_id)
        .where(PersonalCredential.user_id == user_id)
        .order_by(Bot.name, PersonalCredential.env_key)
    )
    return [(row, bot) for row, bot in result.all()]


def plain_value(cipher: Cipher, row: PersonalCredential) -> str | None:
    if row.secret:
        return None
    try:
        return cipher.decrypt(row.value_enc, value_aad(row.bot_id, row.user_id, row.env_key))
    except DecryptError:
        return None


async def update_value(
    session: AsyncSession,
    cipher: Cipher,
    *,
    bot_id: uuid.UUID,
    user_id: uuid.UUID,
    env_key: str,
    value: str,
) -> PersonalCredential | None:
    row = await session.scalar(
        select(PersonalCredential)
        .where(
            PersonalCredential.bot_id == bot_id,
            PersonalCredential.user_id == user_id,
            PersonalCredential.env_key == env_key,
        )
        .with_for_update()
    )
    if row is None:
        return None
    row.value_enc = cipher.encrypt(value, value_aad(bot_id, user_id, env_key))
    row.updated_at = utcnow()
    await session.flush()
    return row


async def delete(
    session: AsyncSession, *, bot_id: uuid.UUID, user_id: uuid.UUID, env_key: str
) -> bool:
    result = await session.execute(
        sql_delete(PersonalCredential)
        .where(
            PersonalCredential.bot_id == bot_id,
            PersonalCredential.user_id == user_id,
            PersonalCredential.env_key == env_key,
        )
        .returning(PersonalCredential.id)
    )
    return result.first() is not None
