"""bot_leases 读写（spec §5.4、§6.5）。"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import exists, func, literal, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus.notify import notify
from coreman.core.db.models import Bot, BotLease
from coreman.core.observability.metrics import TAKEOVERS, after_commit

STALE_AFTER_SECONDS = 30
# 认领语句逐字取自 spec §6.5；过期判定用 make_interval 绑参，不做 SQL 字符串拼接。
# 不看 drain_requested_by：排空标记只在「持有者还活着」时才该挡住换手，而那由下面的
# holder/heartbeat 条件挡着。标记自己挡认领的话，排空做到一半挂掉的网关会让这个 bot 永久
# 没人接（标记再没人来清）；认领成功时顺手把标记清掉，排空就此结束。
_ACQUIRE_SQL = text(
    """
UPDATE bot_leases SET holder_instance=:me, generation=generation+1,
                      acquired_at=now(), heartbeat_at=now(),
                      drain_requested_by=NULL, drain_requested_at=NULL, released_at=NULL,
                      connection_state='connecting'
WHERE bot_id=:bot_id AND platform=:platform
  AND (holder_instance IS NULL OR heartbeat_at < now() - make_interval(secs => :stale))
RETURNING bot_id
"""
)


async def ensure_rows(session: AsyncSession, platform: str) -> None:
    """为该平台的每个 bot 补一行租约（含停用的 bot：停用只由 acquirable 过滤，行本身保留）。"""
    missing = select(Bot.id, literal(platform)).where(
        Bot.platform == platform,
        ~exists(select(BotLease.bot_id).where(BotLease.bot_id == Bot.id)),
    )
    stmt = insert(BotLease).from_select(["bot_id", "platform"], missing)
    await session.execute(stmt.on_conflict_do_nothing(index_elements=[BotLease.bot_id]))


async def acquirable(session: AsyncSession, platform: str) -> list[uuid.UUID]:
    stale = func.now() - text(f"interval '{STALE_AFTER_SECONDS} seconds'")
    stmt = (
        select(BotLease.bot_id)
        .join(Bot, Bot.id == BotLease.bot_id)
        .where(
            Bot.enabled.is_(True),
            BotLease.platform == platform,
            # 与 _ACQUIRE_SQL 同源：排空标记不参与筛选，只看有没有活着的持有者。
            (BotLease.holder_instance.is_(None)) | (BotLease.heartbeat_at < stale),
        )
        .order_by(BotLease.bot_id)
    )
    return list((await session.execute(stmt)).scalars())


async def acquire(
    session: AsyncSession, *, bot_id: uuid.UUID, platform: str, instance_id: str
) -> BotLease | None:
    """抢占式认领：无持有者或持有者心跳过期才成功，代次 +1；成功时同事务 notify。"""
    row = (
        await session.execute(
            _ACQUIRE_SQL,
            {
                "me": instance_id,
                "bot_id": bot_id,
                "platform": platform,
                "stale": float(STALE_AFTER_SECONDS),
            },
        )
    ).first()
    if row is None:
        return None
    after_commit(session, lambda: TAKEOVERS.labels(platform).inc())
    await notify(session, "lease_changed", {"bot_id": str(bot_id)})
    return await session.get(BotLease, bot_id, populate_existing=True)


async def heartbeat(session: AsyncSession, instance_id: str) -> int:
    """为本实例持有的全部租约续心跳；返回续上的条数。"""
    rows = (
        await session.execute(
            update(BotLease)
            .where(BotLease.holder_instance == instance_id)
            .values(heartbeat_at=func.now())
            .returning(BotLease.bot_id)
        )
    ).all()
    return len(rows)


async def release(
    session: AsyncSession,
    *,
    bot_id: uuid.UUID,
    instance_id: str,
    generation: int,
    state: str = "disconnected",
) -> None:
    """只有当前持有者能释放；非持有者调用无副作用、不发通知。

    `state` 是释放时落下的 `connection_state`：默认 disconnected，被抢连踢掉后交还租约时传
    `kicked`（释放之后这一行已经不是本实例的了，`set_state` 的持有者守卫会挡住补写）。

    `generation` 守卫不可省：停用→秒级重新启用这种操作会让同一个实例先后持有 gen 与 gen+1
    两代租约，只按 bot+instance 释放的话，上一代排空里迟到的那次 release 会把新一代的租约
    也放掉——连接随即变孤儿被停掉，用户看到的就是莫名其妙的第三次重连。

    释放就是排空的第 ④ 步，所以顺手把排空标记清掉：按 bot 排空要的是「这个 bot 换一次连接」
    （单实例部署表现为重连，有备用实例时是交接），不是把它永久摘下线。排空没做完之前别人也
    抢不走——那段时间本实例的心跳还在续，`acquirable` / `acquire` 的持有者条件挡着。
    """
    row = (
        await session.execute(
            update(BotLease)
            .where(
                BotLease.bot_id == bot_id,
                BotLease.holder_instance == instance_id,
                BotLease.generation == generation,
            )
            .values(
                holder_instance=None,
                released_at=func.now(),
                connection_state=state,
                drain_requested_by=None,
                drain_requested_at=None,
            )
            .returning(BotLease.bot_id)
        )
    ).first()
    if row is not None:
        await notify(session, "lease_changed", {"bot_id": str(bot_id)})


async def set_state(
    session: AsyncSession, bot_id: uuid.UUID, state: str, *, instance_id: str
) -> None:
    """只有当前持有者能写连接状态：上一任的连接回调迟到时不得覆盖新持有者的状态。"""
    await session.execute(
        update(BotLease)
        .where(BotLease.bot_id == bot_id, BotLease.holder_instance == instance_id)
        .values(connection_state=state)
    )


async def request_drain(session: AsyncSession, bot_id: uuid.UUID, by: str) -> None:
    await session.execute(
        update(BotLease)
        .where(BotLease.bot_id == bot_id)
        .values(drain_requested_by=by, drain_requested_at=func.now())
    )
    await notify(session, "lease_changed", {"bot_id": str(bot_id)})


async def live_holder(session: AsyncSession, bot_id: uuid.UUID) -> str | None:
    """这一行此刻有没有活着的持有者（心跳没过期）；没有返回 None。

    判据与 `acquirable` 同源。按 bot 排空是「让持有它的网关换一次连接」，没有持有者时这个
    请求没有任何人会执行——标记只会挂在行上，管理台却显示「排空中」。
    """
    stale = func.now() - text(f"interval '{STALE_AFTER_SECONDS} seconds'")
    stmt = select(BotLease.holder_instance).where(
        BotLease.bot_id == bot_id,
        BotLease.holder_instance.is_not(None),
        BotLease.heartbeat_at >= stale,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def disabled_among(session: AsyncSession, bot_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
    """这些 bot 里已经被停用的。

    网关每轮拿它复核自己持有的连接：`config_changed` 通知会在 LISTEN 重连的窗口里丢，只靠
    通知的话，停用的机器人会一直挂着连接到进程重启为止。
    """
    if not bot_ids:
        return set()
    stmt = select(Bot.id).where(Bot.id.in_(list(bot_ids)), Bot.enabled.is_(False))
    return set((await session.execute(stmt)).scalars())


async def held_by(session: AsyncSession, instance_id: str) -> list[BotLease]:
    stmt = select(BotLease).where(BotLease.holder_instance == instance_id).order_by(BotLease.bot_id)
    return list(
        (await session.execute(stmt, execution_options={"populate_existing": True})).scalars()
    )


async def list_leases(session: AsyncSession) -> list[BotLease]:
    stmt = select(BotLease).order_by(BotLease.platform, BotLease.bot_id)
    return list(
        (await session.execute(stmt, execution_options={"populate_existing": True})).scalars()
    )
