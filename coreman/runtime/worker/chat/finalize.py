"""收尾阶段：抢终态、额度后处理、提问卡片、终稿投递与 chat_logs。"""

from __future__ import annotations

import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from coreman.core.bots.permissions import can_switch_relay
from coreman.core.bus import outbox, streams, tasks
from coreman.core.chat import chat_logs, interactions
from coreman.core.chat.rate_limit import (
    is_rate_limited,
    pick_idle_server,
    quota_table,
    quota_warning,
)
from coreman.core.db.models import (
    BotMember,
    RelayServer,
    Task,
    User,
)
from coreman.core.i18n.messages import msg
from coreman.core.wecom.cards import (
    choice_card,
    make_choice_prefix,
    make_ratelimit_prefix,
    question_task_id,
    switch_offer_card,
)
from coreman.runtime.worker.chat.base import ChatStageBase
from coreman.runtime.worker.chat.models import Outcome, Prepared, Verdict
from coreman.runtime.worker.chat.records import log_entry
from coreman.runtime.worker.context import TaskContext


class FinalizeStage(ChatStageBase):
    """收尾：抢到终态之后才推送、开卡片、落日志。"""

    def _with_note(self, ctx: TaskContext, text: str, note: str) -> str:
        """把额度表 / 预警接在终稿后面，但让「✅ 任务已完成」继续压尾。

        `_classify` 已经把 ✅ 拼进终稿了，直接追加会变成「✅ 之后还有一大张表」，读起来像
        任务完成之后又出了什么事；剥掉再拼回去，顺序保持「表/预警 → ✅」。
        """
        done = msg("done_suffix", ctx.locale)
        if text.endswith(done):
            return text[: -len(done)] + note + done
        return text + note

    async def _postprocess_rate_limit(
        self, ctx: TaskContext, pre: Prepared, verdict: Verdict
    ) -> tuple[Verdict, dict[str, Any] | None]:
        """额度后处理：触限附额度表、未触限附预警、管理员再推一张切换卡。只在有终稿的收尾做。

        取消 / 超时那几路不进来：用户根本没拿到模型的回答，一张额度表帮不上任何忙。
        """
        if verdict.log_status not in ("success", "error") or not verdict.final_text:
            return verdict, None
        now = datetime.now(UTC)
        limited = is_rate_limited(verdict.final_text)
        async with ctx.session_factory() as session:
            relays = list(
                (
                    await session.execute(
                        select(RelayServer)
                        .where(RelayServer.is_active.is_(True))
                        .order_by(RelayServer.name)
                    )
                ).scalars()
            )
            current = next((r for r in relays if r.id == pre.relay.id), None)
            if current is None:
                # 这一轮跑完的工夫实例被下线了：额度数据已经无从谈起，终稿原样交付。
                return verdict, None
            note = (
                quota_table(relays, current_id=current.id, now=now, locale=ctx.locale)
                if limited
                else quota_warning(current, now, ctx.locale)
            )
            if note:
                verdict = replace(
                    verdict, final_text=self._with_note(ctx, verdict.final_text, note)
                )
            if not limited or pre.intake.speaker.user_id is None:
                return verdict, None
            user = await session.get(User, pre.intake.speaker.user_id)
            member_ids = set(
                (
                    await session.execute(
                        select(BotMember.user_id).where(BotMember.bot_id == pre.intake.bot.id)
                    )
                )
                .scalars()
                .all()
            )
            # 切换是写操作：认不出人、或者这个人本来就没权限，就只给表不给卡。
            if user is None or not can_switch_relay(user, pre.intake.bot, member_ids):
                return verdict, None
            creator = await session.get(User, pre.intake.bot.created_by)
            target = pick_idle_server(
                relays,
                current=current,
                bot=pre.intake.bot,
                user=user,
                creator_team_id=creator.team_id if creator else None,
            )
            if target is None:
                return verdict, None
            prefix = make_ratelimit_prefix(
                pre.intake.bot.bot_key, pre.intake.speaker.platform_user_id, now=time.time()
            )
            pct = (
                float(target.rate_limit_7d_used_pct)
                if target.rate_limit_7d_used_pct is not None
                else None
            )
            state = {
                "idx": 0,
                "platform_user_id": pre.intake.speaker.platform_user_id,
                "user_id": str(user.id),
                "current_relay_id": str(current.id),
                "current_name": current.name,
                "target_relay_id": str(target.id),
                "target_name": target.name,
                "target_pct_7d": pct,
                "chat_id": pre.intake.chat_id,
                "chat_type": pre.intake.chat_type,
            }
            await interactions.open_state(
                session,
                bot_id=pre.intake.bot.id,
                kind=interactions.KIND_RELAY_SWITCH,
                scope_key=interactions.choice_scope(
                    pre.intake.bot.id, pre.intake.speaker.platform_user_id
                ),
                state=state,
                task_id_prefix=prefix,
                # 30 分钟后作废：额度数据早就换过几轮，那时候再按这张卡切是照着陈数据切。
                expires_at=now + timedelta(minutes=30),
            )
            await session.commit()
        ctx.log.info("relay_switch_offered", target=target.name, prefix=prefix)
        return verdict, switch_offer_card(
            task_id=question_task_id(prefix, 0),
            current_name=current.name,
            target_name=target.name,
            pct_text=f"{pct:.0f}%" if pct is not None else msg("rl_pct_unknown", ctx.locale),
            icon_url=await self._icon_url(ctx),
            locale=ctx.locale,
        )

    async def _finalize(self, ctx: TaskContext, pre: Prepared, out: Outcome) -> None:
        intake = pre.intake
        verdict = self._classify(ctx, pre, out)
        pre.writer.thinking.add_end(msg("thinking_end", ctx.locale))
        supervisor = pre.supervisor
        elapsed = int(ctx.clock() - pre.started_clock)
        # 收尾的第一件事是抢终态：守卫没过说明 reaper 已经替这个任务写过终态、也已经告诉过
        # 用户「异常中断」。那时候再推送、再改写终稿、再补一条 chat_log，用户会收到两份自相
        # 矛盾的回答，管理台也会多一条假成功记录——所以就此收手，一个字都不再落。
        async with ctx.session_factory() as session:
            # Same lock order as timeout/stop: collaboration first, then its task.
            # Otherwise a simultaneous B completion and human stop can deadlock.
            if intake.bot.platform == "feishu":
                from coreman.core.db.models import BotCollaboration

                cid = ctx.task.payload.get("collaboration_id")
                condition = (
                    BotCollaboration.id == uuid.UUID(cid)
                    if cid
                    else BotCollaboration.source_task_id == ctx.task.id
                )
                await session.scalar(select(BotCollaboration).where(condition).with_for_update())
                from coreman.core.db.models import HumanCollaboration

                hid = ctx.task.payload.get("human_collaboration_id")
                await session.scalar(
                    select(HumanCollaboration)
                    .where(
                        HumanCollaboration.id == uuid.UUID(hid)
                        if hid
                        else HumanCollaboration.source_task_id == ctx.task.id
                    )
                    .with_for_update()
                )
            # Cancellation wins even when it lands between the last SSE frame and finalization.
            current = await session.get(Task, ctx.task.id, with_for_update=True)
            if current and current.cancel_requested_at:
                verdict = self._classify(
                    ctx, pre, replace(out, cancelled=True, reason=current.cancel_reason)
                )
            elif current and current.payload.get("collaboration_handoff") and not out.cancelled:
                # Registration is durable; EOF before the next poll must not undo accepted help.
                verdict = self._classify(ctx, pre, replace(out, collaboration_handoff=True))
            owned = await tasks.finish(
                session,
                ctx.task.id,
                status=verdict.task_status,
                error_code=verdict.error_code,
                error_message=verdict.error_message,
                only_active=True,
            )
            collaboration_silent = False
            if owned and intake.bot.platform == "feishu":
                from coreman.runtime.worker.chat.collaboration import final_transition

                verdict, collaboration_silent = await final_transition(session, ctx, pre, verdict)
                from coreman.runtime.worker.chat import human_collaboration

                verdict = await human_collaboration.final_transition(session, ctx, pre, verdict)
            entry = (
                log_entry(
                    ctx,
                    intake,
                    status=verdict.log_status,
                    relay_session_id=pre.info.relay_session_id,
                    response_content=verdict.final_text,
                    tools_used=out.tools,
                )
                if collaboration_silent
                else log_entry(
                    ctx,
                    intake,
                    status=verdict.log_status,
                    relay_session_id=pre.info.relay_session_id,
                    response_content=verdict.final_text,
                    tools_used=out.tools,
                    error_code=verdict.error_code,
                    error_message=verdict.error_message,
                    usage=out.usage,
                    content=pre.content,
                )
            )
            # 记录与任务同一事务结束：不会出现任务结束了、记录还停在进行中。
            logged = owned and await chat_logs.finish_turn(session, entry)
            await session.commit()
        if not owned:
            ctx.log.warning("task_already_finalized", status=verdict.task_status, elapsed_s=elapsed)
            return
        if collaboration_silent:
            await pre.writer.complete(verdict.final_text)
            if not logged:
                ctx.chat_logs.submit_finish(entry)
            return
        logged_text = verdict.final_text
        verdict, offer = await self._postprocess_rate_limit(ctx, pre, verdict)
        # 待答状态与卡片排在抢到终态之后：被 reaper 收过尾的那一轮已经告诉用户「异常中断」，
        # 再开一轮提问就是让用户对着一张没人接的卡片作答。
        card = (
            await self._open_choice(ctx, pre, out.questions)
            if verdict.log_status == "ask_user" and out.questions
            else None
        )
        # 触限时模型不会同时提问，两张卡不会真的打架；真撞上了以切换卡为准——额度没了，
        # 那一轮问答无论怎么答都跑不起来。
        card = offer or card
        if out.reason == "hard_ttl":
            # 硬 TTL 是「被掐掉」，不是完成：只发终止通知，不发 ✅ 完成推送。
            await supervisor.on_expired()
            await pre.writer.complete(verdict.final_text, pending_card=card)
        elif supervisor.switched:
            # 只有真成功才配 ✅：出错 / 超时 / 被停都走不冠完成前缀的那条路。
            await supervisor.on_finish(
                verdict.final_text,
                success=verdict.log_status == "success",
                waiting=verdict.log_status == "ask_user",
            )
            await pre.writer.complete(verdict.final_text, pending_card=card)
            # 网关早就不跟这条流了，它那条「finish 之后发卡片」的路不会再走：卡片得自己送。
            await self._queue_card(ctx, pre, card)
        else:
            # 先 complete 再看它带回来的投递状态：这样「网关翻 proactive」与「worker 收尾」
            # 无论谁先，终稿都只由后手那一边负责送达，不会两边都以为对方会送。
            done = await pre.writer.complete(verdict.final_text, pending_card=card)
            await self._push_if_proactive(ctx, pre, verdict, done, card)
            await self._notify_long_task(ctx, pre, verdict, done, elapsed)
        # 额度表 / 预警是抢到终态之后才接上的，记录里的回复要补成用户实际收到的这一份。
        if not logged:
            # 同事务里没写成（已记告警）：交给写入端事后补写；中途出错没走到这里的，由巡检结掉。
            ctx.chat_logs.submit_finish(
                replace(entry, response_content=ctx.redact(verdict.final_text))
            )
        elif verdict.final_text != logged_text:
            ctx.chat_logs.submit_amend(ctx.task.id, ctx.redact(verdict.final_text))
        ctx.log.info(
            "task_finished",
            status=verdict.log_status,
            elapsed_s=elapsed,
            events=out.counted,
            tools=len(out.tools),
        )
        await self._after_finalize(ctx, pre, verdict)

    async def _notify_long_task(
        self,
        ctx: TaskContext,
        pre: Prepared,
        verdict: Verdict,
        done: streams.Completion,
        elapsed: int,
    ) -> None:
        """耗时 ≥LONG_TASK_SECONDS 且正常完成时，另发一条完成提醒。

        流式气泡是原地刷新，企微 / 飞书都不会因此弹新消息通知，用户切走之后不知道已经
        完成。只有终稿仍由网关按流收尾（`delivery_mode=stream`）才需要：主动推送与后台切换
        那几路的终稿本身就是一条新消息，已经会提醒；提问轮的卡片同理，出错 / 被停不算完成。
        群聊与私聊一样直接发到会话里，不 @ 发言者。
        """
        if (
            verdict.log_status != "success"
            or done.delivery_mode != "stream"
            or elapsed < self.LONG_TASK_SECONDS
        ):
            return
        delay = timedelta(seconds=self.LONG_TASK_NOTICE_DELAY_SECONDS)
        async with ctx.session_factory() as session:
            await outbox.add(
                session,
                bot_id=pre.intake.bot.id,
                platform=pre.intake.bot.platform,
                kind="send",
                dedupe_key=f"{ctx.task.id}:send:long_done",
                target={"chat_id": pre.intake.chat_id},
                payload={"markdown": msg("long_task_done", ctx.locale, seconds=elapsed)},
                not_before=datetime.now(UTC) + delay,
            )
            await session.commit()
        ctx.log.info("long_task_notice_queued", elapsed_s=elapsed)

    async def _after_finalize(self, ctx: TaskContext, pre: Prepared, verdict: Verdict) -> None:
        """收尾之后的挂钩（清理本轮独有的状态）。只在这一轮真的抢到终态时才会走到。"""

    @staticmethod
    async def _icon_url(ctx: TaskContext) -> str:
        """卡片左上角的小图标；没配就整个字段不发（见 cards._source）。"""
        return str(await ctx.settings_store.get("card_icon_url", default="") or "")

    async def _open_choice(
        self, ctx: TaskContext, pre: Prepared, questions: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """写 interaction_states(choice) 并渲染首题卡片；同一发言者的旧状态被覆盖。

        `context` 是提交轮重建这次请求要的全部东西（relay 会话、模型、工作目录、提示词）：
        答完题再回到模型时，原来那个任务早就结束了，只有这份快照能把上下文接回去。里面
        一个密钥都不能有——env 与凭据留在 bots 表里，提交轮自己去解。提示词也不存：
        提交轮本来就按本轮重新构建身份与授权（见 choice_submit），而存下来的那份带着
        首轮的身份标签，留着只是让一个用不上的标签多躺在库里。
        """
        # 题面与选项是模型写的，会落进 interaction_states 再渲染成卡片发给用户：
        # 和终稿走的是两条路，所以这里单独过一次闸。
        questions = ctx.redact_json(questions)
        intake = pre.intake
        prefix = make_choice_prefix(
            intake.bot.bot_key,
            intake.speaker.platform_user_id,
            now=time.time(),
            rnd=uuid.uuid4().hex,
        )
        context = {
            "relay_session_id": str(pre.info.relay_session_id),
            "stream_id": ctx.stream_id,
            "relay_server_id": str(pre.relay.id),
            "model": intake.bot.model,
            "working_dir": intake.bot.working_dir,
            "backend": pre.request.backend,
            "chat_type": intake.chat_type,
            "chat_id": intake.chat_id,
            "session_key": intake.session_key,
            "user_id": str(intake.speaker.user_id) if intake.speaker.user_id else None,
            "platform_user_id": intake.speaker.platform_user_id,
            "sse_timeout_seconds": intake.bot.sse_timeout_seconds,
            "verbosity_level": intake.bot.verbosity_level,
            "effort_level": intake.bot.effort_level,
        }
        state = {
            "questions": questions,
            "answers": [],
            "current_index": 0,
            "waiting_for_text": False,
            "waiting_since": None,
            "context": context,
        }
        async with ctx.session_factory() as session:
            await interactions.open_state(
                session,
                bot_id=intake.bot.id,
                kind=interactions.KIND_CHOICE,
                scope_key=interactions.choice_scope(intake.bot.id, intake.speaker.platform_user_id),
                state=state,
                task_id_prefix=prefix,
            )
            await session.commit()
        ctx.log.info("ask_user_opened", questions=len(questions), prefix=prefix)
        return choice_card(
            questions[0],
            index=0,
            total=len(questions),
            task_id=question_task_id(prefix, 0),
            icon_url=await self._icon_url(ctx),
            locale=ctx.locale,
        )

    async def _queue_card(
        self, ctx: TaskContext, pre: Prepared, card: dict[str, Any] | None
    ) -> None:
        """主动模式下卡片也得自己入队；幂等键与网关那一路相同，两边抢着入也只会发一张。"""
        if card is None:
            return
        async with ctx.session_factory() as session:
            await outbox.add(
                session,
                bot_id=pre.intake.bot.id,
                platform=pre.intake.bot.platform,
                kind="send",
                dedupe_key=f"{ctx.task.id}:card:0",
                target={"chat_id": pre.intake.chat_id},
                payload={"card": card},
            )
            await session.commit()

    async def _push_if_proactive(
        self,
        ctx: TaskContext,
        pre: Prepared,
        verdict: Verdict,
        done: streams.Completion,
        card: dict[str, Any] | None = None,
        *,
        plain: bool = False,
    ) -> None:
        """流被别人改成了 proactive（网关排空、接管、租约换代）：终稿没人推了，自己推。

        判据用收尾那条 UPDATE 带回来的状态，不再另开一个事务去读：读和写之间正好插进一次
        drain，就会两边都以为对方负责终稿。`offset` 是网关已经投递出去的前缀长度（排空的
        finish 帧带走了那一段），只推剩下的，免得用户把同一段话看两遍。

        本任务自己切的后台走 `on_finish`；这里只兜「不是我切的」那一种，dedupe_key 固定
        `:send:final`，与后台增量的序号键不冲突，重复收尾也只会入队一条。

        只有真成功才配 ✅：出错 / 超时 / 被停走 `finish_failed`（同 `on_finish` 的口径），
        否则被接管的失败任务会被说成「任务已完成」。
        """
        if done.delivery_mode != "proactive":
            return
        pusher = pre.supervisor.pusher
        rest = (
            verdict.final_text[done.offset :]
            if verdict.log_status == "success" and not plain
            else verdict.final_text
        )
        ctx.log.info(
            "proactive_final_push",
            stream_version=done.version,
            status=verdict.log_status,
            offset=done.offset,
        )
        if verdict.log_status == "success" and not plain:
            text = pusher.cap(msg("bg_done_prefix", ctx.locale) + rest)
        else:
            (text,) = pusher.finish_failed(rest)
        await pre.supervisor.push(text, dedupe_key=f"{ctx.task.id}:send:final")
        # 卡片排在终稿之后：先正文后选项卡，顺序反了用户会对着一张没头没尾的卡片发愣。
        await self._queue_card(ctx, pre, card)
