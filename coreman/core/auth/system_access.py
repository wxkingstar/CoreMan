"""按当前用户和当前授权解析业务系统；不信任机器人 env 中的任何身份字段。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.auth.tokens import active_key, issue_token
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, BotSystemGrant, BusinessSystem, User
from coreman.core.prompting.system_prompt import Speaker

# 业务系统 key 同时是发言者令牌的 audience。CoreMan 管理 API 自己按 audience="coreman"
# 接受 bot_token（api/bot_auth.py，且免 CSRF）：若允许登记名为 coreman 的系统，平台就会
# 为每位发言者签发一枚能以其身份调用管理 API 的令牌，并注入 bot 管理员可控的 CLI。
PLATFORM_AUDIENCE = "coreman"
# 创建时拒绝；历史库里若已有同名行，签发与授权也一律跳过。
RESERVED_SYSTEM_KEYS = frozenset({PLATFORM_AUDIENCE})


@dataclass
class SystemAccess:
    env: dict[str, str] = field(default_factory=dict)
    prompt: str = ""


async def build_system_access(
    session: AsyncSession, cipher: Cipher, *, bot: Bot, speaker: Speaker, issuer: str
) -> SystemAccess:
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
    if not systems:
        return SystemAccess()
    key = await active_key(session, cipher)
    env = {
        f"BOT_TOKEN_{system.key.upper()}": issue_token(
            key,
            cipher,
            issuer=issuer,
            login=user.login_name or "",
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
    env["COREMAN_SYSTEMS"] = env["BOT_SYSTEMS_CONFIG"] = json.dumps(configs, ensure_ascii=False)
    prompt = "## 业务系统访问\n\n" + "\n".join(
        f"- {s.name}: {s.base_url or ''} (env: BOT_TOKEN_{s.key.upper()})" for s in systems
    )
    prompt += (
        "\n\n完整配置见 `$COREMAN_SYSTEMS`（兼容 `$BOT_SYSTEMS_CONFIG`）。"
        "令牌只属于当前发言者，不得复用此前轮次的值。"
    )
    return SystemAccess(env, prompt)
