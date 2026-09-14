"""审计日志查询（spec §5.3 audit_logs）：只读列表，ai_committee / platform_admin 可见。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import get_session
from coreman.api.pagination import PageParams, paginate
from coreman.api.permissions import require_roles
from coreman.api.security import verify_csrf
from coreman.core.db.models import AuditLog, User
from coreman.core.timeutils import aware_utc

router = APIRouter(
    prefix="/api/admin/audit-logs", tags=["audit-logs"], dependencies=[Depends(verify_csrf)]
)
# B008：同 platform_apps，require_roles(...) 不能写进参数默认值里，挪成模块级单例。
_READERS = require_roles("ai_committee", "platform_admin")


def escape_like(value: str) -> str:
    """LIKE/ILIKE 通配符转义：用户搜 `%`、`_` 时按字面匹配，不当通配符。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _as_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def audit_out(row: AuditLog) -> dict[str, Any]:
    return {
        "id": row.id,
        "actor_id": str(row.actor_id) if row.actor_id else None,
        "actor_login": row.actor_login,
        "action": row.action,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "diff": row.diff,
        # asyncpg 把 INET 读成 ipaddress 对象，直接进 JSON 会 500。
        "ip": str(row.ip) if row.ip else None,
        "created_at": row.created_at.isoformat(),
    }


@router.get("")
async def list_audit_logs(
    actor: str | None = None,
    action: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    _: User = Depends(_READERS),
    params: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    # id 自增且不会并列，比 created_at 更稳（同一请求里的多条审计时间戳相同）。
    stmt = select(AuditLog).order_by(AuditLog.id.desc())
    if actor:
        like = AuditLog.actor_login.ilike(f"%{escape_like(actor)}%", escape="\\")
        actor_id = _as_uuid(actor)
        # 合法 uuid 既可能是 actor_id，也可能是有人把 uuid 当登录名搜，两边都试。
        stmt = stmt.where(or_(AuditLog.actor_id == actor_id, like) if actor_id else like)
    if action:
        # 前缀匹配：`bot.` 命中所有 bot 动作。
        stmt = stmt.where(AuditLog.action.like(f"{escape_like(action)}%", escape="\\"))
    if target_type:
        stmt = stmt.where(AuditLog.target_type == target_type)
    if target_id:
        stmt = stmt.where(AuditLog.target_id == target_id)
    if since is not None:
        stmt = stmt.where(AuditLog.created_at >= aware_utc(since))
    if until is not None:
        stmt = stmt.where(AuditLog.created_at <= aware_utc(until))
    page = await paginate(session, stmt, params)
    rows: list[AuditLog] = page["items"]
    return {"code": 0, "data": {**page, "items": [audit_out(r) for r in rows]}}
