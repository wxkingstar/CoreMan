"""业务系统权限申请：所有者确认后，为他的个人助理申请一个白名单外的业务系统，平台管理员批准或拒绝。

对这位助理开放的系统所有者自己就能开（他是这位助理的机器人管理员），不走这里。这里只记申请、
通知审批人、改缺口状态；批准时写授权在 API 层（`coreman/api/system_grants.apply_grants`）。
理由与拒绝理由是写给对方看的话，明文存，长度在这里限住。谁能发起、谁能审批由调用方校验：
发起只能是所有者本人且当面确认过（无人值守的轮次不能提交），审批是 ai_committee / platform_admin。
只 flush，不提交；日志只有编号与计数。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import gaps
from coreman.core.assistants.records import is_assistant
from coreman.core.auth.token_providers import RESERVED_SYSTEM_KEYS
from coreman.core.bots.permissions import MANAGER_ROLES
from coreman.core.bus import outbox
from coreman.core.db.models import (
    AssistantGap,
    Bot,
    BotSystemGrant,
    BusinessSystem,
    PlatformApp,
    SystemAccessRequest,
    User,
    UserIdentity,
)
from coreman.core.logging import get_logger
from coreman.core.timeutils import utcnow

log = get_logger(__name__)
MAX_REASON = 300
MAX_NOTE = 300
STALE_BOT = "批准时这位助理已停用或不在了，申请自动撤回"
STALE_GAP = "批准时这个缺口已不在等待申请（所有者不开了或已解决），申请自动撤回"


class RequestError(Exception):
    """调用方能照着改的拒绝；`code` 原样返回，不带任何内容。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _text(value: str, limit: int) -> str:
    clean = " ".join(value.split())
    if not clean or len(clean) > limit:
        raise RequestError("invalid_reason")
    return clean


def clean_reason(value: str) -> str:
    """申请理由按入库的样子整理（空白合并）；空的或超长的抛 invalid_reason。"""
    return _text(value, MAX_REASON)


async def _notify(
    session: AsyncSession, bot: Bot, row: SystemAccessRequest, system: BusinessSystem, owner: User
) -> int:
    """私信能审批的人：与技能审批同一个通知应用；没有唯一的通知应用时不发，只在管理台列出。"""
    apps = list(
        await session.scalars(
            select(PlatformApp).where(
                PlatformApp.platform == bot.platform,
                PlatformApp.enabled,
                PlatformApp.capabilities.contains(["notify"]),
            )
        )
    )
    if len(apps) != 1:
        return 0
    found = (
        await session.execute(
            select(User, UserIdentity)
            .join(UserIdentity, UserIdentity.user_id == User.id)
            .where(
                User.status == "active",
                User.role.in_(MANAGER_ROLES),
                UserIdentity.platform == bot.platform,
            )
            .order_by(User.id)
        )
    ).all()
    level = "读写" if row.write else "只读"
    content = (
        f"业务系统权限申请待处理\n申请人：{owner.display_name}（为自己的个人助理）\n"
        f"系统：{system.name}（{system.key}），{level}\n理由：{row.reason}\n"
        "请在 CoreMan 管理台「业务系统 → 权限申请」里处理。"
    )
    for manager, identity in found:
        await outbox.add(
            session,
            bot_id=None,
            platform=bot.platform,
            kind="notify",
            dedupe_key=f"system-access-request:{row.id}:{manager.id}",
            target={
                "platform_app_id": str(apps[0].id),
                "user_id": str(manager.id),
                "platform_user_id": identity.platform_user_id,
            },
            payload={"msgtype": "text", "content": content},
        )
    return len(found)


async def requestable(
    session: AsyncSession, bot: Bot, gap_seq: int
) -> tuple[AssistantGap, BusinessSystem]:
    """这个缺口现在能不能提交申请：拿咨询锁、锁缺口行后核对，不写任何东西。

    Raises:
        RequestError: not_business_system / gap_not_open / owner_can_grant /
            system_not_requestable
        GapError: gap_not_found
    """
    await gaps.lock(session, bot.id)
    gap = await gaps.get(session, bot.id, gap_seq, lock=True)
    if gap.kind != "business_system":
        raise RequestError("not_business_system")
    if gap.status != "open":
        raise RequestError("gap_not_open")
    fix = await gaps.fix_for(session, bot.id, gap)
    if fix == "self_grant":
        raise RequestError("owner_can_grant")
    if fix != "request":
        raise RequestError("system_not_requestable")
    system = await session.get(BusinessSystem, gap.key)
    assert system is not None  # fix_for 已确认登记过
    return gap, system


async def open_request(
    session: AsyncSession, bot: Bot, gap_seq: int, *, write: bool, reason: str, actor: User
) -> SystemAccessRequest:
    """所有者确认后发起：只给已启用、白名单里没有这位助理的系统建单。

    调用方保证 `actor` 是这位助理的所有者，且这一轮有人在场。

    Raises:
        RequestError: invalid_reason / not_business_system / gap_not_open /
            owner_can_grant / system_not_requestable / already_pending
        GapError: gap_not_found
    """
    text = _text(reason, MAX_REASON)
    gap, system = await requestable(session, bot, gap_seq)
    live = await session.scalar(
        select(SystemAccessRequest.id).where(
            SystemAccessRequest.bot_id == bot.id,
            SystemAccessRequest.system_key == gap.key,
            SystemAccessRequest.status == "pending",
        )
    )
    if live is not None:
        raise RequestError("already_pending")
    now = utcnow()
    # 显式写创建时间：同一事务里撤回又重提时，`latest_for` 仍分得出先后。
    row = SystemAccessRequest(
        bot_id=bot.id,
        gap_id=gap.id,
        system_key=gap.key,
        write=write,
        reason=text,
        requested_by=actor.id,
        created_at=now,
    )
    try:
        # 部分唯一索引兜底：查重之后别处抢先建了一张在途的，只回滚这一小段。
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        raise RequestError("already_pending") from None
    gap.status, gap.updated_at = "requested", now
    await session.flush()
    sent = await _notify(session, bot, row, system, actor)
    log.info("system_access_requested", bot_id=str(bot.id), request_id=str(row.id), notified=sent)
    return row


# 加锁顺序（与 gaps.py、写授权的 `system_grants.lock_bot` / `apply_grants` 一致，处处相同）：
# 这位助理的缺口咨询锁 → 申请行 → 缺口行 → 机器人行 → 系统行（按 key 排序，一条语句锁齐）。


async def _locked(session: AsyncSession, request_id: uuid.UUID) -> SystemAccessRequest:
    """先不加锁读出属于哪位助理，拿这位助理的咨询锁，再锁申请行并确认还在途。"""
    bot_id = await session.scalar(
        select(SystemAccessRequest.bot_id).where(SystemAccessRequest.id == request_id)
    )
    if bot_id is None:
        raise RequestError("request_not_found")
    await gaps._lock(session, bot_id)
    return await _pending(session, request_id)


async def _pending(session: AsyncSession, request_id: uuid.UUID) -> SystemAccessRequest:
    row = await session.get(
        SystemAccessRequest, request_id, with_for_update=True, populate_existing=True
    )
    if row is None:
        raise RequestError("request_not_found")
    if row.status != "pending":
        raise RequestError("request_decided")
    return row


async def _gap(session: AsyncSession, row: SystemAccessRequest) -> AssistantGap | None:
    """申请挂的缺口，加锁并读最新（缺口删了时为 None）。"""
    if row.gap_id is None:
        return None
    return await session.get(AssistantGap, row.gap_id, with_for_update=True, populate_existing=True)


def _reopen(gap: AssistantGap | None) -> bool:
    """缺口还在「已申请」就回到 open；返回是否改了。"""
    if gap is None or gap.status != "requested":
        return False
    gap.status, gap.updated_at = "open", utcnow()
    return True


async def withdraw(session: AsyncSession, bot_id: uuid.UUID, gap_seq: int) -> SystemAccessRequest:
    """所有者撤回这个缺口上在途的申请，缺口回到 open。

    Raises:
        GapError: gap_not_found（不是这位助理的缺口）
        RequestError: request_not_found（这个缺口上没有在途的申请，含非业务系统的缺口）
    """
    await gaps._lock(session, bot_id)
    gap = await gaps.get(session, bot_id, gap_seq)
    found = await session.scalar(
        select(SystemAccessRequest.id).where(
            SystemAccessRequest.bot_id == bot_id,
            SystemAccessRequest.gap_id == gap.id,
            SystemAccessRequest.status == "pending",
        )
    )
    if found is None:
        raise RequestError("request_not_found")
    row = await _pending(session, found)
    row.status, row.decided_at = "withdrawn", utcnow()
    _reopen(await _gap(session, row))
    await session.flush()
    log.info("system_access_withdrawn", bot_id=str(bot_id), request_id=str(row.id))
    return row


async def reject(
    session: AsyncSession, request_id: uuid.UUID, *, actor: User, note: str
) -> SystemAccessRequest:
    """拒绝：缺口回到 open，被它卡住的事交回助理（带上拒绝理由）。调用方校验 `actor` 能审批。

    缺口已经不在「已申请」（所有者不开了、已经解决）时只记拒绝，不再交回提醒。

    Raises:
        RequestError: invalid_reason / request_not_found / request_decided
    """
    text = _text(note, MAX_NOTE)
    row = await _locked(session, request_id)
    row.status, row.decided_by, row.decided_at = "rejected", actor.id, utcnow()
    row.decision_note = text
    gap = await _gap(session, row)
    reopened = _reopen(gap)
    await session.flush()
    if reopened and gap is not None:
        await gaps.tell(
            session, row.bot_id, gap, "gap_rejected", level="batch", extra={"note": text}
        )
    log.info("system_access_rejected", bot_id=str(row.bot_id), request_id=str(row.id))
    return row


async def mark_approved(
    session: AsyncSession, request_id: uuid.UUID, *, actor: User
) -> tuple[SystemAccessRequest, BusinessSystem, bool]:
    """批准：把助理加进系统白名单（版本号随之加 1），不写授权（由 API 层 `apply_grants` 写，
    写完再调 `settle_approved`）。

    第三项：这一次是否把助理加进了白名单（原本不含它）。调用方据此记 `system.update` 审计。
    调用方校验 `actor` 能审批。

    加锁见上方顺序：机器人行与写授权要用到的全部系统行（现有授权 + 这个系统）都在这里锁好。

    批准时重新核对：助理还在（启用中的个人助理），挂的缺口（如果有）还在「已申请」。不再成立时
    这张申请记为撤回（`decided_by` 是这位管理员，备注写明原因）、不动白名单，并抛
    `request_stale`——这时行已经 flush，调用方要提交后再把错误告诉管理员。

    Raises:
        RequestError: request_not_found / request_decided / request_stale / system_not_requestable
    """
    row = await _locked(session, request_id)
    gap = await _gap(session, row)
    bot = await session.get(Bot, row.bot_id, with_for_update=True, populate_existing=True)
    if bot is None or not bot.enabled or not is_assistant(bot):
        stale = STALE_BOT
    elif row.gap_id is not None and (gap is None or gap.status != "requested"):
        stale = STALE_GAP
    else:
        stale = None
    if stale is not None:
        row.status, row.decided_by, row.decided_at = "withdrawn", actor.id, utcnow()
        row.decision_note = stale
        _reopen(gap)
        await session.flush()
        log.info("system_access_stale", bot_id=str(row.bot_id), request_id=str(row.id))
        raise RequestError("request_stale")
    # 写授权（`apply_grants`）要锁的系统是现有授权加上这个系统：在改白名单之前按 key 一次锁齐，
    # 之后写授权不再新拿系统锁，不会与别的助理的批准、管理台改权限交叉等待。
    granted = set(
        await session.scalars(
            select(BotSystemGrant.system_key).where(BotSystemGrant.bot_id == row.bot_id)
        )
    )
    locked = (
        await session.scalars(
            select(BusinessSystem)
            .where(BusinessSystem.key.in_(granted | {row.system_key}))
            .order_by(BusinessSystem.key)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).all()
    system = next((s for s in locked if s.key == row.system_key), None)
    if system is None or not system.enabled or system.key in RESERVED_SYSTEM_KEYS:
        raise RequestError("system_not_requestable")
    added = system.allowed_bot_ids is not None and row.bot_id not in system.allowed_bot_ids
    if added:
        system.allowed_bot_ids = [*(system.allowed_bot_ids or []), row.bot_id]
    row.status, row.decided_by, row.decided_at = "approved", actor.id, utcnow()
    await session.flush()
    log.info("system_access_approved", bot_id=str(row.bot_id), request_id=str(row.id), added=added)
    return row, system, added


async def settle_approved(session: AsyncSession, row: SystemAccessRequest) -> bool:
    """批准并写完授权（`apply_grants` 按级别解决缺口）之后调：缺口还在「已申请」说明批下来的
    级别仍不够它（例如报的是要写、批的是只读），把它放回 open 并交回助理，让它自己再核对或
    重新登记。返回是否放回了。"""
    await gaps._lock(session, row.bot_id)
    gap = await _gap(session, row)
    if not _reopen(gap) or gap is None:
        return False
    await session.flush()
    await gaps.tell(session, row.bot_id, gap, "gap_approved_short", level="batch")
    log.info("system_access_approved_short", bot_id=str(row.bot_id), request_id=str(row.id))
    return True


async def latest_for(
    session: AsyncSession, gap_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, SystemAccessRequest]:
    """每个缺口最近的一张申请（不论状态）。"""
    wanted = list(gap_ids)
    if not wanted:
        return {}
    rows = await session.scalars(
        select(SystemAccessRequest)
        .where(SystemAccessRequest.gap_id.in_(wanted))
        .order_by(SystemAccessRequest.created_at, SystemAccessRequest.id)
    )
    return {row.gap_id: row for row in rows if row.gap_id is not None}


def view(
    row: SystemAccessRequest, *, bot_name: str, system_name: str, requester: str
) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "bot_id": str(row.bot_id),
        "bot_name": bot_name,
        "system_key": row.system_key,
        "system_name": system_name,
        "write": row.write,
        "reason": row.reason,
        "requester": requester,
        "status": row.status,
        "decision_note": row.decision_note,
        "created_at": row.created_at.isoformat(),
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
    }
