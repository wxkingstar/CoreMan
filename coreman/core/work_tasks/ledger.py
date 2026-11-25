"""统一账本：把 API 任务、AI 员工互相求助、向同事求助镜像成任务，任务页上一处看全。

这些事各有自己的执行逻辑（触达、过期、续跑），这里不插手：调度器定期按原表建镜像、刷新状态。
镜像只读（`origin_ref` 不为空），在原处处理；发生在任务会话里的求助挂成那件任务的子任务。
见 docs/superpowers/specs/2026-11-25-company-task-platform.md §7。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.chat import bot_collaboration
from coreman.core.db.models import (
    ApiTask,
    Bot,
    BotCollaboration,
    BotCollaborationRoute,
    HumanCollaboration,
    Task,
    User,
    WorkTask,
    WorkTaskEvent,
)
from coreman.core.db.models.bot_collaboration import HUMAN_ACTIVE
from coreman.core.work_tasks.service import BRIEF_MAX, SUMMARY_MAX, TITLE_MAX, clip, name_of

# 只镜像最近这段时间里新建的记录；更早的历史不回填。
WINDOW = timedelta(days=7)
BATCH = 200
_RUNNING = ("queued", "claimed", "running")


def _title(text: str, prefix: str = "") -> str:
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return clip(prefix + first, TITLE_MAX) or prefix.rstrip("：") or "（无标题）"


async def _parent_of(session: AsyncSession, chat_task_id: int | None) -> WorkTask | None:
    """求助发生在任务会话里：挂成那件任务的子任务。"""
    task = await session.get(Task, chat_task_id) if chat_task_id else None
    key = (task.session_key or "") if task else ""
    if not key.startswith("work:"):
        return None
    row: WorkTask | None = await session.scalar(
        select(WorkTask).where(WorkTask.code == key.removeprefix("work:"))
    )
    return row


def _apply(row: WorkTask, status: str, now: datetime, summary: str = "", waiting: str = "") -> bool:
    """把原记录的状态写进镜像；返回有没有变。"""
    new: tuple[str, dict[str, Any] | None]
    if status == "done":
        new = ("done", None)
    elif status == "cancelled":
        new = ("cancelled", None)
    elif waiting:
        new = ("waiting", {"kind": "external", "human_text": clip(waiting, SUMMARY_MAX)})
    else:
        new = ("in_progress", None)
    changed = (row.status, row.waiting_on) != new or (summary and row.summary != summary)
    if not changed:
        return False
    row.status, row.waiting_on = new
    if summary:
        row.summary = clip(summary, SUMMARY_MAX)
    row.closed_at = now if row.status in ("done", "cancelled") else None
    row.updated_at = now
    return True


def _mirror(
    *,
    ref: str,
    title: str,
    brief: str,
    source: str,
    parent: WorkTask | None,
    now: datetime,
    **fields: Any,
) -> WorkTask:
    return WorkTask(
        origin_ref=ref,
        title=title,
        brief=brief[:BRIEF_MAX],
        status="in_progress",
        source=source,
        start_mode="auto",
        claimed_at=now,
        review_required=False,
        parent_id=parent.id if parent else None,
        root_id=(parent.root_id or parent.id) if parent else None,
        depth=(parent.depth + 1) if parent else 0,
        **fields,
    )


async def _existing(session: AsyncSession, refs: list[str]) -> dict[str, WorkTask]:
    if not refs:
        return {}
    rows = await session.scalars(select(WorkTask).where(WorkTask.origin_ref.in_(refs)))
    return {str(r.origin_ref): r for r in rows}


def _event(session: AsyncSession, row: WorkTask, text: str, detail: dict[str, Any]) -> None:
    session.add(
        WorkTaskEvent(
            work_task_id=row.id,
            kind="mirrored",
            actor_type="system",
            human_text=clip(text, 80),
            agent_detail=detail,
        )
    )


async def _api_tasks(session: AsyncSession, now: datetime) -> int:
    rows = list(
        await session.scalars(
            select(ApiTask)
            .where(ApiTask.created_at >= now - WINDOW)
            .order_by(ApiTask.created_at.desc())
            .limit(BATCH)
        )
    )
    mirrors = await _existing(session, [f"api:{r.id}" for r in rows])
    changed = 0
    for api in rows:
        ref = f"api:{api.id}"
        task = await session.get(Task, api.task_id) if api.task_id else None
        status = task.status if task else "queued"
        mirror = mirrors.get(ref)
        if mirror is None:
            user = await session.get(User, api.user_id)
            via = f"业务系统 {api.client_key}" if api.client_key else "API"
            mirror = _mirror(
                ref=ref,
                title=_title(api.prompt),
                brief=api.prompt,
                source="api",
                parent=None,
                now=now,
                summary=clip(f"{name_of(user)}经{via}发起", SUMMARY_MAX),
                requester_user_id=api.user_id,
                requester_name=name_of(user),
                principal_user_id=api.user_id,
                assignee_bot_id=api.bot_id,
                platform="feishu",
                chat_id=api.chat_id,
                created_at=api.created_at,
            )
            session.add(mirror)
            await session.flush()
            _event(session, mirror, f"{name_of(user)}经{via}发起", {"api_task_id": str(api.id)})
            changed += 1
        waiting = "等运行时额度恢复" if api.quota_waiting_since and status in _RUNNING else ""
        target = (
            "done"
            if status == "succeeded"
            else "cancelled"
            if status in ("failed", "cancelled")
            else "running"
        )
        summary = ""
        if target == "cancelled" and task is not None:
            summary = f"没做完：{task.error_message or task.error_code or status}"
        if _apply(mirror, target, now, summary, waiting):
            changed += 1
    return changed


async def _bot_help(session: AsyncSession, now: datetime) -> int:
    rows = list(
        await session.scalars(
            select(BotCollaboration)
            .where(BotCollaboration.created_at >= now - WINDOW)
            .order_by(BotCollaboration.created_at.desc())
            .limit(BATCH)
        )
    )
    mirrors = await _existing(session, [f"bot_help:{r.id}" for r in rows])
    changed = 0
    for help_row in rows:
        ref = f"bot_help:{help_row.id}"
        mirror = mirrors.get(ref)
        if mirror is None:
            route = await session.get(BotCollaborationRoute, help_row.route_id)
            if route is None:
                continue
            source = await session.get(Bot, route.source_bot_id)
            helper = await session.get(Bot, route.target_bot_id)
            parent = await _parent_of(session, help_row.source_task_id)
            mirror = _mirror(
                ref=ref,
                title=_title(help_row.question, "求助："),
                brief=help_row.question,
                source="collaboration",
                parent=parent,
                now=now,
                summary="",
                requester_bot_id=route.source_bot_id,
                requester_name=source.name if source else "",
                principal_user_id=help_row.origin_user_id,
                assignee_bot_id=route.target_bot_id,
                platform="feishu",
                chat_id=route.chat_id,
                created_at=help_row.created_at,
            )
            session.add(mirror)
            await session.flush()
            _event(
                session,
                mirror,
                f"{source.name if source else ''}向{helper.name if helper else ''}求助",
                {"question": help_row.question},
            )
            changed += 1
        if help_row.status in bot_collaboration.ACTIVE:
            target, summary = "running", ""
        elif help_row.status == "completed":
            target, summary = "done", help_row.response or "已答复"
        else:
            target, summary = "cancelled", f"没完成：{help_row.error or help_row.status}"
        if _apply(mirror, target, now, summary):
            if target == "done" and help_row.response:
                mirror.digest = help_row.response[:BRIEF_MAX]
            changed += 1
    return changed


async def _human_help(session: AsyncSession, now: datetime) -> int:
    rows = list(
        await session.scalars(
            select(HumanCollaboration)
            .where(HumanCollaboration.created_at >= now - WINDOW)
            .order_by(HumanCollaboration.created_at.desc())
            .limit(BATCH)
        )
    )
    mirrors = await _existing(session, [f"human_help:{r.id}" for r in rows])
    changed = 0
    for help_row in rows:
        ref = f"human_help:{help_row.id}"
        mirror = mirrors.get(ref)
        helper = await session.get(User, help_row.helper_user_id)
        if mirror is None:
            bot = await session.get(Bot, help_row.bot_id)
            parent = await _parent_of(session, help_row.source_task_id)
            mirror = _mirror(
                ref=ref,
                title=_title(help_row.question, "问："),
                brief=help_row.question,
                source="collaboration",
                parent=parent,
                now=now,
                summary="",
                requester_bot_id=help_row.bot_id,
                requester_name=bot.name if bot else "",
                principal_user_id=help_row.origin_user_id,
                assignee_user_id=help_row.helper_user_id,
                platform="feishu",
                chat_id=help_row.origin_chat_id,
                created_at=help_row.created_at,
            )
            session.add(mirror)
            await session.flush()
            _event(
                session,
                mirror,
                f"{bot.name if bot else ''}向{name_of(helper)}求助",
                {"question": help_row.question},
            )
            changed += 1
        if help_row.status in ("pending", "waiting"):
            target, summary, waiting = "running", "", f"等{name_of(helper)}在飞书里回复"
        elif help_row.status in HUMAN_ACTIVE or help_row.status == "completed":
            target, summary, waiting = "done", help_row.response or "已答复", ""
        else:
            target, summary, waiting = (
                "cancelled",
                f"没完成：{help_row.error or help_row.status}",
                "",
            )
        if _apply(mirror, target, now, summary, waiting):
            changed += 1
    return changed


async def sync(session: AsyncSession, now: datetime) -> int:
    """建新镜像、刷新镜像状态；返回改动条数。"""
    return (
        await _api_tasks(session, now)
        + await _bot_help(session, now)
        + await _human_help(session, now)
    )
