"""Employee-admin workspace access over the authenticated runtime channel."""

from __future__ import annotations

import re
import uuid
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip, current_user, get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.security import verify_csrf
from coreman.core.audit import record_audit
from coreman.core.bots.git_token import GIT_TAG, GIT_TOKEN_AAD, PRESET_AAD, git_token, preset_token
from coreman.core.bots.permissions import is_bot_admin
from coreman.core.bots.secrets import decrypt_json
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, BotMember, EnvPreset, RelayServer, Task, User
from coreman.core.relay.agent_client import AgentError, call_agent
from coreman.core.timeutils import utcnow

router = APIRouter(
    prefix="/api/admin/bots/{bot_id}/workspace",
    tags=["bot-workspace"],
    dependencies=[Depends(verify_csrf)],
)


class FileWriteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)
    content: str = Field(max_length=1024 * 1024)
    expected_hash: str | None = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("content")
    @classmethod
    def bound_content(cls, value: str) -> str:
        if len(value.encode()) > 1024 * 1024:
            raise ValueError("文本超过 1 MiB")
        return value


class GitConfigIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    git_url: str = Field(max_length=2048)
    access_token: str | None = Field(default=None, max_length=4096)
    # 令牌取自环境预设：给了 token_preset 就忽略 access_token；不给则改回员工自己的令牌。
    token_preset: str | None = Field(default=None, max_length=100)
    token_var: str | None = Field(default=None, max_length=200)

    @field_validator("git_url")
    @classmethod
    def safe_url(cls, value: str) -> str:
        if not value:
            return value
        if re.search(r"[\s\x00-\x1f\x7f]", value):
            raise ValueError("仓库地址不能包含空白或控制字符")
        parsed = urlsplit(value)
        if (
            parsed.scheme == "https"
            and parsed.hostname
            and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
            and parsed.path not in ("", "/")
        ):
            return value
        if re.fullmatch(r"git@[A-Za-z0-9.-]+:[A-Za-z0-9_./-]+", value):
            return value
        raise ValueError("请使用不含凭证的 HTTPS 或 git@host:path 仓库地址")


class BackupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    files: list[str] = Field(default_factory=list, max_length=1000)
    message: str = Field(default="备份工作文件", min_length=1, max_length=500)

    @field_validator("message", mode="before")
    @classmethod
    def default_message(cls, value: Any) -> Any:
        return "备份工作文件" if isinstance(value, str) and not value.strip() else value


class AdoptIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: Literal[True]


def workspace_out(bot: Bot) -> dict[str, Any]:
    return {
        "directory": bot.working_dir,
        "state": bot.workspace_state,
        "error": bot.workspace_error,
        "phase": bot.workspace_phase,
        "deadline": bot.workspace_deadline,
        "memory_snapshot_at": bot.memory_snapshot_at,
        "operation_id": str(bot.workspace_operation_id) if bot.workspace_operation_id else None,
        "git_url": bot.git_url,
        "branch": bot.git_branch,
        "has_token": bool(bot.git_token_enc),
        "token_preset": bot.git_token_preset,
        "token_var": bot.git_token_var,
        "last_backup_at": bot.git_last_backup_at,
    }


async def require_admin(
    session: AsyncSession,
    bot_id: uuid.UUID,
    user: User,
    *,
    write: bool = False,
    initialize: bool = False,
    allow_failed: bool = False,
) -> Bot:
    query = select(Bot).where(Bot.id == bot_id)
    if write:
        query = query.with_for_update()
    bot = await session.scalar(query)
    if bot is None:
        raise not_found("AI 员工不存在")
    members = list(
        await session.scalars(select(BotMember.user_id).where(BotMember.bot_id == bot_id))
    )
    if user.status != "active" or not is_bot_admin(user, bot, members):
        raise ApiError(403, 403, "仅 AI 员工管理员可访问工作文件")
    if write:
        if bot.workspace_state in {"migrating", "initializing", "busy"}:
            raise ApiError(409, 409, "工作目录正在处理中，请稍后重试")
        if not initialize and bot.workspace_state not in (
            {"ready", "failed"} if allow_failed else {"ready"}
        ):
            raise ApiError(409, 409, "工作目录尚未就绪，请先完成初始化或重试迁移")
        active = await session.scalar(
            select(Task.id)
            .where(Task.bot_id == bot.id, Task.status.in_(("claimed", "running")))
            .limit(1)
        )
        if active is not None:
            raise ApiError(409, 409, "AI 员工正在执行任务，结束后可修改或备份文件")
    return bot


async def invoke(
    session: AsyncSession,
    bot: Bot,
    cipher: Cipher,
    operation: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    relay = await session.get(RelayServer, bot.relay_server_id) if bot.relay_server_id else None
    if relay is None or not relay.is_active or relay.runtime_node_id is None:
        raise ApiError(409, 409, "运行时未分配或不可用")
    trusted = {**(payload or {}), "working_dir": bot.working_dir, "bot_id": str(bot.id)}
    try:
        info = await call_agent(relay, cipher, "ping")
        if info.get("workspace_protocol") != 1:
            raise ApiError(409, 409, "请先升级运行时以使用工作目录管理")
        return await call_agent(relay, cipher, operation, trusted)
    except AgentError as exc:
        if getattr(exc, "code", None) == "conflict":
            raise ApiError(409, 409, "文件已被修改，请重新读取后合并您的更改") from exc
        if getattr(exc, "code", None) == "remote_not_empty":
            raise ApiError(
                409,
                409,
                "远端仓库已有内容，与当前工作目录不是同一仓库。"
                "请改用空仓库，或用仓库内容初始化工作目录",
            ) from exc
        raise ApiError(
            502, 502, "工作目录操作未完成，请检查运行时在线状态、版本及 Git 配置"
        ) from exc


async def git_payload(session: AsyncSession, bot: Bot, cipher: Cipher) -> dict[str, Any]:
    if not bot.git_url:
        raise ApiError(422, 422, "请先保存 Git 仓库地址")
    return {"git_url": bot.git_url, "git_access_token": await git_token(session, cipher, bot)}


async def audit(session: AsyncSession, request: Request, user: User, bot: Bot, action: str) -> None:
    await record_audit(
        session,
        action=f"workspace.{action}",
        actor_id=user.id,
        actor_login=user.login_name,
        target_type="bot",
        target_id=str(bot.id),
        ip=client_ip(request),
    )


@router.get("")
async def get_workspace(
    bot_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return {"code": 0, "data": workspace_out(await require_admin(session, bot_id, user))}


@router.get("/files")
async def list_files(
    bot_id: uuid.UUID,
    request: Request,
    path: str = "",
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = await require_admin(session, bot_id, user)
    data = await invoke(session, bot, request.app.state.cipher, "workspace-list", {"path": path})
    return {"code": 0, "data": data}


@router.get("/file")
async def read_file(
    bot_id: uuid.UUID,
    request: Request,
    path: str,
    offset: int = Query(default=0, ge=0),
    length: int = Query(default=262144, ge=1, le=262144),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = await require_admin(session, bot_id, user)
    data = await invoke(
        session,
        bot,
        request.app.state.cipher,
        "workspace-read",
        {"path": path, "offset": offset, "length": length},
    )
    return {"code": 0, "data": data}


@router.put("/file")
async def write_file(
    bot_id: uuid.UUID,
    body: FileWriteIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = await require_admin(session, bot_id, user, write=True, allow_failed=True)
    data = await invoke(
        session, bot, request.app.state.cipher, "workspace-write", body.model_dump()
    )
    await audit(session, request, user, bot, "write")
    await session.commit()
    return {"code": 0, "data": data}


@router.put("/git")
async def configure_git(
    bot_id: uuid.UUID,
    body: GitConfigIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = await require_admin(session, bot_id, user, write=True, initialize=True)
    bot.git_url = body.git_url or None
    if body.token_preset:
        # 保存时就校验预设开放给 Git 且有这个变量，免得备份时才发现。
        await preset_token(
            session, request.app.state.cipher, body.token_preset, body.token_var or ""
        )
        bot.git_token_preset, bot.git_token_var = body.token_preset, body.token_var
    else:
        bot.git_token_preset = bot.git_token_var = None
    if body.access_token is not None:
        bot.git_token_enc = (
            request.app.state.cipher.encrypt(body.access_token, GIT_TOKEN_AAD)
            if body.access_token
            else ""
        )
    await audit(session, request, user, bot, "git_configure")
    await session.commit()
    return {"code": 0, "data": workspace_out(bot)}


@router.get("/git/presets")
async def git_presets(
    bot_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """可用作 Git 令牌的环境预设：只列打了 git 标签的，只给变量名不给值。"""
    await require_admin(session, bot_id, user)
    rows = await session.scalars(
        select(EnvPreset).where(EnvPreset.tags.contains([GIT_TAG])).order_by(EnvPreset.group_key)
    )
    cipher = request.app.state.cipher
    return {
        "code": 0,
        "data": [
            {
                "group_key": row.group_key,
                "label": row.label,
                "vars": sorted(decrypt_json(cipher, row.vars_enc, PRESET_AAD)),
            }
            for row in rows
        ],
    }


@router.get("/git/status")
async def git_status(
    bot_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = await require_admin(session, bot_id, user)
    data = await invoke(session, bot, request.app.state.cipher, "workspace-git-status")
    return {"code": 0, "data": {**workspace_out(bot), **data}}


@router.post("/git/test")
async def git_test(
    bot_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = await require_admin(session, bot_id, user)
    cipher = request.app.state.cipher
    data = await invoke(
        session, bot, cipher, "workspace-git-test", await git_payload(session, bot, cipher)
    )
    if data.get("branch"):
        bot.git_branch = data["branch"]
        await session.commit()
    return {"code": 0, "data": data}


@router.post("/git/backup")
async def git_backup(
    bot_id: uuid.UUID,
    body: BackupIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = await require_admin(session, bot_id, user, write=True)
    cipher = request.app.state.cipher
    data = await invoke(
        session,
        bot,
        cipher,
        "workspace-git-backup",
        {**await git_payload(session, bot, cipher), **body.model_dump()},
    )
    if data.get("pushed") is not True:
        raise ApiError(502, 502, "文件尚未上传到远端，请重试备份")
    bot.git_last_backup_at = utcnow()
    if data.get("branch"):
        bot.git_branch = data["branch"]
    await audit(session, request, user, bot, "backup")
    await session.commit()
    return {"code": 0, "data": {**data, "last_backup_at": bot.git_last_backup_at}}


@router.post("/git/adopt")
async def git_adopt(
    bot_id: uuid.UUID,
    body: AdoptIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """放弃工作目录现有文件，用远端仓库默认分支的内容初始化。"""
    from coreman.core.bots.workspace_transfer import instruction_content

    bot = await require_admin(session, bot_id, user, write=True)
    cipher = request.app.state.cipher
    data = await invoke(
        session,
        bot,
        cipher,
        "workspace-git-adopt",
        {**await git_payload(session, bot, cipher), "content": instruction_content(bot)},
    )
    if data.get("branch"):
        bot.git_branch = data["branch"]
    await audit(session, request, user, bot, "git_adopt")
    await session.commit()
    return {"code": 0, "data": workspace_out(bot)}


@router.post("/initialize")
async def initialize(
    bot_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    from coreman.core.bots.workspace_transfer import initialize_workspace

    bot = await require_admin(session, bot_id, user, write=True, initialize=True)
    try:
        await initialize_workspace(session, bot, request.app.state.cipher)
    except ApiError:
        await session.commit()
        raise
    await audit(session, request, user, bot, "initialize")
    await session.commit()
    return {"code": 0, "data": workspace_out(bot)}
