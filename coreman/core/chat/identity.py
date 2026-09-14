"""发言者身份解析（spec §8.2 步骤 1）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import User, UserIdentity
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
    """按 `platform_user_id` 或 `open_id` 找身份行，连同所属员工一起返回。"""
    stmt = (
        select(User, UserIdentity)
        .join(UserIdentity, UserIdentity.user_id == User.id)
        .where(
            UserIdentity.platform == platform,
            or_(UserIdentity.platform_user_id == value, UserIdentity.open_id == value),
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
