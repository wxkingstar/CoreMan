"""Runtime installation, enrollment, telemetry and outbound-only command delivery."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import shlex
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import current_user, get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.permissions import require_roles
from coreman.api.security import verify_csrf
from coreman.core.audit import record_audit
from coreman.core.db.models import (
    ModelCatalog,
    RelayServer,
    RuntimeCall,
    RuntimeChunk,
    RuntimeInstallLink,
    RuntimeNode,
    Team,
    User,
)
from coreman.core.runtime_nodes.transport import (
    CHUNK_SIZE,
    LEASE_SECONDS,
    MAX_RESPONSE,
    TERMINAL,
    chunk_aad,
    envelope_aad,
    now,
    online,
)

router = APIRouter(prefix="/api/admin/runtime-nodes", dependencies=[Depends(verify_csrf)])
public_router = APIRouter(prefix="/api/runtime", tags=["runtime-daemon"])
MANAGERS = require_roles("ai_committee", "platform_admin")
ROOT = Path(__file__).resolve().parents[3]
NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


class RedactInstallAccess(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            args = list(record.args)
            if isinstance(args[2], str) and "/api/runtime/install/" in args[2]:
                args[2] = "/api/runtime/install/[REDACTED]"
                record.args = tuple(args)
        return True


logging.getLogger("uvicorn.access").addFilter(RedactInstallAccess())


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def absolute_root(value: str) -> str:
    if (
        not value.startswith("/")
        or value.startswith("//")
        or ".." in value.split("/")
        or any(ord(c) < 32 for c in value)
        or "\x7f" in value
    ):
        raise ValueError("项目主目录必须是无上级跳转的绝对路径")
    result = str(PurePosixPath(value))
    if result == "/":
        raise ValueError("不能以系统根目录作为项目主目录")
    return result


class InstallOptions(BaseModel):
    proxy: str = Field(default="", max_length=500)
    control_proxy: str = Field(default="", max_length=500)
    environment: Literal["auto", "host", "chroot", "nspawn"] = "auto"
    claude_path: str = Field(default="", max_length=500)
    codex_path: str = Field(default="", max_length=500)
    git_hosts: list[str] = Field(
        default_factory=lambda: ["github.com"], max_length=30
    )
    max_concurrent: int = Field(default=10, ge=1, le=32)
    install_claude_probe: bool = False

    @field_validator("proxy", "control_proxy")
    @classmethod
    def validate_proxy(cls, value: str) -> str:
        if value:
            url = urlsplit(value)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username
                or url.password
            ):
                raise ValueError("代理使用不含账号密码的 HTTP(S) 地址")
        return value

    @field_validator("claude_path", "codex_path")
    @classmethod
    def validate_cli_path(cls, value: str) -> str:
        return absolute_root(value) if value else value

    @field_validator("git_hosts")
    @classmethod
    def validate_git_hosts(cls, value: list[str]) -> list[str]:
        import re

        if not value or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", h) for h in value):
            raise ValueError("Git 白名单需要主机名")
        return value


class InstallIn(BaseModel):
    name: str = Field(default="", max_length=100)
    workspace_root: str = Field(min_length=2, max_length=400)
    team_id: uuid.UUID | None = None
    visibility: Literal["all", "admins"] = "all"
    options: InstallOptions = Field(default_factory=InstallOptions)

    _root = field_validator("workspace_root")(absolute_root)


class EnrollIn(BaseModel):
    install_token: str = Field(max_length=160)
    node_id: uuid.UUID
    node_token: str = Field(min_length=43, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    hostname: str = Field(min_length=1, max_length=200)
    username: str = Field(min_length=1, max_length=100)
    platform: Literal["linux", "darwin"]
    architecture: Literal["amd64", "arm64"]
    environment: Literal["host", "chroot", "nspawn"] = "host"
    version: str = Field(max_length=100)
    workspace_root: str = Field(min_length=2, max_length=400)

    _root = field_validator("workspace_root")(absolute_root)


class Capability(BaseModel):
    installed: bool = False
    version: str = Field(default="", max_length=100)
    login: Literal["ready", "required", "unknown"] = "unknown"
    health: Literal["healthy", "down", "auth_fail", "timeout", "unknown"] = "unknown"
    detail: str = Field(default="", max_length=255)
    models: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("models")
    @classmethod
    def model_lengths(cls, value: list[str]) -> list[str]:
        if any(not m or len(m) > 150 for m in value):
            raise ValueError("模型名称长度无效")
        return list(dict.fromkeys(value))


class HeartbeatIn(BaseModel):
    claude: Capability
    codex: Capability
    version: str = Field(max_length=100)
    service_status: Literal[
        "systemd-user", "systemd-user-session", "systemd", "launchd", "supervised", "foreground"
    ]


class PollIn(BaseModel):
    running: list[uuid.UUID] = Field(default_factory=list, max_length=64)
    abandoned: list[uuid.UUID] = Field(default_factory=list, max_length=64)
    slots: int = Field(default=1, ge=0, le=32)


class FrameIn(BaseModel):
    seq: int = Field(ge=0)
    data: str = Field(default="", max_length=CHUNK_SIZE * 4 // 3 + 4)
    status_code: int | None = Field(default=None, ge=100, le=599)
    content_type: str = Field(default="application/json", max_length=100)
    done: bool = False
    error: Literal["execution_failed", "connection_lost", "response_too_large"] | None = None


class NodePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    is_active: bool | None = None
    draining: bool | None = None


def link_out(link: RuntimeInstallLink) -> dict[str, Any]:
    state = (
        "revoked"
        if link.revoked_at
        else "used"
        if link.used_at
        else "expired"
        if link.expires_at <= now()
        else "ready"
    )
    return {
        "id": str(link.id),
        "name": link.name,
        "workspace_root": link.workspace_root,
        "team_id": str(link.team_id) if link.team_id else None,
        "state": state,
        "expires_at": link.expires_at,
        "created_at": link.created_at,
        "node_id": str(link.node_id) if link.node_id else None,
    }


async def load_link(session: AsyncSession, token: str, *, lock: bool = False) -> RuntimeInstallLink:
    try:
        raw_id, secret = token.split(".", 1)
        identity = uuid.UUID(raw_id)
    except ValueError as exc:
        raise ApiError(401, 401, "安装链接无效") from exc
    stmt = select(RuntimeInstallLink).where(RuntimeInstallLink.id == identity)
    if lock:
        stmt = stmt.with_for_update()
    link = await session.scalar(stmt)
    if not link or not secrets.compare_digest(link.token_hash, digest(secret)) or link.revoked_at:
        raise ApiError(401, 401, "安装链接无效或已撤销")
    return link


async def node_auth(request: Request, session: AsyncSession) -> RuntimeNode:
    try:
        identity = uuid.UUID(request.headers.get("X-Runtime-ID", ""))
    except ValueError as exc:
        raise ApiError(401, 401, "运行时身份无效") from exc
    node = await session.get(RuntimeNode, identity)
    token = request.headers.get("Authorization", "").removeprefix("Bearer ")
    if (
        not node
        or not node.is_active
        or not 43 <= len(token) <= 128
        or not secrets.compare_digest(node.token_hash, digest(token))
    ):
        raise ApiError(401, 401, "运行时已停用或凭证无效")
    return node


@router.post("/install-links")
async def create_link(
    body: InstallIn,
    request: Request,
    actor: User = Depends(MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if body.team_id and not await session.get(Team, body.team_id):
        raise ApiError(422, 422, "团队不存在")
    token = secrets.token_urlsafe(32)
    link = RuntimeInstallLink(
        token_hash=digest(token),
        created_by=actor.id,
        name=body.name,
        team_id=body.team_id,
        visibility=body.visibility,
        workspace_root=body.workspace_root,
        expires_at=now() + timedelta(hours=24),
        options=body.options.model_dump(),
    )
    session.add(link)
    await session.flush()
    await record_audit(
        session,
        action="runtime.install_link.create",
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="runtime_install_link",
        target_id=str(link.id),
        diff={"workspace_root": [None, body.workspace_root]},
    )
    await session.commit()
    base = request.app.state.settings.public_base_url
    url = f"{base}/api/runtime/install/{link.id}.{token}/install.sh"
    return {
        "code": 0,
        "data": {**link_out(link), "url": url, "command": f"curl -fsSL {shlex.quote(url)} | sh"},
    }


@router.get("/install-links")
async def list_links(
    _: User = Depends(MANAGERS), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    rows = await session.scalars(
        select(RuntimeInstallLink).order_by(RuntimeInstallLink.created_at.desc()).limit(200)
    )
    return {"code": 0, "data": [link_out(r) for r in rows]}


@router.delete("/install-links/{link_id}")
async def revoke_link(
    link_id: uuid.UUID,
    actor: User = Depends(MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    link = await session.get(RuntimeInstallLink, link_id)
    if not link:
        raise not_found("安装链接不存在")
    link.revoked_at = now()
    await record_audit(
        session,
        action="runtime.install_link.revoke",
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="runtime_install_link",
        target_id=str(link_id),
    )
    await session.commit()
    return {"code": 0, "data": None}


@router.get("")
async def list_nodes(
    user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    from coreman.api.routers.relay_servers import bot_counts, relay_out, team_names
    from coreman.core.relay.models import load_catalog

    stmt = select(RuntimeNode).order_by(RuntimeNode.created_at.desc()).limit(200)
    if user.role not in {"ai_committee", "platform_admin"}:
        stmt = stmt.where(RuntimeNode.visibility == "all")
    nodes = list(await session.scalars(stmt))
    relays = list(
        await session.scalars(
            select(RelayServer).where(RelayServer.runtime_node_id.in_([n.id for n in nodes]))
        )
    )
    catalog = await load_catalog(session)
    names = await team_names(session, {n.team_id for n in nodes if n.team_id})
    counts = await bot_counts(session, {r.id for r in relays})
    data = []
    for n in nodes:
        backends = [
            await relay_out(session, r, catalog, names, counts)
            for r in relays
            if r.runtime_node_id == n.id
        ]
        data.append(
            {
                "id": str(n.id),
                "name": n.name,
                "hostname": n.hostname,
                "username": n.username,
                "platform": n.platform,
                "architecture": n.architecture,
                "environment": n.environment,
                "workspace_root": n.workspace_root,
                "version": n.version,
                "online": online(n),
                "is_active": n.is_active,
                "draining": n.draining,
                "heartbeat_at": n.heartbeat_at,
                "capabilities": n.capabilities,
                "service_status": n.service_status,
                "team_name": names.get(n.team_id) if n.team_id else None,
                "backends": backends,
            }
        )
    return {"code": 0, "data": data}


@router.patch("/{node_id}")
async def patch_node(
    node_id: uuid.UUID,
    body: NodePatch,
    actor: User = Depends(MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    node = await session.scalar(
        select(RuntimeNode).where(RuntimeNode.id == node_id).with_for_update()
    )
    if not node:
        raise not_found("运行时不存在")
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    diff = {k: [getattr(node, k), v] for k, v in changes.items()}
    for key, value in changes.items():
        setattr(node, key, value)
    if "is_active" in changes:
        await session.execute(
            update(RelayServer)
            .where(RelayServer.runtime_node_id == node_id)
            .values(is_active=node.is_active)
        )
    if not node.is_active:
        await session.execute(
            update(RuntimeCall)
            .where(RuntimeCall.node_id == node_id, RuntimeCall.status.not_in(TERMINAL))
            .values(status="cancelled", request_enc="")
        )
    await record_audit(
        session,
        action="runtime.update",
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="runtime_node",
        target_id=str(node_id),
        diff=diff,
    )
    await session.commit()
    return {"code": 0, "data": None}


@public_router.get("/install/{token}/install.sh")
async def install_script(
    token: str, request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    link = await load_link(session, token)
    if link.expires_at <= now() or link.used_at:
        raise ApiError(410, 410, "安装链接已过期或已使用；已安装节点请直接重启服务")
    cfg = {
        "api_url": request.app.state.settings.public_base_url,
        "install_token": token,
        "workspace_root": link.workspace_root,
        **link.options,
    }
    template = (ROOT / "runtime_daemon/install.sh").read_text()
    script = template.replace(
        "__COREMAN_CONFIG__", base64.b64encode(json.dumps(cfg).encode()).decode()
    )
    return Response(script, media_type="text/x-shellscript", headers=NO_STORE)


@public_router.get("/install/{token}/{platform}/{arch}/bundle")
async def download_bundle(
    token: str,
    platform: Literal["linux", "darwin"],
    arch: Literal["amd64", "arm64"],
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    link = await load_link(session, token)
    if link.expires_at <= now() or link.used_at:
        raise ApiError(410, 410, "安装链接已过期或已使用")
    path = (
        Path(request.app.state.settings.runtime_bundle_dir)
        / f"coreman-runtime-{platform}-{arch}.tar.gz"
    )
    if not path.is_file():
        raise ApiError(503, 503, "该平台安装包尚未发布，请先构建 Runtime 发布包")
    checksum = path.with_suffix(path.suffix + ".sha256").read_text().split()[0]
    return FileResponse(
        path, headers={**NO_STORE, "X-SHA256": checksum}, media_type="application/gzip"
    )


@public_router.post("/enroll")
async def enroll(
    body: EnrollIn, request: Request, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    link = await load_link(session, body.install_token, lock=True)
    node = await session.get(RuntimeNode, body.node_id)
    if link.used_at:
        if (
            link.node_id != body.node_id
            or not node
            or not node.is_active
            or not secrets.compare_digest(node.token_hash, digest(body.node_token))
        ):
            raise ApiError(409, 409, "安装链接已被使用")
    else:
        if link.expires_at <= now():
            raise ApiError(410, 410, "安装链接已过期")
        if node:
            raise ApiError(409, 409, "节点身份已存在")
        if body.workspace_root != link.workspace_root:
            raise ApiError(422, 422, "项目主目录与安装链接不一致")
        node = RuntimeNode(
            id=body.node_id,
            token_hash=digest(body.node_token),
            name=link.name or f"{body.username}@{body.hostname}",
            team_id=link.team_id,
            visibility=link.visibility,
            workspace_root=body.workspace_root,
            hostname=body.hostname,
            username=body.username,
            platform=body.platform,
            architecture=body.architecture,
            environment=body.environment,
            version=body.version,
            capabilities={},
            heartbeat_at=now(),
        )
        session.add(node)
        await session.flush()
        for provider in ("claude", "codex"):
            agent_token = hmac.new(
                body.node_token.encode(), provider.encode(), hashlib.sha256
            ).hexdigest()
            session.add(
                RelayServer(
                    runtime_node_id=node.id,
                    name=f"{node.id}/{provider}",
                    model_provider=provider,
                    team_id=link.team_id,
                    visibility=link.visibility,
                    supported_models_mode="restricted",
                    supported_models=[],
                    agent_token_enc=request.app.state.cipher.encrypt(
                        agent_token, "relay_servers.agent_token_enc"
                    ),
                )
            )
        link.node_id, link.used_at = node.id, now()
        await record_audit(
            session,
            action="runtime.enroll",
            actor_id=link.created_by,
            target_type="runtime_node",
            target_id=str(node.id),
            diff={"install_link": [None, str(link.id)]},
        )
        await session.flush()
    relays = list(
        await session.scalars(
            select(RelayServer).where(RelayServer.runtime_node_id == body.node_id)
        )
    )
    await session.commit()
    return {
        "code": 0,
        "data": {
            "node_id": str(body.node_id),
            "backends": {r.model_provider: {"relay_id": str(r.id)} for r in relays},
        },
    }


@public_router.post("/heartbeat")
async def heartbeat(
    body: HeartbeatIn, request: Request, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    node = await node_auth(request, session)
    await session.execute(
        update(RuntimeNode)
        .where(RuntimeNode.id == node.id)
        .values(
            heartbeat_at=now(),
            version=body.version,
            service_status=body.service_status,
            capabilities={p: getattr(body, p).model_dump() for p in ("claude", "codex")},
        )
    )
    from sqlalchemy.dialects.postgresql import insert

    from coreman.core.relay.models import load_catalog

    catalog = await load_catalog(session)

    for provider in ("claude", "codex"):
        cap = getattr(body, provider)
        # CLI discovery never overrides the real inference health probe's result.
        values: dict[str, Any] = {
            "supported_models": cap.models if cap.installed else [],
            "relay_version": cap.version,
            "relay_mode": "v1",
        }
        if provider == "codex":
            # The bundled driver advertises discovery defaults, not an account
            # allowlist. Codex forwards arbitrary model IDs to the CLI, so let
            # installed daemon backends follow the admin-managed catalog.
            # Reconcile on every heartbeat for existing nodes as well as new ones.
            values["supported_models_mode"] = "inherit" if cap.installed else "restricted"
        elif cap.installed:
            # Native Claude CLI accepts catalog model IDs directly. Keep other
            # providers (MiniMax/Kimi/etc.) out unless the driver advertises them.
            native = [
                row.model
                for row in catalog
                if row.provider == "claude"
                and not row.retired
                and row.model.startswith(("claude-", "vllm/claude-"))
            ]
            retired = {row.model for row in catalog if row.provider == "claude" and row.retired}
            values["supported_models"] = [
                model for model in dict.fromkeys([*native, *cap.models]) if model not in retired
            ]
        if not cap.installed or cap.login == "required":
            values.update(
                health_status="down" if not cap.installed else "auth_fail",
                health_detail=cap.detail or "AI 未安装或未登录",
                health_checked_at=now(),
            )
        await session.execute(
            update(RelayServer)
            .where(RelayServer.runtime_node_id == node.id, RelayServer.model_provider == provider)
            .values(**values)
        )
        for model in cap.models:
            await session.execute(
                insert(ModelCatalog).values(provider=provider, model=model).on_conflict_do_nothing()
            )
    # Bound cleanup, indexed by deadline; terminal payloads are not retained indefinitely.
    expired = (
        select(RuntimeCall.id)
        .where(RuntimeCall.deadline < now())
        .order_by(RuntimeCall.deadline)
        .limit(100)
    )
    await session.execute(delete(RuntimeCall).where(RuntimeCall.id.in_(expired)))
    await session.commit()
    return {"code": 0, "data": {"draining": node.draining}}


@public_router.post("/poll")
async def poll(
    body: PollIn, request: Request, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    node = await node_auth(request, session)
    if body.abandoned:
        await session.execute(
            update(RuntimeCall).where(
                RuntimeCall.node_id == node.id,
                RuntimeCall.id.in_(body.abandoned),
                RuntimeCall.status.not_in(TERMINAL),
            ).values(status="failed", error="control_lost", request_enc="")
        )
    moment = now()
    await session.execute(
        update(RuntimeCall)
        .where(
            RuntimeCall.node_id == node.id,
            RuntimeCall.status.not_in(TERMINAL),
            (RuntimeCall.consumer_at < moment - timedelta(seconds=LEASE_SECONDS))
            | (RuntimeCall.deadline <= moment),
        )
        .values(status="cancelled", request_enc="")
    )
    active = set(
        await session.scalars(
            select(RuntimeCall.id).where(
                RuntimeCall.node_id == node.id,
                RuntimeCall.id.in_(body.running),
                RuntimeCall.status.not_in(TERMINAL),
            )
        )
    )
    cancelled = [str(i) for i in body.running if i not in active]
    commands = []
    if not node.draining and body.slots:
        rows = list(
            await session.scalars(
                select(RuntimeCall)
                .where(RuntimeCall.node_id == node.id, RuntimeCall.status == "queued")
                .order_by(RuntimeCall.created_at)
                .limit(min(body.slots, 4))
                .with_for_update(skip_locked=True)
            )
        )
        for row in rows:
            payload = json.loads(
                request.app.state.cipher.decrypt(row.request_enc, envelope_aad(row.id))
            )
            row.status, row.request_enc = "running", ""
            commands.append({"id": str(row.id), "provider": row.provider, **payload})
    await session.commit()
    return {
        "code": 0,
        "data": {"commands": commands, "cancelled": cancelled, "draining": node.draining},
    }


@public_router.post("/calls/{call_id}/frames")
async def frame(
    call_id: uuid.UUID,
    body: FrameIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    node = await node_auth(request, session)
    row = await session.scalar(
        select(RuntimeCall)
        .where(RuntimeCall.id == call_id, RuntimeCall.node_id == node.id)
        .with_for_update()
    )
    if not row:
        raise not_found("运行时请求不存在")
    if row.status == "cancelled" or row.deadline <= now():
        raise ApiError(410, 410, "运行时请求已取消")
    if body.seq < row.next_seq:
        return {"code": 0, "data": None}  # idempotent publish after a lost HTTP response
    if row.status in TERMINAL or row.status != "running" or body.seq != row.next_seq:
        raise ApiError(409, 409, "响应序号不匹配")
    try:
        decoded = base64.b64decode(body.data, validate=True)
    except ValueError as exc:
        raise ApiError(422, 422, "响应编码无效") from exc
    if len(decoded) > CHUNK_SIZE or row.response_bytes + len(decoded) > MAX_RESPONSE:
        raise ApiError(413, 413, "运行时响应过大")
    buffered = await session.scalar(
        select(func.count()).select_from(RuntimeChunk).where(RuntimeChunk.call_id == call_id)
    )
    if buffered and buffered >= 128:
        raise ApiError(429, 429, "等待响应消费者")
    if body.status_code is not None:
        if row.status_code is not None and row.status_code != body.status_code:
            raise ApiError(409, 409, "响应头已经提交")
        row.status_code = body.status_code
        row.content_type = body.content_type.replace("\r", "").replace("\n", "")
    if decoded:
        session.add(
            RuntimeChunk(
                call_id=call_id,
                seq=body.seq,
                data_enc=request.app.state.cipher.encrypt(body.data, chunk_aad(call_id, body.seq)),
            )
        )
    row.next_seq += 1
    row.response_bytes += len(decoded)
    if body.done or body.error:
        row.status, row.error = ("failed", body.error) if body.error else ("done", None)
        row.deadline = min(row.deadline, now() + timedelta(minutes=5))
    await session.commit()
    return {"code": 0, "data": None}


@router.get("/{node_id}/{provider}/session/{session_id}")
async def session_view(
    node_id: uuid.UUID,
    provider: Literal["claude", "codex"],
    session_id: str,
    _: User = Depends(MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> Response:
    import re

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", session_id):
        raise not_found("会话不存在")
    if not await session.get(RuntimeNode, node_id):
        raise not_found("运行时不存在")
    template = (ROOT / "runtime_daemon/drivers/pkg/sessions/session_viewer.html").read_text()
    template = re.sub(r"<link[^>]+https://[^>]+>", "", template)
    template = re.sub(r'<script src="https://[^>]+></script>', "", template)
    template = template.replace(
        "marked.setOptions({",
        "const marked = {setOptions() {}};\n"
        "const hljs = {getLanguage() {return false}, highlightElement() {}};\n"
        "marked.setOptions({",
    )
    template = template.replace("hljsLink.href =", "if (hljsLink) hljsLink.href =")
    template = template.replace("{{.SessionID}}", session_id).replace("{{.EventsJSON}}", "[]")
    template = template.replace(
        "const sessionId = location.pathname.split('/').filter(Boolean)[1];",
        f"const sessionId = '{session_id}';",
    )
    template = template.replace(
        "const wsUrl = wsProto + '//' + location.host + '/session/' + sessionId + '/ws';",
        "const wsUrl = location.pathname + '/events';",
    )
    template = template.replace("WebSocket.CONNECTING", "EventSource.CONNECTING").replace(
        "WebSocket.OPEN", "EventSource.OPEN"
    )
    template = template.replace("new WebSocket(wsUrl)", "new EventSource(wsUrl)")
    template = template.replace("ws.onclose = () => {", "ws.onerror = () => {\n    ws.close();")
    # Render untrusted model text as escaped preformatted content; never execute model HTML.
    template = template.replace(
        "return marked.parse(text);", "return '<pre>' + escHtml(text) + '</pre>';"
    )
    return Response(template, media_type="text/html", headers=NO_STORE)


@router.get("/{node_id}/{provider}/session/{session_id}/events")
async def session_events(
    node_id: uuid.UUID,
    provider: Literal["claude", "codex"],
    session_id: str,
    _: User = Depends(MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> Response:
    import re

    import httpx
    from fastapi.responses import StreamingResponse

    from coreman.core.runtime_nodes.transport import ReverseTransport

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", session_id):
        raise not_found("会话不存在")
    if not await session.get(RuntimeNode, node_id):
        raise not_found("运行时不存在")
    # Release the reader transaction before holding a long-lived stream.
    await session.commit()

    async def stream() -> AsyncIterator[bytes]:
        async with httpx.AsyncClient(
            transport=ReverseTransport(node_id, provider),
            base_url="http://runtime",
            timeout=30,
        ) as client:
            async with client.stream("GET", f"/session/{session_id}/events") as response:
                if response.status_code != 200:
                    yield b"event: unavailable\ndata: {}\n\n"
                    return
                async for chunk in response.aiter_bytes():
                    yield chunk

    return StreamingResponse(stream(), media_type="text/event-stream", headers=NO_STORE)
