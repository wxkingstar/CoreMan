"""管理员配置告警收件人；只存已有应用和人员引用，不接受任意地址。"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip, get_session
from coreman.api.errors import ApiError
from coreman.api.permissions import require_roles
from coreman.api.security import verify_csrf
from coreman.api.versioning import require_if_match
from coreman.core.audit import record_audit
from coreman.core.db.models import AlertState, PlatformApp, Setting, User, UserIdentity

router = APIRouter(
    prefix="/api/admin/alert-settings", tags=["alerts"], dependencies=[Depends(verify_csrf)]
)
ADMIN = require_roles("platform_admin")


class Channel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform_app_id: uuid.UUID
    user_id: uuid.UUID


class AlertSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channels: list[Channel] = Field(max_length=20)


@router.get("")
async def get_alert_settings(
    _: User = Depends(ADMIN), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    row = await session.get(Setting, "alert_channels")
    return {"code": 0, "data": row.value if row else {"version": 0, "channels": []}}


@router.put("")
async def put_alert_settings(
    body: AlertSettingsIn,
    request: Request,
    actor: User = Depends(ADMIN),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await session.execute(text("SELECT pg_advisory_xact_lock(721309130017)"))
    row = await session.get(Setting, "alert_channels", populate_existing=True)
    old: dict[str, Any] = row.value if row else {"version": 0, "channels": []}
    require_if_match(request, int(old["version"]))
    keys = {(item.platform_app_id, item.user_id) for item in body.channels}
    if len(keys) != len(body.channels):
        raise ApiError(422, 422, "告警接收渠道不能重复")
    for item in body.channels:
        app = await session.get(PlatformApp, item.platform_app_id)
        user = await session.get(User, item.user_id)
        if not app or not app.enabled or "notify" not in app.capabilities:
            raise ApiError(422, 422, "请选择启用的通知应用")
        if (
            not user
            or user.status != "active"
            or user.source == "bootstrap"
            or user.role not in {"platform_admin", "ai_committee"}
        ):
            raise ApiError(422, 422, "告警接收人须为已同步且启用的管理员或 AI 委员会成员")
        if not await session.scalar(
            select(UserIdentity.id).where(
                UserIdentity.user_id == user.id, UserIdentity.platform == app.platform
            )
        ):
            raise ApiError(422, 422, "告警接收人未绑定所选平台")
    value = {
        "version": int(old["version"]) + 1,
        "channels": body.model_dump(mode="json")["channels"],
    }
    if row:
        row.value = value
        row.updated_by = actor.id
    else:
        session.add(Setting(key="alert_channels", value=value, updated_by=actor.id))
    if old["channels"] != value["channels"]:
        await session.execute(
            update(AlertState)
            .where(AlertState.active)
            .values(firing=False, generation=AlertState.generation + 1)
        )
    await record_audit(
        session,
        actor_id=actor.id,
        actor_login=actor.login_name,
        action="settings.alert_channels.update",
        target_type="settings",
        target_id="alert_channels",
        diff={"channel_count": len(keys)},
        ip=client_ip(request),
    )
    await session.commit()
    return {"code": 0, "data": value}
