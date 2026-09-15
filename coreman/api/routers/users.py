"""用户管理（spec §10.2、§10.3 团队与用户页）。

列表/详情对所有登录用户开放：协作者、白名单、定时任务接收人的选择器要按名字搜人。
但通讯录明细（手机号、邮箱、平台身份、部门路径等）只给 ai_committee / platform_admin，
其余角色拿到的是 `PUBLIC_USER_FIELDS` 裁剪后的记录。
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from coreman.api.deps import client_ip, current_user, get_session
from coreman.api.errors import ApiError, forbidden, not_found
from coreman.api.pagination import PageParams, paginate
from coreman.api.permissions import MANUAL_TRACKED, can_edit_user
from coreman.api.routers.audit_logs import escape_like
from coreman.api.security import verify_csrf
from coreman.core.audit import diff_dict, record_audit
from coreman.core.bots.permissions import MANAGER_ROLES
from coreman.core.db.models import Department, Team, User, UserDepartment

router = APIRouter(prefix="/api/admin/users", tags=["users"], dependencies=[Depends(verify_csrf)])
_NOT_NULLABLE = ("role", "locale", "bot_accessible", "status")
# 非管理角色可见的字段：够选择器显示与筛选，不含任何联系方式或平台身份。
PUBLIC_USER_FIELDS = (
    "id",
    "login_name",
    "display_name",
    "avatar_url",
    "status",
    "role",
    "team_id",
    "team_name",
    "source",
    "locale",
)


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


def sees_directory_details(viewer: User) -> bool:
    """通讯录明细只给管理角色（spec §10.2 未把用户目录授予 member / team_lead）。"""
    return viewer.role in MANAGER_ROLES


def user_out(
    u: User, team_name: str | None, dept_paths: list[str], *, full: bool = True
) -> dict[str, Any]:
    """`full=False` 时只输出 `PUBLIC_USER_FIELDS`，且不触碰 identities 关系（调用方可不预加载）。"""
    public: dict[str, Any] = {
        "id": str(u.id),
        "login_name": u.login_name,
        "display_name": u.display_name,
        "avatar_url": u.avatar_url,
        "status": u.status,
        "role": u.role,
        "team_id": str(u.team_id) if u.team_id else None,
        "team_name": team_name,
        "source": u.source,
        "locale": u.locale,
    }
    if not full:
        return public
    return {
        **public,
        "email": u.email,
        "mobile": u.mobile,
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


async def _render(session: AsyncSession, users: list[User], *, full: bool) -> list[dict[str, Any]]:
    names = await _team_names(session)
    # 部门路径属于明细：非管理角色不查，也就不会被意外带出。
    paths = await _dept_paths(session, [u.id for u in users]) if full else {}
    return [
        user_out(u, names.get(u.team_id) if u.team_id else None, paths.get(u.id, []), full=full)
        for u in users
    ]


@router.get("")
async def list_users(
    keyword: str | None = Query(default=None, max_length=200),
    team_id: uuid.UUID | None = None,
    role: str | None = None,
    status: str | None = None,
    unassigned: bool = False,
    params: PageParams = Depends(),
    viewer: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    full = sees_directory_details(viewer)
    stmt = select(User).order_by(User.display_name, User.created_at)
    if full:
        stmt = stmt.options(selectinload(User.identities))
    if keyword:
        kw = f"%{escape_like(keyword)}%"
        matches = [User.display_name.ilike(kw, escape="\\"), User.login_name.ilike(kw, escape="\\")]
        # 邮箱看不到就不能拿来搜：否则可以逐字符试探出别人的邮箱。
        if full:
            matches.append(User.email.ilike(kw, escape="\\"))
        stmt = stmt.where(or_(*matches))
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
    return {"code": 0, "data": {**page, "items": await _render(session, users, full=full)}}


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
    viewer: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    u = await _load(session, user_id)
    full = sees_directory_details(viewer)
    return {"code": 0, "data": (await _render(session, [u], full=full))[0]}


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
    # team_lead 也能改本团队成员：回显同样按角色裁剪，不能借一次 PATCH 拿到明细。
    full = sees_directory_details(actor)
    return {"code": 0, "data": (await _render(session, [target], full=full))[0]}
