"""Reserved reminder commands run before the connect command, choices and the assistant."""

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import tasks
from coreman.core.reminders import handle_request
from coreman.runtime.worker.chat.models import Intake
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.replies import reply_once


async def intercept(session: AsyncSession, ctx: TaskContext, intake: Intake) -> bool:
    if intake.bot.platform != "feishu":
        return False
    reply = await handle_request(session, ctx.task, ctx.cipher)
    if reply is None:
        return False
    reply = reply.replace(
        "](/self-reminders)", "](" + ctx.public_base_url.rstrip("/") + "/self-reminders)"
    )
    await reply_once(session, ctx, reply_context=intake.inbound.reply_context, text=reply)
    await tasks.finish(session, ctx.task.id, status="succeeded", result={"self_reminder": True})
    return True
