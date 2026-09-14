"""`interaction_states` 数据访问（spec §5.4）。每个函数接收 AsyncSession，调用方控制事务。

`state` 的形状：
- choice: {questions, answers, current_index, waiting_for_text, waiting_since, context{...}}
- relay_switch: {idx, current_relay_id, current_name, target_relay_id, target_name,
  target_pct_7d, chat_id, user_id}
- session_switch: {sessions: [{relay_session_id, preview}]}
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, delete, or_, select, update
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import InteractionState

KIND_CHOICE = "choice"
KIND_RELAY_SWITCH = "relay_switch"
KIND_SESSION_SWITCH = "session_switch"


def choice_scope(bot_id: uuid.UUID, platform_user_id: str) -> str:
    """choice / relay_switch 的作用域是「这个 bot 上的这个发言者」：群里各人各一份。"""
    return f"{bot_id}:{platform_user_id}"


def session_scope(bot_id: uuid.UUID, session_key: str) -> str:
    """session_switch 的作用域是会话本身：切的是这个会话用哪条 relay 会话。"""
    return f"{bot_id}:{session_key}"


def _rowcount(res: Any) -> int:
    """`AsyncSession.execute` 静态返回 `Result`，只有 DML 拿到的 `CursorResult` 才有 rowcount。"""
    return int(cast("CursorResult[Any]", res).rowcount or 0)


async def open_state(
    session: AsyncSession,
    *,
    bot_id: uuid.UUID,
    kind: str,
    scope_key: str,
    state: dict[str, Any],
    task_id_prefix: str | None = None,
    expires_at: datetime | None = None,
) -> InteractionState:
    """同 (kind, scope_key) 只保留最新一条：先删旧行再插入（唯一约束 DEFERRABLE，同事务内成立）。

    新问题来了就作废上一轮的待答状态——旧卡片上的按钮从此按不动（找不到状态即视为过期），
    否则用户点了半天旧卡片，答案会被写进一个已经没人等的槽里。
    """
    await session.execute(
        delete(InteractionState).where(
            InteractionState.kind == kind, InteractionState.scope_key == scope_key
        )
    )
    row = InteractionState(
        bot_id=bot_id,
        kind=kind,
        scope_key=scope_key,
        state=state,
        task_id_prefix=task_id_prefix,
        expires_at=expires_at,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def get_open(
    session: AsyncSession, *, kind: str, scope_key: str, now: datetime | None = None
) -> InteractionState | None:
    """还等着用户下一步的那条。过期判定放在 SQL 里：不能指望看护任务已经跑过。"""
    now = now or datetime.now(UTC)
    stmt = select(InteractionState).where(
        InteractionState.kind == kind,
        InteractionState.scope_key == scope_key,
        InteractionState.status == "open",
        or_(InteractionState.expires_at.is_(None), InteractionState.expires_at > now),
    )
    return (
        await session.execute(stmt, execution_options={"populate_existing": True})
    ).scalar_one_or_none()


async def find_by_prefix(
    session: AsyncSession, *, bot_id: uuid.UUID, task_id_prefix: str
) -> InteractionState | None:
    """卡片回调只带得回 task_id：按前缀反查是哪一轮（不限 status，过期的也要认出来回过期卡）。"""
    stmt = select(InteractionState).where(
        InteractionState.bot_id == bot_id, InteractionState.task_id_prefix == task_id_prefix
    )
    return (
        await session.execute(stmt, execution_options={"populate_existing": True})
    ).scalar_one_or_none()


async def patch_state(session: AsyncSession, state_id: uuid.UUID, patch: dict[str, Any]) -> None:
    """JSONB 合并（`state || patch`），只改给出的键。

    整块覆写会丢掉并发那一侧刚写进去的键（答题与限流切换可能同时落在一条状态上）；
    `patch` 显式 CAST 成 jsonb，否则 `jsonb || unknown` 会被 Postgres 解成文本拼接。
    """
    await session.execute(
        update(InteractionState)
        .where(InteractionState.id == state_id)
        .values(state=InteractionState.state.op("||", return_type=JSONB)(sql_cast(patch, JSONB)))
        .execution_options(synchronize_session=False)
    )


async def set_status(
    session: AsyncSession, state_id: uuid.UUID, status: str, *, expected: str = "open"
) -> bool:
    """原子状态迁移：只有当前是 `expected` 才改，返回是否改到（重复提交的防线）。"""
    res = await session.execute(
        update(InteractionState)
        .where(InteractionState.id == state_id, InteractionState.status == expected)
        .values(status=status)
        .execution_options(synchronize_session=False)
    )
    return _rowcount(res) == 1


async def remove(session: AsyncSession, state_id: uuid.UUID) -> bool:
    res = await session.execute(delete(InteractionState).where(InteractionState.id == state_id))
    return _rowcount(res) == 1


async def clear_for_speaker(
    session: AsyncSession, *, bot_id: uuid.UUID, platform_user_id: str
) -> int:
    """reset/stop：这个发言者在这个 bot 上的 choice 与 relay_switch 状态全部作废。"""
    res = await session.execute(
        delete(InteractionState).where(
            InteractionState.bot_id == bot_id,
            InteractionState.kind.in_((KIND_CHOICE, KIND_RELAY_SWITCH)),
            InteractionState.scope_key == choice_scope(bot_id, platform_user_id),
        )
    )
    return _rowcount(res)


async def clear_for_session(session: AsyncSession, *, bot_id: uuid.UUID, session_key: str) -> int:
    """清会话时连带作废这个会话的切换待答状态：要切的那些会话映射已经不在了。"""
    res = await session.execute(
        delete(InteractionState).where(
            InteractionState.bot_id == bot_id,
            InteractionState.kind == KIND_SESSION_SWITCH,
            InteractionState.scope_key == session_scope(bot_id, session_key),
        )
    )
    return _rowcount(res)


async def expire_due(session: AsyncSession, now: datetime) -> int:
    """看护动作：到点没人答的置为 expired（留行不删，管理台还要看得到这轮问过什么）。"""
    res = await session.execute(
        update(InteractionState)
        .where(
            InteractionState.status == "open",
            InteractionState.expires_at.is_not(None),
            InteractionState.expires_at < now,
        )
        .values(status="expired")
        .execution_options(synchronize_session=False)
    )
    return _rowcount(res)
