"""提问人的飞书授权还差一步时，记下这一轮；授权完成后自动续接，不用他再发一遍。

飞书授权是设备码流程：用户点完同意后，要有人来查一次结果才会落库。私聊里由 AI 员工
核验，群里没人会去查，所以看护进程替这些等待中的提问人查，查到已连接就续接原来那条消息。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import tasks
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, FeishuDocConsent, FeishuPersonalGrant, Task
from coreman.core.feishu_docs.links import Link
from coreman.core.feishu_docs.policy import DocsScope
from coreman.core.feishu_personal import policy, service
from coreman.core.feishu_personal.service import PersonalError
from coreman.core.timeutils import utcnow

AWAIT_TTL = timedelta(minutes=30)
BATCH = 20
PAYLOAD_KEY = "feishu_docs_resume"


async def wait_for_authorization(
    session: AsyncSession, scope: DocsScope, link: Link, *, now: datetime | None = None
) -> None:
    """本群本人只留一条等待：同一段时间里问的几篇文档并到一起，续接最近这一轮。"""
    if scope.speaker_user_id is None:
        return
    now = now or utcnow()
    row = (
        await session.scalars(
            select(FeishuDocConsent)
            .where(
                FeishuDocConsent.bot_id == scope.bot.id,
                FeishuDocConsent.chat_id == scope.chat_id,
                FeishuDocConsent.user_id == scope.speaker_user_id,
                FeishuDocConsent.status == "awaiting_auth",
                FeishuDocConsent.expires_at > now,
            )
            .with_for_update()
            .limit(1)
        )
    ).first()
    if row is None:
        session.add(
            FeishuDocConsent(
                bot_id=scope.bot.id,
                user_id=scope.speaker_user_id,
                chat_id=scope.chat_id,
                tokens=[link.token],
                urls=[link.url],
                status="awaiting_auth",
                share_offered=False,
                shared=False,
                origin_task_id=scope.task.id,
                expires_at=now + AWAIT_TTL,
            )
        )
        await session.flush()
        return
    if link.token not in row.tokens:
        row.tokens, row.urls = [*row.tokens, link.token], [*row.urls, link.url]
    row.origin_task_id, row.expires_at = scope.task.id, now + AWAIT_TTL


async def _poll(
    session: AsyncSession, cipher: Cipher, row: FeishuDocConsent, origin: Task, now: datetime
) -> None:
    """授权进行中且到了该查的时候，查一次飞书的授权结果。"""
    grant = await session.get(
        FeishuPersonalGrant, (row.bot_id, row.user_id), populate_existing=True
    )
    if (
        grant is None
        or grant.status != "pending"
        or not grant.pending_expires_at
        or grant.pending_expires_at <= now
        or (grant.next_poll_at is not None and grant.next_poll_at > now)
    ):
        return
    bot = await session.get(Bot, row.bot_id)
    if bot is None:
        return
    scope = policy.Scope(
        origin,
        bot,
        row.user_id,
        grant.tenant_key,
        grant.app_id,
        grant.open_id,
        grant.platform_user_id,
        row.chat_id,
        None,
    )
    try:
        await service.authorization_status(session, cipher, scope)
    except (PersonalError, ValueError):
        return


async def tick(session: AsyncSession, now: datetime, cipher: Cipher) -> int:
    """替等待中的提问人查授权结果；已连接的续接原来那一轮。返回续接的条数。"""
    rows = list(
        await session.scalars(
            select(FeishuDocConsent)
            .where(
                FeishuDocConsent.status == "awaiting_auth",
                FeishuDocConsent.expires_at > now,
            )
            .order_by(FeishuDocConsent.created_at)
            .with_for_update(skip_locked=True)
            .limit(BATCH)
        )
    )
    resumed = 0
    for row in rows:
        origin = await session.get(Task, row.origin_task_id) if row.origin_task_id else None
        if origin is None or origin.inbound_event_id is None:
            row.status = "expired"
            continue
        await _poll(session, cipher, row, origin, now)
        grant = await session.get(FeishuPersonalGrant, (row.bot_id, row.user_id))
        if grant is None or grant.status != "connected" or not grant.token_enc:
            continue
        payload = {key: value for key, value in origin.payload.items() if key != "doc_links"}
        task = await tasks.enqueue(
            session,
            tasks.NewTask(
                bot_id=origin.bot_id,
                kind="chat",
                user_id=origin.user_id,
                session_key=origin.session_key,
                inbound_event_id=origin.inbound_event_id,
                dedupe_key=f"feishu-docs:{row.id}:resume",
                payload={
                    **payload,
                    PAYLOAD_KEY: str(row.id),
                    # 来源那一轮可能还没结束：同一会话串行，等它收尾再续接。
                    "serialize_session": True,
                },
            ),
        )
        row.status, row.decided_at = "resumed", now
        resumed += task is not None
    return resumed
