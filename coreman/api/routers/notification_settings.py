"""通知凭证设置：只给平台管理员，密码加密且不进入返回值和审计。"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip, get_session
from coreman.api.errors import ApiError
from coreman.api.permissions import require_roles
from coreman.api.security import verify_csrf
from coreman.api.versioning import require_if_match
from coreman.core.audit import record_audit
from coreman.core.db.models import Setting, User
from coreman.core.relay.safe_transport import validate_host

router = APIRouter(
    prefix="/api/admin/notification-settings",
    tags=["notifications"],
    dependencies=[Depends(verify_csrf)],
)
ADMIN = require_roles("platform_admin")


class SmtpIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    host: str = Field(min_length=1, max_length=253, pattern=r"^[a-zA-Z0-9.:-]+$")
    port: int = Field(default=465, ge=1, le=65535)
    security: Literal["tls", "starttls"] = "tls"
    username: str = Field(default="", max_length=254)
    password: str | None = Field(default=None, max_length=4096)
    sender: str = Field(min_length=3, max_length=254)

    @field_validator("host")
    @classmethod
    def host_allowed(cls, value: str) -> str:
        validate_host(value)
        return value

    @field_validator("sender")
    @classmethod
    def sender_allowed(cls, value: str) -> str:
        if value.count("@") != 1 or any(c in value for c in "\r\n<> ,;\x00"):
            raise ValueError("请输入单一邮件地址")
        return value


def _out(row: Setting | None) -> dict[str, Any]:
    if row is None:
        return {
            "version": 0,
            "enabled": False,
            "host": "",
            "port": 465,
            "security": "tls",
            "username": "",
            "sender": "",
            "has_password": False,
        }
    return {k: v for k, v in row.value.items() if k != "password_enc"} | {
        "has_password": bool(row.value.get("password_enc"))
    }


@router.get("")
async def get_settings(
    _: User = Depends(ADMIN), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    return {"code": 0, "data": _out(await session.get(Setting, "notification_smtp"))}


@router.put("")
async def put_settings(
    body: SmtpIn,
    request: Request,
    actor: User = Depends(ADMIN),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    # 包括第一次建行也串行；与值和审计同一事务，避免丢失密码更新。
    await session.execute(text("SELECT pg_advisory_xact_lock(721309130008)"))
    row = await session.get(Setting, "notification_smtp", populate_existing=True)
    old = row.value if row else {}
    require_if_match(request, int(old.get("version", 0)))
    values = body.model_dump(exclude={"password"})
    values["password_enc"] = (
        request.app.state.cipher.encrypt(body.password, "notifications.smtp_password")
        if body.password
        else old.get("password_enc", "")
    )
    if body.enabled and body.username and not values["password_enc"]:
        raise ApiError(422, 422, "启用认证时需要 SMTP 密码")
    if (
        not body.password
        and old.get("password_enc")
        and any(old.get(k) != values[k] for k in ("host", "port", "username"))
    ):
        raise ApiError(422, 422, "更换认证目标时请重新填写密码")
    values["version"] = int(old.get("version", 0)) + 1
    if row is None:
        row = Setting(key="notification_smtp", value=values, updated_by=actor.id)
        session.add(row)
    else:
        row.value = values
        row.updated_by = actor.id
    await record_audit(
        session,
        actor_id=actor.id,
        actor_login=actor.login_name,
        action="notifications.configure",
        target_type="settings",
        target_id="notification_smtp",
        diff={
            "enabled": body.enabled,
            "password_changed": bool(body.password),
            "version": values["version"],
        },
        ip=client_ip(request),
    )
    await session.commit()
    return {"code": 0, "data": _out(row)}
