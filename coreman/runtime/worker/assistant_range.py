"""所有者在私聊里改授权的消息范围：发「授权范围」→ 三选一的卡片 → 点一个 → 原地换成授权链接。

文字命令在入站阶段认出发言人后截下（不经模型）。回调先过 `CardActionHandler` 的核对（卡片确实
由这条记录发到这个会话），这里再核对：记录是启用中的个人助理、点的人就是所有者、令牌是平台签的
且没过期（绑定记录、发卡片的那一轮、到期时刻与所选范围）。
"""

from __future__ import annotations

import hmac
import time
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import message_range
from coreman.core.assistants.records import is_assistant
from coreman.core.bus import outbox, tasks
from coreman.core.chat.identity import resolve_speaker
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, InboundEvent
from coreman.core.feishu_personal.errors import PersonalError
from coreman.core.prompting import Speaker
from coreman.runtime.worker.context import TaskContext

COMMANDS = frozenset({"授权范围", "/授权范围"})
EVENT_KEY = "assistant_message_range"
TTL_SECONDS = 1800
TITLE = "授权的消息范围"
EXPIRED = "这张卡片已过期；要改的话再发一次「授权范围」。"
FAILED = "没能发起重新授权，请稍后再试，或到「我的助理」页修改。"
INTRO = (
    "选择助理能读哪些会话。飞书只能按「单聊 / 群聊」细分：选了只群聊，单聊的消息飞书本身就不给"
    "助理读，反之亦然。具体某个群、某个人不托管，请发「托管设置」。\n"
    "**改了要重新授权**：原来的授权当场作废，授权完成之前助理读不了你的飞书。"
)


def _mac(cipher: Cipher, bot_id: uuid.UUID, task_id: int, expires: int, choice: str) -> str:
    return cipher.mac("assistant-message-range", f"{bot_id}:{task_id}:{expires}:{choice}")


def _plain(text: str) -> dict[str, str]:
    return {"tag": "plain_text", "content": text}


def _text_card(text: str) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {"title": _plain(TITLE)},
        "body": {"elements": [{"tag": "markdown", "content": text}]},
    }


def card(
    cipher: Cipher, bot_id: uuid.UUID, task_id: int, now: float, current: str
) -> dict[str, Any]:
    expires = int(now) + TTL_SECONDS
    card_task = f"personal:{task_id}"
    lines = [
        f"- **{message_range.TITLES[choice]}**"
        + ("（现在）" if choice == current else "")
        + f"：{message_range.HINTS[choice]}"
        for choice in message_range.RANGES
    ]
    buttons = [
        {
            "tag": "button",
            "type": "default" if choice == current else "primary",
            "text": _plain(message_range.TITLES[choice]),
            "behaviors": [
                {
                    "type": "callback",
                    "value": {
                        "task_id": card_task,
                        "event_key": EVENT_KEY,
                        "level": f"{expires}.{choice}."
                        + _mac(cipher, bot_id, task_id, expires, choice),
                    },
                }
            ],
        }
        for choice in message_range.RANGES
        if choice != current
    ]
    return {
        "schema": "2.0",
        "task_id": card_task,
        "header": {"template": "turquoise", "title": _plain(TITLE)},
        "body": {
            "elements": [
                {"tag": "markdown", "content": INTRO + "\n" + "\n".join(lines)},
                *buttons,
            ]
        },
    }


async def intercept(
    session: AsyncSession,
    ctx: TaskContext,
    bot: Bot,
    speaker: Speaker,
    chat_id: str,
    text: str,
) -> bool:
    """个人助理私聊里发了「授权范围」：所有者收到三选一的卡片，别人的就此结束；返回真。"""
    if text.strip() not in COMMANDS:
        return False
    owner = speaker.user_id is not None and speaker.user_id == bot.owner_id
    if owner:
        current = message_range.view(await message_range.grant_of(session, bot))["message_range"]
        await outbox.add(
            session,
            bot_id=bot.id,
            platform="feishu",
            kind="send",
            dedupe_key=f"assistant-message-range-card:{ctx.task.id}",
            target={"chat_id": chat_id},
            payload={"card": card(ctx.cipher, bot.id, ctx.task.id, time.time(), current)},
        )
    await tasks.finish(
        session,
        ctx.task.id,
        status="succeeded",
        result={"assistant": "range_offered" if owner else "range_not_owner"},
        only_active=True,
        bot_id=bot.id,
    )
    return True


async def _update(
    session: AsyncSession, bot: Bot, inbound: InboundEvent, card_body: dict[str, Any]
) -> None:
    await outbox.add(
        session,
        bot_id=bot.id,
        platform="feishu",
        kind="card_update",
        dedupe_key=f"{inbound.id}:card_update",
        target={"chat_id": inbound.chat_id, "message_id": inbound.reply_context.get("message_id")},
        payload={"card": card_body},
    )


async def handle(
    session: AsyncSession, ctx: TaskContext, bot: Bot, inbound: InboundEvent, action: dict[str, Any]
) -> str:
    """三选一卡片的回调；调用方（`CardActionHandler`）已核对卡片由这条记录发到这个会话。"""
    if not is_assistant(bot) or bot.assistant_state != "active":
        return "ignored"
    try:
        task_id = int(str(action.get("task_id") or "").removeprefix("personal:"))
        expires_raw, choice, mac = str(action.get("level") or "").split(".", 2)
        expires = int(expires_raw)
    except ValueError:
        return "ignored"
    if choice not in message_range.RANGES:
        return "ignored"
    # 按字节比：compare_digest 遇到非 ASCII 的 str 会抛 TypeError。
    expected = _mac(ctx.cipher, bot.id, task_id, expires, choice)
    if not hmac.compare_digest(mac.encode(), expected.encode()):
        return "ignored"
    speaker = await resolve_speaker(
        session, platform="feishu", platform_user_id=str(inbound.sender_platform_user_id or "")
    )
    if speaker.user_id is None or speaker.user_id != bot.owner_id:
        return "ignored"
    if expires < time.time():
        await _update(session, bot, inbound, _text_card(EXPIRED))
        return "range_expired"
    try:
        url = await message_range.change(session, ctx.cipher, bot, choice, send=False)
    except PersonalError as exc:
        ctx.log.warning("assistant_message_range_failed", code=exc.code)
        await _update(session, bot, inbound, _text_card(FAILED))
        return f"range_{exc.code}"
    await _update(session, bot, inbound, message_range.card(choice, url))
    return "range_changed"
