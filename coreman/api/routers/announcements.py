"""公告管理（仅 ai_committee / platform_admin 可用）。

公告是运维数据（几十条封顶），列表不分页，一次把 relay / bot 的名字一起 join 出来，
省得前端再为每行发一次请求。`time_status` 只看时间窗，不看 `is_active`——
「停用了但还在窗口里」和「启用了但还没到点」是两件事，管理台要分别看得见。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from coreman.api.deps import client_ip, get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.permissions import require_roles
from coreman.api.security import verify_csrf
from coreman.core.audit import diff_dict, record_audit
from coreman.core.db.models import Announcement, Bot, RelayServer, User
from coreman.core.timeutils import aware_utc

router = APIRouter(
    prefix="/api/admin/announcements", tags=["announcements"], dependencies=[Depends(verify_csrf)]
)
# B008：同 teams，require_roles(...) 不能写进参数默认值里，挪成模块级单例。
_MANAGERS = require_roles("ai_committee", "platform_admin")
Scope = Literal["global", "relay", "bot"]
# scope → (要有 relay_server_id, 要有 bot_id)；与库里的 CHECK `scope_target` 同一口径。
_SCOPE_TARGETS: dict[str, tuple[bool, bool]] = {
    "global": (False, False),
    "relay": (True, False),
    "bot": (False, True),
}


class AnnouncementIn(BaseModel):
    scope: Scope
    relay_server_id: uuid.UUID | None = None
    bot_id: uuid.UUID | None = None
    content: str = Field(min_length=1, max_length=5000)
    is_active: bool = True
    start_at: datetime | None = None
    end_at: datetime | None = None

    # 字段级先归一，`_consistent` 里的先后比较才不会拿 naive 和 aware 相比——那会抛
    # TypeError，Pydantic 不把它转成 422，会一路掉进 500；naive 直接入库也会被
    # asyncpg 按进程本地时区解释，悄悄偏掉几个小时。
    @field_validator("start_at", "end_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else aware_utc(value)

    @model_validator(mode="after")
    def _consistent(self) -> AnnouncementIn:
        wants = _SCOPE_TARGETS[self.scope]
        if (self.relay_server_id is not None, self.bot_id is not None) != wants:
            raise ValueError("公告范围与目标不匹配")
        if self.start_at and self.end_at and self.end_at < self.start_at:
            raise ValueError("结束时间不能早于开始时间")
        return self


def time_status(row: Announcement, now: datetime) -> str:
    """只由时间窗推导：还没开始 pending，已经过期 expired，其余 active。"""
    if row.start_at is not None and row.start_at > now:
        return "pending"
    if row.end_at is not None and row.end_at < now:
        return "expired"
    return "active"


def _out(
    row: Announcement, relay: RelayServer | None, bot: Bot | None, now: datetime
) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "scope": row.scope,
        "relay_server_id": str(row.relay_server_id) if row.relay_server_id else None,
        "relay_name": relay.name if relay else None,
        "bot_id": str(row.bot_id) if row.bot_id else None,
        "bot_key": bot.bot_key if bot else None,
        "bot_name": bot.name if bot else None,
        "content": row.content,
        "is_active": row.is_active,
        "start_at": row.start_at,
        "end_at": row.end_at,
        "time_status": time_status(row, now),
        "created_by": str(row.created_by) if row.created_by else None,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _snapshot(row: Announcement) -> dict[str, Any]:
    """审计 diff 用的可比较快照：时间统一成 ISO 串，UUID 统一成字符串。"""
    return {
        "scope": row.scope,
        "relay_server_id": str(row.relay_server_id) if row.relay_server_id else None,
        "bot_id": str(row.bot_id) if row.bot_id else None,
        "content": row.content,
        "is_active": row.is_active,
        "start_at": row.start_at.isoformat() if row.start_at else None,
        "end_at": row.end_at.isoformat() if row.end_at else None,
    }


async def _check_targets(session: AsyncSession, body: AnnouncementIn) -> None:
    """目标必须真实存在：外键错会变成 500，这里提前拦成 422。"""
    if (
        body.relay_server_id is not None
        and await session.get(RelayServer, body.relay_server_id) is None
    ):
        raise ApiError(422, 422, "目标运行时不存在")
    if body.bot_id is not None and await session.get(Bot, body.bot_id) is None:
        raise ApiError(422, 422, "目标机器人不存在")


async def _load(session: AsyncSession, ann_id: uuid.UUID) -> Announcement:
    row = await session.get(Announcement, ann_id)
    if row is None:
        raise not_found("公告不存在")
    return row


async def _one(session: AsyncSession, row: Announcement) -> dict[str, Any]:
    relay = await session.get(RelayServer, row.relay_server_id) if row.relay_server_id else None
    bot = await session.get(Bot, row.bot_id) if row.bot_id else None
    return _out(row, relay, bot, datetime.now(UTC))


async def _audit(
    session: AsyncSession,
    request: Request,
    actor: User,
    action: str,
    row_id: uuid.UUID,
    before: dict[str, Any],
    after: dict[str, Any],
) -> None:
    await record_audit(
        session,
        action=action,
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="announcement",
        target_id=str(row_id),
        diff=diff_dict(before, after),
        ip=client_ip(request),
    )


@router.get("")
async def list_announcements(
    _: User = Depends(_MANAGERS), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    relay, bot = aliased(RelayServer), aliased(Bot)
    stmt = (
        select(Announcement, relay, bot)
        .outerjoin(relay, relay.id == Announcement.relay_server_id)
        .outerjoin(bot, bot.id == Announcement.bot_id)
        # updated_at 会并列（同一批次改的），补 id 兜底保证顺序稳定。
        .order_by(Announcement.is_active.desc(), Announcement.updated_at.desc(), Announcement.id)
    )
    now = datetime.now(UTC)
    rows = (await session.execute(stmt)).all()
    return {"code": 0, "data": [_out(a, r, b, now) for a, r, b in rows]}


@router.post("", status_code=201)
async def create_announcement(
    body: AnnouncementIn,
    request: Request,
    actor: User = Depends(_MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _check_targets(session, body)
    row = Announcement(**body.model_dump(), created_by=actor.id)
    session.add(row)
    await session.flush()
    await _audit(session, request, actor, "announcement.create", row.id, {}, _snapshot(row))
    await session.commit()
    await session.refresh(row)
    return {"code": 0, "data": await _one(session, row)}


@router.put("/{ann_id}")
async def update_announcement(
    ann_id: uuid.UUID,
    body: AnnouncementIn,
    request: Request,
    actor: User = Depends(_MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    row = await _load(session, ann_id)
    await _check_targets(session, body)
    before = _snapshot(row)
    for key, value in body.model_dump().items():
        setattr(row, key, value)
    await _audit(session, request, actor, "announcement.update", row.id, before, _snapshot(row))
    await session.commit()
    await session.refresh(row)
    return {"code": 0, "data": await _one(session, row)}


@router.post("/{ann_id}/toggle")
async def toggle_announcement(
    ann_id: uuid.UUID,
    request: Request,
    actor: User = Depends(_MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    row = await _load(session, ann_id)
    before = _snapshot(row)
    row.is_active = not row.is_active
    await _audit(session, request, actor, "announcement.toggle", row.id, before, _snapshot(row))
    await session.commit()
    await session.refresh(row)
    return {"code": 0, "data": await _one(session, row)}


@router.delete("/{ann_id}")
async def delete_announcement(
    ann_id: uuid.UUID,
    request: Request,
    actor: User = Depends(_MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    row = await _load(session, ann_id)
    before = _snapshot(row)
    await session.delete(row)
    # 用入参 ann_id 记审计：row.id 在 delete 之后就过期了，再读会触发同步 IO。
    await _audit(session, request, actor, "announcement.delete", ann_id, before, {})
    await session.commit()
    return {"code": 0, "data": None}
