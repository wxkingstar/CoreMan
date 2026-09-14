"""应用通知落 outbox；目标在入队和实际发送前各验证一次。"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.crypto import Cipher
from coreman.core.db.models import Escalation, OutboxItem, PlatformApp, User, UserIdentity
from coreman.core.platforms.feishu import FeishuClient
from coreman.core.platforms.wecom import WeComClient


class NotificationError(Exception):
    pass


class NotificationSkipped(NotificationError):
    pass


async def escalation_current(session: AsyncSession, item: OutboxItem) -> None:
    from coreman.core.escalations.service import is_expired
    from coreman.core.timeutils import utcnow

    identity = item.target.get("escalation_id")
    if not identity:
        return
    row = await session.scalar(
        select(Escalation).where(Escalation.escalation_id == identity).with_for_update()
    )
    phase = item.target.get("escalation_phase")
    if row is None:
        raise NotificationSkipped("求助记录不存在")
    if isinstance(phase, str) and phase.startswith("media_failed_"):
        if row.status not in {"pending", "replied"} or is_expired(row, utcnow()):
            raise NotificationSkipped("求助已结束")
        return
    if phase in {"completed", "cancelled", "expired"}:
        if row.status != phase:
            raise NotificationSkipped("求助状态已改变")
        return
    if (
        row.status != "pending"
        or is_expired(row, utcnow())
        or row.rounds != item.target.get("escalation_round")
    ):
        raise NotificationSkipped("求助已结束、收到回复或进入下一轮")
    if phase in {"nudge1", "nudge2"}:
        sent = await session.scalar(
            select(OutboxItem.id).where(
                OutboxItem.dedupe_key == f"escalation:{identity}:ask:0:0",
                OutboxItem.status == "sent",
            )
        )
        if row.replies or sent is None or phase != f"nudge{row.nudge_stage}":
            raise NotificationSkipped("催办状态已失效")


async def send_notification(session: AsyncSession, item: OutboxItem, cipher: Cipher) -> None:
    if key := item.target.get("alert_key"):
        from coreman.core.db.models import AlertState, Setting

        state = await session.get(AlertState, key)
        receiver = await session.get(User, uuid.UUID(item.target["user_id"]))
        config = await session.get(Setting, "alert_channels")
        binding = {
            "platform_app_id": item.target["platform_app_id"],
            "user_id": item.target["user_id"],
        }
        if (
            state is None
            or state.generation != item.target.get("alert_generation")
            or state.firing == bool(item.target.get("alert_recovered"))
            or receiver is None
            or receiver.role not in {"platform_admin", "ai_committee"}
            or config is None
            or binding not in config.value.get("channels", [])
        ):
            raise NotificationSkipped("告警已恢复、进入新事件或接收配置已变更")
    await escalation_current(session, item)
    if identity := item.target.get("skill_approval_id"):
        from coreman.core.db.models import SkillApproval

        approval = await session.get(SkillApproval, uuid.UUID(identity))
        reviewer = await session.get(User, uuid.UUID(item.target["user_id"]))
        if (
            approval is None
            or approval.status != "pending"
            or reviewer is None
            or reviewer.role != "ai_committee"
        ):
            raise NotificationSkipped("技能审批已处理或审核权限已变更")
    channel = item.target.get("channel")
    if channel:
        from coreman.core.notification_channels import send_email, send_webhook

        if channel == "webhook":
            await send_webhook(item, cipher)
        elif channel == "email":
            await send_email(session, item, cipher)
        else:
            raise NotificationError("未知通知渠道")
        return
    app = await session.get(PlatformApp, uuid.UUID(item.target["platform_app_id"]))
    user = await session.get(User, uuid.UUID(item.target["user_id"]))
    if app is None or not app.enabled or "notify" not in app.capabilities:
        raise NotificationError("通知应用已停用或撤销通知能力")
    if user is None or user.status != "active" or user.source == "bootstrap":
        raise NotificationError("接收用户已停用或不存在")
    identity = (
        await session.execute(
            select(UserIdentity).where(
                UserIdentity.user_id == user.id,
                UserIdentity.platform == app.platform,
                UserIdentity.platform_user_id == item.target["platform_user_id"],
            )
        )
    ).scalar_one_or_none()
    if identity is None:
        raise NotificationError("接收用户绑定已变更")
    if app.platform == "feishu":
        if not app.app_id:
            raise NotificationError("飞书应用缺少 app_id")
        feishu = FeishuClient(
            app.app_id, cipher.decrypt(app.secret_enc, "platform_apps.secret_enc")
        )
        try:
            message_id = await feishu.notify_user(
                identity.platform_user_id,
                item.payload["content"],
                key=f"notification:{item.id}",
                markdown=item.payload.get("msgtype") == "markdown",
            )
            item.payload = {**item.payload, "_feishu_message_id": message_id}
            if item.target.get("escalation_id") and item.target.get("escalation_phase") in {
                "ask",
                "followup",
            }:
                escalation = await session.scalar(
                    select(Escalation).where(
                        Escalation.escalation_id == item.target["escalation_id"]
                    )
                )
                if escalation:
                    escalation.notify_message_id = message_id
            await session.flush()
        finally:
            await feishu.aclose()
        return
    if app.platform != "wecom" or not app.corp_id or not app.app_id or not app.app_id.isdecimal():
        raise NotificationError("通知应用配置不完整或平台未支持")
    client = WeComClient(app.corp_id, cipher.decrypt(app.secret_enc, "platform_apps.secret_enc"))
    try:
        await client.send_message(
            agent_id=int(app.app_id),
            user_id=identity.platform_user_id,
            content=item.payload["content"],
            msgtype=item.payload["msgtype"],
        )
    finally:
        await client.aclose()
