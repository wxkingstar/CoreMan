"""待跟进工具：助理在个人工具 MCP 上读写自己的待跟进清单。

登记在助理工具注册表的 `knowledge` 组：不依赖飞书授权，子任务也能用。日期一律是所有者时区的
日期；每个结果都带 `today`，助理据此算「明天」「下周一」。分派时只认这一轮核验过的助理记录
（`scope.bot`），读写不到别人的条目。

登记在注册表自己的文件末尾（`coreman/core/assistants/tools.py`），这里不导入它。
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import followups
from coreman.core.assistants import settings as assistant_settings
from coreman.core.assistants.approvals import service as approvals_service
from coreman.core.assistants.records import is_assistant
from coreman.core.crypto import Cipher
from coreman.core.feishu_personal.policy import Scope
from coreman.core.feishu_personal.toolbase import (
    Arguments,
    Day,
    Identifier,
    input_schema,
    valid_days,
)
from coreman.core.timeutils import utcnow

Category = Literal["decide", "promised", "waiting", "remind"]
Label = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[Ff]?\d{1,9}$"),
    Field(description="Follow-up id such as F012"),
]
Title = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=followups.MAX_TITLE),
    Field(description="One sentence that makes sense on its own"),
]
Notes = Annotated[str, StringConstraints(max_length=followups.MAX_NOTES)]
Handler = Literal["assistant", "job", "person", "owner"]
HandlerName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=followups.MAX_HANDLER_NAME),
    Field(description="With handler=person: how the owner calls that colleague"),
]
JobRef = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=64),
    Field(description="With handler=job: the job id from assistant_spawn or assistant_job_list"),
]
HANDLER = (
    "Who is moving it forward now: assistant (you), job (a sub-task or a public employee you"
    " asked; give `job`), person (a colleague; give `handler_name`), owner (the owner himself)"
)
NEEDS_OWNER = (
    "true when only the owner in person can get it done (his decision on the spot, personal"
    " credentials, authorization, pinned pages, a write refused with owner_turn_required): the"
    " digest lists it with a button that lets him start it with you right away"
)


class Add(Arguments):
    category: Category
    title: Title
    notes: Notes = ""
    review_on: Day | None = None
    due_on: Day | None = None
    origin_message_id: Identifier | None = Field(
        default=None, description="The Feishu message that caused it, if any"
    )
    needs_owner: bool = Field(default=False, description=NEEDS_OWNER)
    handler: Handler | None = Field(default=None, description=HANDLER)
    handler_name: HandlerName | None = None
    job: JobRef | None = None

    _days = valid_days("review_on", "due_on")


class Update(Arguments):
    id: Label
    category: Category | None = None
    title: Title | None = None
    notes: Notes | None = None
    review_on: Day | None = None
    due_on: Day | None = None
    needs_owner: bool | None = Field(default=None, description=NEEDS_OWNER)
    handler: Handler | None = Field(default=None, description=HANDLER)
    handler_name: HandlerName | None = None
    job: JobRef | None = None
    approval_id: Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)] | None = (
        Field(
            default=None,
            description="An approval id (from approval_list) to show under this follow-up",
        )
    )
    clear: list[Literal["notes", "review_on", "due_on"]] = Field(
        default_factory=list, max_length=3, description="Fields to empty"
    )

    _days = valid_days("review_on", "due_on")


class Snooze(Arguments):
    id: Label
    until: Day

    _days = valid_days("until")


class Progress(Arguments):
    id: Label
    note: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=followups.MAX_PROGRESS),
        Field(description="One or two sentences: what moved forward, found out or changed"),
    ]


class Close(Arguments):
    id: Label
    outcome: Literal["done", "dropped"] = "done"
    notes: Notes | None = Field(default=None, description="Replaces the notes: how it ended")


class Listing(Arguments):
    scope: Annotated[str, StringConstraints(strip_whitespace=True, max_length=20)] = Field(
        default="today",
        description=(
            "today (due or up for review today) | open (everything not closed) | all |"
            " decide | promised | waiting | remind, or one id such as F012 for its full notes"
        ),
    )
    limit: int = Field(default=50, ge=1, le=200)


OWN: dict[str, tuple[type[Arguments], str]] = {
    "followup_add": (
        Add,
        "Record something the owner must not lose track of across days. category: decide"
        " (waiting for the owner's decision), promised (the owner promised someone), waiting"
        " (waiting on someone else), remind (remind the owner on a date). Dates are the"
        " owner's local dates. Returns its id (F012).",
    ),
    "followup_update": (
        Update,
        "Change an open follow-up: category, title, notes, dates. Only the given fields"
        " change; list a field in `clear` to empty it.",
    ),
    "followup_snooze": (
        Snooze,
        "Hide an open follow-up until a later local date; it comes back on that day.",
    ),
    "followup_close": (
        Close,
        "Close a follow-up: done, or dropped when it is no longer needed.",
    ),
    "followup_progress": (
        Progress,
        "Log progress on an open follow-up: appends a dated line to its notes and marks it as"
        " moving. Use it whenever you found something out or pushed it forward; the owner's"
        " board flags items with no progress for days.",
    ),
    "followup_list": (
        Listing,
        "List the owner's follow-ups by scope. Items carry the title and whether notes"
        " exist; pass one id as scope to read its notes.",
    ),
}

HINTS = {
    "followup_not_found": "没有这个编号；用 followup_list 看现有的。",
    "followup_closed": "这条已经关闭，不能再改；需要的话新记一条。",
    "snooze_not_future": "until 必须是 today 之后的日期。",
    "too_many_followups": "未结的条目已到上限，先关闭办完的。",
    "invalid_scope": "scope 只能是 today、open、all、四个分类名之一，或一个编号。",
    "approval_not_found": "没有这张审批单；用 approval_list 查编号。",
    "nothing_to_update": "没有给出要改的项。",
    "invalid_handler_name": "handler=person 时要给 handler_name（同事的称呼，50 字以内）。",
    "job_not_found": (
        "handler=job 时要给 job：用 assistant_job_list 查到的、你自己派出的子任务编号。"
    ),
    "notes_full": "备注里开头的说明已经太长，进展写不进去：先用 followup_update 把 notes 精简。",
    "invalid_progress": "note 写一两句话，不超过 300 字。",
    "content_locked": "所有者的数据密钥不可用，标题与备注现在写不了；不要重试，如实告诉所有者。",
}


def definitions() -> list[dict[str, Any]]:
    return [
        {"name": name, "description": text, "inputSchema": input_schema(args)}
        for name, (args, text) in OWN.items()
    ]


def _job(value: str | None) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        raise followups.FollowupError("job_not_found") from None


def _day(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


async def _list(
    session: AsyncSession, bot_id: uuid.UUID, args: Listing, today: date
) -> dict[str, Any]:
    if args.scope in followups.SCOPES:
        # 多取一条，只为知道后面还有没有。
        rows = await followups.list_(session, bot_id, args.scope, today=today, limit=args.limit + 1)
        return {
            "items": [followups.view(row, notes=False) for row in rows[: args.limit]],
            "truncated": len(rows) > args.limit,
        }
    try:
        seq = followups.parse_label(args.scope)
    except followups.FollowupError:
        raise followups.FollowupError("invalid_scope") from None
    return {"followup": followups.view(await followups.get(session, bot_id, seq))}


async def _run(
    session: AsyncSession, bot_id: uuid.UUID, name: str, args: Any, today: date
) -> dict[str, Any]:
    if name == "followup_list":
        return await _list(session, bot_id, args, today)
    if name == "followup_add":
        row = await followups.add(
            session,
            bot_id,
            category=args.category,
            title=args.title,
            notes=args.notes or None,
            review_on=_day(args.review_on),
            due_on=_day(args.due_on),
            origin_message_id=args.origin_message_id,
            needs_owner=args.needs_owner,
            handler=args.handler or "assistant",
            handler_name=args.handler_name,
            job_id=_job(args.job),
        )
        return {"followup": followups.view(row)}
    seq = followups.parse_label(args.id)
    if name == "followup_progress":
        row = await followups.progress(session, bot_id, seq, args.note, today=today)
        return {"followup": followups.view(row)}
    if name == "followup_snooze":
        row = await followups.snooze(
            session, bot_id, seq, date.fromisoformat(args.until), today=today
        )
    elif name == "followup_close":
        row = await followups.close(
            session, bot_id, seq, dropped=args.outcome == "dropped", notes=args.notes
        )
    else:
        changes: dict[str, Any] = {}
        for field in ("category", "title", "notes", "needs_owner"):
            if getattr(args, field) is not None:
                changes[field] = getattr(args, field)
        for field in ("review_on", "due_on"):
            if getattr(args, field) is not None:
                changes[field] = date.fromisoformat(getattr(args, field))
        if args.handler is not None:
            changes.update(
                handler=args.handler, handler_name=args.handler_name, job_id=_job(args.job)
            )
        for field in args.clear:
            changes[field] = None
        if not changes and args.approval_id is None:
            raise followups.FollowupError("nothing_to_update")
        # 挂单与改字段要么都生效、要么都不生效：后一步报错时前一步的 flush 也撤掉。
        async with session.begin_nested():
            if args.approval_id is not None:
                try:
                    approval_id = uuid.UUID(args.approval_id)
                except ValueError:
                    raise followups.FollowupError("approval_not_found") from None
                row = await followups.link_approval(session, bot_id, seq, approval_id)
            if changes:
                row = await followups.update(session, bot_id, seq, **changes)
    return {"followup": followups.view(row)}


async def dispatch(
    session: AsyncSession, cipher: Cipher, scope: Scope, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """执行一个待跟进工具；调用方（MCP 入口）负责提交。`cipher` 用不上：内容由数据密钥加解密。"""
    found = OWN.get(name)
    if found is None or not is_assistant(scope.bot):
        return {"error": "invalid_tool_or_arguments"}
    try:
        args: Any = found[0].model_validate(arguments)
    except ValidationError as exc:
        return {
            "error": "invalid_tool_or_arguments",
            "invalid": approvals_service._problems(exc),
        }
    bot_id = scope.bot.id
    today = assistant_settings.local_today(await assistant_settings.get(session, bot_id), utcnow())
    try:
        out = await _run(session, bot_id, name, args, today)
    except followups.FollowupError as exc:
        out = {"error": exc.code}
        if exc.code in HINTS:
            out["hint"] = HINTS[exc.code]
        if exc.code == "too_many_followups":
            out["limit"] = followups.MAX_OPEN
    return {**out, "today": today.isoformat()}
