"""回调媒体异步下载，避免在平台回调窗口内等待文件传输。"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select

from coreman.core.bus import tasks
from coreman.core.config import get_settings
from coreman.core.db.models import Escalation, PlatformApp, Task, User
from coreman.core.escalations.service import notify_localized
from coreman.core.object_store import object_store
from coreman.core.platforms.feishu import FeishuClient
from coreman.core.platforms.wecom import WeComClient
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.context import TaskContext


class EscalationMediaHandler:
    kind = "escalation_media"

    async def run(self, ctx: TaskContext) -> None:
        heartbeat = asyncio.create_task(self._heartbeat(ctx))
        work = asyncio.create_task(self._download(ctx))
        stopping = asyncio.create_task(ctx.cancel_event.wait())
        try:
            async with asyncio.timeout(180):
                ready, _ = await asyncio.wait({work, stopping}, return_when=asyncio.FIRST_COMPLETED)
                if stopping in ready:
                    work.cancel()
                    await asyncio.gather(work, return_exceptions=True)
                    await self._fail(ctx, "cancelled")
                else:
                    await work
        except asyncio.CancelledError:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            await self._fail(ctx, "cancelled")
            raise
        except Exception as exc:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            await self._fail(ctx, type(exc).__name__)
        finally:
            heartbeat.cancel()
            work.cancel()
            stopping.cancel()
            await asyncio.gather(heartbeat, work, stopping, return_exceptions=True)

    async def _heartbeat(self, ctx: TaskContext) -> None:
        while True:
            await ctx.heartbeat()
            await asyncio.sleep(10)

    async def _download(self, ctx: TaskContext) -> None:
        payload = ctx.task.payload
        cfg = get_settings()
        store = object_store(cfg)
        async with ctx.session_factory() as session:
            app = await session.get(PlatformApp, uuid.UUID(payload["platform_app_id"]))
            user = await session.get(User, ctx.task.user_id) if ctx.task.user_id else None
            escalation = await session.scalar(
                select(Escalation).where(Escalation.escalation_id == payload["escalation_id"])
            )
            if (
                app is None
                or not app.enabled
                or "callback" not in app.capabilities
                or (app.platform == "wecom" and not app.corp_id)
                or (app.platform == "feishu" and not app.app_id)
                or user is None
                or user.status != "active"
                or escalation is None
                or escalation.platform_app_id != app.id
                or escalation.to_user_id != user.id
            ):
                raise ValueError("callback_owner_unavailable")
            secret = ctx.cipher.decrypt(app.secret_enc, "platform_apps.secret_enc")
            client = (
                FeishuClient(app.app_id or "", secret)
                if app.platform == "feishu"
                else WeComClient(app.corp_id or "", secret)
            )
            extension = {"image": ".jpg", "voice": ".amr", "video": ".mp4", "file": ".bin"}.get(
                payload["media_type"], ".bin"
            )
            try:
                source = (
                    client.media_stream(
                        payload["message_id"],
                        payload["media_id"],
                        kind="image" if payload["media_type"] == "image" else "file",
                    )
                    if isinstance(client, FeishuClient)
                    else client.media_stream(payload["media_id"])
                )
                try:
                    asset = await store.put(
                        session,
                        source,
                        filename=f"attachment{extension}",
                        content_type="application/octet-stream",
                        now=utcnow(),
                    )
                finally:
                    await source.aclose()
            finally:
                await client.aclose()
            # 下载完才拿状态行锁；并发媒体回复各自合并，不覆盖别的回复。
            escalation = await session.scalar(
                select(Escalation)
                .where(Escalation.escalation_id == payload["escalation_id"])
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            task = await session.scalar(
                select(Task).where(Task.id == ctx.task.id).with_for_update()
            )
            if task is not None and task.cancel_requested_at:
                raise asyncio.CancelledError()
            if task is None or task.status not in tasks.ACTIVE:
                await session.rollback()
                return
            if escalation is None:
                raise ValueError("escalation_removed")
            url = store.url(asset)
            escalation.replies = [
                {
                    **reply,
                    "content": (str(payload.get("caption") or "") + "\n" + url).strip(),
                    "media": {
                        "type": payload["media_type"],
                        "status": "ready",
                        "object_id": str(asset.id),
                        "url": url,
                        "expires_at": asset.expires_at.isoformat(),
                    },
                }
                if reply.get("message_id") == payload["message_id"]
                else reply
                for reply in escalation.replies
            ]
            await tasks.finish(
                session,
                ctx.task.id,
                status="succeeded",
                only_active=True,
                result={"object_id": str(asset.id)},
            )
            await session.commit()

    async def _fail(self, ctx: TaskContext, error: str) -> None:
        async with ctx.session_factory() as session:
            row = await session.scalar(
                select(Escalation)
                .where(Escalation.escalation_id == ctx.task.payload.get("escalation_id"))
                .with_for_update()
            )
            owned = await tasks.finish(
                session,
                ctx.task.id,
                status="cancelled" if error == "cancelled" else "failed",
                error_code=error,
                only_active=True,
            )
            if owned and row is not None:
                row.replies = [
                    {
                        **reply,
                        "content": "媒体下载失败，请用文字回复。",
                        "media": {
                            "type": ctx.task.payload.get("media_type"),
                            "status": "failed",
                            "error": error,
                        },
                    }
                    if reply.get("message_id") == ctx.task.payload.get("message_id")
                    else reply
                    for reply in row.replies
                ]
                if row.status in ("pending", "replied") and error != "cancelled":
                    await notify_localized(
                        session,
                        row,
                        "esc_media_failed",
                        f"media_failed_{ctx.task.id}",
                    )
            await session.commit()
