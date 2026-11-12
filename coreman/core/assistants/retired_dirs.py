"""删掉节点上不再用的助理工作目录：停用时的当前目录、换运行时换走的旧目录。

登记（`retire`）与删除分开：登记在停用或换运行时的同一个事务里；删除由看护进程（`tick`）向目录所在
实例的节点发 `workspace-remove`，节点离线就退避重试，直到删掉或节点答复「不是这位助理的」。节点那边
只删带本记录所有权标记的目录、不跟随任何链接（见 runtime_daemon/workspace.py `Workspace.remove`）。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.crypto import Cipher
from coreman.core.db.models import AssistantRetiredDir, Bot, RelayServer
from coreman.core.logging import get_logger
from coreman.core.relay.agent_client import AgentError, call_agent

log = get_logger(__name__)
BATCH = 10
NODE_SECONDS = 60
BACKOFF_MAX = timedelta(hours=6)


async def retire(
    session: AsyncSession, *, bot_id: uuid.UUID, relay_id: uuid.UUID, working_dir: str
) -> None:
    """登记一个要删的目录；同一实例同一路径只登记一次。调用方提交。"""
    await session.execute(
        insert(AssistantRetiredDir)
        .values(id=uuid.uuid4(), bot_id=bot_id, relay_server_id=relay_id, working_dir=working_dir)
        .on_conflict_do_nothing(constraint="uq_assistant_retired_dirs_path")
    )


def _backoff(attempts: int) -> timedelta:
    return min(BACKOFF_MAX, timedelta(minutes=2 ** min(attempts, 9)))


async def _in_use(session: AsyncSession, row: AssistantRetiredDir, relay: RelayServer) -> bool:
    """这个目录现在又是某条启用记录的工作目录（同一节点同一路径）：绝不删。"""
    bot = await session.get(Bot, row.bot_id)
    if bot is None or not bot.enabled or bot.working_dir != row.working_dir:
        return False
    current = await session.get(RelayServer, bot.relay_server_id) if bot.relay_server_id else None
    return current is not None and current.runtime_node_id == relay.runtime_node_id


async def _one(session: AsyncSession, cipher: Cipher, row: AssistantRetiredDir) -> str:
    relay = await session.get(RelayServer, row.relay_server_id)
    if relay is None or await _in_use(session, row, relay):
        await session.delete(row)
        return "dropped"
    try:
        async with asyncio.timeout(NODE_SECONDS):
            result = await call_agent(
                relay,
                cipher,
                "workspace-remove",
                {"working_dir": row.working_dir, "bot_id": str(row.bot_id)},
            )
    except (AgentError, TimeoutError) as exc:
        row.attempts += 1
        row.error_code = getattr(exc, "code", None) or type(exc).__name__
        row.next_attempt_at = row.next_attempt_at + _backoff(row.attempts)
        return "retry"
    if result.get("busy") is True:
        # 节点上还有沙箱里的轮次用着这个目录：等它结束再来。
        row.attempts += 1
        row.error_code = "busy"
        row.next_attempt_at = row.next_attempt_at + _backoff(row.attempts)
        return "retry"
    if result.get("removed") is not True:
        # 目录在，但不带这位助理的标记（初始化没做完、或已被别人占用）：不归我们删。
        log.warning("assistant_dir_not_owned", retired=str(row.id))
    await session.delete(row)
    return "removed"


async def _postpone(
    factory: async_sessionmaker[AsyncSession], row_id: uuid.UUID, now: datetime
) -> None:
    """出了意外的那条推迟一阵，不让它每一轮都卡在同一个地方。"""
    try:
        async with factory() as session:
            await session.execute(
                update(AssistantRetiredDir)
                .where(AssistantRetiredDir.id == row_id)
                .values(
                    attempts=AssistantRetiredDir.attempts + 1,
                    next_attempt_at=now + BACKOFF_MAX,
                    error_code="unexpected",
                )
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 推迟不了就下一轮再说
        log.warning("assistant_dir_postpone_failed", error=type(exc).__name__)


async def tick(
    factory: async_sessionmaker[AsyncSession], cipher: Cipher, now: datetime
) -> dict[str, int]:
    """处理到期的几条；每条一个事务、互不影响。"""
    counts = {"assistant_dirs_removed": 0, "assistant_dirs_retry": 0}
    async with factory() as session:
        due = list(
            await session.scalars(
                select(AssistantRetiredDir.id)
                .where(AssistantRetiredDir.next_attempt_at <= now)
                .order_by(AssistantRetiredDir.next_attempt_at)
                .limit(BATCH)
            )
        )
    for row_id in due:
        try:
            async with factory() as session:
                row = await session.scalar(
                    select(AssistantRetiredDir)
                    .where(AssistantRetiredDir.id == row_id)
                    .with_for_update(skip_locked=True)
                )
                if row is None:
                    continue
                row.next_attempt_at = max(row.next_attempt_at, now)
                outcome = await _one(session, cipher, row)
                await session.commit()
        except Exception as exc:  # noqa: BLE001 一条出错不挡后面的
            log.warning("assistant_dir_remove_failed", error=type(exc).__name__)
            await _postpone(factory, row_id, now)
            continue
        if outcome == "removed":
            counts["assistant_dirs_removed"] += 1
        elif outcome == "retry":
            counts["assistant_dirs_retry"] += 1
    return counts
