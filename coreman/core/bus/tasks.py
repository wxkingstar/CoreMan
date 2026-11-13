"""tasks 表读写。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import sealed
from coreman.core.bus.notify import notify
from coreman.core.db.models import Task
from coreman.core.observability.metrics import TASK_DURATION, after_commit

# 认领语句只 RETURNING id，随后按主键取回 ORM 行。工作目录未就绪的员工不认领任务；切换作业例外，
# 它本身就是让员工离开未就绪状态的途径（初始化失败、未分配运行时），被别的迁移占着时它自己会拒绝。
_CLAIM_SQL = text(
    """
UPDATE tasks SET status='claimed', claimed_by=:instance, claimed_at=now(), heartbeat_at=now(),
                 attempts=attempts+1
WHERE id = (SELECT candidate.id FROM tasks candidate
            JOIN bots workspace_bot ON workspace_bot.id=candidate.bot_id
            WHERE (workspace_bot.workspace_state='ready' OR candidate.kind='relay_switch')
            AND candidate.status='queued'
            AND candidate.lane=:lane AND candidate.run_after<=now()
            AND (candidate.kind='command'
                 OR candidate.payload->>'serialize_session' IS DISTINCT FROM 'true'
                 OR NOT EXISTS (
                     SELECT 1 FROM tasks prior WHERE prior.bot_id=candidate.bot_id
                     AND prior.session_key=candidate.session_key AND prior.id<>candidate.id
                     AND (prior.status IN ('claimed','running') OR
                          (prior.status='queued' AND prior.id<candidate.id
                           AND prior.cancel_requested_at IS NULL
                           AND prior.payload->>'stale_hold' IS DISTINCT FROM 'true'))
                 ))
            ORDER BY candidate.priority DESC, candidate.id LIMIT 1
            FOR UPDATE OF candidate SKIP LOCKED FOR SHARE OF workspace_bot SKIP LOCKED)
RETURNING id
"""
)
ACTIVE = ("claimed", "running")
OPEN = ("queued", "claimed", "running")


class Heartbeat(StrEnum):
    """`heartbeat` 的三态（全库统一用它判「我还该不该继续跑」）。

    Attributes:
        OK: 行仍在 ACTIVE，没人要求取消
        CANCELLED: 行仍在 ACTIVE，但被请求取消（用户 stop / 被替代 / 管理台）
        LOST: 行已不在 ACTIVE 或根本不存在——多半是 reaper 判这个 worker 失联、
            替它写了终态；持有者必须就地停手，不能再改这一行
    """

    OK = "ok"
    CANCELLED = "cancelled"
    LOST = "lost"


@dataclass
class NewTask:
    bot_id: uuid.UUID
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    lane: str = "normal"
    priority: int = 0
    session_key: str | None = None
    user_id: uuid.UUID | None = None
    inbound_event_id: int | None = None
    run_after: datetime | None = None
    dedupe_key: str | None = None


async def enqueue(session: AsyncSession, new: NewTask) -> Task | None:
    """入队；带 dedupe_key 且已存在同键任务时返回 None（幂等），不发通知。"""
    values: dict[str, Any] = {
        "bot_id": new.bot_id,
        "kind": new.kind,
        "lane": new.lane,
        "priority": new.priority,
        "session_key": new.session_key,
        "user_id": new.user_id,
        "inbound_event_id": new.inbound_event_id,
        "payload": new.payload,
        "dedupe_key": new.dedupe_key,
    }
    if new.run_after is not None:
        values["run_after"] = new.run_after
    values = await sealed.seal_values(session, Task, new.bot_id, values)
    stmt = insert(Task).values(**values)
    if new.dedupe_key is not None:
        stmt = stmt.on_conflict_do_nothing(index_elements=[Task.dedupe_key])
    row = (await session.execute(stmt.returning(Task.id))).first()
    if row is None:
        return None
    await notify(session, "tasks_queued", {"lane": new.lane, "bot_id": str(new.bot_id)})
    return await get(session, int(row[0]))


async def claim(session: AsyncSession, *, lane: str, instance_id: str) -> Task | None:
    # 所有机器人共用这条队列：一条解不开的任务抛错会回滚认领，下一轮又领到它，整条车道就停了。
    sealed.tolerate_undecryptable(session)
    row = (await session.execute(_CLAIM_SQL, {"instance": instance_id, "lane": lane})).first()
    if row is None:
        return None
    task = await get(session, int(row[0]))
    if task is not None and sealed.is_undecryptable(task):
        # 解不开（主密钥配错、密文被改）：落终态不再被认领。不写报错文字——写它要用同一把
        # 密钥封存，主密钥配错时同样写不进去。
        await finish(
            session,
            task.id,
            status="failed",
            error_code=sealed.UNDECRYPTABLE_CODE,
            bot_id=task.bot_id,
        )
        return None
    if task is not None and sealed.is_locked(task):
        # 个人助理的数据密钥已销毁：内容读不出来，不交给处理器。
        await finish(
            session,
            task.id,
            status="failed",
            error_code=sealed.LOCKED_CODE,
            error_message="所有者的数据密钥已销毁，任务内容不可读",
            bot_id=task.bot_id,
        )
        return None
    return task


async def get(session: AsyncSession, task_id: int) -> Task | None:
    return await session.get(Task, task_id, populate_existing=True)


async def start(session: AsyncSession, task_id: int) -> None:
    await session.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(status="running", started_at=func.now(), heartbeat_at=func.now())
    )


async def heartbeat(session: AsyncSession, task_id: int) -> tuple[Heartbeat, str | None]:
    """写 heartbeat_at；返回 (三态, 取消原因)。

    「行已不在 ACTIVE」必须与「没人要求取消」分开：前者意味着这一行已经被别人（reaper）
    结掉了，继续跑下去就会出现两条终态、两条 chat_log。
    """
    row = (
        await session.execute(
            update(Task)
            .where(Task.id == task_id, Task.status.in_(ACTIVE))
            .values(heartbeat_at=func.now())
            .returning(Task.cancel_requested_at, Task.cancel_reason)
        )
    ).first()
    if row is None:
        return Heartbeat.LOST, None
    return (Heartbeat.CANCELLED if row[0] is not None else Heartbeat.OK), row[1]


async def finish(
    session: AsyncSession,
    task_id: int,
    *,
    status: str,
    result: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    only_active: bool = False,
    bot_id: uuid.UUID | None = None,
) -> bool:
    """写终态；返回是否真的写进去了。

    `only_active=True` 时加 `status IN ACTIVE` 守卫：被 reaper 收割过的行不得再被原持有者
    改回 succeeded（守卫与 UPDATE 同一条语句，不存在读后写的窗口）。

    Args:
        bot_id: 任务所属的机器人，封存 `result` 与 `error_message` 时用；没传且有这两样要写时
            按主键查一次
    """
    # 截断按明文长度做，在加密之前。
    message = error_message[:5000] if error_message else None
    content: dict[str, Any] = {}
    if result is not None:
        content["result"] = result
    if message is not None:
        content["error_message"] = message
    if content:
        if bot_id is None:
            bot_id = await session.scalar(select(Task.bot_id).where(Task.id == task_id))
        stored = await sealed.seal_values(session, Task, bot_id, content)
        result, message = stored.get("result"), stored.get("error_message")
    else:
        stored = content
    stmt = update(Task).where(Task.id == task_id)
    if only_active:
        stmt = stmt.where(Task.status.in_(ACTIVE))
    row = (
        await session.execute(
            stmt.values(
                status=status,
                finished_at=func.now(),
                result=result,
                error_code=error_code,
                error_message=message,
            ).returning(Task.id, Task.kind, Task.started_at, Task.finished_at)
        )
    ).first()
    # 这条语句会把写入的值同步到会话里已加载的任务对象上：助理的换回明文，调用方不用重取。
    sealed.keep_plain(session, Task, content, stored)
    if row is not None and row[2] is not None:
        kind, elapsed = row[1], max(0, (row[3] - row[2]).total_seconds())
        after_commit(session, lambda: TASK_DURATION.labels(kind, status).observe(elapsed))
    return row is not None


async def request_cancel(session: AsyncSession, task_id: int, reason: str) -> bool:
    """仅对未结束且尚未被请求取消的任务生效；成功时同事务 notify task_cancel。"""
    row = (
        await session.execute(
            update(Task)
            .where(Task.id == task_id, Task.status.in_(OPEN), Task.cancel_requested_at.is_(None))
            .values(
                cancel_requested_at=func.now(),
                # 取消原因不加密（心跳、运维接口、收尾都直接读）：个人助理的行只留代码。
                cancel_reason=sealed.code_for_assistants(Task.bot_id, reason, "cancelled"),
            )
            .returning(Task.id)
        )
    ).first()
    if row is None:
        return False
    await notify(session, "task_cancel", {"task_id": task_id})
    return True


async def active_for_session(
    session: AsyncSession, bot_id: uuid.UUID, session_key: str
) -> list[Task]:
    stmt = (
        select(Task)
        .where(Task.bot_id == bot_id, Task.session_key == session_key, Task.status.in_(ACTIVE))
        .order_by(Task.id)
    )
    rows = list(
        (await session.execute(stmt, execution_options={"populate_existing": True})).scalars()
    )
    await sealed.open_rows(session, rows)
    return rows


async def open_for_session(
    session: AsyncSession, bot_id: uuid.UUID, session_key: str, kind: str
) -> list[Task]:
    """会话里某类尚未结束（排队、已认领或运行中）的任务。"""
    stmt = (
        select(Task)
        .where(
            Task.bot_id == bot_id,
            Task.session_key == session_key,
            Task.kind == kind,
            Task.status.in_(OPEN),
        )
        .order_by(Task.id)
    )
    rows = list(
        (await session.execute(stmt, execution_options={"populate_existing": True})).scalars()
    )
    await sealed.open_rows(session, rows)
    return rows


async def release_hold(session: AsyncSession, task: Task) -> None:
    """等确认的排队消息放行：立刻可认领，不再问第二次。调用方持有行锁。"""
    payload = {k: v for k, v in task.payload.items() if k != "stale_hold"}
    task.payload = {**payload, "stale_confirmed": True}
    task.run_after = func.now()
    await session.flush()
    await notify(session, "tasks_queued", {"lane": task.lane, "bot_id": str(task.bot_id)})


async def defer(session: AsyncSession, task_id: int, *, seconds: float) -> bool:
    """把已认领、未被请求取消的任务放回队列，`seconds` 秒后才能再被认领；返回是否命中。

    不算失败、不写终态：下一次认领照常 attempts+1，调用方据此给重排次数封顶。
    已被请求取消的不放回——那是用户 stop 或被更新的消息替代，应当按取消收尾。
    """
    row = (
        await session.execute(
            update(Task)
            .where(
                Task.id == task_id,
                Task.status.in_(ACTIVE),
                Task.cancel_requested_at.is_(None),
            )
            .values(
                status="queued",
                claimed_by=None,
                claimed_at=None,
                started_at=None,
                heartbeat_at=None,
                run_after=func.now() + timedelta(seconds=max(seconds, 0.0)),
            )
            .returning(Task.lane, Task.bot_id)
        )
    ).first()
    if row is None:
        return False
    await notify(session, "tasks_queued", {"lane": row[0], "bot_id": str(row[1])})
    return True


async def has_newer(
    session: AsyncSession,
    bot_id: uuid.UUID,
    session_key: str,
    *,
    task_id: int,
    kind: str,
) -> bool:
    """同会话里是否已有比 `task_id` 更晚入队、会替代它的同类任务（不论状态）。

    排队等前面做完的任务（载荷 `serialize_session`：群里别人交代的事、API 发起的轮次）
    不算：它们本来就排在后面，不能因为前一条被放回队列就把它挤掉。
    """
    stmt = (
        select(Task.id)
        .where(
            Task.bot_id == bot_id,
            Task.session_key == session_key,
            Task.kind == kind,
            Task.id > task_id,
        )
        .limit(1)
    )
    stmt = stmt.where(Task.payload["serialize_session"].astext.is_distinct_from("true"))
    return (await session.scalar(stmt)) is not None


async def in_the_way(
    session: AsyncSession, bot_id: uuid.UUID, session_key: str, *, task_id: int
) -> bool:
    """同会话里有没有该让的任务：别的任务在跑，或有不等 `task_id` 就会开跑的 chat 消息在排队。

    排在 `task_id` 后面等它做完的消息（载荷 `serialize_session`、编号更大）不算：认领语句
    不会放它先跑，让给它就成了两边互等。等确认的消息（`stale_hold`）同样不算。
    """
    waiting = and_(
        Task.status == "queued",
        Task.kind == "chat",
        Task.cancel_requested_at.is_(None),
        Task.payload["stale_hold"].astext.is_distinct_from("true"),
        or_(
            Task.payload["serialize_session"].astext.is_distinct_from("true"),
            Task.id < task_id,
        ),
    )
    stmt = (
        select(Task.id)
        .where(
            Task.bot_id == bot_id,
            Task.session_key == session_key,
            Task.id != task_id,
            or_(Task.status.in_(ACTIVE), waiting),
        )
        .limit(1)
    )
    return (await session.scalar(stmt)) is not None


async def supersede(
    session: AsyncSession, bot_id: uuid.UUID, session_key: str, *, except_task_id: int
) -> list[int]:
    """同 (bot, session_key) 串行：把其它活动任务标记为 superseded，返回被标记的 task_id。"""
    ids = [t.id for t in await active_for_session(session, bot_id, session_key)]
    out: list[int] = []
    for tid in ids:
        if tid == except_task_id:
            continue
        if await request_cancel(session, tid, "superseded"):
            out.append(tid)
    return out


async def queue_depth(session: AsyncSession) -> dict[str, int]:
    stmt = select(Task.lane, func.count()).where(Task.status == "queued").group_by(Task.lane)
    rows = (await session.execute(stmt)).all()
    depth = {"normal": 0, "fast": 0}
    depth.update({str(lane): int(n) for lane, n in rows})
    return depth


async def list_active(session: AsyncSession, *, limit: int = 200) -> list[Task]:
    stmt = select(Task).where(Task.status.in_(ACTIVE)).order_by(Task.id).limit(limit)
    return list(
        (await session.execute(stmt, execution_options={"populate_existing": True})).scalars()
    )
