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
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, StrictBool, field_validator
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from coreman.api.deps import client_ip, get_session
from coreman.api.errors import ApiError, not_found
from coreman.core.assistants import keys, sealed, transcripts
from coreman.core.assistants.runtime import ASSISTANT_CONCURRENCY_KEY, node_sandboxed
from coreman.core.audit import record_audit
from coreman.core.crypto import DecryptError
from coreman.core.db.models import (
    ModelCatalog,
    RelayServer,
    RuntimeCall,
    RuntimeChunk,
    RuntimeInstallLink,
    RuntimeNode,
)
from coreman.core.logging import get_logger
from coreman.core.relay.models import BACKENDS, default_model, known_effort_flags
from coreman.core.runtime_nodes.bundle import stale_bundle_reason
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
log = get_logger(__name__)
# 转录传输（上传与下载）同时最多这么多个，等位置最多这么多秒；读请求体的总时限。
TRANSCRIPT_TRANSFERS = 2
TRANSFER_WAIT_SECONDS = 60.0
BODY_SECONDS = 300.0
TRANSFER_SLOTS = asyncio.Semaphore(TRANSCRIPT_TRANSFERS)
ROOT = Path(__file__).resolve().parents[3]
NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
# 长轮询最多挂起的秒数（只有节点带 wait 才挂起）；一批响应帧与待消费分片的上限。
# 心跳按 CLI 未安装 / 未登录记下的健康详情；登录恢复后据此认出并清掉。
HEARTBEAT_HEALTH_DETAIL = "AI 未安装或未登录"
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


class SeatState(BaseModel):
    """节点上一个席位（同一种 AI 的一个登录账号）的状态，只用于管理台展示与切换。"""

    id: str = Field(pattern=r"^(default|[a-f0-9]{12})$")
    label: str = Field(default="", max_length=60)
    account: str = Field(default="", max_length=255)
    login: Literal["ready", "required", "unknown"] = "unknown"
    active: StrictBool = False

    @field_validator("label", mode="before")
    @classmethod
    def printable(cls, value: object) -> str:
        return "".join(c for c in str(value or "") if c.isprintable()).strip()[:60]


class Capability(BaseModel):
    # 驱动能在原有能力之上附加挂载本人飞书工具（MCP）。旧节点上报的
    # feishu_personal_restricted_v1 是替换式的受限模式，已不再使用，按未知字段丢弃。
    feishu_personal_tools_v1: StrictBool = False
    # 驱动能在飞书群聊里挂载群文档工具（MCP，coreman_feishu_docs）。旧节点没有，群里读不了文档。
    feishu_group_docs_v1: StrictBool = False
    # 驱动能在原有能力之上附加挂载本人企业微信工具（MCP）。
    wecom_personal_tools_v1: StrictBool = False
    # 驱动的会话记录可以给本人看：不含环境变量（机器人密钥、访问令牌）。
    # 旧节点没有，本人凭链接查看需等升级。
    owner_session_view_v1: StrictBool = False
    # 驱动能挂载业务系统操作目录（MCP）。旧节点没有，业务系统沿用原来的一行说明。
    systems_catalog_v1: StrictBool = False
    # 驱动能把业务系统 MCP（token_delivery=mcp）挂成 system_<key>。旧节点上这类系统显示不可用。
    system_mcp_v1: StrictBool = False
    # 驱动续不上原会话时（Claude --resume 找不到会话、Codex 找不到 rollout），用平台带的续接文本
    # 开新会话。旧节点没有，会从空会话开始。
    resume_fallback_v1: StrictBool = False
    # 驱动能把新会话从另一个会话分叉出来（飞书话题继承群主会话）。旧节点没有，退回附上群主会话
    # 最近的对话。
    session_fork_v1: StrictBool = False
    # 记忆在工作目录的 .coreman-memory/，Claude 与 Codex 共用。旧节点没有，Codex 读不到记忆。
    memory_layout_v1: StrictBool = False
    # 守护进程自检通过、能在沙箱里跑个人助理的轮次（见 runtime_daemon/sandbox.py）。旧节点没有，
    # 也就收不到助理的轮次。写坏的值（非布尔真值）按没有处理，不让心跳被拒。
    assistant_sandbox_v1: StrictBool = False
    # 节点能在多个席位之间切换（见 runtime_daemon/seats.py）。旧节点没有，管理台提示升级。
    seat_switch_v1: StrictBool = False
    seats: list[SeatState] = Field(default_factory=list, max_length=21)

    @field_validator("assistant_sandbox_v1", mode="before")
    @classmethod
    def sandbox_flag(cls, value: object) -> bool:
        return value is True

    installed: bool = False
    version: str = Field(default="", max_length=100)
    # CLI 登录的账号邮箱，只用于展示；旧节点不上报时为空。
    account: str = Field(default="", max_length=255)
    login: Literal["ready", "required", "unknown"] = "unknown"
    health: Literal["healthy", "down", "auth_fail", "timeout", "unknown"] = "unknown"
    plan_type: str | None = Field(default=None, max_length=40, pattern=r"^[a-zA-Z0-9_.-]+$")
    account_verified: StrictBool | None = None
    detail: str = Field(default="", max_length=255)
    models: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("models")
    @classmethod
    def model_lengths(cls, value: list[str]) -> list[str]:
        if any(not m or len(m) > 150 for m in value):
            raise ValueError("模型名称长度无效")
        return list(dict.fromkeys(value))


class RootChangeResult(BaseModel):
    id: str = Field(max_length=100)
    path: str = Field(min_length=2, max_length=400)
    status: Literal["applied", "failed"]


class ProxyState(BaseModel):
    """节点启动时定下的出站代理：config.json 的 proxy 优先，否则沿用服务环境的变量。"""

    source: Literal["coreman", "environment", "none"]
    url: str = Field(default="", max_length=500)
    # config.json 的 proxy 已被改成别的值，重启 Daemon 才会生效。
    pending: StrictBool = False

    @field_validator("url", mode="before")
    @classmethod
    def printable(cls, value: object) -> str:
        # 手工改坏的配置只影响展示。节点已去掉账号密码，这里再兜底一次，密码不落库。
        text = "".join(c for c in str(value or "") if c.isprintable()).strip()[:500]
        scheme, sep, rest = text.partition("://")
        if not sep:
            scheme, rest = "", text
        netloc, slash, path = rest.partition("/")
        if "@" in netloc:
            netloc = netloc.rsplit("@", 1)[1]
        return scheme + sep + netloc + slash + path

    @field_validator("pending", mode="before")
    @classmethod
    def flag(cls, value: object) -> bool:
        return value is True


class HeartbeatIn(BaseModel):
    claude: Capability
    codex: Capability
    # 协议 2 的节点从支持 OpenCode 的版本起上报；旧节点缺省为未安装。
    opencode: Capability = Field(default_factory=Capability)
    version: str = Field(max_length=100)
    service_status: Literal[
        "systemd-user", "systemd-user-session", "systemd", "launchd", "supervised", "foreground"
    ]
    protocol: int = Field(default=1, ge=1, le=1000)
    # 协议 2 起上报；旧节点缺省为空，管理台显示为未知。
    max_concurrent: int | None = Field(default=None, ge=1, le=256)
    active_calls: int | None = Field(default=None, ge=0, le=1024)
    # 节点 config.json 的 Git 主机白名单，只读展示；旧节点不上报时为空。
    git_hosts: list[str] | None = None
    # CLI 实际继承到的出站代理，只读展示；旧节点不上报时为空（管理台显示为未知）。
    proxy: ProxyState | None = None
    root_edit_supported: bool = False
    root_change_result: RootChangeResult | None = None
    # 本机指纹（机器编号的哈希，64 位十六进制）与守护进程的 uid；旧节点不上报。只用于判断
    # 同一台机器上的节点是不是同一个系统用户，写坏了按没上报处理，不让心跳被拒。
    machine: str | None = None
    uid: int | None = None

    @field_validator("machine", mode="before")
    @classmethod
    def machine_hash(cls, value: object) -> str | None:
        if (
            isinstance(value, str)
            and len(value) == 64
            and all(c in "0123456789abcdef" for c in value)
        ):
            return value
        return None

    @field_validator("uid", mode="before")
    @classmethod
    def system_uid(cls, value: object) -> int | None:
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2**32:
            return value
        return None

    @field_validator("proxy", mode="before")
    @classmethod
    def known_proxy(cls, value: object) -> object:
        # 只用于展示的字段不能让心跳被拒：新节点上报了本版本不认识的来源时按未知处理。
        sources = ("coreman", "environment", "none")
        return value if isinstance(value, dict) and value.get("source") in sources else None

    @field_validator("git_hosts", mode="before")
    @classmethod
    def reported_hosts(cls, value: object) -> list[str] | None:
        # 手工改坏的配置不能让整个心跳被拒（节点会显示离线）：原样保留可显示的条目，
        # 写成 URL 之类不匹配的条目也要让管理员看到。
        if not isinstance(value, list):
            return None
        hosts = (
            "".join(c for c in h if c.isprintable()).strip()[:253]
            for h in value[:50]
            if isinstance(h, str)
        )
        return list(dict.fromkeys(h for h in hosts if h))


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
    # 从源码运行时不下发过期的发布包；镜像内没有源码树，不做这项检查。
    stale = stale_bundle_reason(ROOT, path)
    if stale:
        raise ApiError(503, 503, stale)
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
            owner_id=link.created_by,
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
            ip=client_ip(request),
        )
        session.add(node)
        await session.flush()
        # OpenCode 的 relay 等节点心跳报告装了 CLI 再建（ensure_backends），没装的节点不多一行。
        for provider in ("claude", "codex"):
            session.add(new_relay(request, node, provider, body.node_token))
        link.node_id, link.used_at = node.id, now()
        await record_audit(
            session,
            action="runtime.enroll",
            actor_id=link.created_by,
            target_type="runtime_node",
            target_id=str(node.id),
            diff={"install_link": [None, str(link.id)]},
            ip=client_ip(request),
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


def new_relay(request: Request, node: RuntimeNode, provider: str, node_token: str) -> RelayServer:
    """节点上一种 AI 后端的 relay；节点 Agent 的令牌由节点令牌按 provider 派生。"""
    agent_token = hmac.new(node_token.encode(), provider.encode(), hashlib.sha256).hexdigest()
    return RelayServer(
        runtime_node_id=node.id,
        name=f"{node.id}/{provider}",
        model_provider=provider,
        team_id=node.team_id,
        visibility=node.visibility,
        owner_id=node.owner_id,
        supported_models_mode="restricted",
        supported_models=[],
        agent_token_enc=request.app.state.cipher.encrypt(
            agent_token, "relay_servers.agent_token_enc"
        ),
    )


async def ensure_backends(
    session: AsyncSession, request: Request, node: RuntimeNode, body: HeartbeatIn
) -> dict[str, dict[str, str]]:
    """节点各后端的 relay；报告已安装却还没有 relay 的后端（注册之后才支持的，如 OpenCode）
    在这里补建。心跳带的是节点令牌，派生出的 Agent 令牌与节点自己算的一致。返回给节点补进
    config.json 的 backends。"""
    relays = {
        r.model_provider: r
        for r in await session.scalars(
            select(RelayServer).where(RelayServer.runtime_node_id == node.id)
        )
    }
    token = request.headers.get("Authorization", "").removeprefix("Bearer ")
    for provider in BACKENDS:
        if provider not in relays and getattr(body, provider).installed:
            relays[provider] = new_relay(request, node, provider, token)
            session.add(relays[provider])
    await session.flush()
    return {provider: {"relay_id": str(r.id)} for provider, r in relays.items()}


@router.post("/heartbeat")
async def heartbeat(
    body: HeartbeatIn, request: Request, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    node = await node_auth(request, session)
    # Serialize acknowledgements with edits; never accept an old/mismatched result.
    await session.refresh(node, with_for_update=True)
    change = node.root_change or {}
    result = body.root_change_result
    if (
        change.get("status") == "pending"
        and result
        and result.id == change.get("id")
        and result.path == change.get("path")
    ):
        node.root_change = result.model_dump()
        if result.status == "applied":
            node.workspace_root = result.path
    node.root_edit_supported = body.root_edit_supported
    await session.execute(
        update(RuntimeNode)
        .where(RuntimeNode.id == node.id)
        .values(
            heartbeat_at=now(),
            ip=client_ip(request),
            version=body.version,
            service_status=body.service_status,
            protocol_version=body.protocol,
            max_concurrent=body.max_concurrent,
            active_calls=body.active_calls,
            git_hosts=body.git_hosts,
            proxy=body.proxy.model_dump() if body.proxy else None,
            capabilities={p: getattr(body, p).model_dump() for p in BACKENDS},
            machine_fingerprint=body.machine,
            uid=body.uid,
        )
    )
    from sqlalchemy.dialects.postgresql import insert

    from coreman.core.relay.models import load_catalog

    catalog = await load_catalog(session)
    backends = await ensure_backends(session, request, node, body)

    for provider in BACKENDS:
        cap = getattr(body, provider)
        # CLI discovery never overrides the real inference health probe's result.
        values: dict[str, Any] = {
            "supported_models": cap.models if cap.installed else [],
            "relay_version": cap.version,
            "relay_mode": "v1",
        }
        if provider != "claude":
            # The bundled driver advertises discovery defaults, not an account
            # allowlist. Codex and OpenCode forward arbitrary model IDs to the CLI,
            # so let installed daemon backends follow the admin-managed catalog.
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
                health_detail=cap.detail or HEARTBEAT_HEALTH_DETAIL,
                health_checked_at=now(),
            )
        await session.execute(
            update(RelayServer)
            .where(RelayServer.runtime_node_id == node.id, RelayServer.model_provider == provider)
            .values(**values)
        )
        if cap.installed and cap.login == "ready":
            # 心跳自己记的「未安装/未登录」，登录恢复后清掉，等下一次推理检查给结论；
            # 否则一次偶发的未登录会一直挂着。推理检查记的结果不动。
            await session.execute(
                update(RelayServer)
                .where(
                    RelayServer.runtime_node_id == node.id,
                    RelayServer.model_provider == provider,
                    RelayServer.health_status.in_(("down", "auth_fail")),
                    RelayServer.health_detail.in_((HEARTBEAT_HEALTH_DETAIL, "未安装 CLI")),
                )
                .values(health_status="unknown", health_detail=None, health_checked_at=now())
            )
        for model in cap.models:
            await session.execute(
                insert(ModelCatalog)
                .values(provider=provider, model=model, **known_effort_flags(model))
                .on_conflict_do_nothing()
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
    return {
        "code": 0,
        "data": {
            "draining": node.draining,
            "protocol": PROTOCOL_VERSION,
            "root_change": node.root_change,
            "backends": backends,
            # 节点健康检查用的模型：驱动发现的列表第一个未必是账号能用的。
            "probe_models": {p: default_model(p, catalog) for p in BACKENDS},
            "model_activity": {
                r.model_provider: {
                    "observed_at": r.model_health.get("observed_at"),
                    "seat_id": r.model_health.get("seat_id"),
                }
                for r in await session.scalars(
                    select(RelayServer).where(RelayServer.runtime_node_id == node.id)
                )
                if r.model_health and r.model_health.get("observed_at")
            },
        },
    }


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
        # 只看每种 AI 自己的沙箱能力：能隔离 claude、不能隔离 codex 的节点领不到 codex 的助理调用。
        capable = [p for p in BACKENDS if node_sandboxed(node, p)]
        # 并发上限在锁普通调用之前读好：读设置可能再开一个连接，不能让它压在已锁的行上。
        limit = await _assistant_limit(request) if capable else 0
        moment = now()
        claimable = (
            RuntimeCall.node_id == node_id,
            RuntimeCall.status == "queued",
            RuntimeCall.consumer_at >= moment - timedelta(seconds=LEASE_SECONDS),
            RuntimeCall.deadline > moment,
        )
        rows = list(
            await session.scalars(
                select(RuntimeCall)
                .where(*claimable, RuntimeCall.sandbox.is_(False))
                .order_by(RuntimeCall.created_at)
                .limit(min(body.slots, 4))
                .with_for_update(skip_locked=True)
            )
        )
        room = min(body.slots, 4) - len(rows)
        if room > 0 and capable:
            # 助理的查询放在保存点里：它出任何错，只丢掉这一部分，同一轮已领的普通调用照常返回。
            try:
                async with session.begin_nested():
                    rows += await _claim_sandboxed(
                        session, node_id, claimable, room, capable, limit
                    )
            except Exception as exc:
                log.warning("assistant_claim_failed", error_type=type(exc).__name__)
        for row in rows:
            payload = json.loads(
                request.app.state.cipher.decrypt(row.request_enc, envelope_aad(row.id))
            )
            row.status, row.request_enc = "running", ""
            commands.append({"id": str(row.id), "provider": row.provider, **payload})
            await notify_call(session, row.id, node_id)
    return node.draining, cancelled, commands


async def _assistant_limit(request: Request) -> int:
    """每个节点同时跑的助理轮次上限：读不到、写坏了按 2，其余夹在 1 到 32。"""
    try:
        stored = await request.app.state.settings_store.get(ASSISTANT_CONCURRENCY_KEY, default=2)
    except Exception:
        return 2
    if not isinstance(stored, int) or isinstance(stored, bool):
        return 2
    return max(1, min(32, stored))


async def _claim_sandboxed(
    session: AsyncSession,
    node_id: uuid.UUID,
    claimable: tuple[Any, ...],
    room: int,
    providers: Sequence[str],
    limit: int,
) -> list[RuntimeCall]:
    """个人助理的调用：普通调用领完之后才领，只领节点能隔离的 provider，且不超过助理并发上限。

    上限按节点算（不分 provider）：它管的是这台机器同时跑多少个沙箱，不是每种 AI 各多少。
    超出的留在队列里，由调用方按排队上限等待；普通调用从不排在它们后面。
    """
    running = await session.scalar(
        select(func.count())
        .select_from(RuntimeCall)
        .where(
            RuntimeCall.node_id == node_id,
            RuntimeCall.sandbox.is_(True),
            RuntimeCall.status == "running",
        )
    )
    room = min(room, limit - (running or 0))
    if room <= 0:
        return []
    return list(
        await session.scalars(
            select(RuntimeCall)
            .where(*claimable, RuntimeCall.sandbox.is_(True), RuntimeCall.provider.in_(providers))
            .order_by(RuntimeCall.created_at)
            .limit(room)
            .with_for_update(skip_locked=True)
        )
    )


@asynccontextmanager
async def _transfer_slot() -> AsyncIterator[None]:
    """转录传输同时只放 TRANSCRIPT_TRANSFERS 个：一个 50 MB 的转录在本进程里要占好几份内存。"""
    try:
        await asyncio.wait_for(TRANSFER_SLOTS.acquire(), TRANSFER_WAIT_SECONDS)
    except TimeoutError:
        raise ApiError(503, 503, "转录传输繁忙") from None
    try:
        yield
    finally:
        TRANSFER_SLOTS.release()


@router.get("/assistant-transcripts/{transfer}")
async def transcript_download(
    transfer: uuid.UUID, request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    """节点续聊前取转录：只认派这一轮时给它的交接编号。没有、别人的、读不出来都回同一个 404。"""
    node_id = (await node_auth(request, session)).id
    # 等名额可能要很久：先把认证用过的连接还回去，拿到名额之后 download 再开事务。
    await session.rollback()
    # 这个请求只读一个助理的一份转录：解不开按没有处理，不报 500。
    sealed.tolerate_undecryptable(session)
    async with _transfer_slot():
        try:
            data = await transcripts.download(session, node_id=node_id, transfer=transfer)
        except (DecryptError, sealed.ContentLocked, keys.KeyUnavailable):
            data = None
        await session.commit()
    if data is None:
        raise ApiError(404, 404, "转录不可用")
    return Response(data, media_type="application/octet-stream", headers=NO_STORE)


@router.put("/assistant-transcripts/{transfer}")
async def transcript_upload(
    transfer: uuid.UUID, request: Request, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """节点交回本轮转录（请求体是 tar.gz 原始字节）。

    编号失效、不是这个节点的、本轮已办完、存不下（密钥销毁、解不开、行已删）都回同一个 410：
    节点不必再传，也看不出别的节点有没有这个编号。
    """
    node_id = (await node_auth(request, session)).id
    status = request.headers.get("X-Transcript-Status", "")
    if status not in transcripts.STATUSES:
        raise ApiError(422, 422, "转录状态无效")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > transcripts.MAX_BYTES:
        raise ApiError(413, 413, "转录过大")
    # 读请求体可能很久：先把认证用过的连接还回去，不在读取期间占着事务；行锁在读完之后才拿。
    await session.rollback()
    async with _transfer_slot():
        data = await _read_body(request)
        if (status == "ok") != bool(data):
            raise ApiError(422, 422, "转录内容与状态不符")
        sealed.tolerate_undecryptable(session)
        try:
            result = await transcripts.upload(
                session, node_id=node_id, transfer=transfer, status=status, data=data
            )
        except (DecryptError, sealed.ContentLocked, keys.KeyUnavailable, StaleDataError):
            result = "stale"
        if result == "stale":
            await session.rollback()
            raise ApiError(410, 410, "交接编号已失效")
        await session.commit()
    return {"code": 0, "data": {"result": result}}


async def _read_body(request: Request) -> bytes:
    """读请求体：总大小与总时长都有上限，读的过程中不占数据库连接。"""
    chunks: list[bytes] = []
    size = 0

    async def read() -> None:
        nonlocal size
        async for chunk in request.stream():
            size += len(chunk)
            if size > transcripts.MAX_BYTES:
                raise ApiError(413, 413, "转录过大")
            chunks.append(chunk)

    try:
        await asyncio.wait_for(read(), BODY_SECONDS)
    except TimeoutError:
        raise ApiError(408, 408, "转录上传超时") from None
    data = b"".join(chunks)
    chunks.clear()
    return data


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
