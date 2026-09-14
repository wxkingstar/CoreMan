"""机器人管理员的记忆编辑、回收与部署。管理角色不旁路正文权限。"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import current_user, get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.security import verify_csrf
from coreman.api.versioning import require_if_match
from coreman.core.audit import record_audit
from coreman.core.bots.permissions import is_bot_admin
from coreman.core.db.models import Bot, BotMember, Memory, RelayServer, User
from coreman.core.knowledge.memory import MAX_BATCH_BYTES, MAX_FILE_BYTES, content_hash, file_name
from coreman.core.relay.agent_client import AgentError, call_agent
from coreman.core.timeutils import utcnow

router = APIRouter(
    prefix="/api/admin/bots/{bot_id}/memories",
    tags=["bot-memories"],
    dependencies=[Depends(verify_csrf)],
)


async def require_admin(
    session: AsyncSession, identity: uuid.UUID, actor: User, *, lock: bool = False
) -> Bot:
    query = select(Bot).where(Bot.id == identity)
    if lock:
        query = query.with_for_update()
    bot = await session.scalar(query)
    if bot is None:
        raise not_found("记忆或机器人不存在")
    members = list(
        await session.scalars(select(BotMember.user_id).where(BotMember.bot_id == identity))
    )
    if actor.status != "active" or not is_bot_admin(actor, bot, members):
        raise ApiError(403, 403, "仅机器人管理员可管理记忆")
    return bot


def memory_out(row: Memory, *, full: bool = False) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "bot_id": str(row.bot_id),
        "file_name": row.file_name,
        "name": row.name,
        "description": row.description,
        "type": row.type,
        "content_hash": row.content_hash,
        "file_mtime": row.file_mtime,
        "collected_from": row.collected_from,
        "deleted_at": row.deleted_at,
        "version": row.version,
        **({"content": row.content} if full else {}),
    }


async def load(session: AsyncSession, bot: Bot, identity: uuid.UUID) -> Memory:
    row = await session.scalar(
        select(Memory).where(Memory.id == identity, Memory.bot_id == bot.id).with_for_update()
    )
    if row is None:
        raise not_found("记忆或机器人不存在")
    return row


async def audit(
    session: AsyncSession, actor: User, bot: Bot, action: str, identity: uuid.UUID | None = None
) -> None:
    await record_audit(
        session,
        action=f"memory.{action}",
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="bot",
        target_id=str(bot.id),
        diff={"memory_id": [None, str(identity)]} if identity else None,
    )


@router.get("")
async def list_memories(
    bot_id: uuid.UUID,
    include_deleted: bool = False,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = await require_admin(session, bot_id, actor)
    query = select(Memory).where(Memory.bot_id == bot.id)
    if not include_deleted:
        query = query.where(Memory.deleted_at.is_(None))
    rows = await session.scalars(query.order_by(Memory.file_name).limit(500))
    return {"code": 0, "data": [memory_out(row) for row in rows]}


@router.get("/{memory_id}")
async def get_memory(
    bot_id: uuid.UUID,
    memory_id: uuid.UUID,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = await require_admin(session, bot_id, actor)
    return {"code": 0, "data": memory_out(await load(session, bot, memory_id), full=True)}


class MemoryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_name: str = Field(min_length=1, max_length=200)
    content: str = Field(max_length=MAX_FILE_BYTES)
    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    type: str | None = Field(default=None, max_length=50)

    @field_validator("file_name")
    @classmethod
    def filename(cls, value: str) -> str:
        return file_name(value)

    @field_validator("content")
    @classmethod
    def bounded(cls, value: str) -> str:
        if len(value.encode()) > MAX_FILE_BYTES:
            raise ValueError("记忆文件超过 256 KiB")
        return value


def apply(row: Memory, body: MemoryIn) -> None:
    row.content, row.content_hash = body.content, content_hash(body.content)
    row.name, row.description, row.type = body.name, body.description, body.type
    row.file_mtime = row.updated_at = utcnow()
    row.collected_from, row.deleted_at = "admin", None


@router.post("")
async def create_memory(
    bot_id: uuid.UUID,
    body: MemoryIn,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = await require_admin(session, bot_id, actor, lock=True)
    if (
        await session.scalar(
            select(func.count()).select_from(Memory).where(Memory.bot_id == bot.id)
        )
        or 0
    ) >= 500:
        raise ApiError(409, 409, "每个机器人最多保留 500 个记忆文件（含已删除）")
    row = Memory(bot_id=bot.id, file_name=body.file_name)
    apply(row, body)
    session.add(row)
    await session.flush()
    await audit(session, actor, bot, "create", row.id)
    await session.commit()
    return {"code": 0, "data": memory_out(row, full=True)}


@router.put("/{memory_id}")
async def update_memory(
    bot_id: uuid.UUID,
    memory_id: uuid.UUID,
    body: MemoryIn,
    request: Request,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = await require_admin(session, bot_id, actor, lock=True)
    row = await load(session, bot, memory_id)
    require_if_match(request, row.version)
    if body.file_name != row.file_name:
        raise ApiError(422, 422, "文件名不可修改；可另建文件后删除旧文件")
    apply(row, body)
    await audit(session, actor, bot, "edit", row.id)
    await session.commit()
    return {"code": 0, "data": memory_out(row, full=True)}


@router.delete("/{memory_id}")
async def delete_memory(
    bot_id: uuid.UUID,
    memory_id: uuid.UUID,
    request: Request,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = await require_admin(session, bot_id, actor, lock=True)
    row = await load(session, bot, memory_id)
    require_if_match(request, row.version)
    row.deleted_at = row.file_mtime = row.updated_at = utcnow()
    row.content, row.content_hash = "", content_hash("")
    await audit(session, actor, bot, "delete", row.id)
    await session.commit()
    return {"code": 0, "data": memory_out(row)}


async def relay_for_memory(session: AsyncSession, bot: Bot) -> RelayServer:
    relay = await session.get(RelayServer, bot.relay_server_id) if bot.relay_server_id else None
    if relay is None or not relay.is_active or not relay.agent_token_enc:
        raise ApiError(409, 409, "机器人未分配可用的 Agent")
    count = await session.scalar(
        select(func.count())
        .select_from(Bot)
        .where(Bot.relay_server_id == relay.id, Bot.working_dir == bot.working_dir)
    )
    if count != 1:
        raise ApiError(409, 409, "实例工作目录未唯一分配，不能操作记忆")
    return relay


@router.post("/collect")
async def collect_memory(
    bot_id: uuid.UUID,
    request: Request,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = await require_admin(session, bot_id, actor)
    relay = await relay_for_memory(session, bot)
    await audit(session, actor, bot, "collect_requested")
    await session.commit()
    try:
        result = await call_agent(
            relay, request.app.state.cipher, "collect-memory", {"working_dir": bot.working_dir}
        )
    except AgentError as exc:
        raise ApiError(502, 502, str(exc)) from None
    return {"code": 0, "data": result}


@router.post("/deploy")
async def deploy_memory(
    bot_id: uuid.UUID,
    request: Request,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = await require_admin(session, bot_id, actor)
    relay = await relay_for_memory(session, bot)
    rows = list(
        await session.scalars(
            select(Memory).where(Memory.bot_id == bot.id).order_by(Memory.file_name)
        )
    )
    if len(rows) > 500 or sum(len(row.content.encode()) for row in rows) > MAX_BATCH_BYTES:
        raise ApiError(413, 413, "记忆总量过大，请先减少文件")
    versions = {row.id: row.version for row in rows}
    bot_version = bot.version
    payload = [
        {
            "working_dir": bot.working_dir,
            "file_name": row.file_name,
            "content": row.content,
            "content_hash": row.content_hash,
            "file_mtime": row.file_mtime.timestamp() if row.file_mtime else None,
            "deleted": row.deleted_at is not None,
        }
        for row in rows
    ]
    if (
        len(json.dumps({"memories": payload, "protocol_version": 2}, ensure_ascii=False).encode())
        > MAX_BATCH_BYTES - 1024
    ):
        raise ApiError(413, 413, "部署请求超过 Agent 大小限制")
    await audit(session, actor, bot, "deploy_requested")
    await session.commit()
    try:
        capability = await call_agent(relay, request.app.state.cipher, "ping")
        if capability.get("memory_protocol") != 2:
            raise ApiError(409, 409, "Agent 不支持安全记忆部署，请先升级")
        result = await call_agent(
            relay,
            request.app.state.cipher,
            "deploy-memory",
            {"memories": payload, "protocol_version": 2},
        )
    except AgentError as exc:
        raise ApiError(502, 502, str(exc)) from None
    await session.refresh(bot)
    actual = dict(
        (row.id, row.version)
        for row in await session.scalars(
            select(Memory).where(Memory.bot_id == bot.id).execution_options(populate_existing=True)
        )
    )
    if bot.version != bot_version or actual != versions:
        raise ApiError(409, 409, "部署期间配置已变更，请重新回收核对后部署")
    if result.get("protocol_version") != 2:
        raise ApiError(409, 409, "Agent 版本不支持删除和冲突检测，请先升级")
    await audit(session, actor, bot, "deployed")
    await session.commit()
    return {"code": 0, "data": result}
