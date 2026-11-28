"""任务平台的操作：建、指派与转交、接受、回答待选择事项、评论、交付、验收、取消、重开。

两条线（spec §3）：每个动作写一条事件，`human_text` 是给人看的一行，`agent_detail` 是给 Agent
看的全部细节。人话线上的字段有长度上限，超了直接拒绝，不替人截断。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.api_tasks.service import allowed_to_use
from coreman.core.assistants.records import is_assistant
from coreman.core.db.models import (
    WORK_TASK_PRIORITIES,
    Bot,
    User,
    WorkTask,
    WorkTaskEvent,
    WorkTaskWatcher,
)

TITLE_MAX = 30
SUMMARY_MAX = 60
LINE_MAX = 80
REASON_MAX = 200
COMMENT_MAX = 4000
BRIEF_MAX = 20000
ANSWER_MAX = 2000
MANAGER_ROLE = "platform_admin"
# 人类执行人「待我处理」里算还在手上的状态。
HELD = ("todo", "in_progress", "waiting")


class WorkTaskError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Assignee:
    kind: Literal["human", "bot"]
    id: uuid.UUID


def now() -> datetime:
    return datetime.now(UTC)


def clip(text: str, limit: int = LINE_MAX) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


_MD_LEAD = re.compile(r"^\s*(#{1,6}|>+|[-*+]|\d+[.)])\s+")
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_MD_MARK = re.compile(r"\*\*|__|~~|`+")


def plain_line(text: str) -> str:
    """给人看的一句话里不留 Markdown 记号：AI 的回复原文常带加粗、标题、列表符号和链接。"""
    lines = [_MD_LEAD.sub("", line) for line in text.splitlines() if line.strip()]
    return _MD_MARK.sub("", _MD_LINK.sub(r"\1", " ".join(lines)))


def name_of(user: User | None) -> str:
    if user is None:
        return "已删除的成员"
    return user.display_name or user.login_name or ""


def _require(value: str, field: str, limit: int, *, required: bool = True) -> str:
    value = value.strip()
    if required and not value:
        raise WorkTaskError(422, f"{field}不能为空")
    if len(value) > limit:
        raise WorkTaskError(422, f"{field}最多 {limit} 字，请写得更精炼")
    return value


def add_event(
    session: AsyncSession,
    row: WorkTask,
    *,
    kind: str,
    actor_type: str,
    actor_id: str | None,
    human_text: str = "",
    detail: dict[str, Any] | None = None,
    chat_task_id: int | None = None,
) -> WorkTaskEvent:
    row.updated_at = now()
    event = WorkTaskEvent(
        work_task_id=row.id,
        kind=kind,
        actor_type=actor_type,
        actor_id=actor_id,
        human_text=clip(human_text) if human_text else "",
        agent_detail=detail or {},
        chat_task_id=chat_task_id,
    )
    session.add(event)
    return event


def human_event(
    session: AsyncSession,
    row: WorkTask,
    user: User,
    kind: str,
    human_text: str,
    detail: dict[str, Any] | None = None,
) -> WorkTaskEvent:
    return add_event(
        session,
        row,
        kind=kind,
        actor_type="human",
        actor_id=str(user.id),
        human_text=human_text,
        detail=detail,
    )


def can_manage(row: WorkTask, user: User) -> bool:
    """发起人、代表人、执行人、验收人和平台管理员能改；其他成员只能评论和关注。"""
    return user.role == MANAGER_ROLE or user.id in {
        row.requester_user_id,
        row.principal_user_id,
        row.assignee_user_id,
        row.reviewer_user_id,
    }


def _manage(row: WorkTask, user: User) -> None:
    if row.origin_ref:
        # 镜像的账本记录（API 任务、求助）：在原处处理，这里只能看、评论、关注。
        raise WorkTaskError(409, "这是自动记下的记录，请在原处处理")
    if not can_manage(row, user):
        raise WorkTaskError(403, "只有发起人、代表人、执行人、验收人或平台管理员能这样做")


def _open(row: WorkTask) -> None:
    if row.status in ("shadow", "done", "cancelled"):
        raise WorkTaskError(409, "任务已结束，先重开再操作")


async def active_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await session.get(User, user_id)
    if user is None or user.status != "active":
        raise WorkTaskError(422, "这位成员不存在或未启用")
    return user


async def usable_bot(session: AsyncSession, bot_id: uuid.UUID, principal_id: uuid.UUID) -> Bot:
    """能接任务的 AI 员工：启用的飞书员工、不是个人助理、代表人能用它。"""
    bot = await session.get(Bot, bot_id)
    if bot is None:
        raise WorkTaskError(422, "AI 员工不存在")
    if not bot.enabled:
        raise WorkTaskError(422, f"「{bot.name}」已停用")
    if bot.platform != "feishu":
        raise WorkTaskError(422, "目前只有飞书 AI 员工能接任务")
    if is_assistant(bot):
        raise WorkTaskError(422, "个人助理不接任务")
    if not await allowed_to_use(session, bot, principal_id):
        raise WorkTaskError(403, f"代表人不在「{bot.name}」的可用名单里")
    return bot


async def assignee_name(session: AsyncSession, row: WorkTask) -> str:
    if row.assignee_bot_id:
        bot = await session.get(Bot, row.assignee_bot_id)
        return bot.name if bot else "已删除的员工"
    if row.assignee_user_id:
        return name_of(await session.get(User, row.assignee_user_id))
    return "未指派"


# ---- 待选择事项 ----------------------------------------------------------------------------

# 每个待选择事项最后都有「其他」：人可以写自己的话（spec §3.4）。
OTHER = "other"
ACCEPT_OPTIONS = [{"key": "accept", "label": "接受"}, {"key": "decline", "label": "拒绝"}]
REVIEW_OPTIONS = [{"key": "pass", "label": "通过"}, {"key": "reject", "label": "退回"}]
# 这些选项要写一句理由。
NEEDS_TEXT = {"decline", "reject", OTHER}


def make_ask(
    *,
    kind: str,
    to_user_id: uuid.UUID,
    question: str,
    options: list[dict[str, str]],
    why: str = "",
) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex,
        "kind": kind,
        "to_user_id": str(to_user_id),
        "question": question,
        "why": why,
        "options": options,
        "other": True,
        "created_at": now().isoformat(),
    }


def ask_accept(row: WorkTask, to_user_id: uuid.UUID, by_name: str) -> dict[str, Any]:
    return make_ask(
        kind="accept",
        to_user_id=to_user_id,
        question=clip(f"{by_name}把「{row.title}」交给你，接不接？", SUMMARY_MAX),
        options=ACCEPT_OPTIONS,
    )


def ask_review(row: WorkTask, to_user_id: uuid.UUID, result: str) -> dict[str, Any]:
    return make_ask(
        kind="review",
        to_user_id=to_user_id,
        question=clip(f"「{row.title}」已交付：{result}。通过吗？", SUMMARY_MAX),
        options=REVIEW_OPTIONS,
    )


def can_answer(row: WorkTask, user: User) -> bool:
    """被问的人自己，或代表人、平台管理员代答。"""
    return bool(row.ask) and (
        user.id == ask_to(row) or user.id == row.principal_user_id or user.role == MANAGER_ROLE
    )


def ask_to(row: WorkTask) -> uuid.UUID | None:
    raw = (row.ask or {}).get("to_user_id")
    return uuid.UUID(raw) if raw else None


def to_review(row: WorkTask, result: str) -> None:
    """交付后进入待验收：问验收人（没指定时问创建人，AI 员工建的问代表人）。"""
    reviewer = row.reviewer_user_id or row.requester_user_id or row.principal_user_id
    if reviewer is None:
        finish(row, result)
        return
    row.reviewer_user_id = reviewer
    row.status, row.waiting_on, row.closed_at = "review", None, None
    row.summary = clip(result, SUMMARY_MAX)
    row.ask = ask_review(row, reviewer, row.summary)


def finish(row: WorkTask, result: str) -> None:
    row.status, row.ask, row.waiting_on, row.closed_at = "done", None, None, now()
    if result:
        row.summary = clip(result, SUMMARY_MAX)


# ---- 建与指派 ------------------------------------------------------------------------------


async def create(
    session: AsyncSession,
    user: User,
    *,
    title: str,
    summary: str = "",
    brief: str = "",
    assignee: Assignee | None = None,
    reviewer_user_id: uuid.UUID | None = None,
    review_required: bool | None = None,
    priority: str = "normal",
    due_at: datetime | None = None,
) -> WorkTask:
    """网页上建任务：建的人就是发起人和代表人。"""
    title = _require(title, "标题", TITLE_MAX)
    summary = _require(summary, "一句话说明", SUMMARY_MAX, required=False)
    brief = _require(brief, "背景与要求", BRIEF_MAX, required=False)
    if priority not in WORK_TASK_PRIORITIES:
        raise WorkTaskError(422, "优先级只能是 normal、high、urgent")
    if reviewer_user_id is not None:
        await active_user(session, reviewer_user_id)
    row = WorkTask(
        title=title,
        summary=summary,
        brief=brief,
        status="todo",
        source="web",
        requester_user_id=user.id,
        requester_name=name_of(user),
        principal_user_id=user.id,
        reviewer_user_id=reviewer_user_id,
        review_required=bool(review_required) or reviewer_user_id is not None,
        priority=priority,
        due_at=due_at,
        start_mode="manual",
    )
    session.add(row)
    await session.flush()
    row.root_id = row.id
    human_event(session, row, user, "created", f"{name_of(user)}建了任务", {"brief": brief})
    if assignee is not None:
        await assign(session, row, user, assignee, note="", review_required=review_required)
    await session.flush()
    return row


async def assign(
    session: AsyncSession,
    row: WorkTask,
    user: User,
    target: Assignee,
    *,
    note: str = "",
    review_required: bool | None = None,
) -> WorkTask:
    """指派或转交。交给 AI 员工时等人叫它开工（叫开工的人成为代表人，见 `start_refusal`）；
    先记转交人为代表人。要验收时默认由创建人验收：创建人是提需求的人。"""
    _manage(row, user)
    _open(row)
    note = _require(note, "交接说明", BRIEF_MAX, required=False)
    before = await assignee_name(session, row)
    if target.kind == "bot":
        bot = await usable_bot(session, target.id, user.id)
        if row.assignee_bot_id == bot.id:
            raise WorkTaskError(409, f"任务已经在「{bot.name}」手上")
        row.assignee_bot_id, row.assignee_user_id = bot.id, None
        row.principal_user_id = user.id
        row.start_mode, row.claimed_at = "manual", None
        row.status, row.ask, row.waiting_on, row.closed_at = "todo", None, None, None
        row.review_required = True if review_required is None else review_required
        if row.review_required and row.reviewer_user_id is None:
            row.reviewer_user_id = row.requester_user_id or user.id
        after = bot.name
    else:
        person = await active_user(session, target.id)
        if row.assignee_user_id == person.id:
            raise WorkTaskError(409, f"任务已经在{name_of(person)}手上")
        row.assignee_user_id, row.assignee_bot_id = person.id, None
        row.claimed_at, row.waiting_on, row.closed_at = None, None, None
        if review_required is not None:
            row.review_required = review_required
        if person.id == user.id:
            # 指派给自己就是自己接了。
            row.status, row.ask = "in_progress", None
        else:
            row.status = "todo"
            row.ask = ask_accept(row, person.id, name_of(user))
        after = name_of(person)
    # 两种写法都写明是谁做的：「张三把这件事交给 X」「张三：甲 → 乙」。
    text = (
        f"{name_of(user)}：{before} → {after}"
        if before != "未指派"
        else f"{name_of(user)}交给{after}"
    )
    if note:
        text += f"：{note}"
    human_event(
        session,
        row,
        user,
        "handoff",
        text,
        {"from": before, "to": after, "to_kind": target.kind, "note": note},
    )
    return row


# ---- 交付后还要人处理的事 --------------------------------------------------------------------


async def mark_followup(
    session: AsyncSession,
    row: WorkTask,
    user: User,
    index: int,
    *,
    done: bool,
    as_task: bool = False,
) -> WorkTask | None:
    """把一件「还要人处理的事」勾成办了（或改回没办）；`as_task` 时把它建成一件新任务再勾掉。

    谁都能勾：这些事常常写明要某位同事去办，办的人不一定是任务的参与者。返回新建的任务。
    """
    from coreman.core.work_tasks import progress

    items = await progress.followup_items(session, row)
    if not 0 <= index < len(items):
        raise WorkTaskError(404, "没有这件要处理的事，请刷新")
    item = items[index]
    if bool(item["done"]) == done and not as_task:
        return None
    made: WorkTask | None = None
    if as_task:
        if item["done"]:
            raise WorkTaskError(409, "这件事已经办了")
        text = str(item["text"])
        made = await create(
            session, user, title=clip(text, TITLE_MAX), summary=clip(text, SUMMARY_MAX)
        )
        made.related_codes = [row.code]
        row.related_codes = [*(row.related_codes or []), made.code]
        done = True
    line = (
        f"{name_of(user)}把「{clip(str(item['text']), 40)}」建成了 {made.code}"
        if made is not None
        else f"{name_of(user)}{'办了' if done else '改回没办'}：{clip(str(item['text']), 50)}"
    )
    human_event(
        session,
        row,
        user,
        "followup",
        line,
        {
            "index": index,
            "done": done,
            "by_name": name_of(user),
            "task": made.code if made is not None else "",
        },
    )
    return made


# ---- 回答待选择事项 ------------------------------------------------------------------------


@dataclass
class Answered:
    """回答后要做的事：交给 AI 员工重跑的说明（验收退回）。"""

    rerun_note: str | None = None


async def answer(
    session: AsyncSession,
    row: WorkTask,
    user: User,
    *,
    ask_id: str,
    option: str,
    text: str = "",
    channel: Literal["web", "feishu"] = "web",
) -> Answered:
    """网页和飞书卡片共用：任一处先回答就生效，另一处再点会被拒绝（问题已经变了）。"""
    ask = row.ask
    if not ask or ask.get("id") != ask_id:
        raise WorkTaskError(409, "这个问题已经回答过了或已经变了，请刷新")
    on_behalf = user.id != ask_to(row)
    if not can_answer(row, user):
        raise WorkTaskError(403, "只有被问的人能回答（代表人和平台管理员可以代答）")
    keys = {o["key"] for o in ask.get("options", [])} | ({OTHER} if ask.get("other") else set())
    if option not in keys:
        raise WorkTaskError(422, "没有这个选项")
    text = _require(text, "补充说明", ANSWER_MAX, required=option in NEEDS_TEXT)
    label = next((o["label"] for o in ask["options"] if o["key"] == option), "其他")
    detail = {
        "question": ask["question"],
        "options": ask["options"],
        "chosen": option,
        "text": text,
        "by": str(user.id),
        "on_behalf": on_behalf,
        "channel": channel,
    }
    who = name_of(user) + ("（代答）" if on_behalf else "")
    line = f"{who}：{label}" + (f"，{text}" if text else "")
    result = Answered()
    kind = ask["kind"]
    if kind == "question":
        # AI 员工问的问题：选项和「其他」都是答复，这件小事就此完成，答复原文交回问的员工。
        row.ask, row.status, row.closed_at, row.waiting_on = None, "done", now(), None
        row.summary = clip(f"{label}" + (f"：{text}" if text else ""), SUMMARY_MAX)
        human_event(session, row, user, "answer", line, detail)
        return result
    if option == OTHER:
        # 平台的选择题里选「其他」：当作写一句话回给对方，问题还留着，之后照样可以选。
        human_event(session, row, user, "answer", line, detail)
        return result
    if kind == "accept":
        if option == "accept":
            row.status, row.ask = "in_progress", None
        else:
            row.assignee_user_id, row.ask, row.status = None, None, "waiting"
            back = row.principal_user_id or row.requester_user_id
            row.waiting_on = {
                "kind": "human",
                "user_id": str(back) if back else None,
                "human_text": clip(f"{name_of(user)}拒绝了：{text}，等重新指派", SUMMARY_MAX),
            }
    elif kind == "review":
        if option == "pass":
            finish(row, "")
        else:
            row.ask, row.closed_at, row.waiting_on = None, None, None
            row.status = "in_progress" if row.assignee_user_id else "todo"
            if row.assignee_bot_id:
                result.rerun_note = text
    human_event(session, row, user, f"{kind}_{option}", line, detail)
    return result


# ---- 其余动作 ------------------------------------------------------------------------------


async def comment(session: AsyncSession, row: WorkTask, user: User, text: str) -> None:
    text = _require(text, "评论", COMMENT_MAX)
    human_event(session, row, user, "comment", f"{name_of(user)}：{text}", {"text": text})


async def complete(session: AsyncSession, row: WorkTask, user: User, result: str) -> None:
    """人类执行人交付：要验收的进待验收，不要的直接完成。"""
    if row.origin_ref:
        raise WorkTaskError(409, "这是自动记下的记录，请在原处处理")
    if row.assignee_user_id != user.id:
        raise WorkTaskError(403, "只有执行人能交付")
    if row.status not in HELD:
        raise WorkTaskError(409, "任务不在进行中")
    result = _require(result, "结果", SUMMARY_MAX)
    if row.review_required:
        to_review(row, result)
    else:
        finish(row, result)
    human_event(
        session, row, user, "delivered", f"{name_of(user)}交付：{result}", {"result": result}
    )


async def cancel(session: AsyncSession, row: WorkTask, user: User, reason: str) -> None:
    _manage(row, user)
    _open(row)
    reason = _require(reason, "取消原因", REASON_MAX)
    row.status, row.ask, row.waiting_on, row.closed_at = "cancelled", None, None, now()
    human_event(
        session, row, user, "cancelled", f"{name_of(user)}取消：{reason}", {"reason": reason}
    )


async def reopen(session: AsyncSession, row: WorkTask, user: User, reason: str = "") -> None:
    _manage(row, user)
    if row.status not in ("done", "cancelled"):
        raise WorkTaskError(409, "只有已完成或已取消的任务能重开")
    reason = _require(reason, "重开原因", REASON_MAX, required=False)
    row.closed_at, row.ask, row.waiting_on = None, None, None
    if row.assignee_bot_id:
        row.status, row.claimed_at = "todo", None
    elif row.assignee_user_id:
        row.status = "in_progress"
    else:
        row.status = "todo"
    text = f"{name_of(user)}重开" + (f"：{reason}" if reason else "")
    human_event(session, row, user, "reopened", text, {"reason": reason})


async def update(
    session: AsyncSession,
    row: WorkTask,
    user: User,
    *,
    fields: dict[str, Any],
) -> None:
    """改标题、一句话、背景、优先级、截止、验收；`fields` 只含要改的键。"""
    _manage(row, user)
    changes: list[str] = []
    if "title" in fields:
        row.title = _require(str(fields["title"] or ""), "标题", TITLE_MAX)
        changes.append("标题")
    if "summary" in fields:
        row.summary = _require(
            str(fields["summary"] or ""), "一句话说明", SUMMARY_MAX, required=False
        )
        changes.append("说明")
    if "brief" in fields:
        row.brief = _require(str(fields["brief"] or ""), "背景与要求", BRIEF_MAX, required=False)
        changes.append("背景与要求")
    if "priority" in fields:
        if fields["priority"] not in WORK_TASK_PRIORITIES:
            raise WorkTaskError(422, "优先级只能是 normal、high、urgent")
        row.priority = fields["priority"]
        changes.append("优先级")
    if "due_at" in fields:
        row.due_at = fields["due_at"]
        row.due_reminded_at = row.overdue_notified_at = None
        changes.append("截止时间")
    if "reviewer_user_id" in fields:
        reviewer = fields["reviewer_user_id"]
        if reviewer is not None:
            await active_user(session, reviewer)
        row.reviewer_user_id = reviewer
        if row.status == "review" and (row.ask or {}).get("kind") == "review":
            # 验收中换人：问题改问新验收人（没指定时问代表人）。
            to = reviewer or row.principal_user_id
            if to is not None:
                row.ask = ask_review(row, to, row.summary)
        changes.append("验收人")
    if "review_required" in fields:
        row.review_required = bool(fields["review_required"])
        changes.append("是否验收")
    if changes:
        human_event(
            session,
            row,
            user,
            "updated",
            f"{name_of(user)}改了{'、'.join(changes)}",
            {"fields": sorted(fields)},
        )


async def watch(session: AsyncSession, row: WorkTask, user: User, on: bool) -> None:
    if on:
        await session.execute(
            pg_insert(WorkTaskWatcher)
            .values(work_task_id=row.id, user_id=user.id)
            .on_conflict_do_nothing()
        )
    else:
        await session.execute(
            delete(WorkTaskWatcher).where(
                WorkTaskWatcher.work_task_id == row.id, WorkTaskWatcher.user_id == user.id
            )
        )


# ---- 查询 ----------------------------------------------------------------------------------


def needs_me(user_id: uuid.UUID) -> Any:
    """「待我处理」：我手上的、问我的、在等我的，以及我派给 AI 员工还没被领取的。"""
    me = str(user_id)
    return or_(
        and_(WorkTask.assignee_user_id == user_id, WorkTask.status.in_(HELD)),
        WorkTask.ask["to_user_id"].astext == me,
        and_(WorkTask.status == "waiting", WorkTask.waiting_on["user_id"].astext == me),
        and_(
            WorkTask.principal_user_id == user_id,
            WorkTask.assignee_bot_id.is_not(None),
            WorkTask.start_mode == "manual",
            WorkTask.claimed_at.is_(None),
            WorkTask.status == "todo",
        ),
        and_(
            WorkTask.requester_user_id == user_id,
            WorkTask.assignee_bot_id.is_(None),
            WorkTask.assignee_user_id.is_(None),
            WorkTask.status == "todo",
        ),
    )


def created_by(user_id: uuid.UUID) -> Any:
    return or_(WorkTask.requester_user_id == user_id, WorkTask.principal_user_id == user_id)


def watched_by(user_id: uuid.UUID) -> Any:
    return WorkTask.id.in_(
        select(WorkTaskWatcher.work_task_id).where(WorkTaskWatcher.user_id == user_id)
    )


def not_started(row: WorkTask) -> bool:
    """还没开工过的任务（等人叫它开工）。"""
    return row.status == "todo" and row.claimed_at is None


def start_refusal(row: WorkTask, user: User, bot: Bot) -> str | None:
    """`user` 叫 AI 员工 `bot` 开工或接着做 `row`，不行时返回原因。

    准则（docs/superpowers/specs/2026-11-28-dispatcher-inserted-tasks.md）：谁叫开工就以谁的
    身份做，他成为代表人。创建人叫哪位 AI 员工都行，被叫的那位接手；别人只能叫任务指派给的那位。
    """
    if row.assignee_user_id is not None:
        return f"{row.code} 派给了人，不是 AI 员工"
    if row.assignee_bot_id == bot.id or user.id == row.requester_user_id:
        return None
    return (
        f"{row.code} 没有指派给「{bot.name}」：请直接找被指派的 AI 员工，"
        f"或者请创建人{row.requester_name or ''}来叫开工"
    )


def drive(row: WorkTask, user_id: uuid.UUID) -> bool:
    """人工触发：以触发人的身份做，他成为代表人。返回代表人有没有换。"""
    changed = row.principal_user_id != user_id
    row.principal_user_id = user_id
    return changed


def restartable(row: WorkTask) -> bool:
    """人派给 AI 员工的任务这一轮停下来等代表人（卡住、等确认、没交出去）：叫它再做一次。"""
    return (
        row.assignee_bot_id is not None
        and row.start_mode == "manual"
        and row.status == "waiting"
        and (row.waiting_on or {}).get("kind") == "human"
    )


def needs_drive(row: WorkTask) -> bool:
    """要人去叫 AI 员工开工：还没被领取的，或停下来等代表人的。"""
    unclaimed = (
        row.assignee_bot_id is not None
        and row.start_mode == "manual"
        and row.claimed_at is None
        and row.status == "todo"
    )
    return unclaimed or restartable(row)


def drive_hint(row: WorkTask, bot_name: str) -> str:
    """人派给 AI 员工、还没被领取：告诉人怎么叫它开工（spec §5.1）。"""
    return (
        f"{bot_name}还没开工。点「开工」让它现在做，"
        f"或在飞书里对它说「做 {row.code}」，也可以给它配定时领取。"
    )


def current_line(row: WorkTask, names: dict[str, str]) -> str:
    """详情页顶部「现在」那一行：这件事现在在谁手上、等什么。`names` 是 id → 名字。"""
    who = names.get(str(row.assignee_bot_id or row.assignee_user_id or ""), "")
    if row.status == "shadow":
        return "调度试运行的记录，没有真的交出去"
    if row.status == "done":
        return f"已完成：{row.summary}" if row.summary else "已完成"
    if row.status == "cancelled":
        return "已取消"
    if row.ask:
        to = names.get(row.ask.get("to_user_id") or "", "")
        return f"等{to}回答：{row.ask.get('question', '')}"
    if row.status == "review":
        return f"等{names.get(str(row.reviewer_user_id), '')}验收"
    if row.status == "waiting":
        text = str((row.waiting_on or {}).get("human_text") or "等待中")
        if restartable(row):
            text += f"。处理好后点「接着做」，或在飞书里对{who}说「做 {row.code}」让它接着做"
        return text
    if row.status == "todo":
        if row.assignee_bot_id and row.start_mode == "manual" and row.claimed_at is None:
            return drive_hint(row, who)
        return f"待{who}开始" if who else "待指派"
    return f"{who}处理中"
