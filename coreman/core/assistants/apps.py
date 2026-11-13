"""助理共用的飞书应用：凭证以 platform_apps 为准，助理记录上保存一份副本。

副本让现有按记录读取飞书凭证的代码不必区分类型；应用凭证变更时在同一事务里
把副本同步到该应用下的全部助理记录。
"""

from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json, encrypt_json
from coreman.core.crypto import Cipher, DecryptError
from coreman.core.db.models import Bot, PlatformApp

SECRET_AAD = "platform_apps.secret_enc"


def is_assistant_app(app: PlatformApp) -> bool:
    """网关要为它维持长连接的应用：飞书、启用、带 assistant 能力、填了 app_id。"""
    return bool(
        app.platform == "feishu" and app.enabled and "assistant" in app.capabilities and app.app_id
    )


def connection_fingerprint(cipher: Cipher, app: PlatformApp) -> str:
    """长连接用到的凭证的指纹：变了就要重连。

    按解密后的密钥算：管理台每次保存应用都会重新加密（密文变、密钥不变），不能因此重连。
    密文解不开时退回按密文本身比较，不抛错——supervisor 每轮都要算它。
    """
    try:
        secret = cipher.decrypt(app.secret_enc, SECRET_AAD)
    except DecryptError:
        secret = app.secret_enc
    return hashlib.sha256(f"{app.app_id}\n{secret}".encode()).hexdigest()


def app_credentials(cipher: Cipher, app: PlatformApp) -> dict[str, str]:
    if not is_assistant_app(app) or not app.app_id:
        raise ValueError("not_assistant_app")
    return {"app_id": app.app_id, "app_secret": cipher.decrypt(app.secret_enc, SECRET_AAD)}


async def sync_bot_credentials(session: AsyncSession, cipher: Cipher, app: PlatformApp) -> int:
    """把应用凭证写到该应用下每条助理记录；内容没变的行不动，免得网关白白重连。"""
    wanted = app_credentials(cipher, app)
    changed = 0
    for bot in await session.scalars(select(Bot).where(Bot.platform_app_id == app.id)):
        try:
            current = decrypt_json(cipher, bot.credentials_enc, CREDENTIALS_AAD)
        except (DecryptError, ValueError):
            current = {}
        if current != wanted:
            bot.credentials_enc = encrypt_json(cipher, wanted, CREDENTIALS_AAD)
            changed += 1
    await session.flush()
    return changed


async def assistant_app_ids(session: AsyncSession) -> set[str]:
    rows = await session.scalars(
        select(PlatformApp.app_id).where(
            PlatformApp.platform == "feishu",
            PlatformApp.capabilities.contains(["assistant"]),
            PlatformApp.app_id.is_not(None),
        )
    )
    return {app_id for app_id in rows if app_id}
