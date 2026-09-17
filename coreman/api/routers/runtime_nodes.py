"""运行时节点管理端：安装链接、节点列表与启停/排空、会话查看器。

节点自身调用的协议接口（安装脚本、注册、心跳、领取命令、回传响应帧）见 runtime_protocol.py。
"""

from __future__ import annotations

import secrets
import shlex
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip, current_user, get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.permissions import require_roles
from coreman.api.security import verify_csrf
from coreman.core.audit import record_audit
from coreman.core.db.models import (
    ChatLog,
    RelayServer,
    RuntimeCall,
    RuntimeInstallLink,
    RuntimeNode,
    Team,
    User,
)
from coreman.core.runtime_nodes.common import (
    MAX_CA_PEM_BYTES,
    absolute_root,
    token_digest,
    validate_ca_pem,
)
from coreman.core.runtime_nodes.transport import (
    TERMINAL,
    notify_call,
    notify_node,
    now,
    online,
)

router = APIRouter(prefix="/api/admin/runtime-nodes", dependencies=[Depends(verify_csrf)])
MANAGERS = require_roles("ai_committee", "platform_admin")
ROOT = Path(__file__).resolve().parents[3]
NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
CA_FILE_HINT = "coreman-ca.pem"


class InstallOptions(BaseModel):
    proxy: str = Field(default="", max_length=500)
    control_proxy: str = Field(default="", max_length=500)
    environment: Literal["auto", "host", "chroot", "nspawn"] = "auto"
    claude_path: str = Field(default="", max_length=500)
    codex_path: str = Field(default="", max_length=500)
    git_hosts: list[str] = Field(default_factory=lambda: ["github.com"], max_length=30)
    max_concurrent: int = Field(default=10, ge=1, le=32)
    install_claude_probe: bool = False
    # 平台使用私有 CA 时的 PEM 证书：与 proxy 一样随配置注入安装脚本，
    # 安装脚本取出后写成节点数据目录下的 ca.pem，并在节点配置里记为 ca_file。
    ca_pem: str = Field(default="", max_length=MAX_CA_PEM_BYTES)

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

    @field_validator("ca_pem")
    @classmethod
    def validate_ca(cls, value: str) -> str:
        return validate_ca_pem(value)

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
    workspace_root: str = Field(default="/home/ai", min_length=2, max_length=400)
    team_id: uuid.UUID | None = None
    visibility: Literal["all", "admins"] = "all"
    options: InstallOptions = Field(default_factory=InstallOptions)

    _root = field_validator("workspace_root")(absolute_root)


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
        token_hash=token_digest(token),
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
        diff={
            "workspace_root": [None, body.workspace_root],
            **({"ca_pem": [None, "provided"]} if body.options.ca_pem else {}),
        },
        ip=client_ip(request),
    )
    await session.commit()
    base = request.app.state.settings.public_base_url
    url = f"{base}/api/runtime/install/{link.id}.{token}/install.sh"
    data: dict[str, Any] = {**link_out(link), "url": url}
    if body.options.ca_pem:
        # 下载安装脚本这一步本身也要信任私有 CA，curl 不会读取安装链接里的证书。
        data["command"] = f"curl --cacert {CA_FILE_HINT} -fsSL {shlex.quote(url)} | sh"
        data["ca_hint"] = (
            f"平台使用私有 CA：先把同一份 CA 证书保存为当前目录下的 {CA_FILE_HINT}，再执行安装命令"
        )
    else:
        data["command"] = f"curl -fsSL {shlex.quote(url)} | sh"
    return {"code": 0, "data": data}


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
    request: Request,
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
        ip=client_ip(request),
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
                "protocol_version": n.protocol_version,
                "max_concurrent": n.max_concurrent,
                # 离线节点的在执行数已过时，不展示。
                "active_calls": n.active_calls if online(n) else None,
                "team_name": names.get(n.team_id) if n.team_id else None,
                "backends": backends,
            }
        )
    return {"code": 0, "data": data}


@router.patch("/{node_id}")
async def patch_node(
    node_id: uuid.UUID,
    body: NodePatch,
    request: Request,
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
        cancelled = await session.scalars(
            update(RuntimeCall)
            .where(RuntimeCall.node_id == node_id, RuntimeCall.status.not_in(TERMINAL))
            .values(status="cancelled", request_enc="")
            .returning(RuntimeCall.id)
        )
        for call_id in cancelled:
            await notify_call(session, call_id, node_id)
    # 启停或排空变化立即唤醒该节点挂起的长轮询，按新状态重新领取。
    await notify_node(session, node_id)
    await record_audit(
        session,
        action="runtime.update",
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="runtime_node",
        target_id=str(node_id),
        diff=diff,
        ip=client_ip(request),
    )
    await session.commit()
    return {"code": 0, "data": None}


async def _authorize_session_content(
    session: AsyncSession, request: Request, actor: User, session_id: str
) -> None:
    """Unknown sessions have no trustworthy owner; never proxy their raw content.

    Inspect all matching history, not just the latest row: older shared sessions may
    contain personal content even if a later turn has another conversation type.
    """
    try:
        identity = uuid.UUID(session_id)
    except ValueError:
        raise not_found("会话不存在") from None
    rows = (
        await session.execute(
            select(ChatLog.platform, ChatLog.chat_type, ChatLog.user_id).where(
                ChatLog.relay_session_id == identity
            )
        )
    ).all()
    if not rows or any(
        platform == "feishu"
        and chat_type == "single"
        and (request.cookies.get("bot_token") or user_id != actor.id)
        for platform, chat_type, user_id in rows
    ):
        raise not_found("会话不存在")


@router.get("/{node_id}/{provider}/session/{session_id}")
async def session_view(
    request: Request,
    node_id: uuid.UUID,
    provider: Literal["claude", "codex"],
    session_id: str,
    actor: User = Depends(MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> Response:
    import re

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", session_id):
        raise not_found("会话不存在")
    await _authorize_session_content(session, request, actor, session_id)
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
    request: Request,
    node_id: uuid.UUID,
    provider: Literal["claude", "codex"],
    session_id: str,
    actor: User = Depends(MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> Response:
    import re

    import httpx
    from fastapi.responses import StreamingResponse

    from coreman.core.runtime_nodes.transport import ReverseTransport

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", session_id):
        raise not_found("会话不存在")
    await _authorize_session_content(session, request, actor, session_id)
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
