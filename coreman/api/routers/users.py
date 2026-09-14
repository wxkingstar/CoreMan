"""用户管理（spec §10.2、§10.3 团队与用户页）。"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from coreman.api.deps import client_ip, current_user, get_session
from coreman.api.errors import ApiError, forbidden, not_found
from coreman.api.pagination import PageParams, paginate
from coreman.api.permissions import MANUAL_TRACKED, can_edit_user
from coreman.api.security import verify_csrf
from coreman.core.audit import diff_dict, record_audit
from coreman.core.db.models import Department, Team, User, UserDepartment

router = APIRouter(prefix="/api/admin/users", tags=["users"], dependencies=[Depends(verify_csrf)])
_NOT_NULLABLE = ("role", "locale", "bot_accessible", "status")


class UserPatch(BaseModel):
    """全部可选；用 model_fields_set 区分「未传」与「显式 null」。

    team_id/position/skills 允许显式传 null；role/locale/bot_accessible/status 不允许。
    """

    team_id: uuid.UUID | None = None
    role: Literal["platform_admin", "ai_committee", "team_lead", "member"] | None = None
    locale: Literal["zh", "ja", "en"] | None = None
    bot_accessible: bool | None = None
    position: str | None = Field(default=None, max_length=200)
    skills: str | None = Field(default=None, max_length=2000)
    status: Literal["active", "disabled"] | None = None

    def changes(self) -> dict[str, Any]:
        return self.model_dump(include=self.model_fields_set)

    @model_validator(mode="after")
    def _non_empty(self) -> UserPatch:
        if not self.model_fields_set:
            raise ValueError("至少修改一个字段")
        for k in _NOT_NULLABLE:
            if k in self.model_fields_set and getattr(self, k) is None:
                raise ValueError(f"{k} 不能为空")
        return self


def user_out(u: User, team_name: str | None, dept_paths: list[str]) -> dict[str, Any]:
    return {
        "id": str(u.id),
        "login_name": u.login_name,
        "display_name": u.display_name,
        "email": u.email,
        "mobile": u.mobile,
        "avatar_url": u.avatar_url,
        "status": u.status,
        "locale": u.locale,
        "role": u.role,
        "source": u.source,
        "team_id": str(u.team_id) if u.team_id else None,
        "team_name": team_name,
        "position": u.position,
        "skills": u.skills,
        "bot_accessible": u.bot_accessible,
        "manual_fields": list(u.manual_fields or []),
        "last_login_at": u.last_login_at,
        "identities": [
            {"platform": i.platform, "platform_user_id": i.platform_user_id}
            for i in sorted(u.identities, key=lambda i: i.platform)
        ],
        "departments": dept_paths,
    }


async def _dept_paths(
    session: AsyncSession, user_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    if not user_ids:
        return {}
    rows = await session.execute(
        select(UserDepartment.user_id, Department.path)
        .join(Department, Department.id == UserDepartment.department_id)
        .where(UserDepartment.user_id.in_(user_ids))
        .order_by(UserDepartment.is_primary.desc(), Department.path)
    )
    out: dict[uuid.UUID, list[str]] = {}
    for uid, path in rows:
        out.setdefault(uid, []).append(path)
    return out


async def _team_names(session: AsyncSession) -> dict[uuid.UUID, str]:
    return {t.id: t.name_zh for t in (await session.execute(select(Team))).scalars()}


@router.get("")
async def list_users(
    keyword: str | None = None,
    team_id: uuid.UUID | None = None,
    role: str | None = None,
    status: str | None = None,
    unassigned: bool = False,
    params: PageParams = Depends(),
    _: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = (
        select(User)
        .options(selectinload(User.identities))
        .order_by(User.display_name, User.created_at)
    )
    if keyword:
        kw = f"%{keyword}%"
        stmt = stmt.where(
            or_(User.display_name.ilike(kw), User.login_name.ilike(kw), User.email.ilike(kw))
        )
    if team_id:
        stmt = stmt.where(User.team_id == team_id)
    if unassigned:
        stmt = stmt.where(User.team_id.is_(None))
    if role:
        stmt = stmt.where(User.role == role)
    if status:
        stmt = stmt.where(User.status == status)
    page = await paginate(session, stmt, params)
    users: list[User] = page["items"]
    names, paths = await _team_names(session), await _dept_paths(session, [u.id for u in users])
    return {
        "code": 0,
        "data": {
            **page,
            "items": [
                user_out(u, names.get(u.team_id) if u.team_id else None, paths.get(u.id, []))
                for u in users
            ],
        },
    }


async def _load(session: AsyncSession, user_id: uuid.UUID) -> User:
    u = (
        await session.execute(
            select(User).options(selectinload(User.identities)).where(User.id == user_id)
        )
    ).scalar_one_or_none()
    if u is None:
        raise not_found("用户不存在")
    return u


@router.get("/{user_id}")
async def get_user(
    user_id: uuid.UUID,
    _: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    u = await _load(session, user_id)
    names, paths = await _team_names(session), await _dept_paths(session, [u.id])
    return {
        "code": 0,
        "data": user_out(u, names.get(u.team_id) if u.team_id else None, paths.get(u.id, [])),
    }


@router.patch("/{user_id}")
async def patch_user(
    user_id: uuid.UUID,
    body: UserPatch,
    request: Request,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    target = await _load(session, user_id)
    changes = body.changes()
    if not can_edit_user(actor, target, changes):
        raise forbidden()
    if changes.get("team_id") and await session.get(Team, changes["team_id"]) is None:
        raise ApiError(422, 422, "团队不存在")
    before = {
        k: (str(v) if isinstance(v, uuid.UUID) else v)
        for k, v in ((k, getattr(target, k)) for k in changes)
    }
    for k, v in changes.items():
        setattr(target, k, v)
    after = {
        k: (str(v) if isinstance(v, uuid.UUID) else v)
        for k, v in ((k, getattr(target, k)) for k in changes)
    }
    # 只有真正变了的字段才算「手工维护」：提交一次与原值相同的值不应该让同步从此跳过该字段
    manual = set(target.manual_fields or [])
    manual.update(k for k in changes if k in MANUAL_TRACKED and before[k] != after[k])
    target.manual_fields = sorted(manual)
    await record_audit(
        session,
        action="user.update",
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="user",
        target_id=str(target.id),
        diff=diff_dict(before, after),
        ip=client_ip(request),
    )
    await session.commit()
    # 重新用 selectinload 查一遍：session.refresh() 默认只刷新列属性，identities 关系会
    # 被标记为未加载，序列化时同步访问会在 async 会话里抛 MissingGreenlet。
    target = await _load(session, target.id)
    names, paths = await _team_names(session), await _dept_paths(session, [target.id])
    return {
        "code": 0,
        "data": user_out(
            target, names.get(target.team_id) if target.team_id else None, paths.get(target.id, [])
        ),
    }
