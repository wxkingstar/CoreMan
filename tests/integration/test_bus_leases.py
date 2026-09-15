from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import instances, leases
from coreman.core.db.models import Bot, BotLease, User


async def _bots(session: AsyncSession) -> tuple[Bot, Bot]:
    user = User(login_name="c", display_name="c")
    session.add(user)
    await session.flush()
    on = Bot(
        bot_key="on",
        platform="wecom",
        name="on",
        created_by=user.id,
        model="m",
        working_dir="/d",
        credentials_enc="enc:v1:x",
    )
    off = Bot(
        bot_key="off",
        platform="wecom",
        name="off",
        created_by=user.id,
        model="m",
        working_dir="/d",
        credentials_enc="enc:v1:x",
        enabled=False,
    )
    session.add_all([on, off])
    await session.commit()
    return on, off


async def test_acquire_heartbeat_release_and_takeover(db_session: AsyncSession) -> None:
    on, off = await _bots(db_session)
    a = await instances.register(
        db_session, instance_id="gw-a", service="gateway-wecom", version="dev", capacity=None
    )
    b = await instances.register(
        db_session, instance_id="gw-b", service="gateway-wecom", version="dev", capacity=None
    )
    await leases.ensure_rows(db_session, "wecom")
    await db_session.commit()
    assert await leases.acquirable(db_session, "wecom") == [on.id]  # 停用 bot 不在内
    lease = await leases.acquire(db_session, bot_id=on.id, platform="wecom", instance_id=a.id)
    await db_session.commit()
    assert lease and lease.holder_instance == a.id and lease.generation == 1
    assert (
        await leases.acquire(db_session, bot_id=on.id, platform="wecom", instance_id=b.id) is None
    )
    await db_session.rollback()
    # rollback 会过期会话内全部 ORM 对象；异步会话不允许属性访问里隐式加载，用完前先显式刷新。
    for obj in (on, a, b):
        await db_session.refresh(obj)
    assert await leases.acquirable(db_session, "wecom") == []
    assert await leases.heartbeat(db_session, a.id) == 1
    await leases.set_state(db_session, on.id, "subscribed", instance_id=a.id)
    await db_session.commit()
    held = await leases.held_by(db_session, a.id)
    assert [h.bot_id for h in held] == [on.id] and held[0].connection_state == "subscribed"
    # 心跳过期 → 可被 b 接管，代次 +1
    row = (await db_session.execute(select(BotLease).where(BotLease.bot_id == on.id))).scalar_one()
    row.heartbeat_at = datetime.now(UTC) - timedelta(seconds=31)
    await db_session.commit()
    assert await leases.acquirable(db_session, "wecom") == [on.id]
    taken = await leases.acquire(db_session, bot_id=on.id, platform="wecom", instance_id=b.id)
    await db_session.commit()
    assert taken and taken.holder_instance == b.id and taken.generation == 2
    # 非持有者：无效果
    await leases.release(db_session, bot_id=on.id, instance_id=a.id, generation=2)
    await db_session.commit()
    await db_session.refresh(row)
    assert row.holder_instance == b.id
    await leases.request_drain(db_session, on.id, by="admin")
    await db_session.commit()
    assert await leases.acquirable(db_session, "wecom") == []  # 持有者还活着，不换手
    # 代次对不上（上一代排空里迟到的那次 release）：同样无效果
    await leases.release(db_session, bot_id=on.id, instance_id=b.id, generation=1)
    await db_session.commit()
    await db_session.refresh(row)
    assert row.holder_instance == b.id
    await leases.release(db_session, bot_id=on.id, instance_id=b.id, generation=2)
    await db_session.commit()
    await db_session.refresh(row)
    assert (
        row.holder_instance is None
        and row.released_at is not None
        and row.connection_state == "disconnected"
    )
    assert len(await leases.list_leases(db_session)) == 2


async def test_drain_flag_without_a_live_holder_is_recoverable(db_session: AsyncSession) -> None:
    """排空标记不挡认领：排空做到一半挂掉的网关，不能让这个 bot 永久没人接。

    标记只在「持有者还活着」时有意义（那时 holder/heartbeat 条件自己会挡住换手）；持有者
    没了就该让下一轮扫描把它捡起来，认领时顺手清掉标记。
    """
    on, _off = await _bots(db_session)
    a = await instances.register(
        db_session, instance_id="gw-a", service="gateway-wecom", version="dev", capacity=None
    )
    await leases.ensure_rows(db_session, "wecom")
    await leases.request_drain(db_session, on.id, by="admin")  # 无持有者，只有标记
    await db_session.commit()
    assert await leases.acquirable(db_session, "wecom") == [on.id]
    lease = await leases.acquire(db_session, bot_id=on.id, platform="wecom", instance_id=a.id)
    await db_session.commit()
    assert lease and lease.holder_instance == a.id
    assert lease.drain_requested_by is None and lease.drain_requested_at is None


async def test_set_state_only_applies_to_the_current_holder(db_session: AsyncSession) -> None:
    """连接状态只有当前持有者写得动：上一任迟到的回调不得覆盖新持有者的状态。"""
    on, _off = await _bots(db_session)
    for name in ("gw-a", "gw-b"):
        await instances.register(
            db_session, instance_id=name, service="gateway-wecom", version="dev", capacity=None
        )
    await leases.ensure_rows(db_session, "wecom")
    await db_session.commit()
    assert await leases.acquire(db_session, bot_id=on.id, platform="wecom", instance_id="gw-b")
    await leases.set_state(db_session, on.id, "subscribed", instance_id="gw-b")
    await leases.set_state(db_session, on.id, "disconnected", instance_id="gw-a")  # 非持有者
    await db_session.commit()
    row = await db_session.get(BotLease, on.id, populate_existing=True)
    assert row and row.connection_state == "subscribed"


async def test_drain_is_finished_by_release_and_the_bot_comes_back(
    db_session: AsyncSession,
) -> None:
    """排空的终点是「释放」：标记跟着一起清掉，下一轮扫描这个 bot 就能被重新认领。

    不清标记的话，点一次「排空」等于把这个 bot 永久摘下线——停用再启用都救不回来。
    """
    on, _off = await _bots(db_session)
    a = await instances.register(
        db_session, instance_id="gw-a", service="gateway-wecom", version="dev", capacity=None
    )
    await leases.ensure_rows(db_session, "wecom")
    await db_session.commit()
    first = await leases.acquire(db_session, bot_id=on.id, platform="wecom", instance_id=a.id)
    await db_session.commit()
    assert first and first.holder_instance == a.id and first.generation == 1
    await leases.request_drain(db_session, on.id, by="admin")
    await db_session.commit()
    assert await leases.acquirable(db_session, "wecom") == []  # 持有者还活着，不换手
    await leases.release(db_session, bot_id=on.id, instance_id=a.id, generation=1)
    await db_session.commit()
    row = await db_session.get(BotLease, on.id, populate_existing=True)
    assert row and row.drain_requested_by is None and row.drain_requested_at is None
    assert await leases.acquirable(db_session, "wecom") == [on.id]
    again = await leases.acquire(db_session, bot_id=on.id, platform="wecom", instance_id=a.id)
    await db_session.commit()
    assert again and again.holder_instance == a.id and again.generation == 2
    assert again.drain_requested_by is None and again.drain_requested_at is None
