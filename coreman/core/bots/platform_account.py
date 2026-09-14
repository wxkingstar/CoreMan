"""一个平台应用只能归属一个 CoreMan bot，避免连接随机分发给不同机器人。"""

from __future__ import annotations

import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json
from coreman.core.crypto import Cipher, DecryptError
from coreman.core.db.models import Bot
from coreman.core.errors import ApiError


async def reserve_feishu_app(
    session: AsyncSession,
    cipher: Cipher,
    credentials: dict[str, str],
    *,
    bot_id: uuid.UUID | None = None,
) -> None:
    app_id = credentials.get("app_id")
    if not app_id:
        return
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
        {"key": f"feishu-app-assignment:{app_id}"},
    )
    query = select(Bot).where(Bot.platform == "feishu")
    if bot_id is not None:
        query = query.where(Bot.id != bot_id)
    for other in await session.scalars(query):
        try:
            existing = decrypt_json(cipher, other.credentials_enc, CREDENTIALS_AAD)
        except (DecryptError, ValueError):
            continue  # 无法解密的配置不能建立连接，单独由该 bot 的运行状态报告。
        if existing.get("app_id") == app_id:
            raise ApiError(409, 409, "该飞书应用已分配给另一机器人")
