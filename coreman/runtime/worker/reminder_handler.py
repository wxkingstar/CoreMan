"""Static reminders: lock, revalidate, finish and enqueue in one transaction."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from coreman.core.bus import outbox, tasks
from coreman.core.db.models import Bot, CronJob, CronRun, Task, User
from coreman.core.errors import ApiError
from coreman.core.reminders import MODE, require_fixed
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.context import TaskContext


async def run_fixed(ctx: TaskContext) -> bool:
    async with ctx.session_factory() as session:
        job = await session.get(
            CronJob, uuid.UUID(ctx.task.payload["cron_job_id"]), with_for_update=True
        )
        mode = (ctx.task.payload.get("config") or {}).get("execution_mode")
        if (job is None or job.execution_mode != MODE) and mode != MODE:
            return False
        # Match private command ordering (bot before task) so stop cannot deadlock a run.
        bot = await session.get(Bot, ctx.task.bot_id, with_for_update=True, populate_existing=True)
        actor = (
            await session.get(User, ctx.task.user_id, with_for_update=True, populate_existing=True)
            if ctx.task.user_id
            else None
        )
        task = await session.get(Task, ctx.task.id, with_for_update=True, populate_existing=True)
        run = await session.scalar(
            select(CronRun).where(CronRun.task_id == ctx.task.id).with_for_update()
        )
        if not task or task.status not in tasks.ACTIVE or not run or run.status != "running":
            return True
        error = None
        try:
            if (
                not job
                or not bot
                or not actor
                or task.bot_id != bot.id
                or task.user_id != actor.id
                or not job.enabled
                or task.cancel_requested_at
                or job.running_task_id != task.id
                or run.executed_by != actor.id
                or mode != MODE
                or ctx.cancel_event.is_set()
            ):
                raise ApiError(403, 403, "reminder_cancelled_or_unavailable")
            await require_fixed(session, job, bot, actor)
        except ApiError:
            error = "reminder_cancelled_or_unavailable"
        now = utcnow()
        run.status, run.finished_at, run.error_message = (
            ("skipped" if error else "success"),
            now,
            error,
        )
        if not error:
            assert job and bot and actor
            run.reply = job.prompt
            item = await outbox.add(
                session,
                bot_id=bot.id,
                platform="feishu",
                kind="send",
                dedupe_key=f"cron:{run.id}:self",
                target={"chat_id": job.reminder_chat_id, "recipient_user_id": str(actor.id)},
                payload={"markdown": "提醒：" + job.prompt},
            )
            run.delivery = {"outbox_ids": [item.id] if item else [], "errors": {}}
        if job and job.running_task_id == task.id:
            job.running_task_id = None
            job.enabled = False
            job.last_status = run.status
        await tasks.finish(
            session,
            task.id,
            status="cancelled" if error else "succeeded",
            error_code=error,
            result={"cron_run_id": run.id, "self_reminder": True},
            only_active=True,
        )
        await session.commit()
        return True
