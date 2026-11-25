"""Clicks on a batch-write confirmation card, and the background tasks that run the batch.

Only the asker of the turn that staged the batch counts, in the chat the card was sent to. On
confirm the operation is checked again against today's grants and catalog; if it no longer passes
nothing runs. Then run tasks (`batch.RUN_KIND`) send the items one by one as the asker, carrying on
after a failed item, and the conversation continues with the outcome. The asker can stop it from
the progress card. A business system's group delivery staged the batch as its service account:
any group member allowed to use the bot may confirm it, and it then runs as them (they become the
asker for stopping and continuing).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.api_tasks import writes
from coreman.core.bus import tasks
from coreman.core.db.models import Bot, InboundEvent, InteractionState, Task, User
from coreman.core.systems_catalog import batch, confirm
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.systems_write_cards import _answerer, _clicker, _context, _update, taken


async def handle(
    session: AsyncSession,
    ctx: TaskContext,
    bot: Bot,
    inbound: InboundEvent,
    action: dict[str, Any],
) -> str:
    """The caller proved the card was sent by us to this chat. Staged values stay out of logs."""
    verb, _, raw_id = str(action.get("level") or "").partition(":")
    if verb not in (
        "confirm",
        "cancel",
        confirm.RESEND,
        batch.STOP,
        batch.GO,
        batch.CONTINUE,
        batch.RETRY,
    ):
        return "ignored"
    try:
        origin = await session.get(Task, int(str(action["task_id"]).split(":")[1]))
        state_id = uuid.UUID(raw_id)
    except (KeyError, ValueError, IndexError):
        return "ignored"
    if (
        origin is None
        or origin.bot_id != bot.id
        or ctx.task.kind != "card_action"
        or ctx.task.status not in tasks.ACTIVE
        or ctx.task.cancel_requested_at
        or inbound.platform != "feishu"
        or inbound.kind != "card_action"
        or inbound.bot_id != bot.id
    ):
        return "ignored"
    state = await session.scalar(
        select(InteractionState)
        .where(
            InteractionState.id == state_id,
            InteractionState.kind == batch.KIND,
            InteractionState.bot_id == bot.id,
        )
        .with_for_update()
    )
    staged = state.state if state else {}
    if state is None or staged.get("origin_task_id") != origin.id or not staged.get("sent"):
        return "ignored"
    clicked = await _clicker(session, origin, inbound, str(staged.get("chat_type")), ctx.cipher)
    group = await writes.group_origin(session, origin)
    # 群承接模式：确认前群里有权限的人都能答；确认后这一批按确认人执行，停止、继续只认他。
    who = _answerer(staged, state.status, clicked, group, anyone_while=("open", "expired"))
    if who is None:
        await taken(session, bot, inbound, state, clicked, group)
        return "ignored"
    now = utcnow()
    where: dict[str, Any] = {
        "dedupe_key": f"{inbound.id}:card_update",
        "chat_id": inbound.chat_id,
        "message_id": inbound.reply_context.get("message_id"),
    }
    if verb == batch.STOP:
        if state.status != "submitted" or staged.get("finished_at") or staged.get(batch.STOP):
            return "ignored"
        state.state = {**staged, batch.STOP: True}
        if staged.get("paused"):
            # 暂停着没有执行任务在跑：当场收尾，剩余各项记为未执行。
            await session.flush()
            note = await batch.finish(
                session, ctx.cipher, state, None, chunk=int(staged.get("chunk") or 0)
            )
            if note is not None:
                await batch.resume(session, origin, state, note)
            return "systems_batch_stopped"
        await confirm.update_card(
            session,
            state,
            batch.progress_card(state, await batch.counts(session, state.id)),
            **where,
        )
        return "systems_batch_stopping"
    if verb in (batch.GO, batch.CONTINUE, batch.RETRY):
        # 暂停后继续、结束后继续剩余项或重试失败项：这里只改状态并排执行任务，权限与操作
        # 定义由执行任务在每段开头复查，不通过就停下并说明原因。
        if verb == batch.GO:
            queued = await batch.go(session, state)
        else:
            queued = bool(await batch.requeue(session, state, verb, now))
        if not queued:
            return "ignored"
        await confirm.update_card(
            session,
            state,
            batch.progress_card(state, await batch.counts(session, state.id)),
            **where,
        )
        return "systems_batch_resumed"
    if verb == confirm.RESEND:
        # 过期卡片上的「重新发起」：同一份暂存内容发一张新卡（新的明细链接），确认时照样复查。
        if not confirm.lapsed(state, now):
            return "ignored"
        body = await batch.card(session, ctx.cipher, state)
        if body is None:
            # 明细文件没能生成：不发只有抽样的卡，旧卡保持原样，稍后可以再点。
            return "systems_batch_unavailable"
        if await confirm.resend(session, state, body, now, **where):
            return "systems_batch_resent"
        await confirm.expire(session, state, now, **where)
        return "expired"
    if state.status != "open":
        return "expired"
    if state.expires_at is not None and state.expires_at <= now:
        await confirm.expire(session, state, now, **where)
        return "expired"
    if verb == "cancel":
        state.status = "cancelled"
        await _update(session, bot, inbound, "已取消，没有执行。")
        await batch.resume(session, origin, state, "用户取消了这批写操作，都没有执行。不要重试。")
        return "systems_batch_cancelled"
    # 先落库再复查：复查会提交事务、放掉行锁，状态必须先离开「待确认」，之后的取消、过期、
    # 再点确认才都不会生效。进程在这之后没了的话，这一批不会执行，保留期满由清理任务删掉。
    state.status = "submitted"
    state.expires_at = now + confirm.KEEP
    if str(who[0]) != staged.get("user_id"):
        # 群承接模式由群里的人确认：之后各段都按确认人的身份执行。
        state.state = {**staged, "user_id": str(who[0])}
    await session.commit()
    user = await session.get(User, who[0])
    pctx = await _context(session, ctx, bot, origin, user) if user is not None else None
    reason = "无法以你的身份调用业务系统"
    if pctx is not None:
        try:
            await batch.checked(pctx, state)
            reason = ""
        except batch.Halt as exc:
            reason = exc.reason
    locked = await session.get(
        InteractionState, state_id, with_for_update=True, populate_existing=True
    )
    if locked is None or locked.status != "submitted" or "chunk" in locked.state:
        return "expired"
    if reason:
        await _update(session, bot, inbound, f"已不能执行（{reason}），整批都没有执行。")
        await batch.resume(
            session,
            origin,
            locked,
            f"确认时这批写操作已不能执行：{reason}。整批都没有执行，不要自行重试。",
        )
        return "systems_batch_refused"
    await batch.start(session, locked)
    await confirm.update_card(
        session,
        locked,
        batch.progress_card(locked, await batch.counts(session, locked.id)),
        **where,
    )
    return "systems_batch_started"


class SystemsBatchRunHandler:
    """One stretch of a confirmed batch; queues the next until the batch is done."""

    kind = batch.RUN_KIND

    async def run(self, ctx: TaskContext) -> None:
        try:
            batch_id = uuid.UUID(str(ctx.task.payload.get("batch_id")))
            chunk = int(str(ctx.task.payload.get("chunk")))
        except (TypeError, ValueError):
            await self._finish(ctx, "failed", "batch_missing")
            return
        async with ctx.session_factory() as session:
            state = await session.get(InteractionState, batch_id)
            # 已经收尾，或这一段被判失联后另排了一段接手：什么都不做。
            if state is None or state.kind != batch.KIND or not batch._current(state, chunk):
                await tasks.finish(session, ctx.task.id, status="succeeded", only_active=True)
                await session.commit()
                ctx.log.info("systems_batch_chunk", outcome=batch.SUPERSEDED)
                return
            meta = state.state
            origin = await session.get(Task, int(meta["origin_task_id"]))
            bot = await session.get(Bot, state.bot_id)
            user = await session.get(User, uuid.UUID(str(meta["user_id"])))
            halted: str | None = None
            crashed: batch.ItemCrashed | None = None
            pctx = None
            try:
                # 一批可能跑很久：每段都看提问人和 AI 员工是否仍然有效。
                if bot is None or not bot.enabled:
                    raise batch.Halt("AI 员工已停用")
                if user is None or user.status != "active":
                    raise batch.Halt("提问人的账号已停用")
                if origin is not None:
                    pctx = await _context(session, ctx, bot, origin, user)
                if pctx is None:
                    raise batch.Halt("无法以提问人的身份调用业务系统")
                outcome = await batch.run_chunk(
                    pctx,
                    ctx.cipher,
                    state,
                    chunk=chunk,
                    clock=ctx.clock,
                    cancelled=ctx.cancel_event.is_set,
                )
            except batch.Halt as exc:
                await session.rollback()
                halted = exc.reason
                outcome = batch.DONE
            except batch.ItemCrashed as exc:
                await session.rollback()
                crashed = exc
                outcome = batch.MORE
            if halted is not None or crashed is not None:
                # 回滚让已加载的行都过期：重新取，后面要用。
                state = await session.get(InteractionState, batch_id, populate_existing=True)
                origin = await session.get(
                    Task, int(meta["origin_task_id"]), populate_existing=True
                )
                assert state is not None
            if crashed is not None:
                await batch.mark_unknown(session, ctx.cipher, batch_id, crashed.seq, crashed.kind)
            if outcome == batch.CANCELLED and ctx.cancel_reason != "reaper":
                # 管理台停掉了执行任务：这一批到此为止，剩下的记为未执行并告诉提问人。
                halted = "执行被管理员停止"
                outcome = batch.DONE
            if outcome == batch.WAIT:
                # 上一段有一项可能还在途：过了宽限再来，那时仍没有结果才记为结果未知。
                await tasks.defer(session, ctx.task.id, seconds=batch.SEND_GRACE + 5)
                await session.commit()
                ctx.log.info("systems_batch_chunk", outcome=outcome)
                return
            if outcome in (batch.PAUSE_TRIAL, batch.PAUSE_FAILURES):
                await batch.pause(session, ctx.cipher, state, outcome, chunk=chunk)
            elif outcome == batch.DONE:
                note = await batch.finish(session, ctx.cipher, state, halted, chunk=chunk)
                if note is not None and origin is not None:
                    await batch.resume(session, origin, state, note)
            elif outcome == batch.MORE:
                await batch.next_chunk(session, state, chunk=chunk)
            # 被判失联（CANCELLED 且原因是 reaper）或已被接手：什么都不收尾，巡检会另排一段。
            await tasks.finish(session, ctx.task.id, status="succeeded", only_active=True)
            await session.commit()
        ctx.log.info("systems_batch_chunk", outcome=outcome, halted=bool(halted))

    async def _finish(self, ctx: TaskContext, status: str, code: str | None) -> None:
        async with ctx.session_factory() as session:
            await tasks.finish(
                session, ctx.task.id, status=status, error_code=code, only_active=True
            )
            await session.commit()
