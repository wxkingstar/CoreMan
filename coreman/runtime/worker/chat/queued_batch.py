"""上一轮忙的时候排队的消息：合成一轮处理，等太久的先问一句还要不要做。

只合并同一个人连续发的纯文字：这一轮用发起人的身份和凭证跑，别人的话不能借他的权限执行；
图片、引用、命令要走各自的入口，遇到就停，留给下一轮，顺序不乱。
被合并的任务按 cancelled + `merged` 结掉，内容记在这一轮任务的载荷里，重排回来不会丢。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import sealed
from coreman.core.bus import outbox, tasks
from coreman.core.chat import busy_choice
from coreman.core.db.models import InboundEvent, Task
from coreman.core.feishu_docs import resume
from coreman.runtime.worker.chat.inbound import joined_text, strip_mention
from coreman.runtime.worker.chat.models import Intake
from coreman.runtime.worker.context import TaskContext

MERGED = "merged"
_NOT_PLAIN = (
    "origin",
    "collaboration_id",
    "collaboration_phase",
    "human_collaboration_id",
    "work_task_id",
    "api_task_id",
    # 授权、确认后的续接：标记要留到这一轮，不并入别的任务，也不收别的消息。
    resume.PAYLOAD_KEY,
    resume.CONFIRM_RESULT_KEY,
)
_NOTE = (
    "（以上几条是你处理上一轮时，同一个人陆续发来的，按发送时间排列。"
    "以最新的为准；重复的问题只答一次；已被后面的话改掉或已经过时的，不用单独回应。）"
)


@dataclass(frozen=True)
class Entry:
    text: str
    at: datetime


def is_plain(task: Task) -> bool:
    return task.kind == "chat" and not any(task.payload.get(key) for key in _NOT_PLAIN)


def _mergeable_text(task: Task, bot_name: str) -> str | None:
    message = task.payload.get("message") or {}
    parts = [p for p in (message.get("parts") or []) if isinstance(p, dict)]
    if not parts or any(p.get("type") != "text" for p in parts):
        return None
    if (message.get("reply_context") or {}).get("parent_id"):
        return None
    text = strip_mention(joined_text(parts), bot_name).strip()
    return text if text and not text.startswith("/") else None


async def gather(
    session: AsyncSession, ctx: TaskContext, intake: Intake
) -> tuple[Task, list[Entry]]:
    """把紧跟在这一轮后面、能合并的排队消息收进来，返回本轮任务和全部消息（含它自己）。"""
    head = await session.get(Task, ctx.task.id, with_for_update=True, populate_existing=True)
    assert head is not None
    merged = list(head.payload.get(MERGED) or [])
    busy_wait = bool(head.payload.get("busy_wait"))
    sender = head.payload.get("platform_user_id")
    if sender and intake.session_key == head.session_key:
        rows = await session.execute(
            select(Task, InboundEvent.received_at)
            .join(InboundEvent, Task.inbound_event_id == InboundEvent.id)
            .where(
                Task.bot_id == head.bot_id,
                Task.session_key == head.session_key,
                Task.kind == "chat",
                Task.status == "queued",
                Task.cancel_requested_at.is_(None),
                Task.id > head.id,
            )
            .order_by(Task.id)
            .with_for_update(of=Task)
        )
        for task, received_at in rows.all():
            await sealed.open_row(session, task)
            text = _mergeable_text(task, intake.bot.name)
            if (
                text is None
                or not is_plain(task)
                or task.payload.get("platform_user_id") != sender
                or task.payload.get("stale_hold")
            ):
                break
            merged.append({"text": text, "at": received_at.isoformat()})
            busy_wait = busy_wait or bool(task.payload.get("busy_wait"))
            await tasks.finish(
                session,
                task.id,
                status="cancelled",
                error_code=MERGED,
                result={"merged_into": head.id},
                bot_id=task.bot_id,
            )
    if merged != list(head.payload.get(MERGED) or []) or busy_wait != bool(
        head.payload.get("busy_wait")
    ):
        head.payload = {**head.payload, MERGED: merged, "busy_wait": busy_wait}
    entries = [Entry(intake.text, intake.inbound.received_at)]
    entries += [Entry(str(m["text"]), datetime.fromisoformat(str(m["at"]))) for m in merged]
    return head, entries


def _ago(at: datetime, now: datetime) -> str:
    minutes = int((now - at).total_seconds() // 60)
    return f"{minutes} 分钟前" if minutes > 0 else "刚刚"


def apply(
    intake: Intake, parts: list[dict[str, Any]], entries: list[Entry]
) -> tuple[Intake, list[dict[str, Any]]]:
    """多于一条时改写成一段带发送时间的文字；头一条的图片等非文字内容照留。"""
    if len(entries) < 2:
        return intake, parts
    now = datetime.now(UTC)
    lines = "\n".join(f"（{_ago(e.at, now)}）{e.text}" for e in entries)
    text = f"{lines}\n\n{_NOTE}"
    return replace(intake, text=text), [
        *[p for p in parts if p.get("type") != "text"],
        {"type": "text", "text": text},
    ]


async def hold_if_stale(
    session: AsyncSession, ctx: TaskContext, intake: Intake, head: Task, entries: list[Entry]
) -> bool:
    """最早的一条等得太久：不开工，发卡片问还要不要做。返回是否已经放回去等确认。"""
    p = head.payload
    if (
        not p.get("serialize_session")
        or p.get("stale_confirmed")
        or p.get("busy_wait")
        or p.get("stopped_previous")
    ):
        return False
    limit = int(await ctx.settings_store.get("busy_confirm_minutes", default=10))
    waited = (datetime.now(UTC) - entries[0].at).total_seconds()
    if limit <= 0 or waited < limit * 60:
        return False
    head.payload = {**p, "stale_hold": True}
    if not await tasks.defer(session, head.id, seconds=busy_choice.HOLD_SECONDS):
        return False
    target: dict[str, Any] = {"chat_id": intake.chat_id}
    payload: dict[str, Any] = {
        "card": busy_choice.stale_card(
            intake.bot.platform,
            head.id,
            lines=[e.text for e in entries],
            minutes=int(waited // 60),
            locale=ctx.locale,
        )
    }
    message_id = intake.inbound.reply_context.get("message_id")
    if intake.bot.platform == "feishu" and message_id:
        target["message_id"] = message_id
        payload["_reply"] = True
    await outbox.add(
        session,
        bot_id=intake.bot.id,
        platform=intake.bot.platform,
        kind="send",
        dedupe_key=f"task:{head.id}:stale_hold:{head.attempts}",
        target=target,
        payload=payload,
    )
    ctx.log.info("stale_batch_held", messages=len(entries), waited_seconds=int(waited))
    return True
