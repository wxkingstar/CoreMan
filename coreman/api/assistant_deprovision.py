"""开始停用一位助理并记审计：管理员在助理列表里停用、所有者在「我的助理」页停用，共用这一处。

停用不可撤销，重复请求不重复做；审计只在这一次真的开始了停用时记（新开的停用流程，或把宽限期里
的停用推进到现在），重复点、已经停用完的都不记。
"""

from __future__ import annotations

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip
from coreman.core.assistants import deprovisioning, lifecycle
from coreman.core.audit import record_audit
from coreman.core.db.models import Bot, User


async def begin(
    session: AsyncSession, request: Request, bot: Bot, *, trigger: str, actor: User
) -> str:
    """开始停用 `bot`，返回它之后的状态：`deprovisioning`，或早已停用完的 `deprovisioned`。

    Args:
        trigger: `owner`（所有者本人）或 `admin`（平台管理员）

    调用方提交。
    """
    prior = await lifecycle.live(session, bot.owner_id, lock=False) if bot.owner_id else None
    before = (prior.id, prior.action, prior.step) if prior is not None else None
    flow = await deprovisioning.request(session, bot, trigger=trigger, actor_id=actor.id)
    started = flow is not None and (
        before is None
        or before[0] != flow.id
        or before[1] != "deprovision"
        or (before[2] == "grace" and flow.step != "grace")
    )
    if started:
        await record_audit(
            session,
            action="assistant.deprovision",
            actor_id=actor.id,
            actor_login=actor.login_name,
            target_type="bot",
            target_id=str(bot.id),
            diff={},
            ip=client_ip(request),
        )
    return "deprovisioning" if flow else "deprovisioned"
