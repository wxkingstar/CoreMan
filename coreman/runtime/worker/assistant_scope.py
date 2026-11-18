"""所有者在私聊里挑不托管（或只托管）的会话：发「托管设置」→ 按会话名勾选的卡片 → 提交。

文字命令在入站阶段认出发言人后截下（不经模型），与「停用助理」同一处。卡片列出最近活跃的
会话（名字与类型，不读消息，见 `chat_choices`）：托管全部时勾的是不托管的会话，只托管名单时
勾的是要托管的会话。卡片上之前已在名单里的会话预先勾上；提交时只改卡片上列出来的这些，名单
里没列出来的（不在最近的会话里）原样留着。

回调先过 `CardActionHandler` 的核对（卡片确实由这条记录发到这个会话），这里再核对：记录是
启用中的个人助理、点的人就是所有者、令牌是平台签的且没过期（绑定记录、发卡片的那一轮、
到期时刻、改哪份名单与卡片上预先勾上的会话）。
"""

from __future__ import annotations

import hmac
import time
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import chat_choices, watch
from coreman.core.assistants import settings as assistant_settings
from coreman.core.assistants.records import is_assistant
from coreman.core.bus import outbox, tasks
from coreman.core.chat.identity import resolve_speaker
from coreman.core.crypto import Cipher
from coreman.core.db.models import AssistantSettings, Bot, InboundEvent
from coreman.core.feishu_personal.errors import PersonalError
from coreman.core.feishu_personal.toolbase import Call
from coreman.core.prompting import Speaker
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.context import TaskContext

COMMANDS = frozenset({"托管设置", "/托管设置"})
EVENT_KEY = "assistant_chat_scope"
TTL_SECONDS = 1800
TITLE = "托管设置"
FIELDS = ("ignored", "managed")
INTRO = {
    "ignored": (
        "勾上的会话**不托管**：我和平台都不读、不写，已经攒下还没处理的消息作废。"
        "取消勾选就恢复托管，从那一刻起的新消息才会看。\n下面是你最近活跃的会话；不在其中的，"
        "到「我的助理」页按名字搜。"
    ),
    "managed": (
        "你现在是**只托管名单里的会话**：只有勾上的会话我才会看，其余一律不读、不写。"
        "取消勾选的会话，已经攒下还没处理的消息作废。\n下面是你最近活跃的会话；不在其中的，"
        "到「我的助理」页按名字搜。"
    ),
}
LIST_FAILED = "现在读不到你的会话列表（飞书授权可能需要续期），请到「我的助理」页设置托管范围。"
EXPIRED = "这张卡片已过期；要改的话再发一次「托管设置」。"
ERRORS = {
    "too_many_chats": "勾选的会话超过上限（{limit} 个），请少选一些。",
    "chat_in_both_lists": "有会话同时是重点群：先在「我的助理」页把它移出重点群，再设为不托管。",
    "focus_not_managed": "重点群必须在托管名单里：先在「我的助理」页把它移出重点群。",
    "content_locked": "现在取不到你的数据密钥，名单暂时改不了，请稍后再试。",
}


def _mac(
    cipher: Cipher, bot_id: uuid.UUID, task_id: int, expires: int, field: str, shown: list[str]
) -> str:
    joined = ",".join(sorted(shown))
    return cipher.mac("assistant-chat-scope", f"{bot_id}:{task_id}:{expires}:{field}:{joined}")


def _plain(text: str) -> dict[str, str]:
    return {"tag": "plain_text", "content": text}


def _text_card(text: str) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {"title": _plain(TITLE)},
        "body": {"elements": [{"tag": "markdown", "content": text}]},
    }


def card(
    cipher: Cipher,
    bot_id: uuid.UUID,
    task_id: int,
    now: float,
    row: AssistantSettings,
    choices: list[chat_choices.Choice],
) -> dict[str, Any]:
    field = "managed" if row.chat_scope == "listed" else "ignored"
    current = list((row.managed_chats_enc if field == "managed" else row.ignored_chats_enc) or [])
    listed = {choice.chat_id for choice in choices}
    shown = [chat for chat in current if chat in listed]
    expires = int(now) + TTL_SECONDS
    card_task = f"personal:{task_id}"
    # 会话名是别人起的：只放进纯文本。
    select: dict[str, Any] = {
        "tag": "multi_select_static",
        "name": "chats",
        "placeholder": _plain("选择会话"),
        "options": [
            {"text": _plain(chat_choices.label(choice)), "value": choice.chat_id}
            for choice in choices
        ],
    }
    if shown:
        select["selected_values"] = shown
    return {
        "schema": "2.0",
        "task_id": card_task,
        "header": {"template": "turquoise", "title": _plain(TITLE)},
        "body": {
            "elements": [
                {"tag": "markdown", "content": INTRO[field]},
                {
                    "tag": "form",
                    "name": "chat_scope_form",
                    "elements": [
                        select,
                        {
                            "tag": "button",
                            "name": "submit",
                            "form_action_type": "submit",
                            "type": "primary",
                            "text": _plain("保存"),
                            "behaviors": [
                                {
                                    "type": "callback",
                                    "value": {
                                        "task_id": card_task,
                                        "event_key": EVENT_KEY,
                                        "level": f"{expires}.{field}."
                                        + _mac(cipher, bot_id, task_id, expires, field, shown),
                                        "shown": shown,
                                    },
                                }
                            ],
                        },
                    ],
                },
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
    """个人助理私聊里发了「托管设置」：所有者收到勾选卡片，别人的就此结束；返回真。"""
    if text.strip() not in COMMANDS:
        return False
    owner = speaker.user_id is not None and speaker.user_id == bot.owner_id
    result = "scope_not_owner"
    if owner:
        prefs = await assistant_settings.get(session, bot.id)
        scope, _code = await watch.owner_scope(session, bot, prefs)
        choices: list[chat_choices.Choice] | None = None
        if scope is not None and bot.assistant_state == "active":
            try:
                choices = await chat_choices.listing(
                    Call(session, ctx.cipher, scope),
                    own_chat=prefs.chat_id,
                    owner_open_id=prefs.open_id,
                )
            except PersonalError:
                choices = None
        payload = (
            {"card": card(ctx.cipher, bot.id, ctx.task.id, time.time(), prefs, choices)}
            if choices
            else {"card": _text_card(LIST_FAILED)}
        )
        result = "scope_offered" if choices else "scope_list_failed"
        await outbox.add(
            session,
            bot_id=bot.id,
            platform="feishu",
            kind="send",
            dedupe_key=f"assistant-chat-scope:{ctx.task.id}",
            target={"chat_id": chat_id},
            payload=payload,
        )
    await tasks.finish(
        session,
        ctx.task.id,
        status="succeeded",
        result={"assistant": result},
        only_active=True,
        bot_id=bot.id,
    )
    return True


async def _update(session: AsyncSession, bot: Bot, inbound: InboundEvent, text: str) -> None:
    await outbox.add(
        session,
        bot_id=bot.id,
        platform="feishu",
        kind="card_update",
        dedupe_key=f"{inbound.id}:card_update",
        target={"chat_id": inbound.chat_id, "message_id": inbound.reply_context.get("message_id")},
        payload={"card": _text_card(text)},
    )


def _selected(action: dict[str, Any]) -> list[str]:
    """表单里勾上的会话。回调的形状没在真实租户里核对过：单个字符串、字符串列表都认。"""
    selected = action.get("selected")
    found = selected.get("chats") if isinstance(selected, dict) else None
    if isinstance(found, str):
        return [found]
    return [value for value in found if isinstance(value, str)] if isinstance(found, list) else []


async def handle(
    session: AsyncSession, ctx: TaskContext, bot: Bot, inbound: InboundEvent, action: dict[str, Any]
) -> str:
    """勾选卡片的回调；调用方（`CardActionHandler`）已核对卡片由这条记录发到这个会话。"""
    if not is_assistant(bot) or bot.assistant_state != "active":
        return "ignored"
    try:
        task_id = int(str(action.get("task_id") or "").removeprefix("personal:"))
        expires_raw, field, mac = str(action.get("level") or "").split(".", 2)
        expires = int(expires_raw)
    except ValueError:
        return "ignored"
    shown_raw = action.get("shown")
    shown = (
        [chat for chat in shown_raw if isinstance(chat, str)] if isinstance(shown_raw, list) else []
    )
    if field not in FIELDS:
        return "ignored"
    # 按字节比：compare_digest 遇到非 ASCII 的 str 会抛 TypeError。
    expected = _mac(ctx.cipher, bot.id, task_id, expires, field, shown)
    if not hmac.compare_digest(mac.encode(), expected.encode()):
        return "ignored"
    speaker = await resolve_speaker(
        session, platform="feishu", platform_user_id=str(inbound.sender_platform_user_id or "")
    )
    if speaker.user_id is None or speaker.user_id != bot.owner_id:
        return "ignored"
    if expires < time.time():
        await _update(session, bot, inbound, EXPIRED)
        return "scope_expired"
    row = await assistant_settings.get(session, bot.id)
    if (field == "managed") != (row.chat_scope == "listed"):
        # 发卡片之后在「我的助理」页换了托管范围：这张卡片勾的是另一份名单。
        await _update(session, bot, inbound, EXPIRED)
        return "scope_stale"
    current = list((row.managed_chats_enc if field == "managed" else row.ignored_chats_enc) or [])
    chosen = list(dict.fromkeys([*(c for c in current if c not in shown), *_selected(action)]))
    now = utcnow()
    try:
        if field == "managed":
            await assistant_settings.update(session, bot.id, now=now, managed_chats=chosen)
        else:
            await assistant_settings.update(session, bot.id, now=now, ignored_chats=chosen)
    except assistant_settings.SettingsError as exc:
        said = ERRORS.get(exc.code, "没改成，请到「我的助理」页设置。").format(**exc.detail)
        await _update(session, bot, inbound, said)
        return f"scope_{exc.code}"
    said = (
        f"已保存：现在只托管 {len(chosen)} 个会话（另加我们这个私聊）。"
        if field == "managed"
        else f"已保存：{len(chosen)} 个会话不托管。"
    )
    await _update(session, bot, inbound, said + "随时可以再发「托管设置」修改。")
    return "scope_saved"
