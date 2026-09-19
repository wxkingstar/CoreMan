"""按当前用户和当前授权解析业务系统；不信任机器人 env 中的任何身份字段。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.auth.external_key import ExternalKey
from coreman.core.auth.tokens import issue_token, signing_key
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, BotSystemGrant, BusinessSystem, User
from coreman.core.prompting.system_prompt import Speaker

# 业务系统 key 同时是发言者令牌的 audience。CoreMan 管理 API 自己按 audience="coreman"
# 接受 bot_token（api/bot_auth.py，且免 CSRF）：若允许登记名为 coreman 的系统，平台就会
# 为每位发言者签发一枚能以其身份调用管理 API 的令牌，并注入 bot 管理员可控的 CLI。
PLATFORM_AUDIENCE = "coreman"
# 创建时拒绝；历史库里若已有同名行，签发与授权也一律跳过。
RESERVED_SYSTEM_KEYS = frozenset({PLATFORM_AUDIENCE})


class SubjectUnavailable(Exception):
    """这位员工现在算不出一个唯一的 sub。`reason` 决定提示用户去补什么。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


async def token_subject(session: AsyncSession, user: User) -> str:
    """业务系统令牌的主体（sub）：邮箱前缀（小写），业务系统按它对应自己的账号。

    **只认邮箱前缀**。曾经在没有邮箱时退回 `login_name`，但两者共用一个命名空间：
    `login_name` 在首次同步时生成（没拿到邮箱就是平台 userid）且此后不再变，于是甲的
    `login_name` 完全可能等于乙的邮箱前缀，而当时的冲突检测只比对别人的**邮箱**，
    两个人会拿到同一个 sub。取消回退之后，sub 的来源只剩一个，冲突也只需在一个维度上判。

    算不出唯一 sub 时抛 `SubjectUnavailable`，由调用方决定是拒绝签发还是提示用户补全；
    绝不返回一个可能对应两个人的值。
    """
    prefix = (user.email or "").split("@", 1)[0].strip().lower()
    if not prefix:
        raise SubjectUnavailable("email_missing")
    clash = await session.execute(
        select(User.id)
        .where(
            User.id != user.id,
            User.email.is_not(None),
            func.lower(func.split_part(User.email, "@", 1)) == prefix,
        )
        .limit(1)
    )
    if clash.first():
        # 不同域名、同前缀：谁都不给，比给错人强。
        raise SubjectUnavailable("email_prefix_ambiguous")
    return prefix


async def user_for_subject(session: AsyncSession, subject: str) -> User | None:
    """sub → 员工，与 `token_subject` 同一口径的反向查找。

    仍然要求唯一：库里出现两个同前缀邮箱时（`token_subject` 此后会拒签，但先前签出的
    令牌还没过期）一律认不出来。
    """
    prefix = subject.strip().lower()
    if not prefix:
        return None
    rows = (
        await session.execute(
            select(User)
            .where(
                User.email.is_not(None),
                func.lower(func.split_part(User.email, "@", 1)) == prefix,
                User.status == "active",
                User.source != "bootstrap",
            )
            .limit(2)
        )
    ).scalars()
    found = list(rows)
    return found[0] if len(found) == 1 else None


@dataclass
class SystemAccess:
    env: dict[str, str] = field(default_factory=dict)
    prompt: str = ""


# 拿不到唯一 sub 时替代「业务系统访问」段的说明。写成模型对用户的行动指引，不是错误码。
SUBJECT_UNAVAILABLE_PROMPTS = {
    "email_missing": (
        "## 业务系统访问\n\n"
        "当前发言者的账号没有登记邮箱，平台无法为其签发业务系统令牌，本轮**没有**任何"
        "业务系统凭据可用。请告知用户：需要先在通讯录（企业微信 / 飞书）中补全企业邮箱，"
        "同步后才能通过我访问内部系统。不要尝试用其他账号、推测的账号或历史凭据代替。"
    ),
    "email_prefix_ambiguous": (
        "## 业务系统访问\n\n"
        "当前发言者的邮箱前缀与另一个账号重复，无法唯一确定其在业务系统中的身份，平台"
        "因此拒绝签发令牌，本轮**没有**任何业务系统凭据可用。请告知用户联系平台管理员"
        "处理账号邮箱冲突。不要尝试用其他账号、推测的账号或历史凭据代替。"
    ),
}


async def build_system_access(
    session: AsyncSession,
    cipher: Cipher,
    *,
    bot: Bot,
    speaker: Speaker,
    issuer: str,
    external_key: ExternalKey | None,
) -> SystemAccess:
    """external_key 为部署配置的外部签发方密钥（Settings.external_jwt_key），有则用它签。"""
    if not speaker.known or not speaker.login_name:
        return SystemAccess()
    user = await session.get(User, speaker.user_id, populate_existing=True)
    if (
        user is None
        or user.status != "active"
        or user.login_name != speaker.login_name
        or user.source == "bootstrap"
    ):
        return SystemAccess()
    grants = select(BotSystemGrant.system_key).where(BotSystemGrant.bot_id == bot.id)
    systems = list(
        (
            await session.execute(
                select(BusinessSystem)
                .where(
                    BusinessSystem.enabled,
                    BusinessSystem.key.not_in(RESERVED_SYSTEM_KEYS),
                    or_(BusinessSystem.default_for_all_bots, BusinessSystem.key.in_(grants)),
                    or_(
                        BusinessSystem.allowed_bot_ids.is_(None),
                        BusinessSystem.allowed_bot_ids.contains([bot.id]),
                    ),
                )
                .order_by(BusinessSystem.sort_order, BusinessSystem.key)
            )
        ).scalars()
    )
    try:
        subject = await token_subject(session, user)
    except SubjectUnavailable as exc:
        # 令牌一个都不发，并明确告诉用户缺什么：静默不下发会让模型以为业务系统本就没配，
        # 转而去猜账号或换一条路，那比直接说「去补邮箱」危险得多。没配业务系统时不必解释。
        return SystemAccess(prompt=SUBJECT_UNAVAILABLE_PROMPTS[exc.reason] if systems else "")
    if not systems:
        # 没有业务系统也下发 sub：提示词把它写成权威身份之一，就不能时有时无。
        return SystemAccess({"COREMAN_USER_SUBJECT": subject})
    key = await signing_key(session, cipher, external_key)
    env = {
        f"BOT_TOKEN_{system.key.upper()}": issue_token(
            key,
            cipher,
            issuer=issuer,
            login=subject,
            name=user.display_name,
            audience=system.key,
            ttl=bot.sse_timeout_seconds + 300,
        )
        for system in systems
    }
    configs = [
        {
            "key": s.key,
            "name": s.name,
            "description": s.description or "",
            "base_url": s.base_url or "",
            "sitemap_url": s.sitemap_url or "",
            "env_var": f"BOT_TOKEN_{s.key.upper()}",
            "cookie_name": "bot_token",
        }
        for s in systems
    ]
    env["COREMAN_USER_SUBJECT"] = subject
    env["COREMAN_SYSTEMS"] = env["BOT_SYSTEMS_CONFIG"] = json.dumps(configs, ensure_ascii=False)
    prompt = "## 业务系统访问\n\n" + "\n".join(
        f"- {s.name}: {s.base_url or ''} (env: BOT_TOKEN_{s.key.upper()})" for s in systems
    )
    prompt += (
        "\n\n完整配置见 `$COREMAN_SYSTEMS`（兼容 `$BOT_SYSTEMS_CONFIG`）。"
        "令牌只属于当前发言者（业务系统账号见 `$COREMAN_USER_SUBJECT`），"
        "不得复用此前轮次的值，也不得写入文件、回复或工作区。"
    )
    return SystemAccess(env, prompt)
