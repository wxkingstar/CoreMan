"""B 层放行：系统发固定格式的卡片，提问人点回复按钮，点击作为他的下一句话回到本群。

卡片外框、说明和按钮都由这里渲染，AI 员工只能决定为哪些链接发起。回复按钮只有提问人能点
（网关按按钮里的提问人拒绝别人），同一行只算一次；点击开启的那一轮先在这里记下决定，
再照常交给 AI 员工继续。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.audit import record_audit
from coreman.core.bus import outbox
from coreman.core.chat.card_replies import clicked, is_click
from coreman.core.crypto import Cipher
from coreman.core.db.models import FeishuDocConsent, InboundEvent, OutboxItem
from coreman.core.feishu_cards import style
from coreman.core.feishu_cards.reply_buttons import reply_value
from coreman.core.feishu_docs import access, links
from coreman.core.feishu_docs.links import Link
from coreman.core.feishu_docs.policy import DocsScope
from coreman.core.feishu_personal.service import PersonalError
from coreman.core.feishu_personal.toolbase import Call
from coreman.core.timeutils import utcnow

PENDING_TTL = timedelta(minutes=30)
APPROVED_TTL = timedelta(hours=1)
RETENTION = timedelta(days=30)
MAX_LINKS = 5
ONCE = "仅本次读取"
SHARE = "共享给本群并读取"
DECLINE = "不用了"
LABELS = (ONCE, SHARE, DECLINE)
ROW = "consent_row"
_BUTTON_IDS = {ONCE: "consent_once", SHARE: "consent_share", DECLINE: "consent_no"}
NOTICE = (
    "本群看不到这些文档，AI 员工需要以你的身份读取，回答会发在本群，"
    "群里其他人不一定有权限查看原文。只读，不会修改或转发文档。"
)


def card_task_id(row_id: uuid.UUID) -> str:
    return f"feishu-docs:{row_id}"


def _button(label: str, *, requester: str, kind: str, ids: list[str]) -> dict[str, Any]:
    return {
        "tag": "column",
        "width": "auto",
        "elements": [
            {
                "tag": "button",
                "element_id": _BUTTON_IDS[label],
                "type": kind,
                "size": "small",
                "text": {"tag": "plain_text", "content": label},
                "behaviors": [
                    {
                        "type": "callback",
                        "value": reply_value(label, requester=requester, row=ROW, buttons=ids),
                    }
                ],
            }
        ],
    }


def card(
    row: FeishuDocConsent, *, bot_name: str, mention_open_id: str, requester: str
) -> dict[str, Any]:
    labels = [ONCE, *([SHARE] if row.share_offered else []), DECLINE]
    ids = [_BUTTON_IDS[label] for label in labels]
    kinds = {ONCE: "primary", SHARE: "default", DECLINE: "default"}
    listed = "\n".join(f"- {url}" for url in row.urls)
    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": f"<at id={mention_open_id}></at> {NOTICE}"},
        {"tag": "markdown", "content": listed},
        {
            "tag": "column_set",
            "element_id": ROW,
            "flex_mode": "flow",
            "horizontal_spacing": "8px",
            "columns": [
                _button(label, requester=requester, kind=kinds[label], ids=ids) for label in labels
            ],
        },
        {
            "tag": "markdown",
            "content": (
                "<font color='grey'>「仅本次读取」1 小时内、只在你在本群提问时有效；"
                "「共享给本群」会把本群加为文档的可阅读协作者。只有你能点，30 分钟内有效。</font>"
            ),
            "text_size": style.TEXT_NOTE,
        },
    ]
    return {
        "schema": "2.0",
        "task_id": card_task_id(row.id),
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "读取文档需要你同意"},
            "subtitle": {"tag": "plain_text", "content": f"AI 员工「{bot_name}」"},
        },
        "body": {"elements": elements},
    }


@dataclass(frozen=True)
class Requested:
    status: str
    consent_id: uuid.UUID


async def request(
    session: AsyncSession,
    scope: DocsScope,
    requester: access.Requester,
    targets: list[Link],
    *,
    now: datetime | None = None,
) -> Requested:
    """为提问人这条消息里的几篇文档发放行卡片；同样的几篇已有未过期的卡片就复用。"""
    assert scope.speaker_user_id is not None
    now = now or utcnow()
    tokens = sorted({link.token for link in targets})
    pending = await session.scalars(
        select(FeishuDocConsent).where(
            FeishuDocConsent.bot_id == scope.bot.id,
            FeishuDocConsent.chat_id == scope.chat_id,
            FeishuDocConsent.user_id == scope.speaker_user_id,
            FeishuDocConsent.status == "pending",
            FeishuDocConsent.expires_at > now,
        )
    )
    for existing in pending:
        if sorted(existing.tokens) == tokens:
            return Requested("already_requested", existing.id)
    row = FeishuDocConsent(
        bot_id=scope.bot.id,
        user_id=scope.speaker_user_id,
        chat_id=scope.chat_id,
        tokens=tokens,
        urls=[link.url for link in targets],
        status="pending",
        share_offered=requester.reaches(access.SHARE_ENDPOINT),
        shared=False,
        origin_task_id=scope.task.id,
        expires_at=now + PENDING_TTL,
    )
    session.add(row)
    await session.flush()
    item = await outbox.add(
        session,
        bot_id=scope.bot.id,
        platform="feishu",
        kind="send",
        dedupe_key=f"feishu-docs:{row.id}:card",
        target={"chat_id": scope.chat_id},
        payload={
            "card": card(
                row,
                bot_name=scope.bot.name,
                mention_open_id=str(scope.event.sender_open_id),
                requester=str(scope.event.sender_platform_user_id),
            )
        },
    )
    row.card_outbox_id = item.id if item is not None else None
    return Requested("access_requested", row.id)


@dataclass(frozen=True)
class Decision:
    """点击开启的这一轮该告诉 AI 员工什么。"""

    label: str
    urls: tuple[str, ...]
    shared: tuple[str, ...] = ()
    share_failed: tuple[str, ...] = ()

    def note(self) -> str:
        listed = "、".join(self.urls)
        if self.label == DECLINE:
            return (
                f"- 飞书文档放行：提问人拒绝了以他的身份读取 {listed}。"
                "不要再读取或请求读取这些文档，按现有信息回答或说明读不到。"
            )
        parts = [
            f"- 飞书文档放行：提问人同意以他的身份读取 {listed}（1 小时内、只在他提问时有效）。"
            "请用 coreman_feishu_docs 的工具读取后，继续回答他原来的问题。"
        ]
        if self.shared:
            parts.append("已把本群加为这些文档的可阅读协作者：" + "、".join(self.shared) + "。")
        if self.share_failed:
            parts.append(
                "共享给本群没有成功（多半是他没有分享权限）："
                + "、".join(self.share_failed)
                + "，仍可按本次放行读取。"
            )
        return "".join(parts)


async def _card_row(
    session: AsyncSession, scope: DocsScope, message_id: str, now: datetime
) -> FeishuDocConsent | None:
    """被点的卡片对应的待定放行：卡片消息 ID 从出站记录取，本群、本人、未过期。"""
    found = await session.scalars(
        select(FeishuDocConsent)
        .join(OutboxItem, OutboxItem.id == FeishuDocConsent.card_outbox_id)
        .where(
            FeishuDocConsent.bot_id == scope.bot.id,
            FeishuDocConsent.chat_id == scope.chat_id,
            FeishuDocConsent.status == "pending",
            FeishuDocConsent.expires_at > now,
            OutboxItem.bot_id == scope.bot.id,
            OutboxItem.payload["_feishu_message_id"].astext == message_id,
        )
        .with_for_update(of=FeishuDocConsent)
        .execution_options(populate_existing=True)
        .limit(1)
    )
    return found.first()


async def _share(
    session: AsyncSession, cipher: Cipher, who: access.Requester, scope: DocsScope, url: str
) -> bool:
    """以本人身份把本群加为可阅读协作者；不通知。"""
    link = links.parse(url)
    try:
        await Call(session, cipher, who.scope)(
            access.SHARE_ENDPOINT,
            path={"token": link.token},
            params={"type": link.permission_type, "need_notification": "false"},
            json={
                "member_type": "openchat",
                "member_id": scope.chat_id,
                "perm": "view",
                "type": "chat",
            },
        )
    except PersonalError:
        return False
    return True


async def apply_click(
    session: AsyncSession,
    cipher: Cipher,
    scope: DocsScope,
    event: InboundEvent,
    *,
    now: datetime | None = None,
) -> Decision | None:
    """这一轮是提问人在放行卡片上的点击：记下决定并返回给 AI 员工的说明；否则返回 None。"""
    meta = clicked(event.reply_context or {})
    if meta is None or not is_click(event.payload or {}) or scope.speaker_user_id is None:
        return None
    label, message_id = meta.get("label"), meta.get("message_id")
    if label not in LABELS or not isinstance(message_id, str) or not message_id:
        return None
    if meta.get("requester") != event.sender_platform_user_id:
        return None
    now = now or utcnow()
    row = await _card_row(session, scope, message_id, now)
    if row is None or row.user_id != scope.speaker_user_id:
        return None
    if label == SHARE and not row.share_offered:
        return None
    decision = Decision(label, tuple(row.urls))
    if label == DECLINE:
        row.status = "declined"
    else:
        row.status, row.expires_at = "approved", now + APPROVED_TTL
        if label == SHARE:
            who = await access.requester(session, scope)
            shared: list[str] = []
            failed: list[str] = []
            for url in row.urls:
                ok = who is not None and await _share(session, cipher, who, scope, url)
                (shared if ok else failed).append(url)
            row.shared = bool(shared)
            decision = Decision(label, tuple(row.urls), tuple(shared), tuple(failed))
    row.decided_at = now
    await record_audit(
        session,
        action="feishu_doc_consent." + row.status,
        actor_id=scope.speaker_user_id,
        actor_login=None,
        target_type="bot",
        target_id=str(scope.bot.id),
        diff={"chat_id": scope.chat_id, "tokens": list(row.tokens), "shared": row.shared},
    )
    return decision


async def expire_due(session: AsyncSession, now: datetime) -> int:
    result = await session.execute(
        update(FeishuDocConsent)
        .where(FeishuDocConsent.status.in_(("pending", "approved")))
        .where(FeishuDocConsent.expires_at < now)
        .values(status="expired")
        .returning(FeishuDocConsent.id)
        .execution_options(synchronize_session=False)
    )
    return len(result.all())


async def cleanup(session: AsyncSession, now: datetime, *, limit: int = 500) -> int:
    doomed = (
        select(FeishuDocConsent.id)
        .where(FeishuDocConsent.created_at < now - RETENTION)
        .order_by(FeishuDocConsent.created_at)
        .limit(limit)
        .scalar_subquery()
    )
    result = await session.execute(
        delete(FeishuDocConsent)
        .where(FeishuDocConsent.id.in_(doomed))
        .returning(FeishuDocConsent.id)
    )
    return len(result.all())
