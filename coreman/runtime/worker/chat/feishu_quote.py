"""把飞书 parent_id 补成模型读得到的引用内容。

依次尝试：
1. 本 bot 发出消息的正文存档（`feishu_sent_messages`），以及流、出站队列里还没清掉的原文；
2. 官方「获取指定消息的内容」接口，带 `card_msg_content_type=user_card_content`：不带时
   Card JSON 2.0 卡片只回一句「请升级至最新版本客户端」；
3. 本 bot 收到过的入站事件原文：群里 @ 过机器人的消息，在应用没有「读取群内所有消息」
   权限、接口读不到时兜底。

父消息必须与当前消息在同一会话。图片、文件只记「父消息 ID + key」，由内容阶段走官方资源
接口下载，从不访问任意 URL；读不到时显式标注，绝不伪造原文。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    Bot,
    FeishuDelivery,
    FeishuSentMessage,
    InboundEvent,
    OutboxItem,
    TaskStream,
    User,
    UserIdentity,
)
from coreman.core.logging import get_logger
from coreman.core.platforms.feishu import FeishuClient, FeishuError
from coreman.core.platforms.feishu_content import (
    MAX_TEXT,
    Image,
    Rendered,
    bounded,
    mention_names,
    render,
)

MAX_QUOTE_IMAGES = 9
MAX_FORWARDED = 50
UNAVAILABLE = "[引用消息内容不可用]"
RECALLED = "[引用的消息已撤回]"
_MESSAGE_ID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
# 飞书「消息已删除」；撤回的消息接口照常返回，只是带 deleted=true。
_DELETED_CODES = frozenset({230110})

log = get_logger(__name__)


@dataclass(frozen=True)
class Enriched:
    """补过引用的 parts / text；`media_message_id` 是引用里要下载的图片、文件所在的消息。

    它只在核对过「与当前消息同一会话」之后才非空，内容阶段据此放行这条消息的资源下载。
    """

    parts: list[dict[str, Any]]
    text: str
    media_message_id: str | None = None


def _note(parts: list[dict[str, Any]], text: str, note: str) -> Enriched:
    note_text = f"{note}\n\n{text}" if text.strip() else note
    return Enriched([{"type": "text", "text": note}, *parts], note_text)


def _text_quote(text: str) -> dict[str, Any]:
    return {"type": "quote", "kind": "text", "text": bounded(text), "refs": []}


async def _persisted_text(
    session: AsyncSession, *, bot: Bot, chat_id: str, parent_id: str
) -> str | None:
    archived = await session.scalar(
        select(FeishuSentMessage.text).where(
            FeishuSentMessage.bot_id == bot.id,
            FeishuSentMessage.message_id == parent_id,
            FeishuSentMessage.chat_id == chat_id,
        )
    )
    if isinstance(archived, str) and archived.strip():
        return archived
    # 存档上线前发出的消息：流（回复结束后留一小时）和出站队列（留七天）里可能还有原文。
    final_text = await session.scalar(
        select(TaskStream.final_text)
        .join(FeishuDelivery, FeishuDelivery.task_id == TaskStream.task_id)
        .where(
            FeishuDelivery.message_id == parent_id,
            TaskStream.bot_id == bot.id,
            TaskStream.platform == "feishu",
            TaskStream.reply_context["chat_id"].astext == chat_id,
            TaskStream.final_text.is_not(None),
        )
        .order_by(TaskStream.task_id.desc())
        .limit(1)
    )
    if isinstance(final_text, str) and final_text.strip():
        return final_text
    markdown = await session.scalar(
        select(OutboxItem.payload["markdown"].astext)
        .where(
            OutboxItem.bot_id == bot.id,
            OutboxItem.platform == "feishu",
            OutboxItem.status == "sent",
            OutboxItem.target["chat_id"].astext == chat_id,
            OutboxItem.payload["_feishu_message_id"].astext == parent_id,
        )
        .order_by(OutboxItem.id.desc())
        .limit(1)
    )
    return markdown if isinstance(markdown, str) and markdown.strip() else None


async def _fetch(credentials: dict[str, Any], parent_id: str) -> tuple[list[Any] | None, bool]:
    """官方接口读父消息；返回 (items, 已删除)。失败只记错误码，绝不反射上游错误文字。"""
    client = FeishuClient(credentials.get("app_id", ""), credentials.get("app_secret", ""))
    try:
        body = await client.call(
            "GET",
            f"/open-apis/im/v1/messages/{parent_id}",
            params={"card_msg_content_type": "user_card_content"},
        )
    except FeishuError as exc:
        # 群里读别人的消息需要应用开通 im:message.group_msg；缺了是 230027，见 docs/feishu.md。
        log.info("feishu_quote_fetch_failed", code=exc.code)
        return None, exc.code in _DELETED_CODES
    except Exception as exc:  # noqa: BLE001 凭证、网络、超时和畸形响应统一降级
        log.info("feishu_quote_fetch_failed", error=type(exc).__name__)
        return None, False
    finally:
        await client.aclose()
    data = body.get("data") if isinstance(body, dict) else None
    items = data.get("items") if isinstance(data, dict) else None
    return (items if isinstance(items, list) else None), False


def _parent_of(items: list[Any], *, chat_id: str, parent_id: str) -> dict[str, Any] | None:
    """接口返回里的父消息（合并转发时 items 里还有子消息）；不在当前会话的一律不认。"""
    parent = next(
        (i for i in items[:500] if isinstance(i, dict) and i.get("message_id") == parent_id),
        None,
    )
    return parent if parent is not None and parent.get("chat_id") == chat_id else None


async def _speakers(
    session: AsyncSession, bot: Bot, app_id: str, items: list[dict[str, Any]]
) -> dict[str, str]:
    """合并转发里认得出的发言人：本 bot 用自己的名字，同事按通讯录里的 open_id 认。"""
    open_ids = {
        str(sender.get("id"))
        for sender in (item.get("sender") for item in items)
        if isinstance(sender, dict) and sender.get("sender_type") == "user" and sender.get("id")
    }
    names: dict[str, str] = {}
    if open_ids:
        rows = await session.execute(
            select(UserIdentity.open_id, User.display_name)
            .join(User, User.id == UserIdentity.user_id)
            .where(UserIdentity.platform == "feishu", UserIdentity.open_id.in_(open_ids))
        )
        names = {str(oid): str(name) for oid, name in rows.all() if oid and name}
    if app_id:
        names[app_id] = bot.name
    return names


def _merged(items: list[dict[str, Any]], parent_id: str, names: dict[str, str]) -> Rendered:
    """合并转发的子消息逐条写成「发言人: 内容」；认不出的人按出场顺序叫「用户 N」。"""
    lines = ["[合并转发的聊天记录]"]
    unknown: dict[str, str] = {}

    def speaker(item: dict[str, Any]) -> str:
        sender = item.get("sender")
        sender = sender if isinstance(sender, dict) else {}
        sid = str(sender.get("id") or "")
        if sid in names:
            return names[sid]
        if sender.get("sender_type") == "app":
            return "机器人"
        return unknown.setdefault(sid, f"用户 {len(unknown) + 1}")

    def add(upper: str, depth: int) -> None:
        children = [i for i in items if i.get("upper_message_id") == upper]
        for child in children[:MAX_FORWARDED]:
            if child.get("deleted"):
                continue
            indent = "  " * depth
            if child.get("msg_type") == "merge_forward":
                lines.append(f"{indent}{speaker(child)}: [合并转发的聊天记录]")
                if depth < 2:
                    add(str(child.get("message_id") or ""), depth + 1)
                continue
            body = child.get("body")
            rendered = render(
                str(child.get("msg_type") or ""),
                body.get("content") if isinstance(body, dict) else None,
                names=mention_names(child.get("mentions")),
            )
            text = rendered.text() if rendered else "[暂不支持显示的消息]"
            lines.append(f"{indent}{speaker(child)}: {text}")

    add(parent_id, 0)
    return Rendered(segments=["\n".join(lines)])


def _quote_part(rendered: Rendered, message_id: str) -> tuple[dict[str, Any], str | None]:
    """可读内容 → quote part；带图片或文件时一并返回它们所在的消息 ID。"""
    if rendered.file_key and not rendered.segments:
        ref = {"message_id": message_id, "file_key": rendered.file_key, "type": "file"}
        if rendered.file_name:
            ref["filename"] = rendered.file_name
        return {"type": "quote", "kind": "file", "text": None, "refs": [ref]}, message_id
    images = rendered.images[:MAX_QUOTE_IMAGES]
    if not images:
        return _text_quote(rendered.text()), None
    if len(rendered.segments) == 1:
        ref = {"message_id": message_id, "file_key": images[0], "type": "image"}
        return {"type": "quote", "kind": "image", "text": None, "refs": [ref]}, message_id
    # 图文混排：连续的文字并成一段，图片按原位置挂上；超出上限的图片只留占位，文字总长同样封顶。
    items: list[dict[str, Any]] = []
    budget, room = len(images), MAX_TEXT
    for segment in rendered.segments:
        if isinstance(segment, Image) and budget > 0:
            budget -= 1
            ref = {"message_id": message_id, "file_key": segment.key, "type": "image"}
            items.append({"msgtype": "image", "image": ref})
            continue
        text = (segment if isinstance(segment, str) else "[图片]")[:room]
        room -= len(text)
        if not text:
            continue
        if items and items[-1]["msgtype"] == "text":
            items[-1]["text"]["content"] += "\n" + text
        else:
            items.append({"msgtype": "text", "text": {"content": text}})
    part = {"type": "quote", "kind": "mixed", "text": None, "refs": [{"msg_item": items}]}
    return part, message_id


async def _from_inbound(
    session: AsyncSession, *, bot: Bot, chat_id: str, parent_id: str
) -> Rendered | None:
    message = await session.scalar(
        select(InboundEvent.payload["raw"]["event"]["message"]).where(
            InboundEvent.bot_id == bot.id,
            InboundEvent.platform == "feishu",
            InboundEvent.platform_msg_id == parent_id,
            InboundEvent.chat_id == chat_id,
        )
    )
    if not isinstance(message, dict):
        return None
    return render(
        str(message.get("message_type") or ""),
        message.get("content"),
        names=mention_names(message.get("mentions")),
    )


async def enrich(
    session: AsyncSession,
    *,
    bot: Bot,
    cipher: Cipher,
    chat_id: str,
    parent_id: object,
    parts: list[dict[str, Any]],
    text: str,
) -> Enriched:
    """返回补过引用的 parts/text；失败显式标注，但不伪造父消息原文。"""
    if parent_id == "":
        return Enriched(parts, text)
    if not isinstance(parent_id, str) or not _MESSAGE_ID.fullmatch(parent_id) or not chat_id:
        return _note(parts, text, UNAVAILABLE)
    if any(part.get("type") == "quote" for part in parts):
        return Enriched(parts, text)
    quoted = await _persisted_text(session, bot=bot, chat_id=chat_id, parent_id=parent_id)
    if quoted is not None:
        return Enriched([*parts, _text_quote(quoted)], text)
    # 不在官方 HTTP 往返期间占着数据库事务/连接。
    await session.rollback()
    try:
        credentials = decrypt_json(cipher, bot.credentials_enc, CREDENTIALS_AAD)
    except Exception:  # noqa: BLE001 凭证坏了照样降级，下面还有入站事件可以兜底
        credentials = {}
    items, deleted = await _fetch(credentials, parent_id) if credentials else (None, False)
    rendered: Rendered | None = None
    parent = _parent_of(items, chat_id=chat_id, parent_id=parent_id) if items else None
    if parent is not None and parent.get("deleted"):
        deleted = True
    elif parent is not None and parent.get("msg_type") == "merge_forward":
        children = [i for i in items or [] if isinstance(i, dict)]
        app_id = str(credentials.get("app_id") or "")
        rendered = _merged(children, parent_id, await _speakers(session, bot, app_id, children))
    elif parent is not None:
        body = parent.get("body")
        rendered = render(
            str(parent.get("msg_type") or ""),
            body.get("content") if isinstance(body, dict) else None,
            names=mention_names(parent.get("mentions")),
        )
    if deleted:
        return _note(parts, text, RECALLED)
    if rendered is None and (items is None or parent is not None):
        # 接口读不到（多半是群里缺权限）才看入站事件；接口明确说不在本会话的，不再换路子去读。
        rendered = await _from_inbound(session, bot=bot, chat_id=chat_id, parent_id=parent_id)
    if rendered is None:
        return _note(parts, text, UNAVAILABLE)
    part, media_message_id = _quote_part(rendered, parent_id)
    return Enriched([*parts, part], text, media_message_id)
