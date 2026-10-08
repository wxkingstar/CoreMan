"""按当前用户和当前授权解析业务系统；不信任机器人 env 中的任何身份字段。"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.auth.external_key import ExternalKey
from coreman.core.auth.provider_config import HTTPTokenProviderConfig
from coreman.core.auth.token_issues import IssuedRecord
from coreman.core.auth.token_providers import (
    PLATFORM_AUDIENCE as PLATFORM_AUDIENCE,
)
from coreman.core.auth.token_providers import (
    RESERVED_SYSTEM_KEYS as RESERVED_SYSTEM_KEYS,
)
from coreman.core.auth.token_providers import (
    TokenProviderError,
    issue_system_token,
)
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, BotSystemGrant, BusinessSystem, User
from coreman.core.prompting.system_prompt import Speaker
from coreman.core.systems_catalog import policy as catalog_policy


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


async def granted_systems(session: AsyncSession, bot_id: uuid.UUID) -> list[BusinessSystem]:
    """本轮可用的业务系统：签发令牌与目录工具共用这一个口径，工具不能越过它访问别的系统。"""
    grants = select(BotSystemGrant.system_key).where(BotSystemGrant.bot_id == bot_id)
    return list(
        (
            await session.execute(
                select(BusinessSystem)
                .where(
                    BusinessSystem.enabled,
                    BusinessSystem.key.not_in(RESERVED_SYSTEM_KEYS),
                    or_(BusinessSystem.default_for_all_bots, BusinessSystem.key.in_(grants)),
                    or_(
                        BusinessSystem.allowed_bot_ids.is_(None),
                        BusinessSystem.allowed_bot_ids.contains([bot_id]),
                    ),
                )
                .order_by(BusinessSystem.sort_order, BusinessSystem.key)
            )
        ).scalars()
    )


def token_env_var(system_key: str) -> str:
    return f"BOT_TOKEN_{system_key.upper()}"


def auth_mode(system: BusinessSystem) -> str:
    """内置签发方走 Cookie，外部签发方一律 Bearer（与 token_providers 的签发结果一致）。"""
    return "cookie" if system.token_provider == "builtin" else "bearer"


def auth_label(mode: str) -> str:
    return "Authorization: Bearer" if mode == "bearer" else "Cookie: bot_token"


# 目录可用时追加在「业务系统访问」段末尾；只约束标注了「可查操作目录」的系统。
CATALOG_RULES = (
    "标注「可查操作目录」的系统，调用前先用 systems_search 或 systems_browse 找到操作，"
    "再用 systems_describe 确认参数和调用方式；只调用目录中出现的操作，不要猜路径。"
)
# 有平台代理的系统时再追加：这些系统的令牌不进运行环境。
PROXY_RULES = (
    "标注「平台代理」的系统没有令牌变量，用 systems_call 调用：平台按目录校验参数，"
    "以当前发言者的身份代为请求。读操作直接执行；写操作需要管理员为本 AI 员工开启该系统的"
    "「允许写入」；不可撤销（destructive）和涉及资金（financial）的操作不代理，"
    "请用户本人到业务系统里操作。"
)
# 有令牌下发到运行环境的系统时追加：平台拦不住这些调用，只能靠提示词约束高风险操作。
ENV_RISK_RULES = (
    "持令牌直接调用的系统，不可撤销（destructive，如删除、作废、过账、冲销）和涉及资金"
    "（financial，如退款、打款）的操作，执行前先向当前发言者说明要调用的操作、对象和影响，"
    "等对方在后续消息里明确确认后再执行；定时任务等无人能当场确认的场景不执行，只说明应该怎么做。"
)


@dataclass
class SystemAccess:
    env: dict[str, str] = field(default_factory=dict)
    # 进 system prompt 的「业务系统访问」段：只取决于机器人被授权的系统与运行时，与发言者无关。
    prompt: str = ""
    subject: str | None = None
    # 本轮签发成功的令牌（不含令牌本身），调用方按任务上下文写进签发记录（token_issues）。
    issued: list[IssuedRecord] = field(default_factory=list)
    # 进本轮块的状态：这位发言者本轮拿不到凭据、或某个系统签发失败。
    note: str = ""


# 本轮一个令牌都没有时的说明。写成模型对用户的行动指引，不是错误码：静默不下发会让模型以为
# 业务系统本就没配，转而去猜账号或换一条路，那比直接说「去补邮箱」危险得多。
SUBJECT_UNAVAILABLE_NOTES = {
    "email_missing": (
        "- 业务系统：当前发言者的账号没有登记邮箱，平台无法为其签发业务系统令牌，本轮**没有**任何"
        "业务系统凭据可用。请告知用户：需要先在通讯录（企业微信 / 飞书）中补全企业邮箱，"
        "同步后才能通过我访问内部系统。不要尝试用其他账号、推测的账号或历史凭据代替。"
    ),
    "email_prefix_ambiguous": (
        "- 业务系统：当前发言者的邮箱前缀与另一个账号重复，无法唯一确定其在业务系统中的身份，平台"
        "因此拒绝签发令牌，本轮**没有**任何业务系统凭据可用。请告知用户联系平台管理员"
        "处理账号邮箱冲突。不要尝试用其他账号、推测的账号或历史凭据代替。"
    ),
}
IDENTITY_UNAVAILABLE_NOTE = (
    "- 业务系统：本轮发言者身份未验证或账号不可用，本轮**没有**任何业务系统凭据可用。"
    "不要尝试用其他账号、推测的账号或历史凭据代替。"
)


def systems_prompt(systems: list[BusinessSystem], *, mounted: bool) -> str:
    """「业务系统访问」段。只用机器人的授权与运行时能力，不碰发言者和目录内容：
    同一会话里逐字不变（目录刷新、换人都不会改它）。"""
    if not systems:
        return ""
    lines = []
    for system in systems:
        catalog = "，可查操作目录" if mounted and system.openapi_url else ""
        if system.token_delivery == "proxy":
            lines.append(
                f"- {system.name} ({system.key})：{system.base_url or ''}"
                "（平台代理：用 systems_call 调用）"
                if mounted and system.openapi_url
                else f"- {system.name}: 无法调用。这个系统只经平台代理调用，"
                "当前运行环境不支持或系统没有操作目录。不得改用其他身份或历史凭据。"
            )
            continue
        lines.append(
            f"- {system.name} ({system.key})：{system.base_url or ''}"
            f"（env: {token_env_var(system.key)}；{auth_label(auth_mode(system))}{catalog}）"
        )
    prompt = "## 业务系统访问\n\n" + "\n".join(lines)
    prompt += "\n\n完整配置见 `$COREMAN_SYSTEMS`（兼容 `$BOT_SYSTEMS_CONFIG`）。"
    if any(system.token_delivery != "proxy" for system in systems):
        prompt += (
            "令牌每轮按当前发言者重新签发（业务系统账号见 `$COREMAN_USER_SUBJECT`）。"
            "按配置的 auth_mode 使用令牌，只发送到对应业务系统；bearer 使用 Authorization 请求头。"
            "expires_at 是 UTC Unix 秒，令牌过期后停止调用，并请用户重新发起一轮任务；"
            "本轮运行中的进程不会自动更新令牌。本轮签发失败的系统见本轮块。"
        )
        prompt += "\n\n" + ENV_RISK_RULES
    if mounted:
        prompt += "\n\n" + CATALOG_RULES
        if any(s.token_delivery == "proxy" and s.openapi_url for s in systems):
            prompt += PROXY_RULES
    return prompt


async def build_system_access(
    session: AsyncSession,
    cipher: Cipher,
    *,
    bot: Bot,
    speaker: Speaker,
    issuer: str,
    external_key: ExternalKey | None,
    providers: Mapping[str, HTTPTokenProviderConfig] | None = None,
    task_timeout_seconds: int | None = None,
    catalog: catalog_policy.Mount | None = None,
) -> SystemAccess:
    """按系统选择签发方；external_key 仅供 builtin，任务预算可由体检等短任务缩小。

    `catalog`：本轮的运行时能挂载操作目录 MCP 时由调用方给出，凭据随 env 下发。
    """
    systems = await granted_systems(session, bot.id)
    # 目录工具只挂给能挂载它的运行时，而且至少一个系统配了 OpenAPI 地址：没配的系统挂上也查不到。
    mounted = catalog is not None and any(system.openapi_url for system in systems)
    prompt = systems_prompt(systems, mounted=mounted)
    unavailable = SystemAccess(prompt=prompt, note=IDENTITY_UNAVAILABLE_NOTE if systems else "")
    if not speaker.known or not speaker.login_name:
        return unavailable
    user = await session.get(User, speaker.user_id, populate_existing=True)
    if (
        user is None
        or user.status != "active"
        or user.login_name != speaker.login_name
        or user.source == "bootstrap"
    ):
        return unavailable
    try:
        subject = await token_subject(session, user)
    except SubjectUnavailable as exc:
        # 令牌一个都不发，并明确告诉用户缺什么。没配业务系统时不必解释。
        return SystemAccess(
            prompt=prompt, note=SUBJECT_UNAVAILABLE_NOTES[exc.reason] if systems else ""
        )
    if not systems:
        # 没有业务系统也下发 sub：提示词把它写成权威身份之一，就不能时有时无。
        return SystemAccess({"COREMAN_USER_SUBJECT": subject}, subject=subject)
    ttl_seconds = min(bot.sse_timeout_seconds, task_timeout_seconds or bot.sse_timeout_seconds)
    env: dict[str, str] = {}
    configs: list[dict[str, object]] = []
    failures: list[str] = []
    issued: list[IssuedRecord] = []
    for system in systems:
        if system.token_delivery == "proxy":
            # 令牌不进运行环境：由目录 MCP 的 systems_call 在首次调用时由服务端签发。
            configs.append(
                {
                    "key": system.key,
                    "name": system.name,
                    "description": system.description or "",
                    "base_url": system.base_url or "",
                    "openapi_url": system.openapi_url or "",
                    "sitemap_url": system.openapi_url or "",
                    "token_delivery": "proxy",
                }
            )
            continue
        try:
            token = await issue_system_token(
                session,
                cipher,
                system=system,
                subject=subject,
                name=user.display_name,
                ttl_seconds=ttl_seconds,
                issuer=issuer,
                external_key=external_key,
                providers=providers,
            )
        except TokenProviderError as exc:
            failures.append(
                f"- 业务系统 {system.name}（{system.key}）：本轮未获得访问令牌（{exc.code}），"
                "请联系管理员检查令牌提供方和当前用户授权。不得改用其他身份或历史凭据。"
            )
            continue
        env_var = token_env_var(system.key)
        env[env_var] = token.value
        issued.append(
            IssuedRecord(
                system_key=system.key,
                provider=system.token_provider,
                audience=system.token_audience or system.key,
                token_id=token.token_id,
                expires_at=token.expires_at,
            )
        )
        config: dict[str, object] = {
            "key": system.key,
            "name": system.name,
            "description": system.description or "",
            "base_url": system.base_url or "",
            "openapi_url": system.openapi_url or "",
            # 已改名为 openapi_url，保留一个版本给尚未改用新名的技能。
            "sitemap_url": system.openapi_url or "",
            "token_delivery": "env",
            "env_var": env_var,
            "audience": system.token_audience or system.key,
            "auth_mode": token.auth_mode,
            "expires_in": token.expires_in,
            "expires_at": token.expires_at,
        }
        if token.auth_mode == "cookie":
            config["cookie_name"] = "bot_token"
        configs.append(config)
    env["COREMAN_USER_SUBJECT"] = subject
    env["COREMAN_SYSTEMS"] = env["BOT_SYSTEMS_CONFIG"] = json.dumps(configs, ensure_ascii=False)
    if catalog is not None and mounted:
        env[catalog_policy.URL_ENV] = catalog.url
        env[catalog_policy.TOKEN_ENV] = catalog_policy.issue_capability(
            cipher, task_id=catalog.task_id, user_id=user.id, ttl_seconds=catalog.ttl_seconds
        )
    return SystemAccess(env, prompt, subject=subject, issued=issued, note="\n".join(failures))
