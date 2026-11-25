"""任务平台：列表、详情与人在网页上的操作。

所有登录的成员都能看，包括给 Agent 看的那一层（brief、记要、调度依据、事件细节、运行记录）。
能改任务的是发起人、代表人、执行人、验收人和平台管理员；其他成员可以评论和关注。
见 docs/superpowers/specs/2026-11-25-company-task-platform.md。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import current_user, get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.security import verify_csrf
from coreman.core.db.models import (
    WORK_TASK_STATUSES,
    Bot,
    GroupObserveDecision,
    User,
    WorkTask,
    WorkTaskEvent,
    WorkTaskRun,
    WorkTaskWatcher,
)
from coreman.core.work_tasks import execute, flow, service
from coreman.core.work_tasks.service import Assignee, WorkTaskError

router = APIRouter(
    prefix="/api/admin/work-tasks", tags=["work-tasks"], dependencies=[Depends(verify_csrf)]
)


def _active(user: User) -> None:
    if user.status != "active":
        raise ApiError(403, 403, "账号未启用")


def _fail(exc: WorkTaskError) -> ApiError:
    return ApiError(exc.status, exc.status, exc.message)


async def _names(
    session: AsyncSession, bot_ids: set[uuid.UUID], user_ids: set[uuid.UUID]
) -> dict[str, str]:
    """id → 名字，AI 员工和成员放在一张表里（键是 id 的字符串）。"""
    names: dict[str, str] = {}
    if bot_ids:
        rows = await session.execute(select(Bot.id, Bot.name).where(Bot.id.in_(bot_ids)))
        names |= {str(i): n for i, n in rows.all()}
    if user_ids:
        rows = await session.execute(
            select(User.id, User.display_name, User.login_name).where(User.id.in_(user_ids))
        )
        names |= {str(i): d or login for i, d, login in rows.all()}
    return names


def _ids(rows: list[WorkTask]) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    bots = {
        i for r in rows for i in (r.assignee_bot_id, r.dispatcher_bot_id, r.requester_bot_id) if i
    }
    users = {
        i
        for r in rows
        for i in (
            r.requester_user_id,
            r.principal_user_id,
            r.assignee_user_id,
            r.reviewer_user_id,
            service.ask_to(r),
        )
        if i
    }
    for r in rows:
        waiting = (r.waiting_on or {}).get("user_id")
        if waiting:
            users.add(uuid.UUID(waiting))
    return bots, users


def _bot(bot_id: uuid.UUID | None, names: dict[str, str]) -> dict[str, Any] | None:
    if bot_id is None:
        return None
    return {"id": str(bot_id), "name": names.get(str(bot_id), "已删除的员工")}


def _actor(
    bot_id: uuid.UUID | None, user_id: uuid.UUID | None, names: dict[str, str]
) -> dict[str, Any] | None:
    if bot_id is not None:
        return {"type": "bot", "id": str(bot_id), "name": names.get(str(bot_id), "已删除的员工")}
    if user_id is not None:
        return {
            "type": "human",
            "id": str(user_id),
            "name": names.get(str(user_id), "已删除的成员"),
        }
    return None


def task_out(row: WorkTask, names: dict[str, str], viewer: User | None = None) -> dict[str, Any]:
    """给人看的那一层。"""
    ask = row.ask
    return {
        "code": row.code,
        "title": row.title,
        "summary": row.summary,
        "status": row.status,
        "source": row.source,
        "platform": row.platform,
        "chat_id": row.chat_id,
        "requester_name": row.requester_name,
        "requester": _actor(row.requester_bot_id, row.requester_user_id, names),
        "principal": _actor(None, row.principal_user_id, names),
        "assignee": _actor(row.assignee_bot_id, row.assignee_user_id, names),
        "reviewer": _actor(None, row.reviewer_user_id, names),
        "dispatcher": _bot(row.dispatcher_bot_id, names),
        "review_required": row.review_required,
        "priority": row.priority,
        "due_at": row.due_at,
        "start_mode": row.start_mode,
        "claimed_at": row.claimed_at,
        # 人派给 AI 员工、还没被领取：页面上提示人去叫它开工。
        "needs_drive": service.needs_drive(row),
        "now": service.current_line(row, names),
        "ask": (
            {
                "id": ask["id"],
                "kind": ask["kind"],
                "to": _actor(None, service.ask_to(row), names),
                "question": ask["question"],
                "why": ask.get("why") or "",
                "options": ask["options"],
                "other": bool(ask.get("other")),
                "needs_text": sorted(service.NEEDS_TEXT),
                "mine": viewer is not None and str(viewer.id) == ask.get("to_user_id"),
                "can_answer": viewer is not None and service.can_answer(row, viewer),
            }
            if ask
            else None
        ),
        "can_manage": viewer is not None and service.can_manage(row, viewer),
        "related_codes": list(row.related_codes or []),
        "artifacts": list(row.artifacts or []),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "closed_at": row.closed_at,
    }


@router.get("")
async def list_work_tasks(
    view: Literal["all", "needs_me", "created", "watching"] = "all",
    chat_id: str | None = None,
    status: str | None = None,
    assignee_bot_id: uuid.UUID | None = None,
    assignee_user_id: uuid.UUID | None = None,
    include_shadow: bool = True,
    q: str | None = Query(default=None, max_length=100),
    before_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    _active(user)
    conditions: list[Any] = []
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
    names = await _names(session, *_ids(rows))
    grouped = await session.execute(
        select(WorkTask.status, func.count()).where(*conditions).group_by(WorkTask.status)
    )
    counts: dict[str, int] = {str(s): int(n) for s, n in grouped.all()}
    needs_me = await session.scalar(
        select(func.count()).select_from(WorkTask).where(service.needs_me(user.id))
    )
    return {
        "code": 0,
        "data": {
            "items": [{**task_out(r, names, user), "id": r.id} for r in rows],
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


async def _detail(session: AsyncSession, row: WorkTask, user: User) -> dict[str, Any]:
    events = list(
        await session.scalars(
            select(WorkTaskEvent)
            .where(WorkTaskEvent.work_task_id == row.id)
            .order_by(WorkTaskEvent.id)
        )
    )
    runs = list(
        await session.scalars(
            select(WorkTaskRun).where(WorkTaskRun.work_task_id == row.id).order_by(WorkTaskRun.id)
        )
    )
    children = list(
        await session.scalars(
            select(WorkTask).where(WorkTask.parent_id == row.id).order_by(WorkTask.id)
        )
    )
    parent = await session.get(WorkTask, row.parent_id) if row.parent_id else None
    watcher_ids = set(
        await session.scalars(
            select(WorkTaskWatcher.user_id).where(WorkTaskWatcher.work_task_id == row.id)
        )
    )
    decision = await session.get(GroupObserveDecision, row.decision_id) if row.decision_id else None
    bots, users = _ids([row, *children])
    bots |= {uuid.UUID(e.actor_id) for e in events if e.actor_type == "bot" and e.actor_id}
    bots |= {r.bot_id for r in runs if r.bot_id}
    users |= {uuid.UUID(e.actor_id) for e in events if e.actor_type == "human" and e.actor_id}
    users |= watcher_ids
    names = await _names(session, bots, users)
    return {
        **task_out(row, names, user),
        "watching": user.id in watcher_ids,
        "watchers": [{"id": str(i), "name": names.get(str(i), "")} for i in watcher_ids],
        "parent": {"code": parent.code, "title": parent.title} if parent else None,
        "children": [task_out(c, names, user) for c in children],
        "events": [
            {
                "id": e.id,
                "kind": e.kind,
                "actor_type": e.actor_type,
                "actor_name": names.get(e.actor_id or "", ""),
                "human_text": e.human_text,
                "agent_detail": e.agent_detail,
                "created_at": e.created_at,
            }
            for e in events
        ],
        # 给 Agent 看的那一层：页面上默认折叠，所有人都能展开。
        "agent": {
            "brief": row.brief,
            "digest": row.digest,
            "report": row.report,
            "waiting_on": row.waiting_on,
            "runs": [
                {
                    "id": r.id,
                    "bot": _bot(r.bot_id, names),
                    "trigger": r.trigger,
                    "triggered_by": r.triggered_by,
                    "status": r.status,
                    "outcome": r.outcome,
                    "session_key": r.session_key,
                    "chat_task_id": r.chat_task_id,
                    "started_at": r.started_at,
                    "ended_at": r.ended_at,
                }
                for r in runs
            ],
            "decision": (
                {
                    "id": decision.id,
                    "probability": decision.probability,
                    "outcome": decision.outcome,
                    "recent_messages": (decision.state or {}).get("recent_messages") or [],
                    "dispatch": decision.dispatch,
                    "created_at": decision.created_at,
                }
                if decision is not None
                else None
            ),
        },
    }


@router.get("/{code}")
async def get_work_task(
    code: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    _active(user)
    row = await _load(session, code)
    return {"code": 0, "data": await _detail(session, row, user)}


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
    return {"code": 0, "data": await _detail(session, row, user)}


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
