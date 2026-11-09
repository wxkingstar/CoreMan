"""提问人的飞书授权还差一步时，等他完成后自动续接。私聊和群聊共用。

飞书授权是设备码流程：用户点完同意后要有人查一次才落库，看护进程替他查。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import tasks
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, FeishuAuthWait, FeishuPersonalGrant, Task
from coreman.core.feishu_personal import inflow, policy, service
from coreman.core.feishu_personal.service import PersonalError

AWAIT_TTL = inflow.WAIT_TTL
BATCH = 20
PAYLOAD_KEY = "feishu_auth_resume"
# 确认卡续接的标记，定义在这里免得和 confirm 互相导入。
CONFIRM_RESULT_KEY = "feishu_group_write_result"


async def _poll(
    session: AsyncSession, cipher: Cipher, row: FeishuAuthWait, origin: Task, now: datetime
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
    """替等待中的提问人查授权结果；已连接且档位够的续接原来那一轮。返回续接的条数。"""
    expired = await session.scalars(
        select(FeishuAuthWait)
        .where(FeishuAuthWait.status.in_(("offered", "waiting")), FeishuAuthWait.expires_at <= now)
        .with_for_update(skip_locked=True)
        .limit(BATCH)
    )
    for row in expired:
        row.status = "expired"
    rows = list(
        await session.scalars(
            select(FeishuAuthWait)
            .where(FeishuAuthWait.status == "waiting", FeishuAuthWait.expires_at > now)
            .order_by(FeishuAuthWait.created_at)
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
        if not inflow.satisfied(grant, row.need):
            continue
        task = await tasks.enqueue(
            session,
            tasks.NewTask(
                bot_id=origin.bot_id,
                kind="chat",
                user_id=origin.user_id,
                session_key=origin.session_key,
                inbound_event_id=origin.inbound_event_id,
                dedupe_key=f"feishu-auth:{row.id}:resume",
                payload={
                    **{k: v for k, v in origin.payload.items() if k != CONFIRM_RESULT_KEY},
                    PAYLOAD_KEY: str(row.id),
                    # 来源那一轮可能还没结束：同一会话串行，等它收尾再续接。
                    "serialize_session": True,
                },
            ),
        )
        row.status = "resumed"
        resumed += task is not None
    return resumed
