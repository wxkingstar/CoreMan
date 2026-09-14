"""用户通知及旧企微别名；成功表示已持久化入队。"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.infra_auth import require_scope
from coreman.core.db.models import ApiClient, OutboxItem, PlatformApp, User, UserIdentity
from coreman.runtime.bus import outbox

router = APIRouter(tags=["infra-notify"])
NOTIFY = require_scope("notify")


class NotifyIn(BaseModel):
    user_login: str | None = Field(default=None, min_length=1, max_length=100)
    platform_user_id: str | None = Field(default=None, min_length=1, max_length=100)
    wework_user_id: str | None = Field(default=None, min_length=1, max_length=100)
    platform: Literal["wecom", "feishu"] = "wecom"
    platform_app_id: uuid.UUID | None = None
    content: str = Field(min_length=1, max_length=2048)
    msgtype: Literal["text", "markdown"] = "markdown"
    request_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_target(self) -> NotifyIn:
        if sum(bool(v) for v in (self.user_login, self.platform_user_id, self.wework_user_id)) != 1:
            raise ValueError("请指定一个用户标识")
        if len(self.content.encode()) > 2048 or not self.content.strip():
            raise ValueError("正文须非空且不超过 2048 字节")
        for value in (self.platform_user_id, self.wework_user_id):
            if value and (value == "@all" or "|" in value):
                raise ValueError("用户通知只允许单个接收人")
        return self


@router.post("/api/infra/notify/user")
@router.post("/api/robot/wework-notify")
async def notify_user(
    body: NotifyIn,
    client: ApiClient = Depends(NOTIFY),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    query = (
        select(User, UserIdentity)
        .join(UserIdentity, UserIdentity.user_id == User.id)
        .where(
            User.status == "active",
            User.source != "bootstrap",
            UserIdentity.platform == body.platform,
        )
    )
    query = (
        query.where(User.login_name == body.user_login)
        if body.user_login
        else query.where(
            UserIdentity.platform_user_id == (body.platform_user_id or body.wework_user_id)
        )
    )
    recipient = (await session.execute(query)).one_or_none()
    if recipient is None:
        raise not_found("未找到已绑定平台的启用用户")
    user, identity = recipient
    apps_query = select(PlatformApp).where(
        PlatformApp.enabled,
        PlatformApp.platform == body.platform,
        PlatformApp.capabilities.contains(["notify"]),
    )
    if body.platform_app_id:
        apps_query = apps_query.where(PlatformApp.id == body.platform_app_id)
    apps = list((await session.execute(apps_query)).scalars())
    if len(apps) != 1:
        raise ApiError(409, 409, "请配置通知应用；存在多个时须指定 platform_app_id")
    app = apps[0]
    if not app.corp_id or not app.app_id or not app.app_id.isdecimal():
        raise ApiError(409, 409, "通知应用缺少企业 ID 或数字应用 ID")
    target = {
        "platform_app_id": str(app.id),
        "user_id": str(user.id),
        "platform_user_id": identity.platform_user_id,
    }
    payload = {"content": body.content, "msgtype": body.msgtype}
    key = f"notify:{client.app_key}:{body.request_id or uuid.uuid4().hex}"
    item = await outbox.add(
        session,
        bot_id=None,
        platform=body.platform,
        kind="notify",
        dedupe_key=key,
        target=target,
        payload=payload,
    )
    if item is None:
        item = (
            await session.execute(select(OutboxItem).where(OutboxItem.dedupe_key == key))
        ).scalar_one()
        if item.target != target or item.payload != payload:
            raise ApiError(409, 409, "request_id 已用于不同的通知内容")
    await session.commit()
    return {
        "code": 0,
        "success": True,
        "message": "通知已入队",
        "data": {"outbox_id": item.id, "status": item.status},
    }
