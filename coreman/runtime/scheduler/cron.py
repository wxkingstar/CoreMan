"""定时任务认领：只在持有 job 行锁的事务内入队和推进时钟。"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.bus import tasks
from coreman.core.cron.access import job_config, require_operator
from coreman.core.cron.schedule import next_run
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, CronJob, CronRun, Task, User
from coreman.core.errors import ApiError
from coreman.core.i18n.messages import msg

MISFIRE_SECONDS = 300


async def run_tick(factory: async_sessionmaker[AsyncSession], now: datetime) -> int:
    count = 0
    async with factory() as session:
        jobs = (
            await session.scalars(
                select(CronJob)
                .where(
                    CronJob.enabled.is_(True),
                    or_(CronJob.schedule_kind == "recurring", CronJob.consumed_at.is_(None)),
                    or_(
                        CronJob.next_run_at <= now,
                        CronJob.force_run_at <= now,
                        CronJob.expires_at <= now,
                        CronJob.next_run_at.is_(None),
                    ),
                )
                .order_by(CronJob.next_run_at.nullsfirst(), CronJob.id)
                .limit(100)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for job in jobs:
            force = job.force_run_at is not None and job.force_run_at <= now
            due = job.force_run_at if force else job.next_run_at
            bot = await session.get(Bot, job.bot_id)
            actor_id = job.force_run_by if force else job.created_by
            user = await session.get(User, actor_id) if actor_id else None
            invalid = (
                (job.expires_at is not None and job.expires_at <= now)
                or bot is None
                or not bot.enabled
                or user is None
                or user.status != "active"
                or user.source == "bootstrap"
            )
            if not invalid and bot is not None and user is not None:
                try:
                    await require_operator(session, bot, user)
                except ApiError:
                    invalid = True
            if invalid:
                job.enabled = False if not force else job.enabled
                job.force_run_at = None
                job.force_run_by = None
                job.last_status = "skipped"
                if job.schedule_kind == "once":
                    job.enabled = False
                    job.consumed_at = now
                    job.next_run_at = None
                    session.add(
                        CronRun(
                            cron_job_id=job.id,
                            bot_id=job.bot_id,
                            job_name=job.name,
                            status="skipped",
                            prompt=job.prompt,
                            started_at=now,
                            finished_at=now,
                            executed_by=actor_id,
                            trigger_kind="manual" if force else "scheduled",
                            precheck_meta={"reason": "job_or_actor_unavailable"},
                        )
                    )
                count += 1
                continue
            running = await session.get(Task, job.running_task_id) if job.running_task_id else None
            if running is not None and running.status in tasks.OPEN:
                # 保留强制运行请求，活动任务结束后执行；常规时钟推进防积压。
                if job.next_run_at is not None and job.next_run_at <= now:
                    job.next_run_at = next_run(job.cron_expression, job.timezone, now)
                    session.add(
                        CronRun(
                            cron_job_id=job.id,
                            bot_id=job.bot_id,
                            job_name=job.name,
                            status="skipped",
                            prompt=job.prompt,
                            started_at=now,
                            finished_at=now,
                            precheck_meta={"reason": "previous_run_active"},
                        )
                    )
                continue
            if job.running_task_id is not None:
                # 终态任务先由 recover_runs 补齐记录；这里不可丢掉关联再启动下一轮。
                continue
            if job.schedule_kind == "once" and due is None:
                job.next_run_at = job.run_at
                continue
            job.force_run_at = None
            job.force_run_by = None
            try:
                if job.schedule_kind == "once":
                    job.next_run_at = None
                    job.consumed_at = now
                else:
                    job.next_run_at = next_run(job.cron_expression, job.timezone, now)
            except ValueError:
                job.enabled = False
                job.last_status = "failed"
                continue
            if due is None:
                continue
            if not force and now - due > timedelta(seconds=MISFIRE_SECONDS):
                job.last_status = "skipped"
                if job.schedule_kind == "once":
                    job.enabled = False
                session.add(
                    CronRun(
                        cron_job_id=job.id,
                        bot_id=job.bot_id,
                        job_name=job.name,
                        status="skipped",
                        prompt=job.prompt,
                        started_at=now,
                        finished_at=now,
                        precheck_meta={"reason": "misfire", "scheduled_at": due.isoformat()},
                    )
                )
                count += 1
                continue
            task = await tasks.enqueue(
                session,
                tasks.NewTask(
                    bot_id=job.bot_id,
                    kind="cron_run",
                    user_id=actor_id,
                    payload={
                        "cron_job_id": str(job.id),
                        "scheduled_at": due.isoformat(),
                        "config": job_config(job),
                    },
                    dedupe_key=f"cron:{job.id}:{due.isoformat()}",
                ),
            )
            if task is not None:
                job.running_task_id = task.id
                job.last_run_at = now
                job.last_status = "running"
                session.add(
                    CronRun(
                        cron_job_id=job.id,
                        bot_id=job.bot_id,
                        job_name=job.name,
                        task_id=task.id,
                        executed_by=actor_id,
                        trigger_kind="manual" if force else "scheduled",
                        status="running",
                        prompt=job.prompt,
                        started_at=now,
                    )
                )
                count += 1
        await session.commit()
    return count


async def recover_runs(
    factory: async_sessionmaker[AsyncSession], now: datetime, cipher: Cipher
) -> int:
    """补齐 worker 崩溃或通用取消留下的运行记录；不重新调用模型。"""
    from coreman.core.cron.delivery import enqueue_result
    from coreman.core.db.models import ChatLog

    count = 0
    async with factory() as session:
        jobs = (
            await session.scalars(
                select(CronJob)
                .outerjoin(Task, Task.id == CronJob.running_task_id)
                .where(
                    CronJob.running_task_id.is_not(None),
                    or_(Task.id.is_(None), Task.status.not_in(tasks.OPEN)),
                )
                .order_by(CronJob.id)
                .limit(100)
                .with_for_update(of=CronJob, skip_locked=True)
            )
        ).all()
        for job in jobs:
            task = await session.get(Task, job.running_task_id)
            run = await session.scalar(
                select(CronRun).where(CronRun.task_id == job.running_task_id).with_for_update()
            )
            job.running_task_id = None
            if job.schedule_kind == "once":
                job.enabled = False
            if run is None or run.status != "running":
                continue
            run.status, run.finished_at = "failed", now
            run.error_message = "worker_lost" if task is None else task.error_code or task.status
            job.last_status = "failed"
            bot = await session.get(Bot, job.bot_id)
            if bot is not None:
                if task is not None and task.status != "cancelled":
                    run.delivery = await enqueue_result(
                        session,
                        bot=bot,
                        config=task.payload.get("config", {}),
                        run_id=run.id,
                        content=msg(
                            "cron_failed",
                            name=run.job_name,
                            bot=bot.name,
                            reason=msg("cron_worker_lost"),
                        ),
                        cipher=cipher,
                        fallback_user_id=job.created_by,
                    )
                session.add(
                    ChatLog(
                        bot_id=bot.id,
                        bot_key=bot.bot_key,
                        platform=bot.platform,
                        chat_type="cron",
                        message_type="text",
                        task_id=run.task_id,
                        user_id=run.executed_by,
                        request_at=run.started_at,
                        response_at=now,
                        message_content=run.prompt[:10000],
                        status="error",
                        error_code="worker_lost",
                        error_message="执行进程中断，未自动重复调用模型",
                    )
                )
            count += 1
        await session.commit()
    return count
