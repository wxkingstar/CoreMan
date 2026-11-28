"""任务平台：列表、详情与人在网页上的操作。

所有登录的成员都能看，包括给 Agent 看的那一层（brief、记要、调度依据、事件细节、运行记录）。
能改任务的是发起人、代表人、执行人、验收人和平台管理员；其他成员可以评论和关注。
见 docs/superpowers/specs/2026-11-25-company-task-platform.md。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import current_user, get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.security import verify_csrf
from coreman.core.db.models import (
    WORK_TASK_OPEN,
    WORK_TASK_STATUSES,
    Bot,
    User,
    WorkTask,
    WorkTaskEvent,
)
from coreman.core.work_tasks import execute, flow, markdown, service
from coreman.core.work_tasks import view as task_view
from coreman.core.work_tasks.service import Assignee, WorkTaskError

router = APIRouter(
    prefix="/api/admin/work-tasks", tags=["work-tasks"], dependencies=[Depends(verify_csrf)]
)


def _active(user: User) -> None:
    if user.status != "active":
        raise ApiError(403, 403, "账号未启用")


def _fail(exc: WorkTaskError) -> ApiError:
    return ApiError(exc.status, exc.status, exc.message)


_EMPTY_INSIDE = {"steps": 0, "steps_done": 0, "decisions": 0, "decisions_open": 0}


async def _inside(session: AsyncSession, roots: list[int]) -> dict[int, dict[str, int]]:
    """每件顶层的事下面有几步、做完几步，几个决策点、还有几个没定。"""
    out: dict[int, dict[str, int]] = {}
    if not roots:
        return out
    question = (WorkTask.brief.like("[问题]%") & WorkTask.assignee_user_id.is_not(None)).label(
        "question"
    )
    grouped = await session.execute(
        select(WorkTask.root_id, question, WorkTask.status, func.count())
        .where(
            WorkTask.root_id.in_(roots),
            WorkTask.parent_id.is_not(None),
            WorkTask.status.not_in(("shadow", "cancelled")),
        )
        .group_by(WorkTask.root_id, question, WorkTask.status)
    )
    for root_id, is_question, status, count in grouped.all():
        entry = out.setdefault(int(root_id), dict(_EMPTY_INSIDE))
        entry["decisions" if is_question else "steps"] += int(count)
        if is_question and status != "done":
            entry["decisions_open"] += int(count)
        if not is_question and status == "done":
            entry["steps_done"] += int(count)
    return out


@router.get("")
async def list_work_tasks(
    view: Literal["all", "needs_me", "created", "watching"] = "all",
    chat_id: str | None = None,
    status: str | None = None,
    assignee_bot_id: uuid.UUID | None = None,
    assignee_user_id: uuid.UUID | None = None,
    include_shadow: bool = True,
    scope: Literal["top", "all"] = "top",
    q: str | None = Query(default=None, max_length=100),
    before_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    _active(user)
    conditions: list[Any] = []
    # 默认只列顶层的事：拆出来的步骤和问人的决策点收在所属那件事下面（点开看），不然一件事
    # 会在列表里铺开十几行。「待我处理」要看到派给我的每一件；按人、按关键词找时也不收。
    if scope == "top" and view != "needs_me" and not (q or assignee_bot_id or assignee_user_id):
        conditions.append(WorkTask.parent_id.is_(None))
    if view == "needs_me":
        conditions.append(service.needs_me(user.id))
    elif view == "created":
        conditions.append(service.created_by(user.id))
    elif view == "watching":
        conditions.append(service.watched_by(user.id))
    if chat_id:
        conditions.append(WorkTask.chat_id == chat_id)
    if assignee_bot_id:
        conditions.append(WorkTask.assignee_bot_id == assignee_bot_id)
    if assignee_user_id:
        conditions.append(WorkTask.assignee_user_id == assignee_user_id)
    if not include_shadow and status != "shadow":
        conditions.append(WorkTask.status != "shadow")
    if q:
        like = f"%{q}%"
        conditions.append(
            WorkTask.code.ilike(like) | WorkTask.title.ilike(like) | WorkTask.summary.ilike(like)
        )
    stmt = select(WorkTask).where(*conditions)
    if status:
        if status not in WORK_TASK_STATUSES:
            raise ApiError(400, 400, "未知的任务状态")
        stmt = stmt.where(WorkTask.status == status)
    if before_id is not None:
        stmt = stmt.where(WorkTask.id < before_id)
    rows = list(await session.scalars(stmt.order_by(WorkTask.id.desc()).limit(limit)))
    names = await task_view.names_of(session, *task_view.ids_of(rows))
    grouped = await session.execute(
        select(WorkTask.status, func.count()).where(*conditions).group_by(WorkTask.status)
    )
    counts: dict[str, int] = {str(s): int(n) for s, n in grouped.all()}
    needs_me = await session.scalar(
        select(func.count()).select_from(WorkTask).where(service.needs_me(user.id))
    )
    inside = await _inside(session, [r.id for r in rows if r.parent_id is None])
    return {
        "code": 0,
        "data": {
            "items": [
                {
                    **task_view.task_out(r, names, user),
                    "id": r.id,
                    **inside.get(r.id, _EMPTY_INSIDE),
                }
                for r in rows
            ],
            "counts": counts,
            "needs_me": int(needs_me or 0),
        },
    }


async def _load(session: AsyncSession, code: str, *, lock: bool = False) -> WorkTask:
    stmt = select(WorkTask).where(WorkTask.code == code)
    row = await session.scalar(stmt.with_for_update() if lock else stmt)
    if row is None:
        raise not_found("任务不存在")
    return row


@router.get("/stats")
async def work_task_stats(
    days: int = Query(default=30, ge=1, le=365),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """运营统计：积压、各来源处理时长、平均交接次数、AI 员工一次验收通过率。"""
    _active(user)
    since = datetime.now(UTC) - timedelta(days=days)
    open_rows = (
        await session.execute(
            select(
                WorkTask.assignee_bot_id,
                WorkTask.assignee_user_id,
                func.count(),
                func.count().filter(WorkTask.due_at < func.now()),
            )
            .where(WorkTask.status.in_(WORK_TASK_OPEN))
            .group_by(WorkTask.assignee_bot_id, WorkTask.assignee_user_id)
            .order_by(func.count().desc())
            .limit(20)
        )
    ).all()
    names = await task_view.names_of(
        session,
        {b for b, _, _, _ in open_rows if b},
        {u for _, u, _, _ in open_rows if u},
    )
    backlog = [
        {
            "assignee": task_view.actor_out(bot_id, user_id, names),
            "open": int(n),
            "overdue": int(overdue),
        }
        for bot_id, user_id, n, overdue in open_rows
    ]
    hours = func.extract("epoch", WorkTask.closed_at - WorkTask.created_at) / 3600
    by_source = (
        await session.execute(
            select(
                WorkTask.source,
                func.count(),
                func.count().filter(WorkTask.status == "done"),
                func.avg(hours).filter(WorkTask.status == "done"),
            )
            .where(WorkTask.created_at >= since, WorkTask.status != "shadow")
            .group_by(WorkTask.source)
        )
    ).all()
    closed = int(
        await session.scalar(
            select(func.count())
            .select_from(WorkTask)
            .where(
                WorkTask.closed_at >= since,
                WorkTask.status == "done",
                WorkTask.origin_ref.is_(None),
            )
        )
        or 0
    )
    handoffs = int(
        await session.scalar(
            select(func.count())
            .select_from(WorkTaskEvent)
            .join(WorkTask, WorkTask.id == WorkTaskEvent.work_task_id)
            .where(
                WorkTaskEvent.kind == "handoff",
                WorkTask.closed_at >= since,
                WorkTask.status == "done",
            )
        )
        or 0
    )
    reviewed = select(WorkTaskEvent.work_task_id).where(
        WorkTaskEvent.kind.in_(("review_pass", "review_reject")),
        WorkTaskEvent.created_at >= since,
    )
    rejected = select(WorkTaskEvent.work_task_id).where(WorkTaskEvent.kind == "review_reject")
    agent_reviewed = int(
        await session.scalar(
            select(func.count())
            .select_from(WorkTask)
            .where(WorkTask.id.in_(reviewed), WorkTask.assignee_bot_id.is_not(None))
        )
        or 0
    )
    agent_first_pass = int(
        await session.scalar(
            select(func.count())
            .select_from(WorkTask)
            .where(
                WorkTask.id.in_(reviewed),
                WorkTask.assignee_bot_id.is_not(None),
                WorkTask.id.not_in(rejected),
            )
        )
        or 0
    )
    return {
        "code": 0,
        "data": {
            "days": days,
            "backlog": backlog,
            "by_source": [
                {
                    "source": source,
                    "created": int(total),
                    "done": int(done),
                    "avg_hours": round(float(avg), 1) if avg is not None else None,
                }
                for source, total, done, avg in by_source
            ],
            "handoffs_per_task": round(handoffs / closed, 2) if closed else None,
            "agent_first_pass_rate": (
                round(agent_first_pass / agent_reviewed, 3) if agent_reviewed else None
            ),
            "agent_reviewed": agent_reviewed,
        },
    }


@router.get("/{code}", response_model=None)
async def get_work_task(
    code: str,
    format: Literal["json", "md"] = Query("json"),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any] | PlainTextResponse:
    _active(user)
    row = await _load(session, code)
    data = await task_view.detail(session, row, user)
    if format == "md":
        # 给 AI 读的纯文本：没有页面样式和按钮，只有内容。
        return PlainTextResponse(markdown.render(data), media_type="text/markdown; charset=utf-8")
    return {"code": 0, "data": data}


# ---- 操作 ----------------------------------------------------------------------------------


class AssigneeIn(BaseModel):
    type: Literal["human", "bot"]
    id: uuid.UUID

    def value(self) -> Assignee:
        return Assignee(self.type, self.id)


class CreateIn(BaseModel):
    title: str = Field(max_length=200)
    summary: str = Field(default="", max_length=500)
    brief: str = Field(default="", max_length=service.BRIEF_MAX)
    assignee: AssigneeIn | None = None
    reviewer_user_id: uuid.UUID | None = None
    review_required: bool | None = None
    priority: str = "normal"
    due_at: datetime | None = None


class AssignIn(BaseModel):
    assignee: AssigneeIn
    note: str = Field(default="", max_length=service.BRIEF_MAX)
    review_required: bool | None = None


class AnswerIn(BaseModel):
    ask_id: str = Field(max_length=64)
    option: str = Field(max_length=64)
    text: str = Field(default="", max_length=service.ANSWER_MAX)


class TextIn(BaseModel):
    text: str = Field(default="", max_length=service.COMMENT_MAX)


class UpdateIn(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    summary: str | None = Field(default=None, max_length=500)
    brief: str | None = Field(default=None, max_length=service.BRIEF_MAX)
    priority: str | None = None
    due_at: datetime | None = None
    reviewer_user_id: uuid.UUID | None = None
    review_required: bool | None = None


class WatchIn(BaseModel):
    on: bool = True


async def _done(session: AsyncSession, row: WorkTask, user: User) -> dict[str, Any]:
    await session.flush()
    await session.commit()
    row = await _load(session, row.code)
    return {"code": 0, "data": await task_view.detail(session, row, user)}


async def _act(
    request: Request,
    session: AsyncSession,
    user: User,
    code: str,
    op: Callable[[WorkTask], Awaitable[Any]],
) -> dict[str, Any]:
    """改一个任务：锁行、执行、同步飞书卡片（问题变了收旧发新，完成取消通知相关的人）。"""
    _active(user)
    row = await _load(session, code, lock=True)
    before_ask, before_status = row.ask, row.status
    try:
        await op(row)
    except WorkTaskError as exc:
        raise _fail(exc) from exc
    await flow.settle(
        session,
        row,
        before_ask=before_ask,
        before_status=before_status,
        actor_id=user.id,
        base_url=request.app.state.settings.public_base_url,
    )
    return await _done(session, row, user)


@router.post("")
async def create_work_task(
    body: CreateIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    _active(user)
    try:
        row = await service.create(
            session,
            user,
            title=body.title,
            summary=body.summary,
            brief=body.brief,
            assignee=body.assignee.value() if body.assignee else None,
            reviewer_user_id=body.reviewer_user_id,
            review_required=body.review_required,
            priority=body.priority,
            due_at=body.due_at,
        )
    except WorkTaskError as exc:
        raise _fail(exc) from exc
    await flow.settle(
        session,
        row,
        before_ask=None,
        before_status="",
        actor_id=user.id,
        base_url=request.app.state.settings.public_base_url,
    )
    return await _done(session, row, user)


@router.post("/{code}/assign")
async def assign_work_task(
    code: str,
    body: AssignIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await _act(
        request,
        session,
        user,
        code,
        lambda row: service.assign(
            session,
            row,
            user,
            body.assignee.value(),
            note=body.note,
            review_required=body.review_required,
        ),
    )


@router.post("/{code}/answer")
async def answer_work_task(
    code: str,
    body: AnswerIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await _act(
        request,
        session,
        user,
        code,
        lambda row: execute.answer(
            session,
            row,
            user,
            ask_id=body.ask_id,
            option=body.option,
            text=body.text,
            channel="web",
        ),
    )


@router.post("/{code}/comment")
async def comment_work_task(
    code: str,
    body: TextIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    async def op(row: WorkTask) -> None:
        await service.comment(session, row, user, body.text)
        # AI 员工停下来等人（卡住、等确认）时，能管这件事的人补的话直接交回它接着做。
        if row.assignee_bot_id and service.restartable(row) and service.can_manage(row, user):
            bot = await session.get(Bot, row.assignee_bot_id)
            if bot is None:
                return
            try:
                async with session.begin_nested():
                    await execute.start(
                        session,
                        row,
                        bot=bot,
                        trigger="comment",
                        triggered_by={"user_id": str(user.id), "name": service.name_of(user)},
                        note=body.text.strip(),
                        silent=row.requester_bot_id is not None,
                    )
            except WorkTaskError:
                pass  # 交不回去（例如没私聊过）：评论照样记下，页面仍提示怎么叫它开工。

    return await _act(request, session, user, code, op)


@router.post("/{code}/complete")
async def complete_work_task(
    code: str,
    body: TextIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await _act(
        request, session, user, code, lambda row: service.complete(session, row, user, body.text)
    )


@router.post("/{code}/cancel")
async def cancel_work_task(
    code: str,
    body: TextIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await _act(
        request, session, user, code, lambda row: service.cancel(session, row, user, body.text)
    )


@router.post("/{code}/reopen")
async def reopen_work_task(
    code: str,
    body: TextIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await _act(
        request, session, user, code, lambda row: service.reopen(session, row, user, body.text)
    )


@router.patch("/{code}")
async def update_work_task(
    code: str,
    body: UpdateIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await _act(
        request,
        session,
        user,
        code,
        lambda row: service.update(session, row, user, fields=body.model_dump(exclude_unset=True)),
    )


@router.post("/{code}/watch")
async def watch_work_task(
    code: str,
    body: WatchIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    _active(user)
    row = await _load(session, code)
    await service.watch(session, row, user, body.on)
    return await _done(session, row, user)
