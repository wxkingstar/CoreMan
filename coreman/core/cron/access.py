"""定时任务操作和执行时共用的身份边界。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bots.permissions import is_bot_admin
from coreman.core.db.models import Bot, BotMember, User, UserIdentity
from coreman.core.errors import ApiError
from coreman.core.prompting import Speaker


async def require_operator(session: AsyncSession, bot: Bot, actor: User) -> Speaker:
    members = (
        await session.scalars(select(BotMember.user_id).where(BotMember.bot_id == bot.id))
    ).all()
    if (
        actor.status != "active"
        or actor.source == "bootstrap"
        or not is_bot_admin(actor, bot, members)
    ):
        raise ApiError(403, 403, "仅已绑定平台身份的机器人管理员可以执行此操作")
    ident = await session.scalar(
        select(UserIdentity).where(
            UserIdentity.user_id == actor.id, UserIdentity.platform == bot.platform
        )
    )
    if ident is None:
        raise ApiError(403, 403, "请先绑定机器人的平台身份")
    return Speaker(ident.platform_user_id, actor.id, actor.login_name, actor.display_name)


def job_config(job: object) -> dict[str, object]:
    from coreman.core.db.models import CronJob

    assert isinstance(job, CronJob)
    return {
        "execution_mode": job.execution_mode,
        "name": job.name,
        "prompt": job.prompt,
        "system_prompt": job.system_prompt,
        "precheck_script": job.precheck_script,
        "precheck_timeout_seconds": job.precheck_timeout_seconds,
        "target_users": [str(uid) for uid in job.target_users],
        "target_chats": job.target_chats,
        "notify_emails": job.notify_emails,
        "notify_webhook": job.notify_webhook,
        "notify_webhook_url_enc": job.notify_webhook_url_enc,
    }
