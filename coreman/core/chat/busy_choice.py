"""上一条还在处理时又来了新消息：新消息先排队，卡片让发消息的人选「等它做完」「停下上一条」还是「放弃这条」。

不点也行：新消息本来就排在后面，上一条做完自动接着处理；只有点「停下」才会动正在跑的任务。
「看看进度」读上一条的实时状态换成一张进度卡，同时放弃这条：问进度的消息看完就没用了，
卡上不再留按钮。
卡片的 task_id 是 `busy@<新消息的任务 id>`，飞书与企微共用。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import tasks
from coreman.core.db.models import Task, TaskStream
from coreman.core.i18n.messages import msg

CARD_PREFIX = "busy@"
STOP = "busy_stop"
WAIT = "busy_wait"
PROGRESS = "busy_progress"
RESUME = "busy_resume"
DROP = "busy_drop"
CANCEL = "busy_cancel"
# 等确认的消息最多留这么久，没人回就作废。
HOLD_SECONDS = 2 * 60 * 60
# 这么久没有新动作才提示，工具跑一两分钟不吐字是常态。
QUIET_NOTICE_SECONDS = 120
_TASK_ID = re.compile(r"busy@([1-9][0-9]{0,18})")
_TOOL_LINE = re.compile(r"^🔧 \*\*(.+)\*\*$", re.MULTILINE)
# 进度卡上最多列这么多步、这么多行正文。
_RECENT_TOOLS = 4
_RECENT_LINES = 3


@dataclass(frozen=True)
class Progress:
    """排在前面那条此刻的状态。"""

    status: str
    seconds: float
    quiet_seconds: float
    thinking_md: str
    reply_text: str
    mark: str


def card_task_id(task_id: int) -> str:
    return f"{CARD_PREFIX}{task_id}"


def parse_card_task_id(value: str) -> int | None:
    match = _TASK_ID.fullmatch(value)
    return int(match.group(1)) if match else None


def offer_card(
    platform: str, task_id: int, *, progress: str = "", locale: str = "zh"
) -> dict[str, Any]:
    """`progress` 是发卡时上一条已经跑了一阵的进度说明，放在问句前面。"""
    tid = card_task_id(task_id)
    if platform == "wecom":
        sub = msg("busy_default", locale)
        return {
            "card_type": "button_interaction",
            "source": {"desc": msg("card_source_ai", locale)},
            "main_title": {
                "title": msg("busy_title", locale),
                "desc": msg("busy_ask", locale),
            },
            "sub_title_text": f"{progress}\n{sub}" if progress else sub,
            "button_list": [
                {"text": msg("busy_opt_wait", locale), "style": 1, "key": WAIT},
                {"text": msg("busy_opt_stop", locale), "style": 2, "key": STOP},
                {"text": msg("busy_opt_progress", locale), "style": 2, "key": PROGRESS},
                {"text": msg("busy_opt_cancel", locale), "style": 2, "key": CANCEL},
            ],
            "task_id": tid,
        }

    def button(key: str, label: str, kind: str) -> dict[str, Any]:
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": msg(label, locale)},
            "type": kind,
            "width": "fill",
            "behaviors": [{"type": "callback", "value": {"task_id": tid, "event_key": key}}],
        }

    return {
        "schema": "2.0",
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": msg("busy_title", locale)},
        },
        "body": {
            "elements": [
                # 进度单独成块，和下面的问句分开。
                *([{"tag": "markdown", "content": progress}, {"tag": "hr"}] if progress else []),
                {
                    "tag": "markdown",
                    "content": msg("busy_ask", locale)
                    + "\n<font color='grey'>"
                    + msg("busy_default", locale)
                    + "</font>",
                },
                # 竖着排、撑满宽度：手机上三个并排会把文字截成「停下，先做…」。
                button(WAIT, "busy_opt_wait", "primary"),
                button(STOP, "busy_opt_stop", "default"),
                button(PROGRESS, "busy_opt_progress", "default"),
                button(CANCEL, "busy_opt_cancel", "default"),
            ]
        },
        "task_id": tid,
    }


def stale_card(
    platform: str, task_id: int, *, lines: list[str], minutes: int, locale: str = "zh"
) -> dict[str, Any]:
    """排队的消息等得太久：列出来问还要不要做。不点不做，直接发新消息算继续。"""
    tid = card_task_id(task_id)
    title = msg("busy_stale_title", locale, m=minutes)
    shown = [f"· {_clip(line, 60)}" for line in lines[-5:]]
    if len(lines) > 5:
        shown.insert(0, msg("busy_stale_more", locale, n=len(lines) - 5))
    listing = "\n".join(shown)
    hint = msg("busy_stale_hint", locale, h=HOLD_SECONDS // 3600)
    if platform == "wecom":
        return {
            "card_type": "button_interaction",
            "source": {"desc": msg("card_source_ai", locale)},
            "main_title": {"title": title},
            "sub_title_text": f"{listing}\n{hint}",
            "button_list": [
                {"text": msg("busy_opt_resume", locale), "style": 1, "key": RESUME},
                {"text": msg("busy_opt_drop", locale), "style": 2, "key": DROP},
            ],
            "task_id": tid,
        }

    def button(key: str, label: str, kind: str) -> dict[str, Any]:
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": msg(label, locale)},
            "type": kind,
            "width": "fill",
            "behaviors": [{"type": "callback", "value": {"task_id": tid, "event_key": key}}],
        }

    return {
        "schema": "2.0",
        "header": {"template": "orange", "title": {"tag": "plain_text", "content": title}},
        "body": {
            "elements": [
                {"tag": "markdown", "content": listing},
                {"tag": "markdown", "content": f"<font color='grey'>{hint}</font>"},
                button(RESUME, "busy_opt_resume", "primary"),
                button(DROP, "busy_opt_drop", "default"),
            ]
        },
        "task_id": tid,
    }


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def result_card(
    platform: str,
    task_id: str,
    *,
    title: str,
    desc: str,
    summary: str = "",
    icon_url: str = "",
    locale: str = "zh",
) -> dict[str, Any]:
    """点过之后把卡片换成这张：按钮没了，只留结果。

    `summary` 是飞书会话列表里的预览：这张卡在上一条的回复之后，会把那边的「生成中」盖掉，
    选了「等它做完」就得在预览里说清上一条还在跑，不然看着像挂了。
    """
    if platform == "wecom":
        from coreman.core.wecom.cards import notice_card

        return notice_card(
            task_id,
            title=title,
            desc=desc,
            icon_url=icon_url,
            source_desc=msg("card_source_ai", locale),
            locale=locale,
        )
    card: dict[str, Any] = {
        "schema": "2.0",
        "header": {"template": "grey", "title": {"tag": "plain_text", "content": title}},
        "body": {"elements": [{"tag": "markdown", "content": desc}]},
        "task_id": task_id,
    }
    if summary:
        card["config"] = {"summary": {"content": summary}}
    return card


def _elapsed(seconds: float, locale: str) -> str:
    total = max(int(seconds), 0)
    minutes, secs = divmod(total, 60)
    if minutes:
        return msg("busy_elapsed_min", locale, m=minutes, s=secs)
    return msg("busy_elapsed_sec", locale, s=secs)


def _recent_tools(thinking_md: str) -> str:
    """最近几步用的工具，连着重复的合成一项：`Bash ×3 → Read → Edit`。"""
    runs: list[list[Any]] = []
    for name in _TOOL_LINE.findall(thinking_md):
        if runs and runs[-1][0] == name:
            runs[-1][1] += 1
        else:
            runs.append([name, 1])
    return " → ".join(
        f"{name} ×{count}" if count > 1 else str(name) for name, count in runs[-_RECENT_TOOLS:]
    )


def _recent_lines(reply_text: str) -> list[str]:
    lines = [line.strip().lstrip("#>*-` ").strip() for line in reply_text.splitlines()]
    return [_clip(line, 120) for line in lines if line][-_RECENT_LINES:]


def progress_card(
    platform: str, task_id: int, view: Progress, *, icon_url: str = "", locale: str = "zh"
) -> dict[str, Any]:
    """点过「看看进度」后换上的卡：用时、步数、最近几步、最近写到的几行。

    这条消息同时被放弃，所以没有按钮，末尾说明要处理请重新发。
    """
    tid = card_task_id(task_id)
    title = msg("busy_progress_title", locale)
    if view.status == "queued":
        head = msg("busy_progress_queued", locale)
    else:
        head = msg("busy_progress_head", locale, elapsed=_elapsed(view.seconds, locale))
        steps = view.thinking_md.count("🔧")
        if steps:
            head += msg("busy_progress_head_steps", locale, steps=steps)
    quiet = (
        msg("busy_progress_quiet_line", locale, elapsed=_elapsed(view.quiet_seconds, locale))
        if view.status != "queued" and view.quiet_seconds >= QUIET_NOTICE_SECONDS
        else ""
    )
    tools = _recent_tools(view.thinking_md)
    lines = _recent_lines(view.reply_text)
    dropped = msg("busy_progress_dropped", locale)
    if platform == "wecom":
        detail = [
            head,
            quiet,
            f"{msg('busy_progress_tools', locale)}：{tools}" if tools else "",
            f"{msg('busy_progress_wrote', locale)}：{lines[-1]}" if lines else "",
            dropped,
        ]
        return result_card(
            platform,
            tid,
            title=title,
            desc="\n".join(line for line in detail if line),
            icon_url=icon_url,
            locale=locale,
        )
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": f"**{head}**" + (f"\n<font color='orange'>{quiet}</font>" if quiet else ""),
        }
    ]
    if tools:
        elements.append(
            {"tag": "markdown", "content": f"**{msg('busy_progress_tools', locale)}**\n{tools}"}
        )
    if lines:
        elements.append(
            {
                "tag": "markdown",
                "content": f"**{msg('busy_progress_wrote', locale)}**\n" + "\n".join(lines),
            }
        )
    elements += [
        {"tag": "hr"},
        {"tag": "markdown", "content": f"<font color='grey'>{dropped}</font>"},
    ]
    return {
        "schema": "2.0",
        "config": {"summary": {"content": head}},
        "header": {"template": "wathet", "title": {"tag": "plain_text", "content": title}},
        "body": {"elements": elements},
        "task_id": tid,
    }


def progress_text(
    *,
    status: str,
    seconds: float,
    thinking_md: str,
    reply_text: str,
    quiet_seconds: float = 0.0,
    locale: str = "zh",
    subject: str = "busy",
) -> str:
    """上一条的进度：用了多久、做了几步、最近写到哪句。只读实时状态，不调用模型。

    `subject="live"` 是运行中卡片自己的「看看进度」：说的是这一条，不是「上一条」。
    """
    if status == "queued":
        return msg(f"{subject}_progress_queued", locale)
    steps = thinking_md.count("🔧")
    text = msg(f"{subject}_progress_elapsed", locale, elapsed=_elapsed(seconds, locale))
    if steps:
        text += msg("busy_progress_steps", locale, steps=steps)
    if quiet_seconds >= QUIET_NOTICE_SECONDS:
        text += msg("busy_progress_quiet", locale, elapsed=_elapsed(quiet_seconds, locale))
    text += "。"
    lines = [line.strip() for line in reply_text.splitlines() if line.strip()]
    if lines:
        recent = lines[-1].lstrip("#>*- ").strip()
        recent = _clip(recent, 50)
        text += "\n" + msg("busy_progress_recent", locale, recent=recent)
    return text


async def progress_of(
    session: AsyncSession,
    bot_id: uuid.UUID,
    session_key: str,
    sender: str,
    *,
    before_task_id: int,
    min_running: float = 0.0,
    locale: str = "zh",
) -> str:
    """这个人排在前面的那条（优先正在跑的）现在到哪了。

    `min_running` 大于 0：没在跑或刚开始跑时返回空串，发卡时不必多说一句。
    """
    text, _mark = await progress_view(
        session,
        bot_id,
        session_key,
        sender,
        before_task_id=before_task_id,
        min_running=min_running,
        locale=locale,
    )
    return text


async def progress_view(
    session: AsyncSession,
    bot_id: uuid.UUID,
    session_key: str,
    sender: str,
    *,
    before_task_id: int,
    min_running: float = 0.0,
    locale: str = "zh",
) -> tuple[str, str]:
    """`progress_of` 的进度说明，外加一个进展标记：步骤和写出的字都没变，标记就不变。"""
    view = await progress_snapshot(
        session, bot_id, session_key, sender, before_task_id=before_task_id
    )
    if view is None:
        return ("" if min_running else msg("busy_progress_done", locale)), ""
    if min_running and (view.status not in tasks.ACTIVE or view.seconds < min_running):
        return "", ""
    text = progress_text(
        status=view.status,
        seconds=view.seconds,
        thinking_md=view.thinking_md,
        reply_text=view.reply_text,
        quiet_seconds=view.quiet_seconds,
        locale=locale,
    )
    return text, view.mark


async def progress_snapshot(
    session: AsyncSession,
    bot_id: uuid.UUID,
    session_key: str,
    sender: str,
    *,
    before_task_id: int,
) -> Progress | None:
    """这个人排在前面的那条（优先正在跑的）此刻的状态；前面已经没有在办的返回 None。"""
    rows = list(
        await session.scalars(
            select(Task)
            .where(
                Task.bot_id == bot_id,
                Task.session_key == session_key,
                Task.kind == "chat",
                Task.status.in_(tasks.OPEN),
                Task.cancel_requested_at.is_(None),
                Task.id < before_task_id,
                Task.payload["platform_user_id"].astext == sender,
            )
            .order_by(Task.id)
        )
    )
    if not rows:
        return None
    ahead = next((row for row in rows if row.status in tasks.ACTIVE), rows[0])
    stream = await session.get(TaskStream, ahead.id, populate_existing=True)
    now = datetime.now(UTC)
    since = (stream.running_since if stream else None) or ahead.started_at or ahead.claimed_at
    thinking_md = stream.thinking_md if stream else ""
    reply_text = stream.pending_text if stream else ""
    return Progress(
        status=ahead.status,
        seconds=(now - since).total_seconds() if since else 0.0,
        quiet_seconds=(now - stream.updated_at).total_seconds() if stream else 0.0,
        thinking_md=thinking_md,
        reply_text=reply_text,
        mark=f"{ahead.id}:{ahead.status}:{len(thinking_md)}:{len(reply_text)}",
    )
