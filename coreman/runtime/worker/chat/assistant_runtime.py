"""个人助理开轮之前的一道关：所在运行时现在跑不了助理，就不派，私聊告诉所有者。"""

from __future__ import annotations

from coreman.core.assistants import runtime
from coreman.core.bus import tasks
from coreman.core.db.models import Bot
from coreman.runtime.worker.chat.models import Intake
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.replies import reply_once


class AssistantDispatchError(Exception):
    """助理这一轮的派出前检查出了意外：不派，任务只记这个类名与固定说明，不带内部细节。"""

    def __init__(self) -> None:
        super().__init__("assistant_dispatch_failed")


async def paused(ctx: TaskContext, intake: Intake) -> bool:
    """所在运行时不可用（离线、没有沙箱能力、不在池里也不是本人的）：回一句、结掉任务，返回真。

    不回落到别的节点：私有运行时离线时把助理换到池里，等于把个人内容挪到所有者没选的机器上。
    检查本身出了意外也不派（宁可不跑，不能当普通请求发出去）。
    """
    try:
        async with ctx.session_factory() as session:
            bot = await session.get(Bot, intake.bot.id, populate_existing=True)
            reason = "runtime_missing" if bot is None else await runtime.turn_blocker(session, bot)
            if reason is None:
                return False
            ctx.log.warning("assistant_paused", reason=reason)
            await reply_once(
                session,
                ctx,
                reply_context=intake.inbound.reply_context,
                text=runtime.pause_message(reason),
            )
            await tasks.finish(
                session,
                ctx.task.id,
                status="failed",
                error_code="assistant_paused",
                only_active=True,
            )
            await session.commit()
    except Exception as exc:
        ctx.log.warning("assistant_pause_gate_failed", error_type=type(exc).__name__)
        raise AssistantDispatchError from None
    return True
