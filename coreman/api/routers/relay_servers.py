"""Relay 实例（spec §5.3 relay_servers）：CRUD、健康探测与目录预填、agent 令牌、团队负载。

读：任何登录用户，但 `visibility='admins'` 的实例只对 ai_committee / platform_admin 可见；
写：ai_committee / platform_admin。
"""

from __future__ import annotations

import dataclasses
import secrets
import uuid
from collections.abc import Sequence
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip, current_user, get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.pagination import PageParams, paginate
from coreman.api.permissions import require_roles
from coreman.api.routers.model_catalog import load_catalog
from coreman.api.security import verify_csrf
from coreman.api.versioning import require_if_match, set_etag
from coreman.core.audit import diff_dict, record_audit
from coreman.core.bots.relay_policy import relay_visible as relay_visible  # 再导出
from coreman.core.bus.notify import notify
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, ModelCatalog, RelayServer, RuntimeNode, Team, User
from coreman.core.logging import get_logger
from coreman.core.relay import probe
from coreman.core.relay.client import RelayClient
from coreman.core.relay.models import default_model, effective_models
from coreman.core.relay.safe_transport import validate_host

log = get_logger(__name__)
router = APIRouter(
    prefix="/api/admin/relay-servers", tags=["relay-servers"], dependencies=[Depends(verify_csrf)]
)
# B008：同 platform_apps，require_roles(...) 不能写进参数默认值里，挪成模块级单例。
_MANAGERS = require_roles("ai_committee", "platform_admin")
MANAGER_ROLES = ("ai_committee", "platform_admin")
AGENT_TOKEN_AAD = "relay_servers.agent_token_enc"


class RelayIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    # 只收主机名 / IPv4：httpx 拒收带控制字符之类的 host，放行了就会在探测时炸（IPv6 暂不支持）。
    host: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9.-]{1,255}$")

    @field_validator("host")
    @classmethod
    def safe_host(cls, value: str) -> str:
        validate_host(value)
        return value

    clawrelay_port: int = Field(ge=1, le=65535)
    agent_port: int | None = Field(default=None, ge=1, le=65535)
    ssh_user: str | None = Field(default=None, max_length=50)
    runtime_env: Literal["host", "chroot", "nspawn"] = "host"
    chroot_path: str | None = Field(default=None, max_length=500)
    runtime_user: str | None = Field(default=None, max_length=50)
    model_provider: str = Field(pattern=r"^[a-z0-9_-]{1,50}$")
    supported_models_mode: Literal["inherit", "restricted"] = "inherit"
    supported_models: list[str] | None = None
    team_id: uuid.UUID | None = None
    visibility: Literal["all", "admins"] = "all"
    description: str | None = Field(default=None, max_length=500)
    is_active: bool = True


async def notify_relay_changed(session: AsyncSession, relay_id: uuid.UUID) -> None:
    """让网关/工作进程重载这台 relay 的配置（同事务，回滚了就当没发生过）。"""
    await notify(session, "config_changed", {"table": "relay_servers", "id": str(relay_id)})


async def load_relay(session: AsyncSession, relay_id: uuid.UUID) -> RelayServer:
    """按 id 取 relay，取不到就 404（bots 路由复用）。"""
    relay = await session.get(RelayServer, relay_id)
    if relay is None:
        raise not_found("运行时不存在")
    return relay


async def relay_out(
    session: AsyncSession,
    relay: RelayServer,
    catalog: Sequence[ModelCatalog],
    team_names: dict[uuid.UUID, str],
    bot_counts: dict[uuid.UUID, int],
) -> dict[str, Any]:
    """对外视图。目录、团队名、bot 计数由调用方批量查好传进来，避免列表里逐行查库。"""
    eff = effective_models(relay, catalog)
    node = await session.get(RuntimeNode, relay.runtime_node_id) if relay.runtime_node_id else None
    return {
        "id": str(relay.id),
        "name": f"{node.name} / {relay.model_provider}" if node else relay.name,
        "runtime_node_id": str(node.id) if node else None,
        "runtime_name": node.name if node else None,
        "workspace_root": node.workspace_root if node else None,
        "host": relay.host,
        "clawrelay_port": relay.clawrelay_port,
        "agent_port": relay.agent_port,
        "relay_url": relay.relay_url,
        "ssh_user": relay.ssh_user,
        "runtime_env": relay.runtime_env,
        "chroot_path": relay.chroot_path,
        "runtime_user": relay.runtime_user,
        "model_provider": relay.model_provider,
        "supported_models_mode": relay.supported_models_mode,
        "supported_models": relay.supported_models,
        "effective_models": eff,
        "default_model": default_model(relay.model_provider, catalog),
        "team_id": str(relay.team_id) if relay.team_id else None,
        "team_name": team_names.get(relay.team_id) if relay.team_id else None,
        "visibility": relay.visibility,
        "description": relay.description,
        "is_active": relay.is_active,
        "has_agent_token": relay.agent_token_enc is not None,
        "rate_limit_5h_used_pct": _pct(relay.rate_limit_5h_used_pct),
        "rate_limit_5h_resets_at": relay.rate_limit_5h_resets_at,
        "rate_limit_7d_used_pct": _pct(relay.rate_limit_7d_used_pct),
        "rate_limit_7d_resets_at": relay.rate_limit_7d_resets_at,
        "rate_limit_probed_at": relay.rate_limit_probed_at,
        "health_status": relay.health_status,
        "health_checked_at": relay.health_checked_at,
        "health_detail": relay.health_detail,
        "health_latency_ms": relay.health_latency_ms,
        "health_fail_count": relay.health_fail_count,
        "relay_version": relay.relay_version,
        "relay_mode": relay.relay_mode,
        "bot_count": bot_counts.get(relay.id, 0),
        "version": relay.version,
        "created_at": relay.created_at,
        "updated_at": relay.updated_at,
    }


def _pct(value: Any) -> float | None:
    """Numeric 列到 JSON：Decimal 前端读不了，统一转 float。"""
    return float(value) if value is not None else None


def _public(relay: RelayServer) -> dict[str, Any]:
    """RelayIn 那组字段的快照（审计 diff 用，全部 JSON 可序列化）。"""
    return {
        "name": relay.name,
        "host": relay.host,
        "clawrelay_port": relay.clawrelay_port,
        "agent_port": relay.agent_port,
        "ssh_user": relay.ssh_user,
        "runtime_env": relay.runtime_env,
        "chroot_path": relay.chroot_path,
        "runtime_user": relay.runtime_user,
        "model_provider": relay.model_provider,
        "supported_models_mode": relay.supported_models_mode,
        "supported_models": list(relay.supported_models) if relay.supported_models else None,
        "team_id": str(relay.team_id) if relay.team_id else None,
        "visibility": relay.visibility,
        "description": relay.description,
        "is_active": relay.is_active,
    }


def _cipher(request: Request) -> Cipher:
    return request.app.state.cipher  # type: ignore[no-any-return]


async def _team_names(session: AsyncSession, team_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not team_ids:
        return {}
    rows = (await session.execute(select(Team).where(Team.id.in_(team_ids)))).scalars().all()
    return {t.id: t.name_zh for t in rows}


async def _bot_counts(session: AsyncSession, relay_ids: set[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not relay_ids:
        return {}
    rows = await session.execute(
        select(Bot.relay_server_id, func.count())
        .where(Bot.relay_server_id.in_(relay_ids))
        .group_by(Bot.relay_server_id)
    )
    return {rid: count for rid, count in rows.all() if rid is not None}


async def _one_out(session: AsyncSession, relay: RelayServer) -> dict[str, Any]:
    """单条 relay 的视图：目录只取它那个 provider。"""
    team_ids: set[uuid.UUID] = {relay.team_id} if relay.team_id else set()
    return await relay_out(
        session,
        relay,
        await load_catalog(session, relay.model_provider),
        await _team_names(session, team_ids),
        await _bot_counts(session, {relay.id}),
    )


async def _visible_relay(session: AsyncSession, relay_id: uuid.UUID, user: User) -> RelayServer:
    """读接口的取行：不可见的实例按「不存在」处理，不泄漏它的存在性。"""
    relay = await load_relay(session, relay_id)
    if not relay_visible(user, relay):
        raise not_found("运行时不存在")
    return relay


def _check_restricted(body: RelayIn) -> None:
    """restricted 却没给白名单：有效模型集恒为空，机器人一个模型都选不了，直接拒。"""
    if body.supported_models_mode == "restricted" and not body.supported_models:
        raise ApiError(422, 422, "restricted 模式必须指定 supported_models")


async def _check_team(session: AsyncSession, team_id: uuid.UUID | None) -> None:
    if team_id is not None and await session.get(Team, team_id) is None:
        raise ApiError(422, 422, "团队不存在")


async def _check_unique(session: AsyncSession, body: RelayIn, exclude: uuid.UUID | None) -> None:
    """name 与 (host, clawrelay_port) 各有唯一约束：先查再 409，别让 IntegrityError 变 500。"""
    stmt = select(RelayServer).where(
        or_(
            RelayServer.name == body.name,
            and_(
                RelayServer.host == body.host,
                RelayServer.clawrelay_port == body.clawrelay_port,
            ),
        )
    )
    if exclude is not None:
        stmt = stmt.where(RelayServer.id != exclude)
    dup = (await session.execute(stmt.limit(1))).scalars().first()
    if dup is None:
        return
    raise ApiError(
        409, 409, "运行时名称已存在" if dup.name == body.name else "该 host 与端口已被占用"
    )


async def _check_bound_bots(session: AsyncSession, relay_id: uuid.UUID, models: list[str]) -> None:
    """有效模型集变了：绑在这台上的机器人不能被甩到集合外——否则下发给 relay 的模型立刻是
    非法值，只能先把它们迁走或改模型。"""
    stmt = (
        select(Bot.bot_key)
        .where(Bot.relay_server_id == relay_id, Bot.model.not_in(models))
        .order_by(Bot.bot_key)
    )
    orphans = (await session.execute(stmt)).scalars().all()
    if orphans:
        raise ApiError(
            409,
            409,
            f"以下机器人的模型不在新的有效模型集内：{', '.join(orphans)}；"
            "请先切换它们的运行时或模型",
        )


def _make_client(relay: RelayServer) -> RelayClient:
    """建客户端：库里的历史脏 host 会让 httpx 抛，按 422「地址无效」回，不要变成 500。"""
    try:
        return probe.make_client(relay)
    except Exception as exc:
        raise ApiError(422, 422, "运行时地址无效") from exc


async def _best_effort_probe(session: AsyncSession, relay: RelayServer) -> None:
    """注册后顺手探一次并预填目录：探不通（网络不可达、超时）也不能让创建失败。"""
    client: RelayClient | None = None
    try:
        # make_client 也在 try 里：建客户端本身可能抛，而此时行已提交，不能让它变成 500。
        client = probe.make_client(relay)
        await probe.probe_relay(session, relay, client)
        await session.commit()
    except Exception as exc:  # noqa: BLE001 探测是附赠动作，任何失败都只记日志
        log.warning("relay_probe_failed", relay=relay.name, error=f"{type(exc).__name__}: {exc}")
        await session.rollback()
        # rollback 会让 relay 的属性全部过期，异步下再访问就是 MissingGreenlet，先刷回来。
        await session.refresh(relay)
    finally:
        if client is not None:
            await client.aclose()


@router.get("")
async def list_relays(
    user: User = Depends(current_user),
    team_id: uuid.UUID | None = None,
    model_provider: str | None = None,
    is_active: bool | None = None,
    params: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(RelayServer).order_by(RelayServer.name)
    if user.role not in MANAGER_ROLES:
        stmt = stmt.where(RelayServer.visibility == "all")
    if team_id is not None:
        stmt = stmt.where(RelayServer.team_id == team_id)
    if model_provider:
        stmt = stmt.where(RelayServer.model_provider == model_provider)
    if is_active is not None:
        stmt = stmt.where(RelayServer.is_active == is_active)
    page = await paginate(session, stmt, params)
    relays: list[RelayServer] = page["items"]
    catalog = await load_catalog(session)
    team_names = await _team_names(session, {r.team_id for r in relays if r.team_id})
    bot_counts = await _bot_counts(session, {r.id for r in relays})
    items = [await relay_out(session, r, catalog, team_names, bot_counts) for r in relays]
    return {"code": 0, "data": {**page, "items": items}}


# 必须声明在 /{relay_id} 之前，否则 team-load 会被当成 relay_id 去解析 UUID（422）。
@router.get("/team-load")
async def team_load(
    user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """按团队看 relay 与机器人的分布，给「新建机器人挑 relay」和容量评估用。"""
    teams = (
        (await session.execute(select(Team).order_by(Team.sort_order, Team.slug))).scalars().all()
    )
    stmt = select(RelayServer).where(RelayServer.team_id.is_not(None)).order_by(RelayServer.name)
    if user.role not in MANAGER_ROLES:
        # 与列表口径一致：admins-only 实例的存在与名字都不能透给普通成员。
        stmt = stmt.where(RelayServer.visibility == "all")
    relays = (await session.execute(stmt)).scalars().all()
    by_team: dict[uuid.UUID, list[str]] = {}
    for relay in relays:
        if relay.team_id is None:
            continue
        by_team.setdefault(relay.team_id, []).append(relay.name)
    bot_rows = await session.execute(
        select(Bot.team_id, func.count()).where(Bot.team_id.is_not(None)).group_by(Bot.team_id)
    )
    bots_by_team = {tid: count for tid, count in bot_rows.all() if tid is not None}
    unassigned_users = (
        await session.execute(
            select(func.count())
            .select_from(User)
            .where(User.team_id.is_(None), User.status == "active", User.source == "sync")
        )
    ).scalar_one()
    unassigned_bots = (
        await session.execute(select(func.count()).select_from(Bot).where(Bot.team_id.is_(None)))
    ).scalar_one()
    return {
        "code": 0,
        "data": {
            "teams": [
                {
                    "team_id": str(t.id),
                    "team_name": t.name_zh,
                    "relay_count": len(by_team.get(t.id, [])),
                    "relay_names": by_team.get(t.id, []),
                    "bot_count": bots_by_team.get(t.id, 0),
                }
                for t in teams
            ],
            "unassigned_user_count": unassigned_users,
            "unassigned_bot_count": unassigned_bots,
        },
    }


@router.post("", status_code=201)
async def create_relay(
    body: RelayIn,
    request: Request,
    actor: User = Depends(_MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    _check_restricted(body)
    await _check_team(session, body.team_id)
    await _check_unique(session, body, exclude=None)
    relay = RelayServer(**body.model_dump())
    session.add(relay)
    await session.flush()
    await record_audit(
        session,
        action="relay.create",
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="relay_server",
        target_id=str(relay.id),
        diff=diff_dict({}, _public(relay)),
        ip=client_ip(request),
    )
    await session.commit()
    # 探测在创建落库之后：它会补健康列并预填目录，所以视图要等它跑完再算。
    await _best_effort_probe(session, relay)
    return {"code": 0, "data": await _one_out(session, relay)}


@router.get("/{relay_id}")
async def get_relay(
    relay_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return {
        "code": 0,
        "data": await _one_out(session, await _visible_relay(session, relay_id, user)),
    }


@router.put("/{relay_id}")
async def update_relay(
    relay_id: uuid.UUID,
    body: RelayIn,
    request: Request,
    response: Response,
    actor: User = Depends(_MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    relay = await load_relay(session, relay_id)
    if relay.runtime_node_id:
        raise ApiError(409, 409, "请在运行时管理中管理节点；AI 能力由 Daemon 自动发现")
    require_if_match(request, relay.version)
    _check_restricted(body)
    await _check_team(session, body.team_id)
    await _check_unique(session, body, exclude=relay.id)
    catalog = await load_catalog(session)
    # 「改完之后」的有效集拿一个游离对象算：校验不过时不能把改动带进 ORM（更别说带进事务）。
    after_models = effective_models(
        RelayServer(
            model_provider=body.model_provider,
            supported_models_mode=body.supported_models_mode,
            supported_models=body.supported_models,
        ),
        catalog,
    )
    # 集合没变就不查：机器人的模型可能早已退役出集（那是它自己的历史包袱），
    # 不该让「只改个描述」也被拦下。
    if set(after_models) != set(effective_models(relay, catalog)):
        await _check_bound_bots(session, relay.id, after_models)
    before = _public(relay)
    for key, value in body.model_dump().items():
        setattr(relay, key, value)
    await record_audit(
        session,
        action="relay.update",
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="relay_server",
        target_id=str(relay.id),
        diff=diff_dict(before, _public(relay)),
        ip=client_ip(request),
    )
    await notify_relay_changed(session, relay.id)
    await session.commit()
    set_etag(response, relay.version)
    return {"code": 0, "data": await _one_out(session, relay)}


@router.delete("/{relay_id}")
async def delete_relay(
    relay_id: uuid.UUID,
    request: Request,
    actor: User = Depends(_MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    relay = await load_relay(session, relay_id)
    if relay.runtime_node_id:
        raise ApiError(409, 409, "请在运行时管理中管理节点；AI 能力由 Daemon 自动发现")
    in_use = select(Bot.id).where(Bot.relay_server_id == relay.id).limit(1)
    if (await session.execute(in_use)).first():
        raise ApiError(409, 409, "仍有机器人使用该运行时")
    snapshot = _public(relay)
    await session.delete(relay)
    await record_audit(
        session,
        action="relay.delete",
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="relay_server",
        target_id=str(relay_id),
        diff=diff_dict(snapshot, {}),
        ip=client_ip(request),
    )
    await notify_relay_changed(session, relay_id)
    await session.commit()
    return {"code": 0, "data": None}


@router.post("/{relay_id}/probe")
async def probe_endpoint(
    relay_id: uuid.UUID,
    request: Request,
    actor: User = Depends(_MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    relay = await load_relay(session, relay_id)
    before = relay.health_status
    client: RelayClient | None = None
    try:
        client = _make_client(relay)
        health, added = await probe.probe_relay(session, relay, client)
    finally:
        if client is not None:
            await client.aclose()
    await record_audit(
        session,
        action="relay.probe",
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="relay_server",
        target_id=str(relay.id),
        diff={"health_status": [before, relay.health_status]},
        ip=client_ip(request),
    )
    await session.commit()
    return {
        "code": 0,
        "data": {
            "health": dataclasses.asdict(health),
            "added_models": added,
            "relay": await _one_out(session, relay),
        },
    }


@router.get("/{relay_id}/models")
async def relay_models(
    relay_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    relay = await _visible_relay(session, relay_id, user)
    catalog = await load_catalog(session, relay.model_provider)
    return {
        "code": 0,
        "data": {
            "provider": relay.model_provider,
            "mode": relay.supported_models_mode,
            "models": effective_models(relay, catalog),
            "default": default_model(relay.model_provider, catalog),
        },
    }


@router.post("/{relay_id}/agent-token")
async def issue_agent_token(
    relay_id: uuid.UUID,
    request: Request,
    actor: User = Depends(_MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """签发 agent 令牌：明文只在这一次响应里出现，库里只存密文，审计只记「有没有」。"""
    relay = await load_relay(session, relay_id)
    if relay.runtime_node_id:
        raise ApiError(409, 409, "请在运行时管理中管理节点；AI 能力由 Daemon 自动发现")
    token = secrets.token_urlsafe(32)
    had_token = relay.agent_token_enc is not None
    relay.agent_token_enc = _cipher(request).encrypt(token, AGENT_TOKEN_AAD)
    await record_audit(
        session,
        action="relay.agent_token",
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="relay_server",
        target_id=str(relay.id),
        diff={"has_agent_token": [had_token, True]},
        ip=client_ip(request),
    )
    await notify_relay_changed(session, relay.id)
    await session.commit()
    return {"code": 0, "data": {"token": token}}
