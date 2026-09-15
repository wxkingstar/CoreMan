"""团队与团队规则（teams / team_rules）。"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip, current_user, get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.permissions import require_roles
from coreman.api.security import verify_csrf
from coreman.core.audit import diff_dict, record_audit
from coreman.core.db.models import Bot, RelayServer, Team, TeamRule, User

router = APIRouter(prefix="/api/admin/teams", tags=["teams"], dependencies=[Depends(verify_csrf)])
_MANAGERS = require_roles("ai_committee", "platform_admin")


class TeamIn(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,49}$")
    name_zh: str = Field(min_length=1, max_length=100)
    name_ja: str | None = Field(default=None, max_length=100)
    name_en: str | None = Field(default=None, max_length=100)
    sort_order: int = 0
    enabled: bool = True


class RuleIn(BaseModel):
    platform: Literal["wecom", "feishu"] | None = None
    dept_path_contains: str = Field(min_length=1, max_length=200)
    sort_order: int = 0


def _rule_out(r: TeamRule) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "platform": r.platform,
        "dept_path_contains": r.dept_path_contains,
        "sort_order": r.sort_order,
    }


async def _out(session: AsyncSession, team: Team) -> dict[str, Any]:
    count = (
        await session.execute(select(func.count()).select_from(User).where(User.team_id == team.id))
    ).scalar_one()
    rules = (
        (
            await session.execute(
                select(TeamRule)
                .where(TeamRule.team_id == team.id)
                .order_by(TeamRule.sort_order, TeamRule.created_at)
            )
        )
        .scalars()
        .all()
    )
    return {
        "id": str(team.id),
        "slug": team.slug,
        "name_zh": team.name_zh,
        "name_ja": team.name_ja,
        "name_en": team.name_en,
        "sort_order": team.sort_order,
        "enabled": team.enabled,
        "member_count": count,
        "rules": [_rule_out(r) for r in rules],
    }


async def _load(session: AsyncSession, team_id: uuid.UUID) -> Team:
    team = await session.get(Team, team_id)
    if team is None:
        raise not_found("团队不存在")
    return team


@router.get("")
async def list_teams(
    _: User = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    teams = (
        (await session.execute(select(Team).order_by(Team.sort_order, Team.slug))).scalars().all()
    )
    return {"code": 0, "data": [await _out(session, t) for t in teams]}


@router.post("", status_code=201)
async def create_team(
    body: TeamIn,
    request: Request,
    actor: User = Depends(_MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if (await session.execute(select(Team.id).where(Team.slug == body.slug))).first():
        raise ApiError(409, 409, "slug 已存在")
    team = Team(**body.model_dump())
    session.add(team)
    await session.flush()
    await record_audit(
        session,
        action="team.create",
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="team",
        target_id=str(team.id),
        diff=diff_dict({}, body.model_dump()),
        ip=client_ip(request),
    )
    await session.commit()
    return {"code": 0, "data": await _out(session, team)}


@router.put("/{team_id}")
async def update_team(
    team_id: uuid.UUID,
    body: TeamIn,
    request: Request,
    actor: User = Depends(_MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    team = await _load(session, team_id)
    before = {k: getattr(team, k) for k in body.model_dump()}
    for k, v in body.model_dump().items():
        setattr(team, k, v)
    await record_audit(
        session,
        action="team.update",
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="team",
        target_id=str(team.id),
        diff=diff_dict(before, body.model_dump()),
        ip=client_ip(request),
    )
    await session.commit()
    return {"code": 0, "data": await _out(session, team)}


@router.delete("/{team_id}")
async def delete_team(
    team_id: uuid.UUID,
    request: Request,
    actor: User = Depends(_MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    team = await _load(session, team_id)
    if (await session.execute(select(User.id).where(User.team_id == team.id).limit(1))).first():
        raise ApiError(409, 409, "团队下仍有用户")
    if (await session.execute(select(Bot.id).where(Bot.team_id == team.id).limit(1))).first():
        raise ApiError(409, 409, "团队下仍有机器人")
    if (
        await session.execute(select(RelayServer.id).where(RelayServer.team_id == team.id).limit(1))
    ).first():
        raise ApiError(409, 409, "团队下仍有运行时")
    snapshot = {
        "slug": team.slug,
        "name_zh": team.name_zh,
        "name_ja": team.name_ja,
        "name_en": team.name_en,
        "sort_order": team.sort_order,
        "enabled": team.enabled,
    }
    await session.delete(team)
    await record_audit(
        session,
        action="team.delete",
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="team",
        target_id=str(team_id),
        diff=diff_dict(snapshot, {}),
        ip=client_ip(request),
    )
    await session.commit()
    return {"code": 0, "data": None}


@router.put("/{team_id}/rules")
async def replace_rules(
    team_id: uuid.UUID,
    body: list[RuleIn],
    request: Request,
    actor: User = Depends(_MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    team = await _load(session, team_id)
    old = (
        (await session.execute(select(TeamRule).where(TeamRule.team_id == team.id))).scalars().all()
    )
    for r in old:
        await session.delete(r)
    for rule_in in body:
        session.add(TeamRule(team_id=team.id, **rule_in.model_dump()))
    await record_audit(
        session,
        action="team.rules",
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="team",
        target_id=str(team.id),
        diff={"rules": [[_rule_out(r) for r in old], [r.model_dump() for r in body]]},
        ip=client_ip(request),
    )
    await session.commit()
    return {"code": 0, "data": await _out(session, team)}
