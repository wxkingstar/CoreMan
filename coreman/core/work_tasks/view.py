"""任务详情的组装：任务页接口和 AI 员工的任务工具共用（纯文本版见 markdown.py）。"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import (
    WORK_TASK_OPEN,
    Bot,
    GroupObserveDecision,
    User,
    WorkTask,
    WorkTaskEvent,
    WorkTaskRun,
    WorkTaskWatcher,
)
from coreman.core.work_tasks import progress, service


async def names_of(
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


def ids_of(rows: list[WorkTask]) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
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


def bot_out(bot_id: uuid.UUID | None, names: dict[str, str]) -> dict[str, Any] | None:
    if bot_id is None:
        return None
    return {"id": str(bot_id), "name": names.get(str(bot_id), "已删除的员工")}


def actor_out(
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


def ask_out(row: WorkTask, names: dict[str, str], viewer: User | None) -> dict[str, Any] | None:
    """任务上待回答的问题（给人看）；没有时是 None。"""
    ask = row.ask
    if not ask:
        return None
    return {
        "id": ask["id"],
        "kind": ask["kind"],
        "to": actor_out(None, service.ask_to(row), names),
        "question": ask["question"],
        "why": ask.get("why") or "",
        "options": ask["options"],
        "other": bool(ask.get("other")),
        "needs_text": sorted(service.NEEDS_TEXT),
        "mine": viewer is not None and str(viewer.id) == ask.get("to_user_id"),
        "can_answer": viewer is not None and service.can_answer(row, viewer),
    }


def task_out(row: WorkTask, names: dict[str, str], viewer: User | None = None) -> dict[str, Any]:
    """给人看的那一层。"""
    return {
        "code": row.code,
        "title": row.title,
        "summary": row.summary,
        "status": row.status,
        "source": row.source,
        "mirror": bool(row.origin_ref),
        "platform": row.platform,
        "chat_id": row.chat_id,
        "requester_name": row.requester_name,
        "requester": actor_out(row.requester_bot_id, row.requester_user_id, names),
        "principal": actor_out(None, row.principal_user_id, names),
        "assignee": actor_out(row.assignee_bot_id, row.assignee_user_id, names),
        "reviewer": actor_out(None, row.reviewer_user_id, names),
        "dispatcher": bot_out(row.dispatcher_bot_id, names),
        "review_required": row.review_required,
        "priority": row.priority,
        "due_at": row.due_at,
        "start_mode": row.start_mode,
        "claimed_at": row.claimed_at,
        # 人派给 AI 员工、还没被领取：页面上提示人去叫它开工。
        "needs_drive": service.needs_drive(row),
        "now": service.current_line(row, names),
        "ask": ask_out(row, names, viewer),
        "can_manage": viewer is not None and service.can_manage(row, viewer),
        "related_codes": list(row.related_codes or []),
        "artifacts": list(row.artifacts or []),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "closed_at": row.closed_at,
    }


# 前后可以紧挨中文（「做T-7」），不能是别的编号的一部分。
CODE = re.compile(r"(?<![A-Za-z0-9_-])T-\d+(?!\d)")


async def linked_of(session: AsyncSession, row: WorkTask, texts: list[str]) -> dict[str, Any]:
    """页面上出现的任务编号（相关任务、上级、正文里提到的）→ 标题和状态：编号可点、旁边标状态。"""
    codes = set(row.related_codes or [])
    for text in texts:
        codes |= set(CODE.findall(text or ""))
    codes.discard(row.code)
    if not codes:
        return {}
    found = await session.execute(
        select(WorkTask.code, WorkTask.title, WorkTask.status).where(WorkTask.code.in_(codes))
    )
    return {c: {"title": t, "status": s} for c, t, s in found.all()}


# 「经过」里默认不给人看的流水：AI 员工开始/继续处理、等子任务、拆子任务（子任务表里有）、
# 计划、没人回答的自动提醒，以及子任务都有结果后接着做、子任务被接手这类自动领取。
# 页面上点「显示全部」才出来；给 AI 的纯文本版照旧全给。
_ROUTINE_KINDS = frozenset({"started", "waiting", "subtask", "plan", "ask_reminded"})
_ROUTINE_TRIGGERS = frozenset({"resume", "subtask"})
# 拆出来问人的那种子任务（agent_tools.ask_human 写的「问王五（T-9）：……」）算里程碑。
_ASKED_PERSON = re.compile(r"问.+（T-\d+）：")


def milestone(event: WorkTaskEvent) -> bool:
    """这条经过是不是给人看的里程碑：建任务、问人与回答、进展、评论、转交、交付、完成等。"""
    if event.kind == "subtask" and _ASKED_PERSON.match(event.human_text or ""):
        return True
    if not event.human_text or event.kind in _ROUTINE_KINDS:
        return False
    return not (
        event.kind == "claimed" and (event.agent_detail or {}).get("trigger") in _ROUTINE_TRIGGERS
    )


async def open_asks(session: AsyncSession, row: WorkTask) -> list[WorkTask]:
    """这件事自己和它下面各层还没人答的问题（问人、要不要接、待验收），按提出的先后。"""
    chain = list(
        await session.scalars(
            select(WorkTask)
            .where(WorkTask.root_id == (row.root_id or row.id), WorkTask.id != row.id)
            .order_by(WorkTask.id)
        )
    )
    inside = {row.id}
    for item in chain:  # 子任务的 id 总比上级大：按 id 顺着走一遍就能认出整棵子树
        if item.parent_id in inside:
            inside.add(item.id)
    # `ask` 是 JSON 列，没有问题时存的是 JSON 的 null：取出来再看。
    return [
        item
        for item in [row, *chain]
        if item.id in inside
        and item.status in WORK_TASK_OPEN
        and item.ask
        and item.ask.get("to_user_id")
    ]


def _step_note(child: WorkTask, kind: str, asked: list[str], names: dict[str, str]) -> str:
    """一步后面跟的那句话：做完的写结果，在等人的写等谁，还没轮到的写现状。"""
    who = names.get(str(child.assignee_user_id or child.assignee_bot_id or ""), "")
    if kind in ("done", "cancelled"):
        return child.summary if kind == "done" else ""
    if kind == "running":
        return service.current_line(child, names) if child.status == "todo" else ""
    if progress.is_question(child):
        return f"等{who}回答"
    if asked:
        return f"在等{'、'.join(asked)}回答"
    if child.status == "waiting":
        return str((child.waiting_on or {}).get("human_text") or "等人处理")
    if child.status == "review":
        return "等验收"
    return f"等{who}接手"


async def overview(
    session: AsyncSession,
    row: WorkTask,
    children: list[WorkTask],
    asks: list[WorkTask],
    events: list[WorkTaskEvent],
    names: dict[str, str],
    viewer: User | None,
) -> dict[str, Any]:
    """任务页首屏的全貌，和群里的进展卡同一套分块：要人定的、正在做的、接下来的、已完成的、最新一句。

    分类用 `progress.step_kind`，和进展卡一致；这里给结构化的数据，时间由页面按看的人的时区画。
    """
    steps = [c for c in children if c.status != "shadow"]
    asked = await progress.asked_names(session, steps)
    running = [c.id for c in steps if progress.step_kind(c, asked.get(c.id, [])) == "running"]
    latest: dict[int, WorkTaskEvent] = {}
    if running:
        said = await session.scalars(
            select(WorkTaskEvent)
            .where(
                WorkTaskEvent.work_task_id.in_(running),
                WorkTaskEvent.kind == "progress",
                WorkTaskEvent.human_text != "",
            )
            .order_by(WorkTaskEvent.id)
        )
        latest = {e.work_task_id: e for e in said}
    doing: list[dict[str, Any]] = []
    finished: list[dict[str, Any]] = []
    steps_done = steps_total = 0
    waiting_human = False
    for child in steps:
        kind = progress.step_kind(child, asked.get(child.id, []))
        question = progress.is_question(child)
        said_last = latest.get(child.id)
        item = {
            "code": child.code,
            "title": child.title.removeprefix("问：") if question else child.title,
            "status": child.status,
            "kind": kind,
            "question": question,
            "assignee": actor_out(child.assignee_bot_id, child.assignee_user_id, names),
            "since": child.claimed_at or child.created_at,
            "closed_at": child.closed_at,
            "note": said_last.human_text
            if said_last
            else _step_note(child, kind, asked.get(child.id, []), names),
            "note_at": said_last.created_at if said_last else None,
        }
        if question:
            # 待答的问题画在「要人定的」里；答过的进「已完成」。
            if kind in ("done", "cancelled"):
                finished.append(item)
            continue
        if kind == "cancelled":
            finished.append(item)
            continue
        steps_total += 1
        if kind == "done":
            steps_done += 1
            finished.append(item)
        else:
            waiting_human = waiting_human or kind == "waiting_human"
            doing.append(item)
    closed = row.status in ("done", "cancelled", "shadow")
    plan = [] if closed else await progress.plan_steps(session, row, {c.title for c in steps})
    steps_total += len(plan)
    waiting = row.waiting_on or {}
    if closed:
        state = row.status
    elif (
        asks
        or waiting_human
        or row.status == "review"
        or (row.status == "waiting" and waiting.get("kind") == "human")
        or (row.status == "todo" and (row.assignee_user_id or service.needs_drive(row)))
    ):
        state = "need_human"
    else:
        state = "todo" if row.status == "todo" else "running"
    spoken = [e for e in events if e.kind == "progress" and e.human_text]
    return {
        "state": state,
        "steps_done": steps_done,
        "steps_total": steps_total,
        "asks": [
            {"code": a.code, "title": a.title, **(ask_out(a, names, viewer) or {})} for a in asks
        ],
        "doing": doing,
        "plan": plan,
        "done": finished,
        "followups": await progress.followups(session, row) if row.status == "done" else [],
        "latest": (
            {
                "text": spoken[-1].human_text,
                "at": spoken[-1].created_at,
                "actor_name": names.get(spoken[-1].actor_id or "", ""),
            }
            if spoken and not closed
            else None
        ),
    }


async def detail(session: AsyncSession, row: WorkTask, user: User | None) -> dict[str, Any]:
    """任务详情页的那份数据；user 是看的人（AI 员工读时为 None：没有「我的」视角）。"""
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
    asks = await open_asks(session, row)
    bots, users = ids_of([row, *children, *asks])
    bots |= {uuid.UUID(e.actor_id) for e in events if e.actor_type == "bot" and e.actor_id}
    bots |= {r.bot_id for r in runs if r.bot_id}
    users |= {uuid.UUID(e.actor_id) for e in events if e.actor_type == "human" and e.actor_id}
    users |= watcher_ids
    names = await names_of(session, bots, users)
    out = task_out(row, names, user)
    texts = [out["now"], row.summary, *(e.human_text for e in events)]
    children_out = [task_out(c, names, user) for c in children]
    texts += [c["now"] for c in children_out]
    linked = await linked_of(session, row, texts)
    if parent:
        linked[parent.code] = {"title": parent.title, "status": parent.status}
    for c in children:
        linked[c.code] = {"title": c.title, "status": c.status}
    return {
        **out,
        "linked": linked,
        "watching": user is not None and user.id in watcher_ids,
        "watchers": [{"id": str(i), "name": names.get(str(i), "")} for i in watcher_ids],
        "parent": {"code": parent.code, "title": parent.title} if parent else None,
        "children": children_out,
        "overview": await overview(session, row, children, asks, events, names, user),
        "events": [
            {
                "id": e.id,
                "kind": e.kind,
                "actor_type": e.actor_type,
                "actor_name": names.get(e.actor_id or "", ""),
                "human_text": e.human_text,
                "agent_detail": e.agent_detail,
                "created_at": e.created_at,
                "milestone": milestone(e),
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
                    "bot": bot_out(r.bot_id, names),
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
