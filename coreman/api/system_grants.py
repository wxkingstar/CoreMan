"""AI 员工的业务系统授权写入：管理台改系统权限、所有者给自己的助理一键开通、管理员批准申请共用。

调用方负责权限判断（机器人管理员，或批准申请的平台管理员）、If-Match 与提交。
"""

from __future__ import annotations

import uuid

from fastapi import Request
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.errors import ApiError
from coreman.core.audit import diff_dict
from coreman.core.auth.system_access import RESERVED_SYSTEM_KEYS
from coreman.core.bots.events import notify_bot_changed
from coreman.core.db.models import Bot, BotSystemGrant, BusinessSystem, User


async def current_grants(session: AsyncSession, bot_id: uuid.UUID) -> tuple[list[str], list[str]]:
    rows = (
        await session.execute(
            select(BotSystemGrant.system_key, BotSystemGrant.allow_write)
            .where(BotSystemGrant.bot_id == bot_id)
            .order_by(BotSystemGrant.system_key)
        )
    ).all()
    return [key for key, _ in rows], [key for key, allowed in rows if allowed]


async def lock_bot(session: AsyncSession, bot: Bot) -> None:
    """写授权前先拿这位机器人的缺口咨询锁，再锁机器人行并读回最新版本号。

    顺序与缺口、权限申请一致（咨询锁在最前），管理台改权限与所有者一键开通互相排队：先拿锁
    再读现有授权，谁也不会覆盖掉对方刚写的。可重入：同一事务里调几次都行。
    """
    from coreman.core.assistants import gaps

    await gaps.lock(session, bot.id)
    await session.refresh(bot, ["version"], with_for_update=True)


async def apply_grants(
    session: AsyncSession,
    request: Request,
    user: User,
    bot: Bot,
    *,
    keys: list[str],
    write_keys: list[str] | None,
    comment: str = "",
) -> tuple[list[str], list[str]]:
    """写入授权并返回（授权的系统、允许写的系统）；只 flush，调用方提交。

    不做任何权限判断与 If-Match 核对，由调用方负责。要先读现有授权再改的调用方，读之前先调
    `lock_bot`。
    """
    # 函数内引入：这几个辅助函数定义在路由模块里，路由模块又要引用本模块。
    from coreman.api.routers.infrastructure_admin import audit, persist, sync_companions
    from coreman.core.assistants import capabilities, gaps

    bot_id = bot.id
    await lock_bot(session, bot)
    keys = sorted(set(keys))
    if write_keys is not None and not set(write_keys) <= set(keys):
        raise ApiError(422, 422, "允许写入的系统必须在授权列表中")
    systems = list(
        (
            await session.execute(
                select(BusinessSystem)
                .where(BusinessSystem.key.in_(keys))
                .order_by(BusinessSystem.key)
                .with_for_update()
            )
        ).scalars()
    )
    # allowed_bot_ids 为 null 的历史行仍按「对全部机器人开放」处理（不做数据迁移），
    # 管理台会明示这一状态；新建系统缺省是空白名单。
    if len(systems) != len(keys) or any(
        not s.enabled
        or s.key in RESERVED_SYSTEM_KEYS
        or (s.allowed_bot_ids is not None and bot_id not in s.allowed_bot_ids)
        for s in systems
    ):
        raise ApiError(403, 403, "申请包含未开放给此机器人的系统")
    previous = (
        await session.execute(
            delete(BotSystemGrant)
            .where(BotSystemGrant.bot_id == bot_id)
            .returning(BotSystemGrant.system_key, BotSystemGrant.allow_write)
        )
    ).all()
    before = [key for key, _ in previous]
    writes_before = sorted(key for key, allowed in previous if allowed)
    writes = sorted(set(write_keys) if write_keys is not None else set(writes_before) & set(keys))
    session.add_all(
        [
            BotSystemGrant(
                bot_id=bot_id, system_key=key, granted_by=user.id, allow_write=key in writes
            )
            for key in keys
        ]
    )
    installed, uninstalled = await sync_companions(
        session,
        request,
        user,
        bot,
        systems,
        set(before),
        set(before) - set(keys),
    )
    bot.version += 1
    await persist(session)
    await notify_bot_changed(session, bot.id)
    # 个人助理被缺口卡住的系统现在授权了：级别升上去的记为已解决，被卡住的事交回续接
    # （没有缺口时是空查询）。只读授权不会解决报在「能读」级别的缺口。
    await gaps.resolve_quietly(
        session,
        bot.id,
        "business_system",
        {s.key: capabilities.grant_rank(s, s.key in writes) for s in systems},
        log_event="assistant_gap_resolve_failed_on_grant",
    )
    # 申请说明原先只落在已弃用的 system_grant_audit 表，现与授权前后对比一起进审计日志。
    diff = diff_dict(
        {"system_keys": sorted(before), "write_keys": writes_before},
        {
            "system_keys": keys,
            "write_keys": writes,
            "comment": comment or None,
            "companion_installed": installed or None,
            "companion_uninstalled": uninstalled or None,
        },
    )
    await audit(session, request, user, "bot.system_grants", "bot", str(bot_id), diff)
    await persist(session)
    return keys, writes
