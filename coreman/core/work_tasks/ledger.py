"""统一账本：把 API 任务、AI 员工互相求助、向同事求助、人工求助镜像成任务，任务页上一处看全。

这些事各有自己的执行逻辑（触达、过期、续跑），这里不插手：调度器定期按原表建镜像、刷新状态。
镜像只读（`origin_ref` 不为空），在原处处理；发生在任务会话里的求助挂成那件任务的子任务。

范围：新建镜像只看最近 7 天的原记录（更早的历史不回填）；已有镜像只要还没结束，不论多久都继续
刷新，直到原记录结束。私聊里发起的（API 任务回到私聊、私聊或定时任务里向同事求助、人工求助——
它总是私发给被问的人）只记谁找谁、状态，不复制内容：任务页所有成员都能看，私聊内容不该因此外露。
见 docs/superpowers/specs/2026-11-25-company-task-platform.md §7。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.chat import bot_collaboration
from coreman.core.db.models import (
    WORK_TASK_OPEN,
    ApiClient,
    ApiTask,
    Bot,
    BotCollaboration,
    BotCollaborationRoute,
    Escalation,
    HumanCollaboration,
    Task,
    User,
    WorkTask,
    WorkTaskEvent,
)
from coreman.core.work_tasks.service import (
    BRIEF_MAX,
    SUMMARY_MAX,
    TITLE_MAX,
    clip,
    name_of,
    plain_line,
)

log = logging.getLogger(__name__)

WINDOW = timedelta(days=7)
BATCH = 200
_RUNNING = ("queued", "claimed", "running")
_FAILED = ("failed", "cancelled", "timed_out")


def _title(text: str, prefix: str = "") -> str:
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return clip(prefix + plain_line(first), TITLE_MAX) or prefix.rstrip("：") or "（无标题）"


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
    # 一句话是给人看的：原文是 AI 的回复时去掉里面的 Markdown 记号。
    summary = clip(plain_line(summary), SUMMARY_MAX) if summary else ""
    if (row.status, row.waiting_on) == new and (not summary or row.summary == summary):
        return False
    row.status, row.waiting_on = new
    if summary:
        row.summary = summary
    row.closed_at = now if row.status in ("done", "cancelled") else None
    row.updated_at = now
    return True


def _mirror(
    *, ref: str, title: str, brief: str, source: str, parent: WorkTask | None, **fields: Any
) -> WorkTask:
    return WorkTask(
        origin_ref=ref,
        title=title,
        brief=brief[:BRIEF_MAX],
        status="in_progress",
        source=source,
        start_mode="auto",
        claimed_at=fields.get("created_at"),
        review_required=False,
        parent_id=parent.id if parent else None,
        root_id=(parent.root_id or parent.id) if parent else None,
        depth=(parent.depth + 1) if parent else 0,
        **fields,
    )


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


async def _sources(
    session: AsyncSession,
    model: Any,
    prefix: str,
    now: datetime,
    parse: Callable[[str], Any] = uuid.UUID,
) -> tuple[list[Any], dict[str, WorkTask]]:
    """要处理的原记录：最近 7 天新建的，加上镜像还没结束的（不论多久）。"""
    rows = list(
        await session.scalars(
            select(model)
            .where(model.created_at >= now - WINDOW)
            .order_by(model.created_at.desc())
            .limit(BATCH)
        )
    )
    open_refs = list(
        await session.scalars(
            select(WorkTask.origin_ref).where(
                WorkTask.origin_ref.like(f"{prefix}%"), WorkTask.status.in_(WORK_TASK_OPEN)
            )
        )
    )
    known = {r.id for r in rows}
    stale = []
    for ref in open_refs:
        try:
            source_id = parse(str(ref).removeprefix(prefix))
        except ValueError:
            continue
        if source_id not in known:
            stale.append(source_id)
    if stale:
        rows += list(await session.scalars(select(model).where(model.id.in_(stale))))
    refs = [f"{prefix}{r.id}" for r in rows]
    mirrors = {
        str(r.origin_ref): r
        for r in await session.scalars(select(WorkTask).where(WorkTask.origin_ref.in_(refs)))
    }
    return rows, mirrors


async def _each(
    session: AsyncSession, rows: list[Any], one: Callable[[Any], Awaitable[bool]]
) -> int:
    """一条一个保存点：某条出错只跳过它，不拖垮整轮。"""
    changed = 0
    for item in rows:
        try:
            async with session.begin_nested():
                changed += 1 if await one(item) else 0
        except Exception as exc:  # noqa: BLE001 记下类型，下一轮再试
            log.warning("work_task_mirror_failed: %s", type(exc).__name__)
    return changed


async def _api_tasks(session: AsyncSession, now: datetime) -> int:
    rows, mirrors = await _sources(session, ApiTask, "api:", now)

    async def one(api: ApiTask) -> bool:
        ref = f"api:{api.id}"
        task = await session.get(Task, api.task_id) if api.task_id else None
        status = task.status if task else "queued"
        mirror = mirrors.get(ref)
        created = False
        if mirror is None:
            user = await session.get(User, api.user_id)
            # 写出是哪个业务系统发起的：一排「业务系统发起的私聊任务」分不出谁是谁。
            client = await session.get(ApiClient, api.client_key) if api.client_key else None
            via = (client.name if client else "业务系统") if api.client_key else "API"
            private = api.target == "p2p"
            mirror = _mirror(
                ref=ref,
                title=f"{via}发起的私聊任务" if private else _title(api.prompt),
                brief="" if private else api.prompt,
                source="api",
                parent=None,
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
            mirrors[ref] = mirror
            _event(session, mirror, f"{name_of(user)}经{via}发起", {"api_task_id": str(api.id)})
            created = True
        waiting = "等运行时额度恢复" if api.quota_waiting_since and status in _RUNNING else ""
        target = (
            "done" if status == "succeeded" else "cancelled" if status in _FAILED else "running"
        )
        summary = ""
        if target == "cancelled" and task is not None:
            summary = f"没做完：{task.error_code or status}"
        return _apply(mirror, target, now, summary, waiting) or created

    return await _each(session, rows, one)


async def _bot_help(session: AsyncSession, now: datetime) -> int:
    rows, mirrors = await _sources(session, BotCollaboration, "bot_help:", now)

    async def one(help_row: BotCollaboration) -> bool:
        ref = f"bot_help:{help_row.id}"
        mirror = mirrors.get(ref)
        created = False
        if mirror is None:
            route = await session.get(BotCollaborationRoute, help_row.route_id)
            if route is None:
                return False
            source = await session.get(Bot, route.source_bot_id)
            helper = await session.get(Bot, route.target_bot_id)
            parent = await _parent_of(session, help_row.source_task_id)
            # AI 员工互相求助只发生在群里：问题照录。
            mirror = _mirror(
                ref=ref,
                title=_title(help_row.question, "求助："),
                brief=help_row.question,
                source="collaboration",
                parent=parent,
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
            mirrors[ref] = mirror
            _event(
                session,
                mirror,
                f"{source.name if source else ''}向{helper.name if helper else ''}求助",
                {"question": help_row.question},
            )
            created = True
        if help_row.status in bot_collaboration.ACTIVE:
            target, summary = "running", ""
        elif help_row.status == "completed":
            target, summary = "done", help_row.response or "已答复"
        else:
            target, summary = "cancelled", f"没完成：{help_row.error or help_row.status}"
        changed = _apply(mirror, target, now, summary)
        if changed and target == "done" and help_row.response:
            mirror.digest = help_row.response[:BRIEF_MAX]
        return changed or created

    return await _each(session, rows, one)


async def _human_help(session: AsyncSession, now: datetime) -> int:
    rows, mirrors = await _sources(session, HumanCollaboration, "human_help:", now)

    async def one(help_row: HumanCollaboration) -> bool:
        ref = f"human_help:{help_row.id}"
        mirror = mirrors.get(ref)
        helper = await session.get(User, help_row.helper_user_id)
        # 私聊、定时任务里的求助：只记谁问谁，不复制问题和答复。
        private = help_row.origin_chat_type != "group" or help_row.origin_kind == "cron"
        created = False
        if mirror is None:
            bot = await session.get(Bot, help_row.bot_id)
            parent = await _parent_of(session, help_row.source_task_id)
            mirror = _mirror(
                ref=ref,
                title=f"向{name_of(helper)}求助" if private else _title(help_row.question, "问："),
                brief="" if private else help_row.question,
                source="collaboration",
                parent=parent,
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
            mirrors[ref] = mirror
            _event(
                session,
                mirror,
                f"{bot.name if bot else ''}向{name_of(helper)}求助",
                {} if private else {"question": help_row.question},
            )
            created = True
        if help_row.status in ("pending", "waiting"):
            target, summary, waiting = "running", "", f"等{name_of(helper)}在飞书里回复"
        elif help_row.status in ("answered", "resuming", "completed"):
            answer = "" if private else (help_row.response or "")
            target, summary, waiting = "done", answer or "已答复", ""
        else:
            target, summary, waiting = (
                "cancelled",
                f"没完成：{help_row.error or help_row.status}",
                "",
            )
        return _apply(mirror, target, now, summary, waiting) or created

    return await _each(session, rows, one)


# 人工求助怎么结束的：给人看的一句。
_RESOLUTION = {
    "agent": "已答复",
    "observed": "线下已解决",
    "offline": "对方不在，已改线下处理",
    "expired": "没人回复，已过期",
}


async def _escalations(session: AsyncSession, now: datetime) -> int:
    rows, mirrors = await _sources(session, Escalation, "escalation:", now, parse=int)

    async def one(row: Escalation) -> bool:
        ref = f"escalation:{row.id}"
        mirror = mirrors.get(ref)
        to = await session.get(User, row.to_user_id)
        created = False
        if mirror is None:
            bot = await session.get(Bot, row.bot_id)
            asker = await session.get(User, row.from_user_id) if row.from_user_id else None
            who = bot.name if bot else "AI 员工"
            mirror = _mirror(
                ref=ref,
                title=clip(f"人工求助：问{name_of(to)}", TITLE_MAX),
                brief="",
                source="collaboration",
                parent=None,
                summary=clip(f"{who}向{name_of(to)}求助", SUMMARY_MAX),
                requester_bot_id=row.bot_id,
                requester_user_id=asker.id if asker else None,
                requester_name=who,
                principal_user_id=asker.id if asker else None,
                assignee_user_id=row.to_user_id,
                platform="wecom" if row.notify_platform.startswith("wecom") else "feishu",
                created_at=row.created_at,
            )
            session.add(mirror)
            await session.flush()
            mirrors[ref] = mirror
            _event(session, mirror, f"{who}向{name_of(to)}发起人工求助", {"escalation": ref})
            created = True
        if row.status in ("pending", "queued"):
            waiting = f"等{name_of(to)}回复" if row.status == "pending" else "排队等前一条求助结束"
            target, summary = "running", ""
        elif row.status == "replied":
            target, summary, waiting = "running", "", ""
        elif row.status == "completed":
            target, waiting = "done", ""
            summary = _RESOLUTION.get(row.resolution or "agent", "已答复")
        else:
            target, waiting = "cancelled", ""
            summary = _RESOLUTION.get(row.resolution or "", "已取消")
        return _apply(mirror, target, now, summary, waiting) or created

    return await _each(session, rows, one)


async def sync(session: AsyncSession, now: datetime) -> int:
    """建新镜像、刷新没结束的镜像；返回改动条数。"""
    return (
        await _api_tasks(session, now)
        + await _bot_help(session, now)
        + await _human_help(session, now)
        + await _escalations(session, now)
    )
