"""发言者身份解析。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, InboundEvent, User, UserIdentity
from coreman.core.prompting.system_prompt import Speaker

if TYPE_CHECKING:  # 只用于类型：openuserid 反过来要 db.models，直接导会绕成环。
    from coreman.core.chat.openuserid import OpenUseridResolver


def looks_like_open_userid(value: str) -> bool:
    """企微 open_userid（外部应用视角的密文 id）：`wo` 开头且很长。

    这类 id 不在 `user_identities.platform_user_id` 里，只可能落在 `open_id` 列；查不到时
    才值得花一次企微接口去解密（见 `OpenUseridResolver`）。
    """
    return value.startswith("wo") and len(value) >= 30


async def _lookup(
    session: AsyncSession, platform: str, value: str
) -> tuple[User, UserIdentity] | None:
    """飞书仅按主 ID 查找；企微兼容 open_id，连同所属员工返回。"""
    stmt = (
        select(User, UserIdentity)
        .join(UserIdentity, UserIdentity.user_id == User.id)
        .where(
            UserIdentity.platform == platform,
            (UserIdentity.platform_user_id == value)
            if platform == "feishu"
            else or_(UserIdentity.platform_user_id == value, UserIdentity.open_id == value),
        )
        # platform_user_id 命中优先于 open_id：同一个值理论上可能既是甲的 userid 又是乙的
        # open_id，此时按「本平台的主 id」判定。
        .order_by((UserIdentity.platform_user_id == value).desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).first()  # type: ignore[return-value]


async def resolve_speaker(
    session: AsyncSession,
    *,
    platform: str,
    platform_user_id: str,
    resolver: OpenUseridResolver | None = None,
) -> Speaker:
    """平台 id → 内部员工。解析不出来就返回「未知发言者」，绝不抛错拦住这轮对话。

    密文 open_userid 走三步：先查 `open_id` 列，再让 `resolver` 找企微要明文 userid，
    解出来后把 `open_id` 写回那条身份行——下一条消息就是一次普通查询，不必再打企微。
    """
    row = await _lookup(session, platform, platform_user_id)
    if row is not None:
        user, ident = row
        if user.status != "active" or user.source == "bootstrap":
            return Speaker(platform_user_id, None, None, None)
        # 密文 id 命中的是 open_id 列：对外仍用明文 userid（env 与 chat_logs 都要它）。
        pid = (
            ident.platform_user_id if looks_like_open_userid(platform_user_id) else platform_user_id
        )
        return Speaker(pid, user.id, user.login_name, user.display_name)
    if not looks_like_open_userid(platform_user_id) or resolver is None:
        return Speaker(platform_user_id, None, None, None)
    userid = await resolver.resolve(platform_user_id)
    if not userid:
        return Speaker(platform_user_id, None, None, None)
    row = await _lookup(session, platform, userid)
    if row is None:
        return Speaker(platform_user_id, None, None, None)
    user, ident = row
    if user.status != "active" or user.source == "bootstrap":
        return Speaker(userid, None, None, None)
    ident.open_id = platform_user_id
    await session.flush()
    return Speaker(userid, user.id, user.login_name, user.display_name)


async def resolve_feishu_event_speaker(
    session: AsyncSession, *, bot: Bot, event: InboundEvent, cipher: Cipher
) -> Speaker:
    """Resolve union identity from authenticated gateway's durable event only.

    Raw event and normalized columns must agree with the current bot app.
    App-specific open_id is comparison evidence, never a global lookup key.
    """
    pid = event.sender_platform_user_id or ""
    unknown = Speaker(pid, None, None, None)
    try:
        sender = event.payload.get("sender") or {}
        raw = event.payload.get("raw") or {}
        header = raw.get("header") or {}
        source = raw.get("event") or {}
        raw_sender = source.get("sender") or {}
        ids = raw_sender.get("sender_id") or {}
        message = source.get("message") or {}
        union_id = ids.get("union_id")
        raw_chat_type = message.get("chat_type")
        if not union_id and not sender.get("union_id"):
            return await resolve_speaker(session, platform="feishu", platform_user_id=pid)
        credentials = decrypt_json(cipher, bot.credentials_enc, CREDENTIALS_AAD)
        if (
            event.bot_id != bot.id
            or event.platform != "feishu"
            or bot.platform != "feishu"
            or not bot.enabled
            or event.kind != "message"
            or not credentials.get("app_id")
            or not credentials.get("app_secret")
            or header.get("app_id") != credentials["app_id"]
            or header.get("event_type") != "im.message.receive_v1"
            or message.get("chat_id") != event.chat_id
            or message.get("message_id") != event.platform_msg_id
            or not isinstance(raw_chat_type, str)
            or {"p2p": "single", "group": "group"}.get(raw_chat_type) != event.chat_type
            or raw_sender.get("sender_type") != "user"
            or sender.get("sender_type") != "user"
            or not event.sender_open_id
            or ids.get("open_id") != event.sender_open_id
            or sender.get("open_id") != event.sender_open_id
            or (ids.get("user_id") or "") != pid
            or (sender.get("platform_user_id") or "") != pid
            or not isinstance(union_id, str)
            or not union_id.strip()
            or sender.get("union_id") != union_id
        ):
            return unknown
    except (AttributeError, TypeError, ValueError):
        return unknown
    rows = (
        await session.execute(
            select(User, UserIdentity)
            .join(UserIdentity, UserIdentity.user_id == User.id)
            .where(UserIdentity.platform == "feishu", UserIdentity.union_id == union_id)
            .execution_options(populate_existing=True)
        )
    ).all()
    if pid:
        explicit = await _lookup(session, "feishu", pid)
        if explicit is None:
            return unknown
        user, ident = explicit
        if (ident.union_id and ident.union_id != union_id) or any(
            candidate.id != ident.id for _, candidate in rows
        ):
            return unknown
    else:
        if len(rows) != 1:
            return unknown
        user, ident = rows[0]
    if (
        user.status != "active"
        or user.source == "bootstrap"
        or not ident.platform_user_id
        or (pid and pid != ident.platform_user_id)
    ):
        return unknown
    return Speaker(ident.platform_user_id, user.id, user.login_name, user.display_name)
