"""Owner-only view of one's own reminders and scheduled AI tasks.

Fixed reminders can only be cancelled. Scheduled AI tasks are created by confirming a card in
the bot's private chat; here their owner can pause, resume or delete them and read results.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import current_user, get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.security import verify_csrf
from coreman.core import personal_schedules as schedules
from coreman.core.bus import tasks
from coreman.core.cron.schedule import next_run
from coreman.core.db.models import Bot, CronJob, CronRun, OutboxItem, Task, User
from coreman.core.reminders import MODE
from coreman.core.timeutils import utcnow

router = APIRouter(
    prefix="/api/self-reminders", tags=["self-reminders"], dependencies=[Depends(verify_csrf)]
)
REPLY_PREVIEW = 4000


async def _latest_run(session: AsyncSession, job: CronJob) -> CronRun | None:
    run: CronRun | None = await session.scalar(
        select(CronRun).where(CronRun.cron_job_id == job.id).order_by(CronRun.id.desc()).limit(1)
    )
    return run


async def _deliveries(session: AsyncSession, run: CronRun | None) -> list[str]:
    ids = (run.delivery or {}).get("outbox_ids", []) if run else []
    if not ids:
        return []
    return list(
        (await session.scalars(select(OutboxItem.status).where(OutboxItem.id.in_(ids)))).all()
    )


async def _out(session: AsyncSession, job: CronJob) -> dict[str, Any]:
    run = await _latest_run(session, job)
    deliveries = await _deliveries(session, run)
    bot = await session.get(Bot, job.bot_id)
    base = {"id": str(job.id), "bot_name": bot.name if bot else "", "enabled": job.enabled}
    if job.execution_mode == schedules.MODE:
        return {
            **base,
            "type": "schedule",
            "name": job.name,
            "text": job.prompt,
            "schedule_kind": job.schedule_kind,
            "cron_expression": job.cron_expression,
            "timezone": job.timezone,
            "run_at": job.run_at,
            "next_run_at": job.next_run_at if job.enabled else None,
            "status": job.last_status or ("scheduled" if job.enabled else "paused"),
            "running": job.running_task_id is not None,
            "last_run": (
                {
                    "status": run.status,
                    "started_at": run.started_at,
                    "finished_at": run.finished_at,
                    "reply": (run.reply or "")[:REPLY_PREVIEW] or None,
                    "truncated": len(run.reply or "") > REPLY_PREVIEW,
                    "error": run.error_message,
                    "deliveries": deliveries,
                }
                if run
                else None
            ),
            "can_pause": job.enabled,
            "can_resume": not job.enabled
            and (job.schedule_kind == "recurring" or job.consumed_at is None),
        }
    return {
        **base,
        "type": "reminder",
        "text": job.prompt,
        "run_at": job.run_at,
        "status": job.last_status or ("scheduled" if job.enabled else "cancelled"),
        "deliveries": deliveries,
        "can_cancel": job.enabled or "pending" in deliveries,
    }


async def _own(session: AsyncSession, job_id: uuid.UUID, actor: User, mode: str) -> CronJob:
    job = await session.scalar(
        select(CronJob)
        .where(CronJob.id == job_id, CronJob.execution_mode == mode, CronJob.created_by == actor.id)
        .with_for_update()
    )
    if not job:
        raise not_found("提醒不存在" if mode == MODE else "定时任务不存在")
    return job


async def _stop_running(session: AsyncSession, job: CronJob) -> None:
    if job.running_task_id:
        task = await session.get(Task, job.running_task_id, with_for_update=True)
        if task and task.status in tasks.OPEN:
            task.cancel_requested_at = utcnow()


@router.get("")
async def list_own(
    actor: User = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    rows = (
        await session.scalars(
            select(CronJob)
            .where(
                CronJob.execution_mode.in_((MODE, schedules.MODE)),
                CronJob.created_by == actor.id,
            )
            .order_by(CronJob.enabled.desc(), CronJob.created_at.desc())
            .limit(100)
        )
    ).all()
    return {"code": 0, "data": [await _out(session, row) for row in rows]}


@router.post("/{job_id}/cancel")
async def cancel(
    job_id: uuid.UUID,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    job = await _own(session, job_id, actor, MODE)
    job.enabled = False
    job.next_run_at = None
    job.consumed_at = job.consumed_at or utcnow()
    await _stop_running(session, job)
    run = await _latest_run(session, job)
    if run:
        ids = (run.delivery or {}).get("outbox_ids", [])
        for item in (
            await session.scalars(
                select(OutboxItem).where(OutboxItem.id.in_(ids)).with_for_update()
            )
        ).all():
            if item.status == "pending":
                item.status = "skipped"
    result = await _out(session, job)
    await session.commit()
    return {"code": 0, "data": result}


@router.post("/{job_id}/pause")
async def pause(
    job_id: uuid.UUID,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    job = await _own(session, job_id, actor, schedules.MODE)
    job.enabled = False
    job.next_run_at = None
    job.force_run_at = None
    job.force_run_by = None
    result = await _out(session, job)
    await session.commit()
    return {"code": 0, "data": result}


@router.post("/{job_id}/resume")
async def resume(
    job_id: uuid.UUID,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    job = await _own(session, job_id, actor, schedules.MODE)
    if job.enabled:
        return {"code": 0, "data": await _out(session, job)}
    bot = await session.get(Bot, job.bot_id)
    if bot is None:
        raise not_found("机器人不存在")
    await schedules.require_personal(session, job, bot, actor)
    now = utcnow()
    if await schedules.active_count(session, job.bot_id, actor.id) >= schedules.MAX_ACTIVE:
        raise ApiError(
            409, 409, f"每人最多保留 {schedules.MAX_ACTIVE} 个启用中的定时任务，请先暂停或删除"
        )
    if job.schedule_kind == "once":
        if job.consumed_at is not None or job.run_at is None or job.run_at <= now:
            raise ApiError(409, 409, "一次性任务的时间已过，请在私聊里重新创建")
        job.next_run_at = job.run_at
    else:
        try:
            job.next_run_at = next_run(job.cron_expression, job.timezone, now)
        except ValueError as exc:
            raise ApiError(422, 422, str(exc)) from exc
    job.enabled = True
    result = await _out(session, job)
    await session.commit()
    return {"code": 0, "data": result}


@router.delete("/{job_id}")
async def delete(
    job_id: uuid.UUID,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    job = await _own(session, job_id, actor, schedules.MODE)
    # 运行中的一轮先请求取消；运行记录保留（cron_job_id 置空），结果不再投递给别人。
    await _stop_running(session, job)
    await session.delete(job)
    await session.commit()
    return {"code": 0, "data": None}
