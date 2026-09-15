"""通知消费者在事务中持行锁；进程退出自动回滚领取，后续实例可重试。"""

import asyncio

import aiosmtplib
import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.bus import outbox
from coreman.core.crypto import Cipher
from coreman.core.db.models import OutboxItem
from coreman.core.notifications import NotificationError, NotificationSkipped, send_notification
from coreman.core.observability.metrics import OUTBOX_FAILED, after_commit
from coreman.core.platforms.feishu import FeishuError
from coreman.core.platforms.wecom import WeComError


async def deliver_one(factory: async_sessionmaker[AsyncSession], cipher: Cipher) -> bool:
    async with factory() as session:
        async with session.begin():
            item = (
                await session.execute(
                    select(OutboxItem)
                    .where(
                        OutboxItem.kind == "notify",
                        OutboxItem.status == "pending",
                        OutboxItem.not_before <= func.now(),
                    )
                    .order_by(OutboxItem.id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if item is None:
                return False
            try:
                async with asyncio.timeout(25):
                    await send_notification(session, item, cipher)
            except FeishuError as exc:
                if exc.code in {230013, -2}:
                    item.status, item.last_error = "failed", str(exc)
                    item.attempts += 1
                    after_commit(session, OUTBOX_FAILED.inc)
                else:
                    await outbox.mark_failed(session, item.id, str(exc))
            except NotificationSkipped as exc:
                await outbox.mark_skipped(session, item.id, str(exc))
            except (
                NotificationError,
                aiosmtplib.SMTPException,
                OSError,
                WeComError,
                httpx.HTTPError,
                TimeoutError,
                ValueError,
                KeyError,
            ) as exc:
                # 平台 errmsg/HTTP URL 可能包含凭证或业务内容，持久化只保留类别及错误码。
                error = str(exc) if isinstance(exc, NotificationError) else type(exc).__name__
                if isinstance(exc, httpx.HTTPStatusError):
                    error += f" (HTTP {exc.response.status_code})"
                if isinstance(exc, WeComError):
                    error += f" ({exc.errcode})"
                await outbox.mark_failed(session, item.id, error)
            else:
                await outbox.mark_sent(session, item.id)
            return True
