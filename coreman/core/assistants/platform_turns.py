"""平台来源的轮次：平台自己发起、在助理会话里跑的一轮（交回事项、子任务及其结果、下班编译）。

这样的任务没有「所有者刚发来的那条消息」可以核验，却要拿到所有者的个人工具。放行的依据是：
- 任务只能由平台代码创建——`enqueue` 是唯一入口，网关入库与任何接口都产生不了这些类型
  （tests/unit/test_assistant_platform_turn_ledger.py 扫全库守着这一条）；
- 身份来自开通时记下的 `assistant_settings`，是飞书给的操作人，不来自任何请求内容；
- 每次用到都重新核对：记录还是启用中的助理、任务归所有者、会话是这位助理的、所有者的账号、
  通讯录身份与授权行都没变（Task 4 的 `scope_for`）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import settings as assistant_settings
from coreman.core.assistants.kinds import APPROVAL_RESULT_KIND, PLATFORM_TURN_KINDS
from coreman.core.assistants.records import is_assistant
from coreman.core.bus import tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import AssistantSettings, Bot, Task
from coreman.core.logging import get_logger

log = get_logger(__name__)

# 没有入站事件也能跑的任务类型：平台来源的四种，加上在这样的轮次里提交的审批单的结果回合。
ORIGIN_KINDS = (*PLATFORM_TURN_KINDS, APPROVAL_RESULT_KIND)
# 子任务的会话键：`<主会话键>#job:<编号>`。
JOB_SEPARATOR = "#job:"


def is_platform(task: Task) -> bool:
    """这个任务是不是平台自己发起的一轮：没有入站事件，类型在名单里。"""
    return task.inbound_event_id is None and task.kind in ORIGIN_KINDS


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
    return await tasks.enqueue(
        session,
        NewTask(
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
        ),
    )
