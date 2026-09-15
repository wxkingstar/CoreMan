"""实例限定的记忆上报与查询，不以文件路径猜测机器人归属。"""

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import get_session
from coreman.api.errors import ApiError
from coreman.api.routers.infra_relay import ReportTarget, target
from coreman.core.db.models import Bot, Memory
from coreman.core.knowledge.memory import MemoryInput, collect, working_dir
from coreman.core.timeutils import utcnow

router = APIRouter(tags=["infra-memories"])


class CollectIn(ReportTarget):
    memories: list[MemoryInput] = Field(max_length=500)


@router.post("/api/infra/memories/collect")
async def collect_memories(
    body: CollectIn, request: Request, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    relay = await target(request, body, session, scope="memories")
    result = await collect(session, relay, body.memories, utcnow())
    await session.commit()
    return {"code": 0, "data": result}


class QueryIn(ReportTarget):
    working_dir: str = Field(min_length=1, max_length=500)


@router.get("/api/infra/memories")
async def query_memories(
    request: Request, query: QueryIn = Depends(), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    relay = await target(request, query, session, scope="memories")
    try:
        directory = working_dir(query.working_dir)
    except ValueError:
        raise ApiError(422, 422, "记忆工作目录无效") from None
    bots = list(
        await session.scalars(
            select(Bot).where(Bot.relay_server_id == relay.id, Bot.working_dir == directory)
        )
    )
    if len(bots) != 1:
        raise ApiError(409, 409, "目录未唯一分配给本实例机器人")
    rows = list(
        await session.scalars(
            select(Memory).where(Memory.bot_id == bots[0].id).order_by(Memory.file_name)
        )
    )
    return {
        "code": 0,
        "data": {
            "memories": [
                {
                    "file_name": row.file_name,
                    "working_dir": directory,
                    "content": row.content if row.deleted_at is None else "",
                    "content_hash": row.content_hash,
                    "file_mtime": row.file_mtime.timestamp() if row.file_mtime else None,
                    "deleted": row.deleted_at is not None,
                    "version": row.version,
                }
                for row in rows
            ]
        },
    }


@router.get("/api/infra/memories/workspaces")
async def workspaces(
    request: Request,
    query: ReportTarget = Depends(),
    page: int = Query(default=1, ge=1, le=10000),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    relay = await target(request, query, session, scope="memories")
    rows = list(
        await session.scalars(
            select(Bot.working_dir)
            .where(Bot.relay_server_id == relay.id)
            .group_by(Bot.working_dir)
            .having(func.count() == 1)
            .order_by(Bot.working_dir)
            .offset((page - 1) * 500)
            .limit(501)
        )
    )
    return {"code": 0, "data": {"working_dirs": rows[:500], "has_more": len(rows) > 500}}


@router.post("/api/infra/memories/deploy")
async def deploy_memories(
    body: QueryIn, request: Request, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    import json

    from coreman.core.audit import record_audit
    from coreman.core.knowledge.memory import MAX_BATCH_BYTES
    from coreman.core.relay.agent_client import AgentError, call_agent

    relay = await target(request, body, session, scope="memories")
    client = getattr(request.state, "api_client", None)
    caller = f"api:{client.app_key}" if client is not None else f"runtime:{relay.id}"
    try:
        directory = working_dir(body.working_dir)
    except ValueError:
        raise ApiError(422, 422, "记忆工作目录无效") from None
    bots = list(
        await session.scalars(
            select(Bot).where(Bot.relay_server_id == relay.id, Bot.working_dir == directory)
        )
    )
    if len(bots) != 1:
        raise ApiError(409, 409, "目录未唯一分配给本实例机器人")
    bot = bots[0]
    bot_version = bot.version
    rows = list(
        await session.scalars(
            select(Memory).where(Memory.bot_id == bot.id).order_by(Memory.file_name)
        )
    )
    versions = {row.id: row.version for row in rows}
    payload = {
        "protocol_version": 2,
        "memories": [
            {
                "working_dir": directory,
                "file_name": row.file_name,
                "content": row.content,
                "content_hash": row.content_hash,
                "file_mtime": row.file_mtime.timestamp() if row.file_mtime else None,
                "deleted": row.deleted_at is not None,
            }
            for row in rows
        ],
    }
    if (
        len(rows) > 500
        or len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_BATCH_BYTES - 1024
    ):
        raise ApiError(413, 413, "部署请求超过 Agent 大小限制")
    await record_audit(
        session,
        action="memory.infra_deploy_requested",
        actor_login=caller,
        target_type="bot",
        target_id=str(bot.id),
        diff={"relay_id": [None, str(relay.id)]},
    )
    await session.commit()
    try:
        capability = await call_agent(relay, request.app.state.cipher, "ping")
        if capability.get("memory_protocol") != 2:
            raise ApiError(409, 409, "Agent 不支持安全记忆部署，请先升级")
        result = await call_agent(relay, request.app.state.cipher, "deploy-memory", payload)
    except AgentError:
        raise ApiError(502, 502, "记忆部署未完成，请检查实例状态") from None
    await session.refresh(bot)
    actual = {
        row.id: row.version
        for row in await session.scalars(
            select(Memory).where(Memory.bot_id == bot.id).execution_options(populate_existing=True)
        )
    }
    if bot.version != bot_version or actual != versions:
        raise ApiError(409, 409, "部署期间配置已变化，请重新核对")
    if result.get("protocol_version") != 2 or result.get("count") != len(rows):
        raise ApiError(502, 502, "实例未确认完整的记忆部署")
    await record_audit(
        session,
        action="memory.infra_deployed",
        actor_login=caller,
        target_type="bot",
        target_id=str(bot.id),
        diff={"count": [None, len(rows)]},
    )
    await session.commit()
    return {"code": 0, "data": {"count": len(rows)}}
