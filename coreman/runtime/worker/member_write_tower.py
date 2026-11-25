"""A member write the member approved in Tower (`member_tower`): run it once, as staged.

Like the personal assistant's approved writes: the write is checked again (member, OAuth grant,
system, operation definition and visibility), then a one-time token is fetched and redeemed
against the staged request; only a redeemed token for this very approval lets it run. The outcome
is kept for `systems_call_result` and reported to Tower.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants.approvals import tower
from coreman.core.bus import tasks
from coreman.core.db.models import InteractionState
from coreman.core.logging import get_logger
from coreman.core.systems_catalog import member_tower
from coreman.core.timeutils import utcnow
from coreman.runtime.worker import member_write_cards
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.member_write_cards import Done

log = get_logger(__name__)
# Tower unreachable while fetching or redeeming the token: try again this often, this many times.
RETRY_SECONDS = 60.0
MAX_ATTEMPTS = 5


class _Retry(Exception):
    """Tower could not be reached; nothing ran and the approval still stands."""


def _failed(code: str, message: str) -> Done:
    return Done(f"member_write_{code}", {"status": "failed", "message": message}, "")


async def _report(cfg: tower.Config | None, approval: str | None, done: Done) -> None:
    if cfg is None or not approval:
        return
    ok = done.status == "ok"
    verdict, code = ("executed", None) if ok else ("failed", done.status)
    try:
        await tower.report(cfg, approval, verdict, code)
    except tower.TowerError as exc:
        log.warning("member_write_tower_report_failed", approval=approval, code=exc.code)


class MemberWriteExecuteHandler:
    kind = member_tower.EXECUTE_KIND

    async def run(self, ctx: TaskContext) -> None:
        cfg = member_tower.config()
        try:
            state_id = uuid.UUID(str(ctx.task.payload.get("state_id")))
        except ValueError:
            await self._finish(ctx, "ignored")
            return
        try:
            code = await self._run(ctx, cfg, state_id)
        except _Retry:
            if ctx.task.attempts < MAX_ATTEMPTS:
                async with ctx.session_factory() as session:
                    await tasks.defer(session, ctx.task.id, seconds=RETRY_SECONDS)
                    await session.commit()
                return
            code = await self._give_up(ctx, cfg, state_id)
        ctx.log.info("member_write_tower_executed", result=code)
        await self._finish(ctx, code)

    async def _run(self, ctx: TaskContext, cfg: tower.Config | None, state_id: uuid.UUID) -> str:
        async with ctx.session_factory() as session:
            state = await session.scalar(
                select(InteractionState)
                .where(InteractionState.id == state_id, InteractionState.kind == member_tower.KIND)
                .with_for_update()
            )
            if state is None or not member_tower.runnable(state):
                return "ignored"
            approval = str(state.state.get("approval_id") or "")
            done = await self._execute(session, ctx, cfg, state, approval)
            state.status = "submitted"
            state.state = {**state.state, "result": done.outcome}
            await session.commit()
        await _report(cfg, approval, done)
        return done.code

    async def _execute(
        self,
        session: AsyncSession,
        ctx: TaskContext,
        cfg: tower.Config | None,
        state: InteractionState,
        approval: str,
    ) -> Done:
        if cfg is None:
            return _failed("unavailable", "平台的审批配置已经移除，没有执行。")
        checked = await member_write_cards.check(session, ctx, state)
        if isinstance(checked, Done):
            return checked
        try:
            redeemed = await tower.redeem(
                cfg,
                await tower.token(cfg, approval),
                member_tower.request_hash(cfg, state, checked.item),
                str(state.state["caller"]),
            )
        except tower.TowerError as exc:
            if exc.unavailable:
                raise _Retry from None
            # Token used, expired or not for this request: this approval is void.
            return _failed("refused", f"Tower 核验没有通过（{exc.code}），没有执行。")
        if redeemed != approval:
            return _failed("refused", "Tower 核验的审批单对不上，没有执行。")
        # 先落库再调业务系统：中途崩溃、任务重试都不会再执行一遍。
        state.status = "submitted"
        state.state = {**state.state, "sending_at": utcnow().isoformat()}
        await session.commit()
        return await member_write_cards.send(session, checked, refresh=(state,))

    async def _give_up(
        self, ctx: TaskContext, cfg: tower.Config | None, state_id: uuid.UUID
    ) -> str:
        """Tower stayed unreachable: nothing ran; record that, so the agent can say so."""
        done = _failed("tower_unavailable", "审批系统一直连不上，没有执行。可以重新提交。")
        async with ctx.session_factory() as session:
            state = await session.get(InteractionState, state_id, with_for_update=True)
            if state is None or not member_tower.runnable(state):
                return "ignored"
            approval = str(state.state.get("approval_id") or "")
            state.status = "submitted"
            state.state = {**state.state, "result": done.outcome}
            await session.commit()
        await _report(cfg, approval, done)
        return done.code

    async def _finish(self, ctx: TaskContext, code: str) -> None:
        async with ctx.session_factory() as session:
            await tasks.finish(
                session,
                ctx.task.id,
                status="succeeded",
                result={"member_write": code},
                bot_id=ctx.task.bot_id,
                only_active=True,
            )
            await session.commit()
