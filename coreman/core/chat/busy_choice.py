"""上一条还在处理时又来了新消息：新消息先排队，卡片让发消息的人选「等它做完」「停下上一条」还是「放弃这条」。

不点也行：新消息本来就排在后面，上一条做完自动接着处理；只有点「停下」才会动正在跑的任务。
「看看进度」只读上一条的实时状态回一句，卡片和按钮都留着，可以再点。
卡片的 task_id 是 `busy@<新消息的任务 id>`，飞书与企微共用。
"""

from __future__ import annotations

import re
import uuid
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


def card_task_id(task_id: int) -> str:
    return f"{CARD_PREFIX}{task_id}"


def parse_card_task_id(value: str) -> int | None:
    match = _TASK_ID.fullmatch(value)
    return int(match.group(1)) if match else None


def offer_card(
    platform: str, task_id: int, *, progress: str = "", locale: str = "zh"
) -> dict[str, Any]:
    """`progress` 是点过「看看进度」后的进度说明，放在问句前面；按钮照旧。"""
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
                *([{"tag": "markdown", "content": progress}] if progress else []),
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


def progress_text(
    *,
    status: str,
    seconds: float,
    thinking_md: str,
    reply_text: str,
    quiet_seconds: float = 0.0,
    locale: str = "zh",
) -> str:
    """上一条的进度：用了多久、做了几步、最近写到哪句。只读实时状态，不调用模型。"""
    if status == "queued":
        return msg("busy_progress_queued", locale)
    steps = thinking_md.count("🔧")
    text = msg("busy_progress_elapsed", locale, elapsed=_elapsed(seconds, locale))
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
        return "" if min_running else msg("busy_progress_done", locale)
    ahead = next((row for row in rows if row.status in tasks.ACTIVE), rows[0])
    stream = await session.get(TaskStream, ahead.id, populate_existing=True)
    now = datetime.now(UTC)
    since = (stream.running_since if stream else None) or ahead.started_at or ahead.claimed_at
    seconds = (now - since).total_seconds() if since else 0.0
    if min_running and (ahead.status not in tasks.ACTIVE or seconds < min_running):
        return ""
    return progress_text(
        status=ahead.status,
        seconds=seconds,
        thinking_md=stream.thinking_md if stream else "",
        reply_text=stream.pending_text if stream else "",
        quiet_seconds=(now - stream.updated_at).total_seconds() if stream else 0.0,
        locale=locale,
    )
