"""Owner-only fixed-reminder visibility and cancellation; no editing or force run."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import current_user, get_session
from coreman.api.errors import not_found
from coreman.api.security import verify_csrf
from coreman.core.bus import tasks
from coreman.core.db.models import Bot, CronJob, CronRun, OutboxItem, Task, User
from coreman.core.reminders import MODE
from coreman.core.timeutils import utcnow

router = APIRouter(
    prefix="/api/self-reminders", tags=["self-reminders"], dependencies=[Depends(verify_csrf)]
)


async def _out(session: AsyncSession, job: CronJob) -> dict[str, Any]:
    run = await session.scalar(
        select(CronRun).where(CronRun.cron_job_id == job.id).order_by(CronRun.id.desc()).limit(1)
    )
    deliveries = []
    if run:
        ids = (run.delivery or {}).get("outbox_ids", [])
        deliveries = (
            list(
                (
                    await session.scalars(select(OutboxItem.status).where(OutboxItem.id.in_(ids)))
                ).all()
            )
            if ids
            else []
        )
    bot = await session.get(Bot, job.bot_id)
    return {
        "id": str(job.id),
        "bot_name": bot.name if bot else "",
        "text": job.prompt,
        "run_at": job.run_at,
        "enabled": job.enabled,
        "status": job.last_status or ("scheduled" if job.enabled else "cancelled"),
        "deliveries": deliveries,
        "can_cancel": job.enabled or "pending" in deliveries,
    }


@router.get("")
async def list_own(
    actor: User = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    rows = (
        await session.scalars(
            select(CronJob)
            .where(CronJob.execution_mode == MODE, CronJob.created_by == actor.id)
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
    job = await session.scalar(
        select(CronJob)
        .where(CronJob.id == job_id, CronJob.execution_mode == MODE, CronJob.created_by == actor.id)
        .with_for_update()
    )
    if not job:
        raise not_found("提醒不存在")
    job.enabled = False
    job.next_run_at = None
    job.consumed_at = job.consumed_at or utcnow()
    if job.running_task_id:
        task = await session.get(Task, job.running_task_id, with_for_update=True)
        if task and task.status in tasks.OPEN:
            task.cancel_requested_at = utcnow()
    run = await session.scalar(
        select(CronRun).where(CronRun.cron_job_id == job.id).order_by(CronRun.id.desc()).limit(1)
    )
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
