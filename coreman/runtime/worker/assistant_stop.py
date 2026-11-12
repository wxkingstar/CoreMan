"""所有者在私聊里停用自己的助理：发「停用助理」→ 确认卡片 → 点「确认停用」→ 进入停用流程。

文字命令在这一轮派出之前就截下（不经模型，开通没走完或运行时暂停时也能用）。确认卡片走这条记录的
出站箱，卡片回调先过 `CardActionHandler` 的核对（卡片确实由这条记录发到这个会话），这里再核对三样：
记录是个人助理、点的人就是所有者（按飞书给的操作人认）、卡片上的令牌没过期且是平台签的（主密钥
派生的 HMAC，绑定记录、发卡片的那一轮与到期时刻）。重复点、转发出去的卡片都不会多做什么。
"""

from __future__ import annotations

import hmac
import time
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import deprovisioning
from coreman.core.assistants.records import is_assistant
from coreman.core.bus import outbox, tasks
from coreman.core.chat.identity import resolve_speaker
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, InboundEvent
from coreman.runtime.worker.chat.models import Intake
from coreman.runtime.worker.context import TaskContext

COMMANDS = frozenset({"停用助理", "/停用助理"})
EVENT_KEY = "assistant_stop"
TTL_SECONDS = 600


def _mac(cipher: Cipher, bot_id: uuid.UUID, task_id: int, expires: int) -> str:
    return cipher.mac("assistant-stop", f"{bot_id}:{task_id}:{expires}")


def confirm_card(cipher: Cipher, bot_id: uuid.UUID, task_id: int, now: float) -> dict[str, Any]:
    expires = int(now) + TTL_SECONDS
    card_task = f"personal:{task_id}"
    return {
        "schema": "2.0",
        "header": {"template": "red", "title": {"tag": "plain_text", "content": "停用个人助理"}},
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        "停用后：飞书授权撤销，待批的审批单撤回，加密保存的对话与个人数据永久不可读，"
                        "工作目录从运行时上删除。之后可以重新开通，但以前的内容回不来。"
                        "\n确认要停用吗？（10 分钟内有效）"
                    ),
                },
                {
                    "tag": "button",
                    "type": "danger",
                    "text": {"tag": "plain_text", "content": "确认停用"},
                    "behaviors": [
                        {
                            "type": "callback",
                            "value": {
                                "task_id": card_task,
                                "event_key": EVENT_KEY,
                                "level": f"{expires}.{_mac(cipher, bot_id, task_id, expires)}",
                            },
                        }
                    ],
                },
            ]
        },
        "task_id": card_task,
    }


async def intercept(ctx: TaskContext, intake: Intake) -> bool:
    """个人助理的私聊里发了停用命令：发确认卡片、结束这一轮，返回真。其余消息返回假。"""
    if intake.text.strip() not in COMMANDS:
        return False
    async with ctx.session_factory() as session:
        if intake.speaker.user_id is not None and intake.speaker.user_id == intake.bot.owner_id:
            await outbox.add(
                session,
                bot_id=intake.bot.id,
                platform="feishu",
                kind="send",
                dedupe_key=f"assistant-stop:{ctx.task.id}",
                target={"chat_id": intake.chat_id},
                payload={"card": confirm_card(ctx.cipher, intake.bot.id, ctx.task.id, time.time())},
            )
        await tasks.finish(
            session,
            ctx.task.id,
            status="succeeded",
            result={"assistant": "stop_offered"},
            only_active=True,
            bot_id=intake.bot.id,
        )
        await session.commit()
    return True


async def _update(session: AsyncSession, bot: Bot, inbound: InboundEvent, text: str) -> None:
    await outbox.add(
        session,
        bot_id=bot.id,
        platform="feishu",
        kind="card_update",
        dedupe_key=f"{inbound.id}:card_update",
        target={"chat_id": inbound.chat_id, "message_id": inbound.reply_context.get("message_id")},
        payload={
            "card": {
                "schema": "2.0",
                "header": {"title": {"tag": "plain_text", "content": "停用个人助理"}},
                "body": {"elements": [{"tag": "markdown", "content": text}]},
            }
        },
    )


async def handle(
    session: AsyncSession, ctx: TaskContext, bot: Bot, inbound: InboundEvent, action: dict[str, Any]
) -> str:
    """确认卡片的回调；调用方（`CardActionHandler`）已核对卡片由这条记录发到这个会话。"""
    if not is_assistant(bot):
        return "ignored"
    try:
        task_id = int(str(action.get("task_id") or "").removeprefix("personal:"))
        expires_raw, _, mac = str(action.get("level") or "").partition(".")
        expires = int(expires_raw)
    except ValueError:
        return "ignored"
    if not hmac.compare_digest(mac, _mac(ctx.cipher, bot.id, task_id, expires)):
        return "ignored"
    speaker = await resolve_speaker(
        session, platform="feishu", platform_user_id=str(inbound.sender_platform_user_id or "")
    )
    if speaker.user_id is None or speaker.user_id != bot.owner_id:
        return "ignored"
    if expires < time.time():
        await _update(session, bot, inbound, "这张卡片已过期；还要停用的话再发一次「停用助理」。")
        return "assistant_stop_expired"
    await deprovisioning.request(session, bot, trigger="owner", actor_id=bot.owner_id)
    await _update(session, bot, inbound, "正在停用，做完会在这里告诉你。")
    return "assistant_stopping"
