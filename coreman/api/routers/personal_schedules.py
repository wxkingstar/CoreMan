"""技能管理本人定时任务的接口：凭据来自本人已验证私聊的这一轮，只能管自己在这个机器人上的任务。

身份只由凭据指向的任务核验得出（飞书与企业微信各自核验私聊来源），请求里不接受任何用户参数。
新建、修改、重新启用只生成草稿并发确认卡片，本人点确认才落库；暂停、删除、立即运行、查看直接执行。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import get_session
from coreman.api.errors import ApiError, not_found
from coreman.core import personal_schedules as schedules
from coreman.core.bus import tasks
from coreman.core.cron.chats import known_chat_options
from coreman.core.cron.precheck import PrecheckError, run_precheck_in_thread
from coreman.core.db.models import ChatSession, CronJob, CronRun, OutboxItem, Task
from coreman.core.timeutils import utcnow

router = APIRouter(prefix="/api/runtime/personal-schedules", tags=["personal-schedules"])
RUNS_MAX = 20
REPLY_PREVIEW = 4000


async def origin(
    request: Request, session: AsyncSession = Depends(get_session)
) -> schedules.Origin:
    if "origin" in request.headers:
        raise ApiError(403, 403, "Browser origins are not supported")
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or len(auth) > 4096:
        raise ApiError(401, 401, "Invalid schedule capability")
    try:
        capability = schedules.read_capability(request.app.state.cipher, auth[7:])
    except (ValueError, KeyError, TypeError, OverflowError):
        raise ApiError(401, 401, "Invalid schedule capability") from None
    try:
        scope = await schedules.origin_scope(session, capability.task_id, capability.actor)
    except (ValueError, KeyError, TypeError):
        raise ApiError(403, 403, "Verified private owner task required") from None
    # 会话被重置或切换后，旧一轮的凭据不再代表当前对话。
    base = await session.get(ChatSession, (scope.bot.id, scope.task.session_key))
    if base is None or base.relay_session_id != capability.session_id:
        raise ApiError(403, 403, "Private conversation changed")
    return scope


def _rejected(error: dict[str, Any]) -> ApiError:
    return ApiError(
        422, 422, str(error.get("message") or error["error"]), [{"type": error["error"]}]
    )


async def _job(session: AsyncSession, scope: schedules.Origin, job_id: uuid.UUID) -> CronJob:
    job = await schedules.own_job(session, scope.bot, scope.user_id, job_id, lock=True)
    if job is None:
        raise not_found("定时任务不存在")
    return job


def _parse(body: dict[str, Any]) -> schedules.Draft:
    try:
        return schedules.Draft.model_validate(body)
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in err["loc"]) or "body" for err in exc.errors()})
        raise ApiError(
            422,
            422,
            "草稿参数无效：" + "、".join(fields),
            [{"type": "invalid_draft", "fields": fields}],
        ) from None


@router.get("")
async def list_jobs(
    scope: schedules.Origin = Depends(origin), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    rows = (
        await session.scalars(
            select(CronJob)
            .where(
                CronJob.bot_id == scope.bot.id,
                CronJob.created_by == scope.user_id,
                CronJob.execution_mode == schedules.MODE,
            )
            .order_by(CronJob.enabled.desc(), CronJob.created_at.desc())
            .limit(50)
        )
    ).all()
    return {
        "code": 0,
        "data": {
            "items": [schedules.job_view(row) for row in rows],
            "active": sum(1 for row in rows if row.enabled),
            "limits": {
                "max_active": schedules.MAX_ACTIVE,
                "min_interval_minutes": int(schedules.MIN_INTERVAL.total_seconds() // 60),
                "once_max_days": schedules.ONCE_MAX_LEAD.days,
                "max_recipients": schedules.MAX_RECIPIENTS,
                "max_chats": schedules.MAX_CHATS,
                "timezone": schedules.TIMEZONE,
            },
        },
    }


@router.get("/recipients")
async def recipients(
    request: Request,
    q: str = Query(default="", max_length=64),
    scope: schedules.Origin = Depends(origin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """可选的接收人：私聊过这个机器人的同事，以及本人在里面和机器人说过话的群。"""
    users = await schedules.reachable_users(session, scope.bot, q)
    groups = await schedules.known_groups(session, scope.bot, scope.user_id)
    options = await known_chat_options(scope.bot, groups, request.app.state.cipher)
    text = q.strip().lower()
    if text:
        options = [o for o in options if text in o["name"].lower() or text in o["id"].lower()]
    return {
        "code": 0,
        "data": {
            "users": [
                {
                    "id": str(u.id),
                    "name": u.display_name,
                    "login": u.login_name,
                    "self": u.id == scope.user_id,
                }
                for u in users
            ],
            "chats": options[:50],
        },
    }


@router.post("/drafts")
async def draft(
    request: Request,
    scope: schedules.Origin = Depends(origin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """新建草稿，或带 job_id 修改已有任务。本人点确认卡片后才生效。"""
    body = await request.json()
    if not isinstance(body, dict):
        raise ApiError(422, 422, "请求体需要是 JSON 对象")
    job_id = body.pop("job_id", None)
    args = _parse(body)
    job = None
    if job_id is not None:
        try:
            job = await _job(session, scope, uuid.UUID(str(job_id)))
        except ValueError:
            raise not_found("定时任务不存在") from None
    if job is not None and (job.running_task_id or job.force_run_at):
        raise ApiError(409, 409, "任务正在执行或等待立即运行，请结束后再改")
    icon = str(await request.app.state.settings_store.get("card_icon_url", default="") or "")
    result = await schedules.open_draft(
        session, scope, args, action="update" if job else "create", job=job, icon_url=icon
    )
    if "error" in result:
        raise _rejected(result)
    await session.commit()
    return {"code": 0, "data": result}


@router.post("/{job_id}/resume")
async def resume(
    job_id: uuid.UUID,
    request: Request,
    scope: schedules.Origin = Depends(origin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    job = await _job(session, scope, job_id)
    if job.enabled:
        return {"code": 0, "data": {"status": "already_enabled", "job": schedules.job_view(job)}}
    icon = str(await request.app.state.settings_store.get("card_icon_url", default="") or "")
    result = await schedules.open_draft(
        session,
        scope,
        schedules.Draft.model_validate(schedules.draft_of(job)),
        action="resume",
        job=job,
        icon_url=icon,
    )
    if "error" in result:
        raise _rejected(result)
    await session.commit()
    return {"code": 0, "data": result}


@router.post("/{job_id}/pause")
async def pause(
    job_id: uuid.UUID,
    scope: schedules.Origin = Depends(origin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    job = await _job(session, scope, job_id)
    job.enabled = False
    job.next_run_at = None
    job.force_run_at = None
    job.force_run_by = None
    await session.commit()
    return {"code": 0, "data": schedules.job_view(job)}


@router.delete("/{job_id}")
async def delete(
    job_id: uuid.UUID,
    scope: schedules.Origin = Depends(origin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    job = await _job(session, scope, job_id)
    # 运行中的一轮先请求取消；运行记录保留（cron_job_id 置空）。
    if job.running_task_id:
        task = await session.get(Task, job.running_task_id, with_for_update=True)
        if task and task.status in tasks.OPEN:
            task.cancel_requested_at = utcnow()
    name = job.name
    await session.delete(job)
    await session.commit()
    return {"code": 0, "data": {"deleted": str(job_id), "name": name}}


@router.post("/{job_id}/run")
async def run_now(
    job_id: uuid.UUID,
    scope: schedules.Origin = Depends(origin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """按已确认的内容立即跑一次，不改变计划；与定时触发同样在调度前重查边界。"""
    job = await _job(session, scope, job_id)
    now = datetime.now(UTC)
    if not job.enabled or (job.expires_at and job.expires_at <= now):
        raise ApiError(409, 409, "任务已暂停或过期，先启用再运行")
    if job.schedule_kind == "once" and job.consumed_at is not None:
        raise ApiError(409, 409, "一次性任务已执行过")
    if job.running_task_id or job.force_run_at:
        raise ApiError(409, 409, "任务正在执行或已等待触发")
    job.force_run_at = now
    job.force_run_by = scope.user_id
    await session.commit()
    return {"code": 0, "data": schedules.job_view(job)}


@router.get("/{job_id}/runs")
async def runs(
    job_id: uuid.UUID,
    limit: int = Query(default=5, ge=1, le=RUNS_MAX),
    scope: schedules.Origin = Depends(origin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    job = await _job(session, scope, job_id)
    rows = (
        await session.scalars(
            select(CronRun)
            .where(CronRun.cron_job_id == job.id)
            .order_by(CronRun.id.desc())
            .limit(limit)
        )
    ).all()
    ids = [int(v) for row in rows for v in (row.delivery or {}).get("outbox_ids", [])]
    statuses: dict[int, str] = {}
    if ids:
        found = await session.execute(
            select(OutboxItem.id, OutboxItem.status).where(OutboxItem.id.in_(ids))
        )
        statuses = {item_id: status for item_id, status in found.all()}
    items = []
    for row in rows:
        # 别人（旧版本里的管理员强制运行）执行的私密记录不给看。
        hidden = row.private and row.executed_by not in (None, scope.user_id)
        reply = None if hidden else (row.reply or "")
        items.append(
            {
                "id": row.id,
                "status": row.status,
                "trigger": row.trigger_kind,
                "started_at": row.started_at,
                "finished_at": row.finished_at,
                "error": row.error_message,
                "precheck": row.precheck_meta,
                "reply": reply[:REPLY_PREVIEW] if reply else reply,
                "truncated": bool(reply and len(reply) > REPLY_PREVIEW),
                "deliveries": [
                    statuses.get(int(i), "unknown")
                    for i in (row.delivery or {}).get("outbox_ids", [])
                ],
                "delivery_errors": (row.delivery or {}).get("errors") or {},
            }
        )
    return {"code": 0, "data": {"job": schedules.job_view(job), "items": items}}


class PrecheckIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    script: str = Field(max_length=schedules.PRECHECK_MAX)
    ctx: dict[str, Any] = Field(default_factory=dict)


@router.post("/precheck/test")
async def test_precheck(
    body: PrecheckIn, scope: schedules.Origin = Depends(origin)
) -> dict[str, Any]:
    """试跑执行前检查：与正式执行同一个受限解释器，只是换成示例上下文，不调模型也不发消息。"""
    now = datetime.now(UTC).isoformat()
    ctx = {
        "now": now,
        "scheduled_at": now,
        "bot_id": str(scope.bot.id),
        "user_id": str(scope.user_id),
        "job_name": "precheck-test",
        **{k: v for k, v in body.ctx.items() if k in ("now", "scheduled_at", "job_name")},
    }
    try:
        result = await run_precheck_in_thread(body.script, ctx, timeout_seconds=2)
    except PrecheckError as exc:
        return {"code": 0, "data": {"status": "failed_precheck", "error": str(exc)}}
    return {
        "code": 0,
        "data": {
            "status": "success",
            "trigger": result.trigger,
            "reason": result.reason,
            "prompt_appendix": result.prompt_appendix,
        },
    }
