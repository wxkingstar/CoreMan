"""删掉节点上不再用的助理工作目录：停用时的当前目录、换运行时换走的旧目录。

登记（`retire`）与删除分开：登记在停用或换运行时的同一个事务里；删除由看护进程（`tick`）向目录所在
实例的节点发 `workspace-remove`，节点离线、目录还在用、或节点认不准归属（`unverified`）就退避重试，
直到节点答复「已删除」或「不是这位助理的」。归属由节点按它自己保管的登记认（不看助理写得到的目录内
标记），删除不跟随任何链接（见 runtime_daemon/workspace.py `Workspace.remove`）。
"""

from __future__ import annotations

import asyncio
import time
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
# 一轮最多花这么久（节点不可达时一条就可能占满 NODE_SECONDS）：用完就不再取新的，剩下的下一轮再说，
# 免得一台死掉的节点把开通与停用的那条循环拖住十来分钟。
TICK_SECONDS = 90
_clock = time.monotonic
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
    code = _not_final(result)
    if code is not None:
        # 节点上还有沙箱里的轮次用着这个目录、节点认不准归属、或回答不像样：留着登记，过一阵再来。
        row.attempts += 1
        row.error_code = code
        row.next_attempt_at = row.next_attempt_at + _backoff(row.attempts)
        if code != "busy":
            log.warning("assistant_dir_retry", retired=str(row.id), code=code)
        return "retry"
    if result["removed"] is not True:
        # 节点按它的登记确认这个目录记着别的助理：不归我们删。
        log.warning("assistant_dir_not_owned", retired=str(row.id))
    await session.delete(row)
    return "removed"


def _not_final(result: object) -> str | None:
    """节点的回答不能当定论时给出原因代码。

    只有布尔的 `removed`/`existed`、不带 `unverified` 才算定论。
    """
    if not isinstance(result, dict):
        return "bad_node_answer"
    if "unverified" in result:
        return "dir_unverified" if result["unverified"] is True else "bad_node_answer"
    if not isinstance(result.get("removed"), bool) or not isinstance(result.get("existed"), bool):
        return "bad_node_answer"
    if result.get("busy") is True:
        return "busy"
    return None


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
    """处理到期的几条；每条一个事务、互不影响；一轮的总时长以 `TICK_SECONDS` 为限。"""
    counts = {"assistant_dirs_removed": 0, "assistant_dirs_retry": 0}
    started = _clock()
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
        if _clock() - started >= TICK_SECONDS:
            break
        try:
            async with factory() as session:
                # 再核对一次到期：换主期间另一个看护进程可能刚处理过它（并把时间往后推了）。
                row = await session.scalar(
                    select(AssistantRetiredDir)
                    .where(
                        AssistantRetiredDir.id == row_id,
                        AssistantRetiredDir.next_attempt_at <= now,
                    )
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
