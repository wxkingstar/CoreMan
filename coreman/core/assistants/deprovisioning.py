"""停用个人助理：停止盯消息 → 停下在跑的轮次 → 撤回待批的审批单 → 撤销飞书授权 → 销毁数据密钥并
停用记录，工作目录交给 `retired_dirs` 删掉。

三个入口，都只调 `request`：本人在私聊里确认停用（worker 的卡片回调）、成员离开（看护进程发现账号
停用，先宽限 24 小时，期间恢复就作罢）、管理员在助理列表里停用（只回状态，不回任何内容）。另外，
开通半途放弃时也走这里收拾。

`request` 一提交，记录就是 deprovisioning：`runtime.turn_blocker` 挡住一切轮次，之后各步由看护进程
推进，每步一个事务，失败原地重试、永不放弃。密钥最后才销毁：撤回审批单要它所在的记录、撤销授权要
记录上的应用凭证，都不需要数据密钥，但「待批的单先撤回」必须在销毁之前完成。
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import approvals, keys, lifecycle, retired_dirs, watch
from coreman.core.assistants.kinds import APPROVAL_EXECUTE_KIND
from coreman.core.assistants.lifecycle import StepFailed, advance, finish
from coreman.core.assistants.records import is_assistant
from coreman.core.bots.events import notify_bot_changed
from coreman.core.bus import tasks
from coreman.core.crypto import Cipher
from coreman.core.db.models import AssistantLifecycle, Bot, Task, User, UserIdentity
from coreman.core.feishu_personal import service as personal
from coreman.core.timeutils import utcnow

# 成员离开（账号被停用）后等这么久才真的停用：通讯录同步抖一下、管理员误停，都不至于销毁密钥。
GRACE = timedelta(hours=24)
GRACE_CHECK = timedelta(minutes=10)
# 等在跑的轮次停下最多这么久；之后不等了（它们写不进任何东西：记录已不是 active）。
DRAIN_WAIT = timedelta(minutes=15)
DRAIN_CHECK = timedelta(seconds=10)
REASON = "assistant_deprovisioned"
DONE_TEXT = {
    "owner": (
        "个人助理已停用：飞书授权已撤销，待批的审批单已撤回，加密保存的个人内容已不可恢复。"
        "想再用的时候给我发条消息，可以重新开通。"
    ),
    "admin": (
        "管理员停用了你的个人助理：飞书授权已撤销，待批的审批单已撤回，加密保存的个人内容已不可恢复。"
    ),
}


async def _feishu_user(session: AsyncSession, owner_id: uuid.UUID) -> str:
    found = await session.scalar(
        select(UserIdentity.platform_user_id)
        .where(UserIdentity.user_id == owner_id, UserIdentity.platform == "feishu")
        .limit(1)
    )
    return found or ""


async def request(
    session: AsyncSession, bot: Bot, *, trigger: str, actor_id: uuid.UUID | None
) -> AssistantLifecycle | None:
    """开始停用这条助理记录，返回进行中的停用流程；已经停用完的返回 None。调用方提交。

    重复请求不重复做；正在开通的那条流程就此取消。

    Raises:
        ValueError: 不是个人助理的记录
    """
    if not is_assistant(bot) or bot.owner_id is None or bot.platform_app_id is None:
        raise ValueError("not an assistant record")
    # 加锁顺序与看护进程相同：先流程行、后记录。还没有流程行时，同一个人的并发请求在这把事务锁上
    # 排队（看护进程不拿它），排到之后再看一次流程行。
    row = await lifecycle.live(session, bot.owner_id)
    if row is None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"assistant-lifecycle:{bot.owner_id}"},
        )
        row = await lifecycle.live(session, bot.owner_id)
    await session.refresh(bot, with_for_update=True)
    if bot.assistant_state == "deprovisioned":
        # 先看这条记录本身：早已停用完的旧记录不去碰本人别的流程。
        return None
    if row is not None and row.action == "deprovision":
        if row.bot_id != bot.id:
            # 进行中的停用属于本人的另一条记录：那条流程不由这次请求改动。
            return None
        if row.step == "grace" and trigger != "member_left":
            # 宽限期里本人或管理员明确要停用：不再等账号恢复，现在就往下走。
            row.trigger, row.actor_id, row.deadline = trigger, actor_id, None
            advance(row, "drain")
            await session.flush()
        return row
    if row is not None:
        finish(row, "cancelled", "deprovision_requested")
        await session.flush()
    now = utcnow()
    grace = trigger == "member_left"
    new = AssistantLifecycle(
        owner_id=bot.owner_id,
        platform_app_id=bot.platform_app_id,
        bot_id=bot.id,
        action="deprovision",
        step="grace" if grace else "drain",
        trigger=trigger,
        actor_id=actor_id,
        platform_user_id=(row.platform_user_id if row is not None else "")
        or await _feishu_user(session, bot.owner_id),
        deadline=now + GRACE if grace else None,
        next_attempt_at=now + GRACE_CHECK if grace else now,
    )
    bot.assistant_state = "deprovisioning"
    session.add(new)
    await session.flush()
    return new


async def _bot(session: AsyncSession, row: AssistantLifecycle) -> Bot:
    bot = await session.get(Bot, row.bot_id, with_for_update=True) if row.bot_id else None
    if bot is None:
        # 记录被删掉了（管理员删除停用后的记录也只发生在 deprovisioned 之后）：剩下的只有密钥。
        raise StepFailed("bot_missing")
    return bot


async def step_grace(
    session: AsyncSession, cipher: Cipher, row: AssistantLifecycle, messenger: lifecycle.Messenger
) -> list[str]:
    """成员离开后的宽限：账号恢复就作罢（助理照常可用），到期还是停用的才往下走。"""
    bot = await _bot(session, row)
    owner = await session.get(User, row.owner_id)
    now = utcnow()
    if owner is not None and owner.status == "active":
        bot.assistant_state = "active"
        finish(row, "cancelled", "member_returned")
        return []
    if row.deadline is not None and row.deadline > now:
        row.next_attempt_at, row.updated_at = min(row.deadline, now + GRACE_CHECK), now
        return []
    # 宽限期的截止时刻已过，不能留给 drain 当成它自己的等待期限。
    row.deadline = None
    advance(row, "drain")
    return []


async def step_drain(
    session: AsyncSession, cipher: Cipher, row: AssistantLifecycle, messenger: lifecycle.Messenger
) -> list[str]:
    """叫停这条记录没结束的任务（已批准的写操作照常执行完），等在跑的停下，最多等 `DRAIN_WAIT`。

    已批准的写操作排着队也算在跑：它不叫停，要等它执行完，否则授权撤销、密钥销毁之后它只会失败。
    """
    bot = await _bot(session, row)
    # 先停止盯消息、作废没交回的条目，再叫停任务：之后不会再有新的条目进来。
    await watch.stop(session, bot.id)
    now = utcnow()
    if row.deadline is None:
        row.deadline = now + DRAIN_WAIT
    found = (
        await session.execute(
            select(Task.id, Task.kind, Task.status).where(
                Task.bot_id == bot.id, Task.status.in_(tasks.OPEN)
            )
        )
    ).all()
    for task_id, kind, _status in found:
        if kind != APPROVAL_EXECUTE_KIND:
            await tasks.request_cancel(session, task_id, REASON)
    running = [
        task_id
        for task_id, kind, status in found
        if status in tasks.ACTIVE or kind == APPROVAL_EXECUTE_KIND
    ]
    if running and row.deadline > now:
        row.next_attempt_at, row.updated_at = now + DRAIN_CHECK, now
        return []
    advance(row, "withdraw")
    return []


async def step_withdraw(
    session: AsyncSession, cipher: Cipher, row: AssistantLifecycle, messenger: lifecycle.Messenger
) -> list[str]:
    bot = await _bot(session, row)
    await approvals.withdraw_pending(session, bot.id, REASON)
    advance(row, "revoke")
    return []


async def step_revoke(
    session: AsyncSession, cipher: Cipher, row: AssistantLifecycle, messenger: lifecycle.Messenger
) -> list[str]:
    """撤销飞书个人授权：库里的令牌当场清空（之后谁也拿不到）；飞书那边的撤销尽力而为。"""
    bot = await _bot(session, row)
    await personal.revoke_grant(session, bot.id, row.owner_id, cipher)
    advance(row, "destroy")
    return []


def retired_key(bot: Bot) -> str:
    """停用后的 bot_key：腾出原来的名字给以后重新开通的记录。"""
    suffix = f"-x{bot.id.hex[:8]}"
    return bot.bot_key if bot.bot_key.endswith(suffix) else bot.bot_key + suffix


async def step_destroy(
    session: AsyncSession, cipher: Cipher, row: AssistantLifecycle, messenger: lifecycle.Messenger
) -> list[str]:
    """最后一步，一个事务：再撤一次期间冒出来的待批单与授权，销毁数据密钥，登记删目录，停用记录。"""
    bot = await _bot(session, row)
    await approvals.withdraw_pending(session, bot.id, REASON)
    # 撤销之后旧的授权卡片可能又发起了一次授权：在同一个事务里再清一次（幂等）。
    await personal.revoke_grant(session, bot.id, row.owner_id, cipher)
    await keys.destroy_key(session, row.owner_id)
    if bot.relay_server_id is not None:
        await retired_dirs.retire(
            session, bot_id=bot.id, relay_id=bot.relay_server_id, working_dir=bot.working_dir
        )
    bot.assistant_state, bot.enabled, bot.bot_key = "deprovisioned", False, retired_key(bot)
    await notify_bot_changed(session, bot.id)
    finish(row, "done")
    text = DONE_TEXT.get(row.trigger or "")
    return [text] if text else []


STEPS = {
    "grace": step_grace,
    "drain": step_drain,
    "withdraw": step_withdraw,
    "revoke": step_revoke,
    "destroy": step_destroy,
}
