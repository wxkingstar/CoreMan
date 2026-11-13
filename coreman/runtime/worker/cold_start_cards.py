"""冷启动问题卡片的回调：所有者提交或跳过一题。

调用方（`CardActionHandler`）已核对卡片确实由这条记录发到这个会话。这里再核对：记录是启用中的
个人助理、点的人就是所有者（按飞书给的操作人认）、令牌是平台为这一次冷启动的这一题签的、
这一题还没处理过。重复点、转发出去的卡片、别人点的都不会多做什么。

表单里的答案随回调落在任务载荷与入站事件里；助理的这两处内容由 `sealed` 按所有者的数据密钥
封存，不需要另外处理。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import cold_start
from coreman.core.assistants.records import is_assistant
from coreman.core.bus import outbox
from coreman.core.chat.identity import resolve_speaker
from coreman.core.db.models import AssistantSettings, Bot, InboundEvent
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.context import TaskContext

SKIPPED = "已跳过这一题，之后可以直接在这里告诉我。"
STALE = "这一题已经处理过了。"


async def _update(
    session: AsyncSession, bot: Bot, inbound: InboundEvent, step: str, text: str
) -> None:
    """把卡片换成没有按钮的样子。"""
    await outbox.add(
        session,
        bot_id=bot.id,
        platform="feishu",
        kind="card_update",
        dedupe_key=f"{inbound.id}:card_update",
        target={"chat_id": inbound.chat_id, "message_id": inbound.reply_context.get("message_id")},
        payload={"card": cold_start.answered_card(step, text)},
    )


async def handle(
    session: AsyncSession, ctx: TaskContext, bot: Bot, inbound: InboundEvent, action: dict[str, Any]
) -> str:
    if not is_assistant(bot) or bot.assistant_state != "active":
        return "ignored"
    speaker = await resolve_speaker(
        session, platform="feishu", platform_user_id=str(inbound.sender_platform_user_id or "")
    )
    if speaker.user_id is None or speaker.user_id != bot.owner_id:
        return "ignored"
    # 行锁：同一张卡片连点两下，第二下看到的是已经前进了的步骤。
    row = await session.get(AssistantSettings, bot.id, with_for_update=True, populate_existing=True)
    if row is None:
        return "ignored"
    parsed = cold_start.parse(ctx.cipher, bot.id, row, str(action.get("level") or ""))
    if parsed is None:
        return "ignored"
    step, verb = parsed
    if row.cold_start_done_at is not None or row.cold_start_step != step:
        await _update(session, bot, inbound, step, STALE)
        return "cold_start_stale"
    now = utcnow()
    if verb == "skip":
        await cold_start.skip(session, bot, row, step, now)
        await _update(session, bot, inbound, step, SKIPPED)
        return "cold_start_skipped"
    said = await cold_start.answer(session, bot, row, step, action.get("selected"), now)
    await _update(session, bot, inbound, step, said)
    return "cold_start_answered"
