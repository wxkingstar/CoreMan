"""定时任务：独立 Relay 会话、执行者当前授权、原子留痕与结果入队。"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.auth.system_access import build_system_access
from coreman.core.bus import tasks
from coreman.core.chat import chat_logs
from coreman.core.chat import human_collaboration as human
from coreman.core.chat.bot_collaboration import issue_capability
from coreman.core.chat.chat_logs import ChatLogEntry
from coreman.core.chat.collaboration_tools import CRON_POLICY
from coreman.core.chat.redaction import collect_secrets
from coreman.core.cron.access import require_operator
from coreman.core.cron.delivery import enqueue_result
from coreman.core.cron.precheck import PrecheckError, PrecheckResult, run_precheck_in_thread
from coreman.core.db.models import (
    CHAT_LOG_RUNNING,
    Bot,
    CronJob,
    CronRun,
    FeishuPersonalGrant,
    HumanCollaboration,
    RelayServer,
    RuntimeNode,
    Task,
    User,
    WecomPersonalBinding,
)
from coreman.core.errors import ApiError
from coreman.core.feishu_personal import policy
from coreman.core.i18n.messages import msg
from coreman.core.knowledge.installation import effective_env
from coreman.core.personal_schedules import require_personal
from coreman.core.pricing import estimate
from coreman.core.prompting import (
    build_env,
    build_system_prompt,
    load_segments,
    sanitize_user_input,
)
from coreman.core.relay.client import ChatRequest, IncompleteResultError, RelayError
from coreman.core.relay.models import backend_of
from coreman.core.relay.sse import (
    AskUserQuestionEvent,
    FinishEvent,
    RelayErrorEvent,
    SseEvent,
    TextDelta,
    ToolUseStart,
    UsageEvent,
)
from coreman.core.timeutils import utcnow
from coreman.core.wecom_personal import policy as wecom_policy
from coreman.runtime.worker.chat.personal import feishu_guidance
from coreman.runtime.worker.chat.wecom_personal import guidance as wecom_guidance
from coreman.runtime.worker.context import TaskContext

# 结果正文上限（字符）：超出只保留前面这些并附截断提示，任务照常算成功。它同时约束
# cron_runs.reply 的大小；各投递渠道另按自己的分片与条数上限再截一次（见 cron.delivery）。
RESULT_MAX_CHARS = 100_000


class CronStreamError(ValueError):
    """这一轮没有拿到可交付的结果。

    `code` 进 error_code 与统计口径；`detail` 是给人看的原因（relay 回传的原话）；`partial`、
    `usage`、`tools` 是失败前已经拿到的部分，照样留进执行记录。
    """

    def __init__(
        self,
        code: str,
        detail: str | None = None,
        partial: str = "",
        usage: UsageEvent | None = None,
        tools: list[str] | None = None,
    ) -> None:
        super().__init__(code)
        self.code, self.detail, self.partial = code, detail, partial
        self.usage, self.tools = usage, list(tools or [])


class CronRunHandler:
    kind = "cron_run"

    async def run(self, ctx: TaskContext) -> None:
        # 周期心跳由 WorkerService 的心跳循环统一写；外部调用前那一次显式收取取消见 _run。
        from coreman.runtime.worker.reminder_handler import run_fixed

        if await run_fixed(ctx):
            return
        await self._run(ctx)

    async def _run(self, ctx: TaskContext) -> None:
        config = ctx.task.payload.get("config", {})
        if not isinstance(config, dict):
            config = {}
        meta: dict[str, Any] = {}
        usage: UsageEvent | None = None
        tools: list[str] = []
        reply = ""
        status, error = "failed", None
        detail: str | None = None
        session_id = uuid.uuid4()
        execution_model: str | None = None
        execution_relay_id: uuid.UUID | None = None
        # 同事答复后的续跑：不重跑执行前检查，沿用原执行的 Relay 会话，不再挂载求助工具。
        asked_id = ctx.task.payload.get("human_collaboration_id")
        collaborating = False
        try:
            started = time.monotonic()
            if asked_id:
                result = PrecheckResult(True, "", "collaboration_reply")
            else:
                # precheck 只接收非敏感快照，不注入 bot env / token / 任意 Python 对象。
                # 解释器在独立线程里跑：同进程其它任务的心跳、SSE 消费与取消传导不能跟着停摆。
                result = await run_precheck_in_thread(
                    config.get("precheck_script"),
                    {
                        "now": utcnow().isoformat(),
                        "scheduled_at": ctx.task.payload.get("scheduled_at"),
                        "bot_id": str(ctx.task.bot_id),
                        "user_id": str(ctx.task.user_id),
                        "job_name": config.get("name"),
                    },
                    float(config.get("precheck_timeout_seconds", 30)),
                )
            meta = {"elapsed_ms": int((time.monotonic() - started) * 1000), "reason": result.reason}
            if not result.trigger:
                await self._finish(ctx, "skipped", "", None, meta, None, [], session_id)
                return
            async with ctx.session_factory() as session:
                asked = (
                    await session.get(HumanCollaboration, uuid.UUID(asked_id)) if asked_id else None
                )
                if asked_id and (
                    asked is None
                    or asked.status != "resuming"
                    or asked.resume_task_id != ctx.task.id
                ):
                    raise ApiError(409, 409, "collaboration_inactive")
                if asked is not None and asked.relay_session_id is not None:
                    session_id = asked.relay_session_id
                job_id = uuid.UUID(ctx.task.payload["cron_job_id"])
                job = await session.get(CronJob, job_id)
                bot = await session.scalar(
                    select(Bot).where(Bot.id == ctx.task.bot_id).with_for_update()
                )
                actor = await session.get(User, ctx.task.user_id) if ctx.task.user_id else None
                run = await session.scalar(select(CronRun).where(CronRun.task_id == ctx.task.id))
                if (
                    job is None
                    # 续跑属于已经开始的那次执行：一次性任务跑完即停用、任务到期都不拦它。
                    or (not asked_id and not job.enabled)
                    or job.bot_id != ctx.task.bot_id
                    or job.running_task_id != ctx.task.id
                    or bot is None
                    or not bot.enabled
                    or actor is None
                    or run is None
                    or run.status != "running"
                    or run.executed_by != actor.id
                    or (not asked_id and job.expires_at is not None and job.expires_at <= utcnow())
                ):
                    raise ApiError(403, 403, "job_or_actor_unavailable")
                if job.execution_mode == "personal_ai":
                    speaker = await require_personal(session, job, bot, actor)
                else:
                    speaker = await require_operator(session, bot, actor)
                relay = (
                    await session.get(RelayServer, bot.relay_server_id)
                    if bot.relay_server_id
                    else None
                )
                if relay is None or not relay.is_active:
                    raise ApiError(409, 409, "relay_unavailable")
                execution_model, execution_relay_id = bot.model, relay.id
                access = await build_system_access(
                    session,
                    ctx.cipher,
                    bot=bot,
                    speaker=speaker,
                    issuer=str(await ctx.settings_store.get("jwt_issuer", default="coreman")),
                    external_key=ctx.external_jwt_key,
                )
                env = build_env(
                    bot_key=bot.bot_key,
                    platform=bot.platform,
                    chat_id=f"cron:{job.id}",
                    chat_type="cron",
                    platform_user_id=speaker.platform_user_id,
                    session_id=str(session_id),
                    speaker=speaker,
                    bot_env=await effective_env(session, ctx.cipher, bot),
                )
                env.update(access.env)
                # 求助入口只由平台逐次签发，机器人或技能自带的同名变量一律丢弃。
                env = {
                    k: v
                    for k, v in env.items()
                    if not k.startswith(("COREMAN_COLLABORATION_", "COREMAN_BOT_HELP_"))
                }
                backend = backend_of(bot.model, relay.model_provider)
                personal_prompt = await self._personal_tools(
                    session, ctx, job, bot, actor, relay, backend, config, env
                )
                if personal_prompt or job.execution_mode == "personal_ai":
                    run.private = True
                if asked is not None:
                    personal_prompt += human.CRON_RESUME_POLICY
                elif bot.platform == "feishu" and await human.has_partners(session, bot.id):
                    env["COREMAN_COLLABORATION_URL"] = (
                        ctx.public_base_url.rstrip("/") + "/api/runtime/collaboration/mcp"
                    )
                    env["COREMAN_COLLABORATION_TOKEN"] = issue_capability(
                        ctx.cipher, task_id=ctx.task.id, user_id=str(actor.id)
                    )
                    personal_prompt += CRON_POLICY
                    collaborating = True
                # 定时执行同样带着本人的业务系统令牌与个人工具凭据，出站闸门一视同仁。
                ctx.secrets = collect_secrets(env)
                if asked is not None:
                    # 续跑的指令是开始执行时写好的 JSON：原任务、问题和同事答复。
                    prompt = run.prompt
                else:
                    prompt = str(config["prompt"])
                    if result.prompt_appendix:
                        prompt += "\n\n" + result.prompt_appendix
                run.prompt = prompt
                request = ChatRequest(
                    model=bot.model,
                    backend=backend,
                    working_dir=bot.working_dir,
                    session_id=str(session_id),
                    env_vars=env,
                    effort=bot.effort_level,
                    verbosity_level=bot.verbosity_level,
                    user_content=sanitize_user_input(prompt),
                    system_prompt=build_system_prompt(
                        segments=await load_segments(ctx.settings_store),
                        backend=backend,
                        verbosity_level=bot.verbosity_level,
                        speaker=speaker,
                        speaker_changed=False,
                        systems_prompt=access.prompt,
                        bot_prompt=(
                            config.get("system_prompt")
                            or bot.merged_system_prompt
                            or bot.system_prompt
                            or ""
                        ),
                        scheduled=True,
                        extra=personal_prompt,
                    ),
                )
                # 与对话同一口径：调用模型之前先写进行中的记录，与执行记录的私密标记同一事务。
                await chat_logs.open_turn(
                    session,
                    self._log_entry(
                        ctx.task,
                        bot,
                        actor,
                        run,
                        status=CHAT_LOG_RUNNING,
                        relay_session_id=session_id,
                        relay_server_id=relay.id,
                        model=bot.model,
                    ),
                )
                await session.commit()
            # 身份验证后、外部调用前再次收取取消；独立会话不改写用户的 chat_sessions。
            await ctx.heartbeat()
            if ctx.cancel_event.is_set():
                raise asyncio.CancelledError()
            client = ctx.relay_client_factory(relay)
            gen = client.chat_stream(request, total_timeout=float(bot.sse_timeout_seconds))
            stop = asyncio.create_task(ctx.cancel_event.wait())
            consume = asyncio.create_task(self._consume(ctx, gen))
            # 登记求助后由平台断流，不依赖模型自己停下，也不让它继续消耗。
            handoff = asyncio.create_task(
                self._handoff(ctx) if collaborating else asyncio.Event().wait()
            )
            try:
                # 兜底：正常由 chat_stream 的 total_timeout 先到期并给出分类错误；
                # 不再额外封顶 2 小时，与对话一样允许到 sse_timeout_seconds 上限。
                async with asyncio.timeout(bot.sse_timeout_seconds + 60):
                    ready, _ = await asyncio.wait(
                        {stop, consume, handoff}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if stop in ready:
                        raise asyncio.CancelledError()
                    if handoff in ready and consume not in ready:
                        # 说明文字在收尾时按求助账本生成。
                        reply, status = "", "success"
                    else:
                        reply, usage, tools, truncated = consume.result()
                        if truncated:
                            ctx.log.warning("cron_result_truncated", limit=RESULT_MAX_CHARS)
                            reply += msg(
                                "cron_result_truncated", ctx.locale, limit=RESULT_MAX_CHARS
                            )
                        status = "success"
            finally:
                stop.cancel()
                consume.cancel()
                handoff.cancel()
                await asyncio.gather(stop, consume, handoff, return_exceptions=True)
                await gen.aclose()
                await client.aclose()
        except PrecheckError as exc:
            status, error = "failed_precheck", str(exc)
            meta["error"] = error
        except CronStreamError as exc:
            status, error, detail = "failed", exc.code, exc.detail
            reply, usage, tools = exc.partial, exc.usage, exc.tools
        except asyncio.CancelledError:
            status, error = "failed", "cancelled"
            # 进程强制取消仍尝试原子结单；事务失败则由看护补齐。
        except TimeoutError:
            status, error = "failed", "timeout"
        except Exception as exc:
            status, error = "failed", type(exc).__name__
        await self._finish(
            ctx,
            status,
            reply,
            error,
            meta,
            usage,
            tools,
            session_id,
            execution_model,
            execution_relay_id,
            detail=detail,
        )

    async def _personal_tools(
        self,
        session: AsyncSession,
        ctx: TaskContext,
        job: CronJob,
        bot: Bot,
        actor: User,
        relay: RelayServer,
        backend: str,
        config: dict[str, Any],
        env: dict[str, str],
    ) -> str:
        """本人创建、本人执行、只发本人的任务，可以以本人身份用飞书或企业微信；返回要追加的提示词。

        不满足就原样跳过，任务照常运行：授权撤销或运行时太旧不该让定时任务失败。
        """
        if (
            bot.platform not in ("feishu", "wecom")
            or job.created_by != actor.id
            or not policy.self_only(config, actor.id)
            or relay.runtime_node_id is None
        ):
            return ""
        node = await session.get(RuntimeNode, relay.runtime_node_id, populate_existing=True)
        if bot.platform == "wecom":
            return await self._wecom_tools(session, ctx, bot, actor, node, backend, env)
        grant = await session.get(FeishuPersonalGrant, (bot.id, actor.id), populate_existing=True)
        if (
            node is None
            or not node.is_active
            or not policy.runtime_supported(node.capabilities, backend)
            or grant is None
            or grant.status != "connected"
            or not grant.token_enc
        ):
            return ""
        try:
            await policy.scheduled_scope(session, ctx.task.id, str(actor.id))
        except ValueError:
            return ""
        base_url = ctx.public_base_url.rstrip("/")
        env[policy.PREFIX + "URL"] = base_url + "/api/runtime/feishu-personal/mcp"
        env[policy.PREFIX + "TOKEN"] = policy.issue_capability(
            ctx.cipher,
            task_id=ctx.task.id,
            user_id=str(actor.id),
            context_epoch=grant.context_epoch,
            base_session_id=None,
        )
        return feishu_guidance(grant, base_url, scheduled=True)

    @staticmethod
    async def _wecom_tools(
        session: AsyncSession,
        ctx: TaskContext,
        bot: Bot,
        actor: User,
        node: RuntimeNode | None,
        backend: str,
        env: dict[str, str],
    ) -> str:
        binding = await session.get(WecomPersonalBinding, actor.id, populate_existing=True)
        if (
            node is None
            or not node.is_active
            or not wecom_policy.runtime_supported(node.capabilities, backend)
            or binding is None
            or binding.status != "bound"
            or not binding.enabled
        ):
            return ""
        try:
            await wecom_policy.scheduled_scope(session, ctx.task.id, str(actor.id))
        except ValueError:
            return ""
        env[wecom_policy.PREFIX + "URL"] = (
            ctx.public_base_url.rstrip("/") + "/api/runtime/wecom-personal/mcp"
        )
        env[wecom_policy.PREFIX + "TOKEN"] = wecom_policy.issue_capability(
            ctx.cipher,
            task_id=ctx.task.id,
            user_id=str(actor.id),
            context_epoch=binding.context_epoch,
            base_session_id=None,
        )
        return wecom_guidance(binding, scheduled=True)

    @staticmethod
    async def _handoff(ctx: TaskContext) -> None:
        """每秒看一次任务记录：求助登记成功后返回，由调用方关闭模型流。"""
        while True:
            await asyncio.sleep(1)
            async with ctx.session_factory() as session:
                task = await session.get(Task, ctx.task.id)
                if task is not None and task.payload.get("collaboration_handoff"):
                    return

    async def _consume(
        self, ctx: TaskContext, gen: AsyncGenerator[SseEvent, None]
    ) -> tuple[str, UsageEvent | None, list[str], bool]:
        """收完整条流再判定；返回 (正文, 用量, 工具, 是否截断过)。

        与 chat 同一口径：relay 回传的错误优先（保留原话），其次是零事件流，最后才是
        「未确认终态」；连接失败、超时这类通用异常原样抛出。

        正文超过 `RESULT_MAX_CHARS` 只保留前面部分、继续把流收完：终态与用量还在后面，
        长报表不能因为多写了几行就整轮判失败、白烧 token。
        """
        parts: list[str] = []
        errors: list[str] = []
        size = 0
        truncated = False
        events = 0
        usage = None
        finished = False
        tools: list[str] = []
        try:
            async for event in gen:
                if isinstance(event, AskUserQuestionEvent):
                    raise CronStreamError(
                        "interactive_answer_required",
                        partial="".join(parts),
                        usage=usage,
                        tools=tools,
                    )
                if isinstance(event, RelayErrorEvent):
                    errors.append(event.text)
                    continue
                if isinstance(event, FinishEvent):
                    finished = event.reason in ("stop", "end_turn", "completed")
                    continue
                events += 1
                if isinstance(event, TextDelta):
                    room = RESULT_MAX_CHARS - size
                    if len(event.text) > room:
                        truncated = True
                    if room > 0:
                        parts.append(event.text[:room])
                    size += min(len(event.text), max(room, 0))
                elif isinstance(event, UsageEvent):
                    usage = event
                elif isinstance(event, ToolUseStart) and event.name not in tools:
                    tools.append(event.name)
        except RelayError as exc:
            if not errors and not isinstance(exc, IncompleteResultError):
                raise
            finished = False
        # 模型产出在这唯一一处成形，之后分头走推送、cron_runs.reply 与 chat_logs：
        # 在这里过出站闸门，三条路就都干净了（对话链路的对应位置是 chat/classify）。
        reply = ctx.redact("".join(parts).strip()) or ""
        if errors:
            detail = ctx.redact("".join(errors).strip()[:2000]) or ""
            raise CronStreamError("x_relay_error", detail, reply, usage, tools)
        if not finished:
            code = "incomplete_result" if events else "empty_stream"
            raise CronStreamError(code, partial=reply, usage=usage, tools=tools)
        if not reply:
            raise CronStreamError("empty_result", usage=usage, tools=tools)
        return reply, usage, tools, truncated

    async def _finish(
        self,
        ctx: TaskContext,
        status: str,
        reply: str,
        error: str | None,
        meta: dict[str, Any],
        usage: UsageEvent | None,
        tools: list[str],
        relay_session_id: uuid.UUID,
        execution_model: str | None = None,
        execution_relay_id: uuid.UUID | None = None,
        *,
        detail: str | None = None,
    ) -> None:
        unlogged: ChatLogEntry | None = None
        async with ctx.session_factory() as session:
            # 求助账本先于 job 加锁，与收取答复、调度看护同一锁序（账本 → job → task → run）。
            asked_id = ctx.task.payload.get("human_collaboration_id")
            asked = await session.scalar(
                select(HumanCollaboration)
                .where(
                    HumanCollaboration.id == uuid.UUID(asked_id)
                    if asked_id
                    else HumanCollaboration.source_task_id == ctx.task.id
                )
                .with_for_update()
            )
            # 全部路径遵循 job → task → run 锁序，且终态、记录、出站在同一提交中。
            job = await session.scalar(
                select(CronJob)
                .where(CronJob.id == uuid.UUID(ctx.task.payload["cron_job_id"]))
                .with_for_update()
            )
            task = await session.scalar(
                select(Task).where(Task.id == ctx.task.id).with_for_update()
            )
            run = await session.scalar(
                select(CronRun).where(CronRun.task_id == ctx.task.id).with_for_update()
            )
            if (
                task is None
                or task.status not in tasks.ACTIVE
                or run is None
                or run.status != "running"
            ):
                return
            if task.cancel_requested_at is not None:
                status, error, reply, detail = "failed", "cancelled", "", None
            if asked is not None and not asked_id and asked.status == "pending":
                # 这次执行登记了求助：干净交接后才发出问题，推送内容换成等待说明。
                if status == "success":
                    asked.relay_session_id = relay_session_id
                    await human.send_ask(session, asked)
                    helper = await session.get(User, asked.helper_user_id)
                    reply = human.cron_handoff_text(
                        helper.display_name if helper else "同事", asked.question
                    )
                else:
                    await human.close(
                        session, asked, "cancelled", "定时执行未正常结束", notify=False
                    )
            elif asked is not None and asked_id and asked.status == "resuming":
                asked.status = "completed" if status == "success" else "failed"
                asked.error = None if status == "success" else (error or status)
            now = utcnow()
            run.status, run.reply = status, reply or None
            run.error_message = f"{error}: {detail}" if error and detail else error
            run.precheck_meta, run.finished_at = meta, now
            if usage:
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_creation_tokens",
                ):
                    setattr(run, key, getattr(usage, key))
            if job and job.running_task_id == task.id:
                job.running_task_id = None
                job.last_status = status
                if job.schedule_kind == "once":
                    job.enabled = False
            task_status = (
                "succeeded"
                if status in {"success", "skipped"}
                else (
                    "cancelled"
                    if error == "cancelled"
                    else "timed_out"
                    if error == "timeout"
                    else "failed"
                )
            )
            await tasks.finish(
                session,
                task.id,
                status=task_status,
                error_code=error,
                error_message=detail,
                result={"cron_run_id": run.id, "status": status},
                only_active=True,
            )
            bot = await session.get(Bot, task.bot_id)
            actor = await session.get(User, task.user_id) if task.user_id else None
            if bot is not None:
                config = task.payload.get("config", {})
                if status != "skipped" and error != "cancelled":
                    if status == "success":
                        # 头尾标明是哪个定时任务、跑了多久：主动推送和对话回复长得一样，
                        # 不加标记用户会把它当成某次对话的回答。cron_runs.reply 仍存原文。
                        seconds = max(0, int((now - run.started_at).total_seconds()))
                        content = (
                            msg(
                                "cron_push_header",
                                ctx.locale,
                                name=run.job_name,
                                bot=bot.name,
                                seconds=seconds,
                            )
                            + reply
                            + msg("cron_push_footer", ctx.locale)
                        )
                    else:
                        content = msg(
                            "cron_failed",
                            ctx.locale,
                            name=run.job_name,
                            bot=bot.name,
                            reason=detail or error or status,
                        )
                    run.delivery = await enqueue_result(
                        session,
                        bot=bot,
                        config=config,
                        run_id=run.id,
                        content=content,
                        cipher=ctx.cipher,
                        locale=ctx.locale,
                        fallback_user_id=job.created_by if job else None,
                    )
                run.cost_usd = await estimate(
                    session,
                    model=execution_model,
                    at=run.started_at,
                    input_tokens=run.input_tokens,
                    output_tokens=run.output_tokens,
                    cache_read_tokens=run.cache_read_tokens,
                    cache_creation_tokens=run.cache_creation_tokens,
                )
                # 调用过模型的执行已有进行中的记录，这里原地改成终态；预检跳过等没开始的补一行。
                entry = self._log_entry(
                    task,
                    bot,
                    actor,
                    run,
                    status="success" if status in {"success", "skipped"} else "error",
                    relay_session_id=relay_session_id,
                    relay_server_id=execution_relay_id,
                    model=execution_model,
                    response_at=now,
                    response_content=reply,
                    tools_used=tools,
                    error_code=error,
                    error_message=detail,
                    input_tokens=run.input_tokens,
                    output_tokens=run.output_tokens,
                    cache_read_tokens=run.cache_read_tokens,
                    cache_creation_tokens=run.cache_creation_tokens,
                    cost_usd=None if run.cost_usd is None else float(run.cost_usd),
                )
                if not await chat_logs.finish_turn(session, entry):
                    unlogged = entry
            await session.commit()
        if unlogged is not None:
            # 同事务里没写成（已记告警）：交给写入端事后补写，别让记录停在进行中。
            ctx.chat_logs.submit_finish(unlogged)

    @staticmethod
    def _log_entry(
        task: Task,
        bot: Bot,
        actor: User | None,
        run: CronRun,
        *,
        status: str,
        relay_session_id: uuid.UUID,
        relay_server_id: uuid.UUID | None,
        model: str | None,
        **terminal: Any,
    ) -> ChatLogEntry:
        """定时执行的对话记录：开始时写进行中的一行，结束时按同一口径写终态。"""
        return ChatLogEntry(
            bot_id=bot.id,
            bot_key=bot.bot_key,
            platform=bot.platform,
            chat_type="cron",
            message_type="text",
            status=status,
            request_at=run.started_at,
            task_id=task.id,
            user_id=task.user_id,
            user_login=actor.login_name if actor else None,
            user_name=actor.display_name if actor else None,
            relay_session_id=relay_session_id,
            relay_server_id=relay_server_id,
            model=model,
            message_content=run.prompt,
            **terminal,
        )
