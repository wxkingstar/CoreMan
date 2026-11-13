"""个人内容的访问记录：运行时路径之外的每一次解密都记一行（表 `assistant_access_log`）。

「运行时路径」是助理自己的轮次（worker、盯消息、投递、审批执行）。之外的是有人或有命令把内容
拿出来：「我的助理」页的读取、导入与导出、所有者在管理台读自己的对话记录、服务器上的管理命令。
记录里只有编号、动作与对象的代号：`target` 只能是计数或明文的范围名（`open`、`rows=3`），
知识页的路径与标题本身是个人内容，调用方不要传进来。

与审计日志（`coreman/core/audit.py`）的区别：审计记「谁改了平台上的什么」，给管理员看；这里记
「谁读了某位所有者的个人内容」，给所有者本人看，管理员只看得到「最近一次非所有者访问的时间」。

落库与主操作在同一个事务里（调用方提交）：读取接口在返回之前提交，提交不了内容也不会交出去。
写这一行本身失败（外键对不上、表不可用）不挡主操作——放在保存点里，失败只回滚到保存点并记
一条应用日志。`assistant_content_access` 这条应用日志无论落库成败都写，是兜底的第二份记录。
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, insert, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import sealed
from coreman.core.db.models import ACCESS_ACTOR_KINDS, AssistantAccessLog, Bot, User
from coreman.core.logging import get_logger

log = get_logger(__name__)

ACTIONS = (
    # 「我的助理」页的读取
    "approvals.read",
    "followups.read",
    "jobs.read",
    "notes.index",
    "notes.read",
    "settings.read",
    # 所有者在管理台读自己的对话记录、任务详情
    "chatlogs.read",
    "tasks.read",
    # 导入与导出
    "import.preview",
    "import.notes",
    "export",
    # 服务器上的管理命令：重新包裹数据密钥（不读内容）；以所有者的身份读对话记录、任务详情
    # （内容不还原，记的是有人试过）
    "cli.rewrap",
    "cli.chatlogs",
    "cli.tasks",
)
MAX_TARGET = 64
# 不等于任何用户的查看者：管理命令以某位成员的身份建的会话拿它去「只对本人还原」，
# 助理的内容于是一律读不出来（`sealed.open_row(..., viewer=NOBODY)`）。
NOBODY = uuid.UUID(int=0)
RETENTION = timedelta(days=180)
MAX_LIST = 200


@dataclass(frozen=True)
class Seen:
    """给所有者看的一行访问记录。"""

    id: int
    at: datetime
    actor_kind: str
    # 操作人的显示名；管理命令或账号已删除时为空。
    actor_name: str | None
    action: str
    target: str


async def record(
    session: AsyncSession,
    bot_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    action: str,
    target: str = "",
    *,
    kind: str | None = None,
) -> None:
    """记一次解密。调用方在交出内容之前调用，并负责提交。

    Args:
        actor_id: 操作人；服务器上的管理命令没有操作人，传 None
        action: `ACTIONS` 之一
        target: 计数或明文的范围名，超长截断；不要放任何内容
        kind: 省略时推出来——没有操作人是 `system`，操作人是所有者是 `owner`，否则 `admin`；
            管理命令以某位成员的身份操作时，调用方明说 `system`

    Raises:
        ValueError: 不认识的动作或操作人类别（写错的调用点要在测试里就失败）
    """
    if action not in ACTIONS:
        raise ValueError(f"unknown access action: {action}")
    if kind is None:
        if actor_id is None:
            kind = "system"
        else:
            kind = "owner" if await sealed.owner_of(session, bot_id) == actor_id else "admin"
    elif kind not in ACCESS_ACTOR_KINDS:
        raise ValueError(f"unknown actor kind: {kind}")
    cut = target[:MAX_TARGET]
    log.info(
        "assistant_content_access",
        bot_id=str(bot_id),
        actor_id=str(actor_id) if actor_id else None,
        actor_kind=kind,
        action=action,
        target=cut,
    )
    # 先把主操作攒着的改动刷下去：它们出错应当照常抛给调用方，不能被下面的保存点吞掉。
    await session.flush()
    try:
        async with session.begin_nested():
            await session.execute(
                insert(AssistantAccessLog).values(
                    bot_id=bot_id, actor_id=actor_id, actor_kind=kind, action=action, target=cut
                )
            )
    except SQLAlchemyError as exc:
        log.warning(
            "assistant_access_log_failed",
            bot_id=str(bot_id),
            action=action,
            error=type(exc).__name__,
        )


async def record_opened(
    session: AsyncSession, rows: Iterable[Any], actor_id: uuid.UUID | None, action: str
) -> int:
    """一批可能混着普通员工的行：只数真的还原出了内容的助理行，每位助理记一条。

    `sealed.open_rows(..., viewer=…)` 之后调用：查看者不是所有者、密钥已销毁的行内容已被收回，
    `sealed.is_opened` 为假，不算。返回记了几位助理（为 0 时调用方不必提交）。
    """
    counts: Counter[uuid.UUID] = Counter(
        row.bot_id for row in rows if row is not None and sealed.is_opened(row)
    )
    for bot_id, count in counts.items():
        await record(session, bot_id, actor_id, action, f"rows={count}")
    return len(counts)


async def record_refused(
    session: AsyncSession, rows: Iterable[Any], owner_id: uuid.UUID, action: str
) -> int:
    """管理命令以所有者的身份来读，内容没有还原（`viewer=NOBODY`）：记下「有人试过」。

    只数所有者是 `owner_id` 的助理行——换成本人在场的会话，这些行本来会被还原。每位助理记一条
    `system`，不留操作人：敲命令的人在服务器上，不是这位成员本人。别人的助理行对这个身份本来
    就读不出来，与管理台里的非所有者一样不记。返回记了几位助理（为 0 时调用方不必提交）。
    """
    counts: Counter[uuid.UUID] = Counter()
    for row in rows:
        bot_id = getattr(row, "bot_id", None) if type(row) in sealed.COLUMNS else None
        if bot_id is not None and await sealed.owner_of(session, bot_id) == owner_id:
            counts[bot_id] += 1
    for bot_id, count in counts.items():
        await record(session, bot_id, None, action, f"rows={count}", kind="system")
    return len(counts)


async def bots_of(session: AsyncSession, owner_ids: Iterable[uuid.UUID]) -> list[uuid.UUID]:
    """这些人名下还启用着的助理记录。"""
    owners = list(owner_ids)
    if not owners:
        return []
    found = await session.scalars(
        select(Bot.id).where(
            Bot.kind == "assistant", Bot.enabled.is_(True), Bot.owner_id.in_(owners)
        )
    )
    return list(found)


async def list_(
    session: AsyncSession, bot_id: uuid.UUID, *, limit: int = 50, before: int | None = None
) -> list[Seen]:
    """这位助理的访问记录，新的在前。

    Args:
        limit: 至多 `MAX_LIST`
        before: 只要编号小于它的（上一页最后一行的编号）
    """
    stmt = (
        select(AssistantAccessLog, User.display_name)
        .outerjoin(User, User.id == AssistantAccessLog.actor_id)
        .where(AssistantAccessLog.bot_id == bot_id)
        .order_by(AssistantAccessLog.id.desc())
        .limit(max(1, min(limit, MAX_LIST)))
    )
    if before is not None:
        stmt = stmt.where(AssistantAccessLog.id < before)
    return [
        Seen(row.id, row.at, row.actor_kind, name, row.action, row.target)
        for row, name in (await session.execute(stmt)).all()
    ]


async def last_foreign(session: AsyncSession) -> dict[uuid.UUID, datetime]:
    """每位助理最近一次非所有者访问的时间；从来没有的不在结果里。"""
    rows = await session.execute(
        select(AssistantAccessLog.bot_id, func.max(AssistantAccessLog.at))
        .where(AssistantAccessLog.actor_kind != "owner")
        .group_by(AssistantAccessLog.bot_id)
    )
    return {bot_id: seen for bot_id, seen in rows.all()}


async def cleanup(session: AsyncSession, now: datetime, *, limit: int = 500) -> int:
    """删掉超过保留期的行，一次至多 `limit` 行；返回删了多少。调用方提交。"""
    doomed = (
        select(AssistantAccessLog.id)
        .where(AssistantAccessLog.at < now - RETENTION)
        .order_by(AssistantAccessLog.id)
        .limit(limit)
        .scalar_subquery()
    )
    result = await session.execute(
        delete(AssistantAccessLog)
        .where(AssistantAccessLog.id.in_(doomed))
        .returning(AssistantAccessLog.id)
    )
    return len(result.all())
