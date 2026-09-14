"""失联/取消的媒体任务不能让回复永久停留在 pending。"""

from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import Escalation, Task
from coreman.core.escalations.service import notify_localized
from coreman.runtime.bus.tasks import OPEN


async def recover(session: AsyncSession, now: datetime) -> int:
    rows = list(
        await session.scalars(
            select(Escalation)
            .where(
                text("""EXISTS (
                SELECT 1 FROM jsonb_array_elements(escalations.replies) AS reply
                JOIN tasks ON tasks.kind = 'escalation_media'
                  AND tasks.payload->>'escalation_id' = escalations.escalation_id
                  AND tasks.payload->>'message_id' = reply->>'message_id'
                WHERE reply->'media'->>'status' = 'pending'
                  AND tasks.status NOT IN ('queued','claimed','running')
            )""")
            )
            .order_by(Escalation.id)
            .limit(100)
            .with_for_update(skip_locked=True)
        )
    )
    count = 0
    for row in rows:
        replies = []
        changed = False
        for reply in row.replies:
            if reply.get("media", {}).get("status") == "pending":
                task = await session.scalar(
                    select(Task).where(
                        Task.kind == "escalation_media",
                        Task.payload["escalation_id"].astext == row.escalation_id,
                        Task.payload["message_id"].astext == reply.get("message_id"),
                    )
                )
                if task is not None and task.status not in OPEN:
                    reply = {
                        **reply,
                        "content": "媒体处理未完成，请重新发送或用文字回复。",
                        "media": {
                            "type": reply["media"].get("type"),
                            "status": "failed",
                            "error": task.error_code or task.status,
                        },
                    }
                    changed = True
                    if task.status != "cancelled" and row.status in ("pending", "replied"):
                        await notify_localized(
                            session,
                            row,
                            "esc_media_interrupted",
                            f"media_failed_{task.id}",
                        )
            replies.append(reply)
        if changed:
            row.replies, row.updated_at = replies, now
            count += 1
    return count
