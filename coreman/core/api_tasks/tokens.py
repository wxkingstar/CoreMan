"""个人令牌：生成、哈希与认证。库里只存 SHA-256，明文只在创建时交给本人一次。"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import Bot, PersonalAccessToken, User
from coreman.core.timeutils import utcnow

PREFIX = "cmt_"
SHOWN = 12  # 列表里显示的前几位（含前缀）
MAX_DAYS = 365
MAX_PER_USER = 20
NAME_MAX = 64


class TokenError(ValueError):
    """令牌相关的业务错误；`status` 是给 HTTP 层的状态码。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Minted:
    row: PersonalAccessToken
    plaintext: str


def digest(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def usable(row: PersonalAccessToken) -> bool:
    return row.revoked_at is None and row.expires_at > utcnow()


async def mint(
    session: AsyncSession, user: User, *, name: str, bot_ids: list[uuid.UUID], days: int
) -> Minted:
    """建一枚令牌。只能选本人能用、且是飞书的 AI 员工（白名单为空或本人在内）。"""
    from coreman.core.api_tasks.service import allowed_to_use

    name = name.strip()
    if not 1 <= len(name) <= NAME_MAX:
        raise TokenError(422, f"名称须为 1–{NAME_MAX} 个字符")
    if not 1 <= days <= MAX_DAYS:
        raise TokenError(422, f"有效期须为 1–{MAX_DAYS} 天")
    unique = list(dict.fromkeys(bot_ids))
    if not unique:
        raise TokenError(422, "请至少选择一个 AI 员工")
    bots = {b.id: b for b in await session.scalars(select(Bot).where(Bot.id.in_(unique)))}
    for bid in unique:
        bot = bots.get(bid)
        if bot is None or bot.platform != "feishu":
            raise TokenError(422, "只能选择飞书 AI 员工")
        if not await allowed_to_use(session, bot, user.id):
            raise TokenError(403, f"你不在「{bot.name}」的可用名单里")
    # 锁住本人的用户行再数：同一个人并发建令牌在这里排队，上限不会被挤破。
    await session.execute(select(User.id).where(User.id == user.id).with_for_update())
    active = await session.scalar(
        select(func.count())
        .select_from(PersonalAccessToken)
        .where(
            PersonalAccessToken.user_id == user.id,
            PersonalAccessToken.revoked_at.is_(None),
            PersonalAccessToken.expires_at > utcnow(),
        )
    )
    if (active or 0) >= MAX_PER_USER:
        raise TokenError(409, f"每人最多 {MAX_PER_USER} 枚有效令牌，请先吊销不用的")
    plaintext = PREFIX + secrets.token_urlsafe(32)
    row = PersonalAccessToken(
        user_id=user.id,
        name=name,
        token_prefix=plaintext[:SHOWN],
        token_hash=digest(plaintext),
        bot_ids=unique,
        expires_at=utcnow() + timedelta(days=days),
    )
    session.add(row)
    await session.flush()
    return Minted(row, plaintext)


async def authenticate(
    session: AsyncSession, authorization: str
) -> tuple[PersonalAccessToken, User]:
    """`Authorization: Bearer cmt_…` → (令牌, 所有人)。任何不对都只回同一句，不区分原因。"""
    denied = TokenError(401, "令牌无效、已吊销或已过期")
    if not authorization.startswith("Bearer ") or len(authorization) > 512:
        raise denied
    plaintext = authorization[7:].strip()
    if not plaintext.startswith(PREFIX):
        raise denied
    row = await session.scalar(
        select(PersonalAccessToken).where(PersonalAccessToken.token_hash == digest(plaintext))
    )
    if row is None or not usable(row):
        raise denied
    user = await session.get(User, row.user_id)
    if user is None or user.status != "active" or user.source == "bootstrap":
        raise denied
    await session.execute(
        update(PersonalAccessToken)
        .where(PersonalAccessToken.id == row.id)
        .values(last_used_at=utcnow())
    )
    return row, user
