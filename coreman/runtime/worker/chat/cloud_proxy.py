"""云服务代理的环境注入：对话轮与定时任务共用。"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.cloud_proxy import policy
from coreman.core.cloud_proxy.approvers import approver_logins
from coreman.core.db.models import Bot
from coreman.runtime.worker.context import TaskContext


async def configure(
    session: AsyncSession,
    ctx: TaskContext,
    *,
    bot: Bot,
    user_id: uuid.UUID | None,
    origin_kind: str,
    env: dict[str, str],
    session_key: str | None = None,
    relay_session_id: uuid.UUID | None = None,
    event_id: int | None = None,
    cron_job_id: uuid.UUID | None = None,
) -> dict[str, str]:
    # 审批人名单总是下发：agent 要用它在提示里告诉用户「找谁批」，与代理是否启用无关。
    logins = await approver_logins(session, bot.id)
    # 身份未知的发言者拿不到令牌：代理按令牌里的人记账与审批，不能匿名。
    if ctx.cloud_proxy_ready and user_id is not None:
        cap = policy.Capability(
            task_id=ctx.task.id,
            bot_id=bot.id,
            user_id=user_id,
            origin_kind=origin_kind,
            session_key=session_key,
            relay_session_id=relay_session_id,
            event_id=event_id,
            cron_job_id=cron_job_id,
        )
        env = policy.apply_env(
            env,
            ctx.cipher,
            cap,
            public_base_url=ctx.public_base_url,
            sse_timeout_seconds=bot.sse_timeout_seconds,
        )
    env[policy.APPROVERS_ENV] = ",".join(logins)
    return env
