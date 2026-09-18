"""运行时节点管理端：安装链接、节点列表、编辑/启停/排空/删除、会话查看器。

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
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip, current_user, get_session
from coreman.api.errors import ApiError, forbidden, not_found
from coreman.api.permissions import require_roles
from coreman.api.security import verify_csrf
from coreman.core import session_links
from coreman.core.audit import record_audit
from coreman.core.db.models import (
    Bot,
    ChatLog,
    FeishuPersonalGrant,
    RelayServer,
    RuntimeCall,
    RuntimeInstallLink,
    RuntimeNode,
    Task,
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
MANAGER_ROLES = ("ai_committee", "platform_admin")
MANAGERS = require_roles(*MANAGER_ROLES)
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
    workspace_root: str | None = Field(default=None, min_length=2, max_length=400)

    @field_validator("workspace_root")
    @classmethod
    def valid_root(cls, value: str | None) -> str | None:
        return absolute_root(value) if value is not None else None

    # 显式传 null 表示改为公共池；不传表示不改团队。
    team_id: uuid.UUID | None = None
    is_active: bool | None = None
    draining: bool | None = None


def _plain(value: Any) -> Any:
    """审计 diff 落 JSONB，UUID 转成字符串。"""
    return str(value) if isinstance(value, uuid.UUID) else value


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
                "root_change": n.root_change,
                "root_edit_supported": n.root_edit_supported,
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
                "team_id": str(n.team_id) if n.team_id else None,
                "git_hosts": n.git_hosts,
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
        select(RuntimeNode).where(RuntimeNode.id == node_id).with_for_update(key_share=True)
    )
    if not node:
        raise not_found("运行时不存在")
    changes = {
        k: v
        for k, v in body.model_dump(exclude_unset=True).items()
        if v is not None or k == "team_id"
    }
    if changes.get("team_id") and not await session.get(Team, changes["team_id"]):
        raise ApiError(422, 422, "团队不存在")
    diff = {k: [_plain(getattr(node, k)), _plain(v)] for k, v in changes.items()}
    root = changes.pop("workspace_root", None)
    if root is not None and root != node.workspace_root:
        if (node.root_change or {}).get("status") == "pending":
            raise ApiError(409, 409, "项目主目录修改等待 Runtime 确认，请稍后重试")
        if not node.root_edit_supported:
            raise ApiError(409, 409, "请先升级 Runtime，再修改项目主目录")
        if not online(node) or not node.is_active or changes.get("is_active") is False:
            raise ApiError(409, 409, "请先启用运行时并等待其上线")
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"workspace:{node_id}"},
        )
        relay_ids = select(RelayServer.id).where(RelayServer.runtime_node_id == node_id)
        bound = await session.scalar(
            select(Bot.id)
            .where(
                or_(
                    Bot.relay_server_id.in_(relay_ids), Bot.workspace_target_relay_id.in_(relay_ids)
                )
            )
            .limit(1)
        )
        if bound:
            raise ApiError(409, 409, "仍有 AI 员工使用或迁移到该运行时，请先切换它们的运行时")
        node.root_change = {"id": str(uuid.uuid4()), "path": root, "status": "pending"}
    for key, value in changes.items():
        setattr(node, key, value)
    # 实例的团队与启停跟随节点：创建/切换 AI 员工时按实例的团队做可用范围校验。
    # 已绑定的 AI 员工不受团队变更影响。
    relay_values = {k: changes[k] for k in ("team_id", "is_active") if k in changes}
    if relay_values:
        await session.execute(
            update(RelayServer).where(RelayServer.runtime_node_id == node_id).values(**relay_values)
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


@router.delete("/{node_id}")
async def delete_node(
    node_id: uuid.UUID,
    request: Request,
    actor: User = Depends(MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """删除节点及其 Claude/Codex 实例；仍有 AI 员工使用（含迁移目标）时拒绝。

    节点凭证随之失效，主机上的 Daemon 之后只会收到 401，需在主机上卸载。
    """
    node = await session.scalar(
        select(RuntimeNode).where(RuntimeNode.id == node_id).with_for_update()
    )
    if not node:
        raise not_found("运行时不存在")
    # 与创建/切换 AI 员工时的工作目录分配共用同一把锁，避免删除期间有员工绑到该节点上。
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"workspace:{node_id}"},
    )
    relay_ids = select(RelayServer.id).where(RelayServer.runtime_node_id == node_id)
    bots = list(
        await session.scalars(
            select(Bot.name)
            .where(
                or_(
                    Bot.relay_server_id.in_(relay_ids),
                    Bot.workspace_target_relay_id.in_(relay_ids),
                )
            )
            .order_by(Bot.name)
        )
    )
    if bots:
        shown = "、".join(bots[:5]) + (f" 等 {len(bots)} 个" if len(bots) > 5 else "")
        raise ApiError(409, 409, f"仍有 AI 员工使用该运行时：{shown}。请先为它们切换运行时")
    cancelled = await session.scalars(
        update(RuntimeCall)
        .where(RuntimeCall.node_id == node_id, RuntimeCall.status.not_in(TERMINAL))
        .values(status="cancelled", request_enc="")
        .returning(RuntimeCall.id)
    )
    for call_id in cancelled:
        await notify_call(session, call_id, node_id)
    snapshot = {
        "name": node.name,
        "hostname": node.hostname,
        "username": node.username,
        "workspace_root": node.workspace_root,
        "team_id": _plain(node.team_id),
    }
    # 安装链接保留作记录，只解除对节点的引用；已用过的链接不能再注册出同一节点。
    await session.execute(
        update(RuntimeInstallLink).where(RuntimeInstallLink.node_id == node_id).values(node_id=None)
    )
    # 实例上的公告随实例级联删除；调用与分片随节点级联删除。
    await session.execute(delete(RelayServer).where(RelayServer.runtime_node_id == node_id))
    await session.execute(delete(RuntimeNode).where(RuntimeNode.id == node_id))
    # 唤醒该节点挂起的长轮询，让它立即按凭证失效处理。
    await notify_node(session, node_id)
    await record_audit(
        session,
        action="runtime.delete",
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="runtime_node",
        target_id=str(node_id),
        diff={k: [v, None] for k, v in snapshot.items()},
        ip=client_ip(request),
    )
    await session.commit()
    return {"code": 0, "data": None}


async def _link_epoch_current(session: AsyncSession, claims: session_links.LinkClaims) -> bool:
    """飞书资料模式的链接绑定签发时的上下文版本；撤销、切换模式或重新授权后即失效。"""
    if claims.context_epoch is None:
        return True
    grant = await session.get(
        FeishuPersonalGrant, (claims.bot_id, claims.user_id), populate_existing=True
    )
    return grant is not None and grant.context_epoch == claims.context_epoch


async def _owner_viewable(session: AsyncSession, node_id: uuid.UUID, provider: str) -> bool:
    node = await session.get(RuntimeNode, node_id)
    capability = (node.capabilities.get(provider) or {}) if node else {}
    return isinstance(capability, dict) and capability.get("owner_session_view_v1") is True


async def _authorize_session_content(
    session: AsyncSession,
    request: Request,
    actor: User,
    node_id: uuid.UUID,
    provider: str,
    session_id: str,
) -> str:
    """能否查看会话原文；返回放行依据（link / owner / role）供审计。

    私聊归本人，不看角色：普通私聊登录即可看自己的会话；飞书资料模式的会话还要聊天里发出的
    链接（24 小时内、签给当前登录的人、上下文版本未变）。会话历史里不能有别人的记录；第一轮
    还没写记录时，只有链接能证明它属于谁。bot_token 登录一律不走本人这条路：群聊里的智能体
    拿着发言人的 token，不能借此读到他的私聊。非管理员还要节点已升级到记录不含环境变量的驱动。
    其余按角色：管理员可看群聊与企微私聊；飞书私聊可能含个人飞书资料，任何角色都不能代看。
    """
    try:
        identity = uuid.UUID(session_id)
    except ValueError:
        raise not_found("会话不存在") from None
    rows = (
        await session.execute(
            select(
                ChatLog.platform,
                ChatLog.chat_type,
                ChatLog.user_id,
                func.coalesce(Task.result.has_key("feishu_personal"), False),
            )
            .outerjoin(Task, Task.id == ChatLog.task_id)
            .where(ChatLog.relay_session_id == identity)
        )
    ).all()
    personal = any(row[3] for row in rows)
    mine = not request.cookies.get("bot_token") and all(
        chat_type == "single" and user_id == actor.id for _, chat_type, user_id, _ in rows
    )
    token = request.query_params.get("t", "")
    claims = session_links.read(request.app.state.cipher, token) if token else None
    linked = (
        mine
        and claims is not None
        and claims.session_id == identity
        and claims.user_id == actor.id
        and claims.node_id == node_id
        and claims.provider == provider
        and await _link_epoch_current(session, claims)
    )
    if linked or (mine and rows and not personal):
        if actor.role in MANAGER_ROLES or await _owner_viewable(session, node_id, provider):
            return "link" if linked else "owner"
        # 旧驱动的会话记录里带着请求的环境变量（机器人密钥、访问令牌），以前只给管理员看。
        raise ApiError(403, 403, "这个运行时版本较旧，暂时不能查看完整过程，请联系管理员升级运行时")
    if mine and rows:
        raise ApiError(
            403, 403, "飞书资料模式的完整过程只能从聊天里最近一条回复的链接打开，链接 24 小时内有效"
        )
    if actor.role not in MANAGER_ROLES:
        if token:
            raise ApiError(
                403, 403, "链接已失效或不属于当前登录账号，请打开聊天里最新一条回复的链接"
            )
        raise forbidden()
    if not rows or any(
        platform == "feishu" and chat_type == "single" for platform, chat_type, _, _ in rows
    ):
        raise not_found("会话不存在")
    return "role"


def _renewed(result: Response, renewal: Response) -> Response:
    """直接返回 Response 时，current_user 续期写在注入 response 上的 cookie 要手动带上。"""
    for value in renewal.headers.getlist("set-cookie"):
        result.headers.append("set-cookie", value)
    return result


def _error_page(exc: ApiError) -> HTMLResponse:
    """查看页是浏览器直接打开的，错误也回一页能读的文字，而不是 JSON。"""
    from html import escape

    body = (
        '<!doctype html><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>CoreMan</title>"
        '<p style="font:16px/1.6 system-ui,sans-serif;margin:32px 16px">'
        f"{escape(exc.message)}</p>"
    )
    return HTMLResponse(body, status_code=exc.status_code, headers=NO_STORE)


@router.get("/{node_id}/{provider}/session/{session_id}")
async def session_view(
    request: Request,
    response: Response,
    node_id: uuid.UUID,
    provider: Literal["claude", "codex"],
    session_id: str,
    session: AsyncSession = Depends(get_session),
) -> Response:
    import re

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", session_id):
        return _error_page(not_found("会话不存在"))
    try:
        actor = await current_user(request, response, session)
    except ApiError as exc:
        if exc.status_code != 401:
            raise
        # 聊天里点开链接时多半还没登录（飞书、企微内置浏览器各有各的 cookie）：登录后回到原链接。
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(
            "/login?redirect=" + quote(target, safe=""), status_code=302, headers=NO_STORE
        )
    try:
        via = await _authorize_session_content(
            session, request, actor, node_id, provider, session_id
        )
        if not await session.get(RuntimeNode, node_id):
            raise not_found("运行时不存在")
    except ApiError as exc:
        return _renewed(_error_page(exc), response)
    await record_audit(
        session,
        action="runtime.session_view",
        actor_id=actor.id,
        actor_login=actor.login_name or f"user:{actor.id}",
        target_type="runtime_session",
        target_id=session_id,
        diff={"node_id": [None, str(node_id)], "provider": [None, provider], "via": [None, via]},
        ip=client_ip(request),
    )
    await session.commit()
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
        # 带上查询串：私聊链接的凭据要随事件流一起校验。
        "const wsUrl = location.pathname + '/events' + location.search;",
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
    return _renewed(Response(template, media_type="text/html", headers=NO_STORE), response)


@router.get("/{node_id}/{provider}/session/{session_id}/events")
async def session_events(
    request: Request,
    node_id: uuid.UUID,
    provider: Literal["claude", "codex"],
    session_id: str,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    import re

    import httpx
    from fastapi.responses import StreamingResponse

    from coreman.core.runtime_nodes.transport import ReverseTransport

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", session_id):
        raise not_found("会话不存在")
    await _authorize_session_content(session, request, actor, node_id, provider, session_id)
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
