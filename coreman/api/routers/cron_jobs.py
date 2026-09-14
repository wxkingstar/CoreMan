"""定时任务管理；不允许修改他人任务并借用他人的执行身份。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip, current_user, get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.pagination import PageParams, paginate
from coreman.api.security import verify_csrf
from coreman.api.versioning import require_if_match, set_etag
from coreman.core.audit import record_audit
from coreman.core.cron.access import job_config, require_operator
from coreman.core.cron.delivery import enqueue_result
from coreman.core.cron.precheck import PrecheckError, run_precheck, validate_script
from coreman.core.cron.schedule import next_run
from coreman.core.db.models import (
    Bot,
    BotMember,
    CronJob,
    CronRun,
    InboundEvent,
    OutboxItem,
    User,
    UserIdentity,
)
from coreman.core.notification_channels import validate_wecom_webhook
from coreman.core.timeutils import aware_utc

router = APIRouter(
    prefix="/api/admin/cron-jobs", tags=["cron"], dependencies=[Depends(verify_csrf)]
)


class CronIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bot_id: uuid.UUID
    name: str = Field(min_length=1, max_length=100)
    cron_expression: str = Field(max_length=128)
    timezone: str = Field(default="Asia/Shanghai", max_length=100)
    prompt: str = Field(min_length=1, max_length=32000)
    system_prompt: str | None = Field(default=None, max_length=32000)
    precheck_script: str | None = Field(default=None, max_length=32768)
    precheck_timeout_seconds: int = Field(default=30, ge=5, le=120)
    enabled: bool = True
    expires_at: datetime | None = None
    target_users: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    target_chats: list[str] = Field(default_factory=list, max_length=100)
    notify_emails: list[str] = Field(default_factory=list, max_length=50)
    notify_webhook: bool = False
    # null 保留已有地址，空字符串显式清除；明文从不回传。
    notify_webhook_url: str | None = Field(default=None, max_length=500)

    @field_validator("notify_webhook_url")
    @classmethod
    def webhook(cls, value: str | None) -> str | None:
        return validate_wecom_webhook(value) if value else value

    @field_validator("expires_at")
    @classmethod
    def utc(cls, value: datetime | None) -> datetime | None:
        return aware_utc(value) if value else None

    @field_validator("target_chats", "notify_emails")
    @classmethod
    def targets(cls, values: list[str]) -> list[str]:
        if any(not v.strip() or len(v) > 254 or any(c in v for c in "\r\n\x00") for v in values):
            raise ValueError("接收地址无效")
        return list(dict.fromkeys(values))


def _out(row: CronJob, actor: User) -> dict[str, Any]:
    return {
        **{k: v for k, v in job_config(row).items() if k != "notify_webhook_url_enc"},
        "has_webhook_url": bool(row.notify_webhook_url_enc),
        "id": str(row.id),
        "bot_id": str(row.bot_id),
        "cron_expression": row.cron_expression,
        "timezone": row.timezone,
        "enabled": row.enabled,
        "expires_at": row.expires_at,
        "created_by": str(row.created_by),
        "version": row.version,
        "next_run_at": row.next_run_at,
        "force_run_at": row.force_run_at,
        "running_task_id": row.running_task_id,
        "last_run_at": row.last_run_at,
        "last_status": row.last_status,
        "created_at": row.created_at,
        "can_edit": row.created_by == actor.id,
    }


async def _bot(session: AsyncSession, bot_id: uuid.UUID, actor: User) -> Bot:
    bot = await session.get(Bot, bot_id)
    if bot is None:
        raise not_found("机器人不存在")
    await require_operator(session, bot, actor)
    return bot


async def _load(session: AsyncSession, job_id: uuid.UUID, actor: User) -> CronJob:
    row = await session.scalar(select(CronJob).where(CronJob.id == job_id).with_for_update())
    if row is None:
        raise not_found("定时任务不存在")
    await _bot(session, row.bot_id, actor)
    return row


async def _validate(session: AsyncSession, body: CronIn, now: datetime) -> datetime:
    try:
        upcoming = next_run(body.cron_expression, body.timezone, now)
        if body.precheck_script:
            validate_script(body.precheck_script)
    except ValueError as exc:
        raise ApiError(422, 422, str(exc)) from exc
    if body.enabled and body.expires_at is not None and body.expires_at <= now:
        raise ApiError(422, 422, "已过期的任务不能启用")
    if any("@" not in email or any(c in email for c in "<>,; ") for email in body.notify_emails):
        raise ApiError(422, 422, "邮件地址无效")
    if body.target_users:
        valid = set(
            await session.scalars(
                select(User.id).where(
                    User.id.in_(body.target_users),
                    User.status == "active",
                    User.source != "bootstrap",
                )
            )
        )
        if valid != set(body.target_users):
            raise ApiError(422, 422, "接收人不存在或已停用")
        bot = await session.get(Bot, body.bot_id)
        if bot is None:
            raise ApiError(404, 404, "AI 员工不存在")
        bound = set(
            await session.scalars(
                select(UserIdentity.user_id).where(
                    UserIdentity.user_id.in_(body.target_users),
                    UserIdentity.platform == bot.platform,
                )
            )
        )
        if bound != set(body.target_users):
            raise ApiError(422, 422, "接收人尚未绑定该 AI 员工所属平台的身份")
    return upcoming


def _values(body: CronIn, request: Request, old: CronJob | None = None) -> dict[str, Any]:
    values = body.model_dump(exclude={"notify_webhook_url"})
    encrypted = old.notify_webhook_url_enc if old else None
    if body.notify_webhook_url is not None:
        encrypted = (
            request.app.state.cipher.encrypt(body.notify_webhook_url, "notifications.webhook_url")
            if body.notify_webhook_url
            else None
        )
    if body.notify_webhook and not encrypted:
        raise ApiError(422, 422, "启用企微群通知需要填写 Webhook 地址")
    if body.notify_webhook and encrypted:
        try:
            validate_wecom_webhook(
                request.app.state.cipher.decrypt(encrypted, "notifications.webhook_url")
            )
        except ValueError as exc:
            raise ApiError(422, 422, "已保存的企微群地址无效，请重新填写") from exc
    values["notify_webhook_url_enc"] = encrypted
    return values


def _delivery_out(item: OutboxItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "platform": item.platform,
        "channel": item.target.get("channel") or item.platform,
        "status": item.status,
        "attempts": item.attempts,
        "error": item.last_error,
    }


@router.post("/{job_id}/test-notification")
async def test_notification(
    job_id: uuid.UUID,
    request: Request,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    row = await _load(session, job_id, actor)
    if row.created_by != actor.id:
        raise ApiError(403, 403, "只有任务创建者可以发送测试通知")
    require_if_match(request, row.version)
    bot = await _bot(session, row.bot_id, actor)
    result = await enqueue_result(
        session,
        bot=bot,
        config=job_config(row),
        run_id=f"test:{row.id}:{uuid.uuid4()}",
        content=f"CoreMan 通知测试：{row.name}\n这是一条测试消息，不会执行 AI 任务。",
        cipher=request.app.state.cipher,
    )
    if not result["outbox_ids"] and not result["errors"]:
        raise ApiError(422, 422, "请先配置并保存通知接收人或渠道")
    await _audit(session, request, actor, "cron.test_notification", row)
    await session.commit()
    return {"code": 0, "data": result}


@router.get("/{job_id}/notification-tests")
async def notification_tests(
    job_id: uuid.UUID,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _load(session, job_id, actor)
    items = (
        await session.scalars(
            select(OutboxItem)
            .where(OutboxItem.dedupe_key.startswith(f"cron:test:{job_id}:"))
            .order_by(OutboxItem.id.desc())
            .limit(100)
        )
    ).all()
    return {"code": 0, "data": [_delivery_out(item) for item in items]}


async def _audit(
    session: AsyncSession, request: Request, actor: User, action: str, job: CronJob
) -> None:
    await record_audit(
        session,
        actor_id=actor.id,
        actor_login=actor.login_name,
        action=action,
        target_type="cron_job",
        target_id=str(job.id),
        diff={"name": job.name, "version": job.version},
        ip=client_ip(request),
    )


@router.get("/notification-chats")
async def notification_chats(
    bot_id: uuid.UUID,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _bot(session, bot_id, actor)
    chats = (
        await session.scalars(
            select(InboundEvent.chat_id)
            .where(
                InboundEvent.bot_id == bot_id,
                InboundEvent.chat_type == "group",
            )
            .distinct()
            .order_by(InboundEvent.chat_id)
            .limit(200)
        )
    ).all()
    return {"code": 0, "data": chats}


@router.get("")
async def list_jobs(
    bot_id: uuid.UUID | None = None,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    page: PageParams = Depends(),
) -> dict[str, Any]:
    stmt = (
        select(CronJob)
        .join(Bot, Bot.id == CronJob.bot_id)
        .where(
            (Bot.created_by == actor.id)
            | Bot.id.in_(select(BotMember.bot_id).where(BotMember.user_id == actor.id))
        )
        .order_by(CronJob.created_at.desc(), CronJob.id)
    )
    if actor.source == "bootstrap":
        return {
            "code": 0,
            "data": {"items": [], "total": 0, "page": page.page, "per_page": page.per_page},
        }
    if bot_id is not None:
        stmt = stmt.where(CronJob.bot_id == bot_id)
    data = await paginate(session, stmt, page)
    data["items"] = [_out(row, actor) for row in data["items"]]
    return {"code": 0, "data": data}


@router.post("", status_code=201)
async def create_job(
    body: CronIn,
    request: Request,
    response: Response,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _bot(session, body.bot_id, actor)
    upcoming = await _validate(session, body, datetime.now(UTC))
    values = _values(body, request)
    row = CronJob(**values, created_by=actor.id, next_run_at=upcoming)
    session.add(row)
    await session.flush()
    await _audit(session, request, actor, "cron.create", row)
    await session.commit()
    set_etag(response, row.version)
    return {"code": 0, "data": _out(row, actor)}


@router.put("/{job_id}")
async def update_job(
    job_id: uuid.UUID,
    body: CronIn,
    request: Request,
    response: Response,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    row = await _load(session, job_id, actor)
    if row.created_by != actor.id:
        raise ApiError(403, 403, "只有任务创建者可以修改执行内容；可复制为自己的任务")
    require_if_match(request, row.version)
    if row.force_run_at is not None:
        raise ApiError(409, 409, "任务已等待立即运行，请待本次入队后再修改")
    if body.bot_id != row.bot_id:
        raise ApiError(422, 422, "不能更换任务机器人")
    upcoming = await _validate(session, body, datetime.now(UTC))
    if (body.cron_expression, body.timezone, body.enabled) != (
        row.cron_expression,
        row.timezone,
        row.enabled,
    ):
        row.next_run_at = upcoming
    for key, value in _values(body, request, row).items():
        setattr(row, key, value)
    if not row.enabled:
        row.force_run_at = None
        row.force_run_by = None
    row.updated_at = datetime.now(UTC)
    await session.flush()
    await _audit(session, request, actor, "cron.update", row)
    await session.commit()
    set_etag(response, row.version)
    return {"code": 0, "data": _out(row, actor)}


@router.post("/{job_id}/run")
async def force_job(
    job_id: uuid.UUID,
    request: Request,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    row = await _load(session, job_id, actor)
    require_if_match(request, row.version)
    now = datetime.now(UTC)
    if not row.enabled or (row.expires_at and row.expires_at <= now):
        raise ApiError(409, 409, "任务已停用或过期")
    if row.running_task_id or row.force_run_at:
        raise ApiError(409, 409, "任务正在执行或已等待触发")
    row.force_run_at = now
    row.force_run_by = actor.id
    await session.flush()
    await _audit(session, request, actor, "cron.run", row)
    await session.commit()
    return {"code": 0, "data": _out(row, actor)}


@router.post("/{job_id}/disable")
async def disable_job(
    job_id: uuid.UUID,
    request: Request,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    row = await _load(session, job_id, actor)
    require_if_match(request, row.version)
    row.enabled = False
    row.force_run_at = None
    row.force_run_by = None
    await session.flush()
    await _audit(session, request, actor, "cron.disable", row)
    await session.commit()
    return {"code": 0, "data": _out(row, actor)}


@router.delete("/{job_id}")
async def delete_job(
    job_id: uuid.UUID,
    request: Request,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    row = await _load(session, job_id, actor)
    if row.created_by != actor.id:
        raise ApiError(403, 403, "仅创建者可删除任务")
    require_if_match(request, row.version)
    if row.running_task_id:
        raise ApiError(409, 409, "执行中的任务不能删除，请先取消运行")
    await _audit(session, request, actor, "cron.delete", row)
    await session.delete(row)
    await session.commit()
    return {"code": 0}


@router.get("/{job_id}/runs")
async def list_runs(
    job_id: uuid.UUID,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    page: PageParams = Depends(),
) -> dict[str, Any]:
    await _load(session, job_id, actor)
    data = await paginate(
        session,
        select(CronRun).where(CronRun.cron_job_id == job_id).order_by(CronRun.id.desc()),
        page,
    )
    rows = data["items"]
    ids = [int(v) for row in rows for v in row.delivery.get("outbox_ids", [])]
    deliveries = (
        (await session.scalars(select(OutboxItem).where(OutboxItem.id.in_(ids)))).all()
        if ids
        else []
    )
    statuses = {d.id: _delivery_out(d) for d in deliveries}
    data["items"] = [
        {
            "id": r.id,
            "task_id": r.task_id,
            "status": r.status,
            "prompt": r.prompt,
            "reply": r.reply,
            "error_message": r.error_message,
            "precheck_meta": r.precheck_meta,
            "delivery": r.delivery,
            "deliveries": [statuses[i] for i in r.delivery.get("outbox_ids", []) if i in statuses],
            "started_at": r.started_at,
            "finished_at": r.finished_at,
            "executed_by": str(r.executed_by) if r.executed_by else None,
            "trigger_kind": r.trigger_kind,
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "cache_read_tokens": r.cache_read_tokens,
            "cache_creation_tokens": r.cache_creation_tokens,
            "cost_usd": r.cost_usd,
        }
        for r in rows
    ]
    return {"code": 0, "data": data}


class PrecheckIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    script: str = Field(max_length=32768)
    ctx: dict[str, Any] = Field(default_factory=dict)


@router.post("/precheck/test")
async def test_precheck(body: PrecheckIn, _: User = Depends(current_user)) -> dict[str, Any]:
    try:
        result = run_precheck(body.script, body.ctx, timeout_seconds=1)
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


@router.post("/{job_id}/cancel-run")
async def cancel_run(
    job_id: uuid.UUID,
    request: Request,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    from coreman.runtime.bus.tasks import request_cancel

    row = await _load(session, job_id, actor)
    require_if_match(request, row.version)
    if row.running_task_id is not None:
        await request_cancel(session, row.running_task_id, "cron_cancelled")
    row.force_run_at = None
    row.force_run_by = None
    await _audit(session, request, actor, "cron.cancel_run", row)
    await session.commit()
    return {"code": 0, "data": _out(row, actor)}
