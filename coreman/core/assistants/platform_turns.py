"""平台来源的轮次：平台自己发起、在助理会话里跑的一轮（交回事项、子任务及其结果、下班编译）。

这样的任务没有「所有者刚发来的那条消息」可以核验，却要拿到所有者的个人工具。放行的依据是：
- 任务只能由平台代码创建——`enqueue` 是唯一入口，网关入库与任何接口都产生不了这些类型
  （tests/unit/test_assistant_platform_turn_ledger.py 扫全库守着这一条）；
- 身份来自开通时记下的 `assistant_settings`，是飞书给的操作人，不来自任何请求内容；
- 每次用到都重新核对：记录还是启用中的助理、任务归所有者、会话是这位助理的、所有者的账号、
  通讯录身份与授权行都没变（`scope_for`）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import kinds
from coreman.core.assistants import settings as assistant_settings
from coreman.core.assistants.kinds import ORIGIN_KINDS
from coreman.core.assistants.records import is_assistant
from coreman.core.bus import tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import (
    AssistantSettings,
    Bot,
    FeishuPersonalGrant,
    PlatformApp,
    Task,
    User,
    UserIdentity,
)
from coreman.core.feishu_personal.policy import Scope
from coreman.core.logging import get_logger

log = get_logger(__name__)

# 子任务的会话键：`<主会话键>#job:<编号>`。
JOB_SEPARATOR = "#job:"


def is_platform(task: Task) -> bool:
    """这个任务是不是平台自己发起的一轮：没有入站事件，类型在名单里。"""
    return task.inbound_event_id is None and task.kind in ORIGIN_KINDS


def unattended(task: Task) -> bool:
    """这一轮是不是无人在场（`kinds.unattended`）：能力收紧按它判断。

    比 `is_platform` 宽：带着提交那一轮入站事件的审批结果回合也算。`is_platform` 只回答
    「身份从哪里核验」（没有入站事件 → 开通时记下的设置），不回答所有者在不在场。
    """
    return kinds.unattended(task.kind)


def owns_session(row: AssistantSettings, session_key: str | None) -> bool:
    """会话键是不是这位助理的：与所有者的私聊（主会话），或它派生的子任务会话。"""
    if not row.chat_id or not session_key:
        return False
    return session_key == row.chat_id or session_key.startswith(row.chat_id + JOB_SEPARATOR)


def _runnable(bot: Bot) -> bool:
    return (
        bot.enabled
        and is_assistant(bot)
        and bot.assistant_state == "active"
        and bot.owner_id is not None
    )


async def enqueue(
    session: AsyncSession,
    bot: Bot,
    kind: str,
    payload: dict[str, Any],
    *,
    session_key: str | None = None,
    dedupe_key: str,
) -> Task | None:
    """给这位助理排一轮平台来源的任务；创建这些类型任务的唯一入口。调用方提交。

    Args:
        kind: `ORIGIN_KINDS` 之一
        payload: 这一轮的内容（按所有者的数据密钥封存）；`serialize_session`、`bot_key`、
            `platform_user_id` 由这里写，调用方给的同名键被覆盖
        session_key: 不给就是主会话（所有者与助理的私聊）；子任务给 `<主会话键>#job:<编号>`
        dedupe_key: 幂等键

    Returns:
        入队的任务；同键任务已存在，或这位助理现在跑不了平台来源的轮次（不是启用中的助理、
        没记下身份、会话键不是它的）时返回 None

    Raises:
        ValueError: `kind` 不是平台来源的类型
    """
    if kind not in ORIGIN_KINDS:
        raise ValueError(f"not a platform turn kind: {kind}")
    if not _runnable(bot):
        return None
    row = await session.get(AssistantSettings, bot.id)
    key = session_key or (row.chat_id if row is not None else "")
    if row is None or not assistant_settings.identified(row) or not owns_session(row, key):
        log.warning("assistant_platform_turn_refused", bot_id=str(bot.id), kind=kind)
        return None
    new = NewTask(
        bot_id=bot.id,
        kind=kind,
        user_id=bot.owner_id,
        session_key=key,
        dedupe_key=dedupe_key,
        payload={
            **payload,
            # 同一会话串行：所有者正在聊的那一轮没结束时排在它后面。
            "serialize_session": True,
            "bot_key": bot.bot_key,
            "platform_user_id": row.platform_user_id,
        },
    )
    # 总线不收别处来的、没有入站事件的这些类型：只放行这里建的这一个任务。
    admitted = tasks._ADMITTED.set(new)
    try:
        return await tasks.enqueue(session, new)
    finally:
        tasks._ADMITTED.reset(admitted)


async def scope_for(session: AsyncSession, bot: Bot, task: Task) -> Scope:
    """平台来源的一轮以所有者身份用个人工具的核验结果；不来自任何调用方给的身份。

    调用方（`policy.task_scope`）已核对任务仍在运行、没被取消，并在拿到结果后核对能力令牌里的
    人就是所有者。这里核对其余的：任务是平台来源的、归这位助理的所有者、会话是这位助理的；
    记录是启用中的助理；所有者账号可用，通讯录里的飞书身份还是开通时记下的那个；授权行（有的
    话）也是这个身份。授权行与记下的身份对不上时在这里拒绝，不交给 `service._locked`：它遇到
    不一致会清空令牌，一条写错的设置不该把所有者的授权清掉。

    Raises:
        ValueError: 任何一项对不上
    """
    if (
        not is_platform(task)
        or task.bot_id != bot.id
        or bot.platform != "feishu"
        or not _runnable(bot)
        or task.user_id != bot.owner_id
    ):
        raise ValueError("platform_turn_required")
    assert bot.owner_id is not None
    row = await session.get(AssistantSettings, bot.id, populate_existing=True)
    if (
        row is None
        or not assistant_settings.identified(row)
        or not owns_session(row, task.session_key)
    ):
        raise ValueError("platform_turn_required")
    owner = await session.get(User, bot.owner_id, populate_existing=True)
    if owner is None or owner.status != "active" or owner.source == "bootstrap":
        raise ValueError("personal_permission_revoked")
    bound = await session.scalar(
        select(UserIdentity.user_id)
        .where(
            UserIdentity.user_id == owner.id,
            UserIdentity.platform == "feishu",
            UserIdentity.platform_user_id == row.platform_user_id,
        )
        .limit(1)
    )
    if bound is None:
        raise ValueError("personal_grant_required")
    grant = await session.get(FeishuPersonalGrant, (bot.id, owner.id), populate_existing=True)
    if grant is not None and (grant.platform_user_id, grant.open_id, grant.tenant_key) != (
        row.platform_user_id,
        row.open_id,
        row.tenant_key,
    ):
        raise ValueError("personal_grant_required")
    if grant is not None:
        app_id = grant.app_id
    else:
        app = await session.get(PlatformApp, bot.platform_app_id) if bot.platform_app_id else None
        if app is None or not app.app_id:
            raise ValueError("personal_grant_required")
        app_id = app.app_id
    return Scope(
        task,
        bot,
        owner.id,
        row.tenant_key,
        app_id,
        row.open_id,
        row.platform_user_id,
        row.chat_id,
        None,
        True,
    )
