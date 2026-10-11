"""Employee-admin workspace access over the authenticated runtime channel."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip, current_user, get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.security import verify_csrf
from coreman.core.audit import record_audit
from coreman.core.bots.permissions import is_bot_admin
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, BotMember, RelayServer, Task, User
from coreman.core.relay.agent_client import AgentError, call_agent

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


def workspace_out(bot: Bot) -> dict[str, Any]:
    return {
        "directory": bot.working_dir,
        "state": bot.workspace_state,
        "error": bot.workspace_error,
        "phase": bot.workspace_phase,
        "deadline": bot.workspace_deadline,
        "memory_snapshot_at": bot.memory_snapshot_at,
        "operation_id": str(bot.workspace_operation_id) if bot.workspace_operation_id else None,
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
        raise ApiError(502, 502, "工作目录操作未完成，请检查运行时在线状态与版本") from exc


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


@router.get("/git/status")
async def git_status(
    bot_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = await require_admin(session, bot_id, user)
    data = await invoke(session, bot, request.app.state.cipher, "workspace-git-status")
    return {"code": 0, "data": data}


@router.get("/git/diff")
async def git_diff(
    bot_id: uuid.UUID,
    request: Request,
    path: str = Query(min_length=1, max_length=4096),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = await require_admin(session, bot_id, user)
    data = await invoke(
        session, bot, request.app.state.cipher, "workspace-git-diff", {"path": path}
    )
    return {"code": 0, "data": data}


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
