"""下班编译：下班后给助理排一轮平台来源的任务，让它把当天的进展整理进知识库。

每位助理每个自然日（所有者时区）至多一次。不找「上班 → 下班」的边沿（scheduler 重启会错过），
记两个日期：`worked_on`（最近一次被看到在班的那天）与 `compiled_on`（已经处理过编译的那天）；
现在不在班、`worked_on` 晚于 `compiled_on`，就该为 `worked_on` 那天编译。活动窗口从上一次编译
成功时的入队时刻（`compiled_at`）算起，窗口里什么都没发生就不入队。

平台给这一轮的文本只有计数、编号、工具名与状态；内容由助理用自己的工具读。3A 落地后改成
子任务（总览裁定 6）。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.assistants import delivery, followups, platform_turns, runtime, sealed
from coreman.core.assistants import settings as assistant_settings
from coreman.core.assistants.kinds import COMPILE_KIND
from coreman.core.assistants.records import is_assistant
from coreman.core.db.models import (
    AssistantApproval,
    AssistantFollowup,
    AssistantSettings,
    Bot,
    ChatLog,
)
from coreman.core.logging import get_logger
from coreman.core.timeutils import aware_utc

log = get_logger(__name__)

# 每一类最多列这么多行；更多的由助理自己用工具查。
MAX_LINES = 40
# 库里读出来的工具名、状态只留这些字符：平台写的行里不出现别的东西。
_UNSAFE = re.compile(r"[^A-Za-z0-9_\-]")
INSTRUCTIONS = (
    "把今天值得跨天记住的事整理进知识库，做完只回一两句话说整理了什么，不要复述内容：\n"
    "- 先用 notes_search、notes_read 找到相关的页（人、项目、决策各自成页），用 notes_write "
    "补充或新建；当天的流水写进 log/{day}.md。\n"
    "- 被拒的审批单：把所有者不同意的原因归纳成一句话，写进相关的页。\n"
    "- 只写事实与结论，不抄聊天原文；来自别人消息的内容写明是谁说的。\n"
    "- 置顶页在这一轮改不了（没有人在场）：想改的规则写进当天的日志页，明天告诉所有者。\n"
    "- 没有值得记的就只回一句「今天没有要整理的」，什么都不写。"
)


@dataclass(frozen=True)
class Activity:
    """窗口里发生了什么；只有计数与元数据。"""

    turns: int
    # （审批单编号, 工具名, 状态）
    approvals: tuple[tuple[str, str, str], ...]
    # （待跟进编号, 分类, 变化）
    followups: tuple[tuple[str, str, str], ...]

    @property
    def empty(self) -> bool:
        return not (self.turns or self.approvals or self.followups)


def _clean(value: str) -> str:
    return _UNSAFE.sub("", value)[:80]


async def activity(
    session: AsyncSession, bot_id: uuid.UUID, chat_id: str, since: datetime
) -> Activity:
    """`since` 之后这位助理的活动：主会话的对话轮次、审批单、待跟进。只读元数据列。"""
    turns = await session.scalar(
        select(func.count())
        .select_from(ChatLog)
        .where(
            ChatLog.bot_id == bot_id,
            ChatLog.session_key == chat_id,
            ChatLog.request_at > since,
            # 编译这一轮自己不算：否则每天都「有活动」。
            ChatLog.message_type != COMPILE_KIND,
        )
    )
    approvals = (
        await session.execute(
            select(AssistantApproval.id, AssistantApproval.tool, AssistantApproval.status)
            .where(
                AssistantApproval.bot_id == bot_id,
                or_(
                    AssistantApproval.created_at > since,
                    AssistantApproval.decided_at > since,
                    AssistantApproval.executed_at > since,
                ),
            )
            .order_by(AssistantApproval.created_at)
            .limit(MAX_LINES)
        )
    ).all()
    changed = (
        await session.execute(
            select(
                AssistantFollowup.seq,
                AssistantFollowup.category,
                AssistantFollowup.status,
                AssistantFollowup.created_at,
                AssistantFollowup.closed_at,
            )
            .where(
                AssistantFollowup.bot_id == bot_id,
                or_(
                    AssistantFollowup.created_at > since,
                    AssistantFollowup.updated_at > since,
                    AssistantFollowup.closed_at > since,
                ),
            )
            .order_by(AssistantFollowup.seq)
            .limit(MAX_LINES)
        )
    ).all()
    rows: list[tuple[str, str, str]] = []
    for seq, category, status, created_at, closed_at in changed:
        if closed_at is not None and closed_at > since:
            change = f"已关闭（{_clean(status)}）"
        else:
            change = "新增" if created_at > since else "更新"
        rows.append((followups.label(seq), _clean(category), change))
    return Activity(
        turns=int(turns or 0),
        approvals=tuple((str(row[0]), _clean(row[1]), _clean(row[2])) for row in approvals),
        followups=tuple(rows),
    )


def window_start(prefs: AssistantSettings, day: date) -> datetime:
    """活动窗口的起点：上一次编译成功时的入队时刻；从没编译过就是那一天的零点（所有者时区）。"""
    if prefs.compiled_at is not None:
        return aware_utc(prefs.compiled_at)
    zone = assistant_settings.zone(prefs)
    return datetime.combine(day, time(0), tzinfo=zone).astimezone(UTC)


def render(day: date, found: Activity) -> str:
    """交给模型的文本。除了固定的说明，只有计数、编号、工具名与状态。"""
    lines = [
        f"【下班整理 {day.isoformat()}】这一轮由平台在下班时发起，所有者不在场。",
        INSTRUCTIONS.format(day=day.isoformat()),
        "今天的活动（只有编号与状态，内容用工具读）：",
        f"- 对话轮次：{found.turns}",
    ]
    if found.approvals:
        lines.append("- 审批单（用 approval_list 查看）：")
        lines += [
            f"  - {_clean(number)} · {_clean(tool)} · {_clean(status)}"
            for number, tool, status in found.approvals
        ]
    if found.followups:
        lines.append("- 待跟进（用 followup_list 查看，scope 填编号）：")
        lines += [
            f"  - {label} · {category} · {change}" for label, category, change in found.followups
        ]
    return "\n".join(lines)


async def step(
    session: AsyncSession, bot: Bot, prefs: AssistantSettings, now: datetime
) -> str | None:
    """一位助理的一次判断：在班就记下今天上过班；下班后该编译就入队。调用方提交。

    Returns:
        `worked`（记下了今天）、`queued`（入队了）、`idle`（没有活动）、`budget`（预算用完）、
        `paused`（运行时暂停，稍后再看）、`refused`（入不了队）；什么都没做返回 None
    """
    if prefs.cold_start_done_at is None:
        return None
    today = assistant_settings.local_today(prefs, now)
    if assistant_settings.on_shift(prefs, now):
        if prefs.worked_on == today:
            return None
        prefs.worked_on = today
        return "worked"
    day = prefs.worked_on
    if day is None or (prefs.compiled_on is not None and prefs.compiled_on >= day):
        return None
    if await runtime.turn_blocker(session, bot) is not None:
        # 暂停时这一轮只会静默失败：不记成处理过，恢复后再入队。
        return "paused"
    # 下面无论入不入队，这一天都不再看。
    prefs.compiled_on = day
    since = window_start(prefs, day)
    found = await activity(session, bot.id, prefs.chat_id, since)
    if found.empty:
        return "idle"
    if await delivery.budget_left(session, bot, today) <= 0:
        return "budget"
    task = await platform_turns.enqueue(
        session,
        bot,
        COMPILE_KIND,
        {
            "day": day.isoformat(),
            "since": since.isoformat(),
            "until": aware_utc(now).isoformat(),
        },
        dedupe_key=f"assistant-compile:{bot.id}:{day.isoformat()}",
    )
    return "queued" if task is not None else "refused"


async def tick(factory: async_sessionmaker[AsyncSession], now: datetime) -> dict[str, int]:
    """循环入口：冷启动做完的启用中的助理逐个判断，每位一个事务。返回各结果的个数。"""
    async with factory() as session:
        ids = list(
            await session.scalars(
                select(Bot.id)
                .join(AssistantSettings, AssistantSettings.bot_id == Bot.id)
                .where(
                    Bot.kind == "assistant",
                    Bot.enabled.is_(True),
                    Bot.assistant_state == "active",
                    AssistantSettings.cold_start_done_at.is_not(None),
                )
                .order_by(Bot.id)
            )
        )
    counts: dict[str, int] = {}
    for bot_id in ids:
        code: str | None = None
        async with factory() as session:
            try:
                await sealed.guard_logs(session, bot_id)
                bot = await session.get(Bot, bot_id)
                # 别处正锁着这一行（所有者刚发了「下班」）：这一次跳过，5 秒后再来。
                prefs = await session.get(
                    AssistantSettings,
                    bot_id,
                    with_for_update={"skip_locked": True},
                    populate_existing=True,
                )
                if (
                    bot is not None
                    and prefs is not None
                    and bot.enabled
                    and is_assistant(bot)
                    and bot.assistant_state == "active"
                ):
                    code = await step(session, bot, prefs, now)
                await session.commit()
            except Exception as exc:  # noqa: BLE001 一位助理出错不挡别人；只记类型
                await session.rollback()
                log.warning(
                    "assistant_compile_failed", bot_id=str(bot_id), error=type(exc).__name__
                )
                code = "failed"
        if code is not None:
            counts[code] = counts.get(code, 0) + 1
    return counts
