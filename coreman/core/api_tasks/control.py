"""业务系统看自己未结束的任务、批量取消：来了更高级别的事，先腾出 AI 员工。

调用方只看得到、只取消得了自己发起的任务；群里真人的轮次只报「忙不忙」，不报内容、不能取消。
见 docs/superpowers/specs/2026-11-25-api-client-group-delivery.md 第 10 节。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.api_tasks import groups
from coreman.core.bus import outbox, tasks
from coreman.core.db.models import ApiClient, ApiTask, Bot, Task

# 列表、批量取消一次最多涉及这么多条。
LIMIT = 100
# 调用方能按状态筛：排队（含等额度）、执行中、两者都要。
STATES = {"queued": ("queued", "claimed"), "running": ("running",), "open": tasks.OPEN}


async def _bot_id(session: AsyncSession, bot_key: str | None) -> uuid.UUID | None:
    if bot_key is None:
        return None
    bot_id: uuid.UUID | None = await session.scalar(select(Bot.id).where(Bot.bot_key == bot_key))
    if bot_id is None:
        from coreman.core.api_tasks.tokens import TokenError

        raise TokenError(404, "AI 员工不存在")
    return bot_id


async def _user_id(session: AsyncSession, subject: str | None) -> uuid.UUID | None:
    if subject is None:
        return None
    from coreman.core.api_tasks.service import resolve_member

    return (await resolve_member(session, subject)).id


async def open_rows(
    session: AsyncSession,
    client: ApiClient,
    *,
    state: str = "open",
    bot_key: str | None = None,
    chat_id: str | None = None,
    subject: str | None = None,
    ids: list[uuid.UUID] | None = None,
) -> list[ApiTask]:
    """这个调用方发起、还没结束（按总线任务看）的 API 任务，最早的在前。"""
    stmt = (
        select(ApiTask)
        .join(Task, Task.id == ApiTask.task_id)
        .where(
            ApiTask.client_key == client.app_key,
            Task.status.in_(STATES[state]),
            Task.cancel_requested_at.is_(None),
        )
        .order_by(ApiTask.created_at, ApiTask.id)
        .limit(LIMIT)
    )
    bot_id = await _bot_id(session, bot_key)
    if bot_id is not None:
        stmt = stmt.where(ApiTask.bot_id == bot_id)
    if chat_id is not None:
        stmt = stmt.where(ApiTask.target == "chat", ApiTask.chat_id == chat_id)
    user_id = await _user_id(session, subject)
    if user_id is not None:
        stmt = stmt.where(ApiTask.user_id == user_id)
    if ids is not None:
        stmt = stmt.where(ApiTask.id.in_(ids))
    return list(await session.scalars(stmt))


async def group_states(
    session: AsyncSession, client: ApiClient, bot_key: str | None, chat_id: str | None
) -> list[dict[str, Any]]:
    """调用方配了群承接的每个群现在忙不忙：在跑的是不是它自己的任务、排了几条它的任务。"""
    from coreman.core.db.models import ApiClientGroupRoute

    stmt = (
        select(ApiClientGroupRoute, Bot.bot_key)
        .join(Bot, Bot.id == ApiClientGroupRoute.bot_id)
        .where(ApiClientGroupRoute.client_key == client.app_key)
    )
    if bot_key is not None:
        stmt = stmt.where(Bot.bot_key == bot_key)
    if chat_id is not None:
        stmt = stmt.where(ApiClientGroupRoute.chat_id == chat_id)
    out: list[dict[str, Any]] = []
    for route, key in (await session.execute(stmt.order_by(ApiClientGroupRoute.created_at))).all():
        running = list(
            await session.scalars(
                select(Task.id).where(
                    Task.bot_id == route.bot_id,
                    Task.session_key == route.chat_id,
                    Task.status.in_(tasks.ACTIVE),
                )
            )
        )
        mine = (
            set(
                await session.scalars(
                    select(ApiTask.task_id).where(
                        ApiTask.client_key == client.app_key, ApiTask.task_id.in_(running)
                    )
                )
            )
            if running
            else set()
        )
        out.append(
            {
                "bot_key": key,
                "chat_id": route.chat_id,
                # busy：群主会话里有轮次在跑；running_mine：在跑的是不是这个调用方发起的（否则是
                # 群里的人自己的，或别的调用方的，不能由你取消）。
                "busy": bool(running),
                "running_mine": bool(mine),
                "queued": await groups.queued(session, route.bot_id, route.chat_id),
                "queue_max": groups.QUEUE_MAX,
            }
        )
    return out


async def cancel(session: AsyncSession, client: ApiClient, row: ApiTask) -> bool:
    """取消一条：没结束的这一轮（群承接模式连同确认后的续接轮）都停下。返回是否真的取消了。"""
    cancelled = False
    if row.task_id is not None:
        cancelled = await tasks.request_cancel(session, row.task_id, "api_cancel")
    if groups.is_group_row(row):
        from coreman.core.api_tasks.writes import session_key_of

        follow_ups = await session.scalars(
            select(Task.id).where(
                Task.bot_id == row.bot_id,
                Task.session_key == session_key_of(row),
                Task.status.in_(tasks.OPEN),
                Task.cancel_requested_at.is_(None),
                Task.payload["api_task_id"].astext == str(row.id),
            )
        )
        for task_id in follow_ups:
            cancelled = await tasks.request_cancel(session, task_id, "api_cancel") or cancelled
        if cancelled:
            # 群里已经有「在某系统里发起」那句：说一声这条不做了，免得群里的人一直等。
            await outbox.add(
                session,
                bot_id=row.bot_id,
                platform="feishu",
                kind="send",
                dedupe_key=f"api-task:{row.id}:cancelled",
                target={"chat_id": row.chat_id},
                payload={"markdown": f"「{client.name}」取消了这条任务。"},
            )
    return cancelled


async def cancel_many(
    session: AsyncSession, client: ApiClient, rows: list[ApiTask]
) -> list[uuid.UUID]:
    """逐条取消，返回真的取消了的任务 id。调用方负责提交。"""
    done: list[uuid.UUID] = []
    for row in rows:
        if await cancel(session, client, row):
            done.append(row.id)
    return done
