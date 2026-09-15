"""运行时节点协议端：安装脚本与发布包下载、注册、心跳、领取命令与回传响应帧。

节点只出不进：所有请求都由节点主动发起，按节点 ID + 节点令牌鉴权（安装与注册按安装链接）。
管理台侧的节点管理见 runtime_nodes.py。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import get_session
from coreman.api.errors import ApiError, not_found
from coreman.core.audit import record_audit
from coreman.core.db.models import (
    ModelCatalog,
    RelayServer,
    RuntimeCall,
    RuntimeChunk,
    RuntimeInstallLink,
    RuntimeNode,
)
from coreman.core.runtime_nodes.common import PROTOCOL_VERSION, absolute_root, token_digest
from coreman.core.runtime_nodes.transport import (
    CHUNK_SIZE,
    LEASE_SECONDS,
    MAX_RESPONSE,
    TERMINAL,
    chunk_aad,
    envelope_aad,
    notify_call,
    notify_node,
    now,
    poll_seconds,
    subscribe_node,
    unsubscribe_node,
    wait_event,
)

router = APIRouter(prefix="/api/runtime", tags=["runtime-daemon"])
ROOT = Path(__file__).resolve().parents[3]
NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
# 长轮询最多挂起的秒数（只有节点带 wait 才挂起）；一批响应帧与待消费分片的上限。
MAX_POLL_WAIT = 25.0
MAX_BATCH_FRAMES = 64
MAX_BUFFERED_CHUNKS = 128


class RedactInstallAccess(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            args = list(record.args)
            if isinstance(args[2], str) and "/api/runtime/install/" in args[2]:
                args[2] = "/api/runtime/install/[REDACTED]"
                record.args = tuple(args)
        return True


logging.getLogger("uvicorn.access").addFilter(RedactInstallAccess())


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
    # 旧节点不上报协议版本，按 1 处理。
    protocol: int = Field(default=1, ge=1, le=1000)

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
    protocol: int = Field(default=1, ge=1, le=1000)
    # 协议 2 起上报；旧节点缺省为空，管理台显示为未知。
    max_concurrent: int | None = Field(default=None, ge=1, le=256)
    active_calls: int | None = Field(default=None, ge=0, le=1024)


class PollIn(BaseModel):
    running: list[uuid.UUID] = Field(default_factory=list, max_length=64)
    abandoned: list[uuid.UUID] = Field(default_factory=list, max_length=64)
    slots: int = Field(default=1, ge=0, le=32)
    # 新节点带上最长等待秒数做长轮询；旧节点不带，没有命令时立即返回。
    wait: float = Field(default=0, ge=0, le=MAX_POLL_WAIT)


class FrameIn(BaseModel):
    seq: int = Field(ge=0)
    data: str = Field(default="", max_length=CHUNK_SIZE * 4 // 3 + 4)
    status_code: int | None = Field(default=None, ge=100, le=599)
    content_type: str = Field(default="application/json", max_length=100)
    done: bool = False
    error: Literal["execution_failed", "connection_lost", "response_too_large"] | None = None


class FramesIn(BaseModel):
    """一批序号连续的响应帧（协议 2）；旧节点仍按单帧 FrameIn 回传。"""

    frames: list[FrameIn] = Field(min_length=1, max_length=MAX_BATCH_FRAMES)


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
    if (
        not link
        or not secrets.compare_digest(link.token_hash, token_digest(secret))
        or link.revoked_at
    ):
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
        or not secrets.compare_digest(node.token_hash, token_digest(token))
    ):
        raise ApiError(401, 401, "运行时已停用或凭证无效")
    return node


@router.get("/install/{token}/install.sh")
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


@router.get("/install/{token}/{platform}/{arch}/bundle")
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


@router.post("/enroll")
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
            or not secrets.compare_digest(node.token_hash, token_digest(body.node_token))
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
            token_hash=token_digest(body.node_token),
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
            protocol_version=body.protocol,
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
            "protocol": PROTOCOL_VERSION,
        },
    }


@router.post("/heartbeat")
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
            protocol_version=body.protocol,
            max_concurrent=body.max_concurrent,
            active_calls=body.active_calls,
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
    # 消费者已离开（租约过期）或超过期限的在途调用：长轮询不再每次检查，改在心跳里收尾。
    moment = now()
    stale = list(
        await session.scalars(
            update(RuntimeCall)
            .where(
                RuntimeCall.node_id == node.id,
                RuntimeCall.status.not_in(TERMINAL),
                (RuntimeCall.consumer_at < moment - timedelta(seconds=LEASE_SECONDS))
                | (RuntimeCall.deadline <= moment),
            )
            .values(status="cancelled", request_enc="")
            .returning(RuntimeCall.id)
        )
    )
    for call_id in stale:
        await notify_call(session, call_id, node.id)
    if stale:
        await notify_node(session, node.id)
    # Bound cleanup, indexed by deadline; terminal payloads are not retained indefinitely.
    expired = (
        select(RuntimeCall.id)
        .where(RuntimeCall.deadline < now())
        .order_by(RuntimeCall.deadline)
        .limit(100)
    )
    await session.execute(delete(RuntimeCall).where(RuntimeCall.id.in_(expired)))
    await session.commit()
    return {"code": 0, "data": {"draining": node.draining, "protocol": PROTOCOL_VERSION}}


@router.post("/poll")
async def poll(
    body: PollIn, request: Request, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    node = await node_auth(request, session)
    node_id = node.id
    ended: list[uuid.UUID] = []
    if body.abandoned:
        ended += await session.scalars(
            update(RuntimeCall)
            .where(
                RuntimeCall.node_id == node_id,
                RuntimeCall.id.in_(body.abandoned),
                RuntimeCall.status.not_in(TERMINAL),
            )
            .values(status="failed", error="control_lost", request_enc="")
            .returning(RuntimeCall.id)
        )
    if body.running:
        # 只核对节点报告在跑的调用：消费者租约过期或超过期限即取消。排队中的过期调用
        # 由领取条件跳过、心跳收尾，空闲节点的轮询不再执行写语句。
        moment = now()
        ended += await session.scalars(
            update(RuntimeCall)
            .where(
                RuntimeCall.node_id == node_id,
                RuntimeCall.id.in_(body.running),
                RuntimeCall.status.not_in(TERMINAL),
                (RuntimeCall.consumer_at < moment - timedelta(seconds=LEASE_SECONDS))
                | (RuntimeCall.deadline <= moment),
            )
            .values(status="cancelled", request_enc="")
            .returning(RuntimeCall.id)
        )
    for call_id in ended:
        await notify_call(session, call_id, node_id)
    # 先订阅再读库：读库与等待之间写入的命令一定能唤醒本次长轮询。
    wake = subscribe_node(node_id) if body.wait else None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + body.wait
    try:
        while True:
            if wake is not None:
                wake.clear()
            draining, cancelled, commands = await _collect(session, request, node_id, body)
            await session.commit()  # 等待期间不占用数据库连接
            remaining = deadline - loop.time()
            if commands or cancelled or wake is None or remaining <= 0:
                break
            await wait_event(wake, min(poll_seconds(), remaining))
    finally:
        if wake is not None:
            unsubscribe_node(node_id, wake)
    return {
        "code": 0,
        "data": {
            "commands": commands,
            "cancelled": cancelled,
            "draining": draining,
            "protocol": PROTOCOL_VERSION,
        },
    }


async def _collect(
    session: AsyncSession, request: Request, node_id: uuid.UUID, body: PollIn
) -> tuple[bool, list[str], list[dict[str, Any]]]:
    """一轮领取：节点报告在跑、但已进入终态的调用要取消；有空位时领取仍有消费者的排队调用。"""
    node = await session.get(RuntimeNode, node_id, populate_existing=True)
    if node is None or not node.is_active:
        # 等待期间被停用：在跑的全部取消，下一次轮询按凭证失效处理。
        return True, [str(i) for i in body.running], []
    active = set(
        await session.scalars(
            select(RuntimeCall.id).where(
                RuntimeCall.node_id == node_id,
                RuntimeCall.id.in_(body.running),
                RuntimeCall.status.not_in(TERMINAL),
            )
        )
    )
    cancelled = [str(i) for i in body.running if i not in active]
    commands: list[dict[str, Any]] = []
    if not node.draining and body.slots:
        moment = now()
        rows = list(
            await session.scalars(
                select(RuntimeCall)
                .where(
                    RuntimeCall.node_id == node_id,
                    RuntimeCall.status == "queued",
                    RuntimeCall.consumer_at >= moment - timedelta(seconds=LEASE_SECONDS),
                    RuntimeCall.deadline > moment,
                )
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
            await notify_call(session, row.id, node_id)
    return node.draining, cancelled, commands


@router.post("/calls/{call_id}/frames")
async def frame(
    call_id: uuid.UUID,
    body: FramesIn | FrameIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    node = await node_auth(request, session)
    frames = body.frames if isinstance(body, FramesIn) else [body]
    row = await session.scalar(
        select(RuntimeCall)
        .where(RuntimeCall.id == call_id, RuntimeCall.node_id == node.id)
        .with_for_update()
    )
    if not row:
        raise not_found("运行时请求不存在")
    if row.status == "cancelled" or row.deadline <= now():
        raise ApiError(410, 410, "运行时请求已取消")
    # 丢了响应后的重发：已落库的序号直接跳过，其余必须从当前序号起连续，终态帧只能在最后。
    fresh = [item for item in frames if item.seq >= row.next_seq]
    if not fresh:
        return {"code": 0, "data": None}
    if (
        row.status != "running"
        or any(item.seq != row.next_seq + index for index, item in enumerate(fresh))
        or any(item.done or item.error for item in fresh[:-1])
    ):
        raise ApiError(409, 409, "响应序号不匹配")
    try:
        decoded = [base64.b64decode(item.data, validate=True) for item in fresh]
    except ValueError as exc:
        raise ApiError(422, 422, "响应编码无效") from exc
    if (
        any(len(data) > CHUNK_SIZE for data in decoded)
        or row.response_bytes + sum(len(data) for data in decoded) > MAX_RESPONSE
    ):
        raise ApiError(413, 413, "运行时响应过大")
    incoming = sum(1 for data in decoded if data)
    if incoming:
        buffered = await session.scalar(
            select(func.count()).select_from(RuntimeChunk).where(RuntimeChunk.call_id == call_id)
        )
        if (buffered or 0) + incoming > MAX_BUFFERED_CHUNKS:
            raise ApiError(429, 429, "等待响应消费者")
    for item, data in zip(fresh, decoded, strict=True):
        if item.status_code is not None:
            if row.status_code is not None and row.status_code != item.status_code:
                raise ApiError(409, 409, "响应头已经提交")
            row.status_code = item.status_code
            row.content_type = item.content_type.replace("\r", "").replace("\n", "")
        if data:
            session.add(
                RuntimeChunk(
                    call_id=call_id,
                    seq=item.seq,
                    data_enc=request.app.state.cipher.encrypt(
                        item.data, chunk_aad(call_id, item.seq)
                    ),
                )
            )
        row.next_seq += 1
        row.response_bytes += len(data)
        if item.done or item.error:
            row.status, row.error = ("failed", item.error) if item.error else ("done", None)
            row.deadline = min(row.deadline, now() + timedelta(minutes=5))
    await notify_call(session, call_id, node.id)
    await session.commit()
    return {"code": 0, "data": None}
