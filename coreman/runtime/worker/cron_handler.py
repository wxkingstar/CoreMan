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
from coreman.core.chat.redaction import collect_secrets
from coreman.core.cron.access import require_operator
from coreman.core.cron.delivery import enqueue_result
from coreman.core.cron.precheck import PrecheckError, run_precheck_in_thread
from coreman.core.db.models import (
    Bot,
    ChatLog,
    CronJob,
    CronRun,
    FeishuPersonalGrant,
    RelayServer,
    RuntimeNode,
    Task,
    User,
    WecomPersonalGrant,
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
        try:
            # precheck 只接收非敏感快照，不注入 bot env / token / 任意 Python 对象。
            # 解释器在独立线程里跑：同进程其它任务的心跳、SSE 消费与取消传导不能跟着停摆。
            started = time.monotonic()
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
                job_id = uuid.UUID(ctx.task.payload["cron_job_id"])
                job = await session.get(CronJob, job_id)
                bot = await session.scalar(
                    select(Bot).where(Bot.id == ctx.task.bot_id).with_for_update()
                )
                actor = await session.get(User, ctx.task.user_id) if ctx.task.user_id else None
                run = await session.scalar(select(CronRun).where(CronRun.task_id == ctx.task.id))
                if (
                    job is None
                    or not job.enabled
                    or job.bot_id != ctx.task.bot_id
                    or job.running_task_id != ctx.task.id
                    or bot is None
                    or not bot.enabled
                    or actor is None
                    or run is None
                    or run.status != "running"
                    or run.executed_by != actor.id
                    or (job.expires_at is not None and job.expires_at <= utcnow())
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
                backend = backend_of(bot.model, relay.model_provider)
                personal_prompt = await self._personal_tools(
                    session, ctx, job, bot, actor, relay, backend, config, env
                )
                if personal_prompt or job.execution_mode == "personal_ai":
                    run.private = True
                # 定时执行同样带着本人的业务系统令牌与个人工具凭据，出站闸门一视同仁。
                ctx.secrets = collect_secrets(env)
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
                    )
                    + personal_prompt,
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
            try:
                # 兜底：正常由 chat_stream 的 total_timeout 先到期并给出分类错误；
                # 不再额外封顶 2 小时，与对话一样允许到 sse_timeout_seconds 上限。
                async with asyncio.timeout(bot.sse_timeout_seconds + 60):
                    ready, _ = await asyncio.wait(
                        {stop, consume}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if stop in ready:
                        raise asyncio.CancelledError()
                    reply, usage, tools, truncated = consume.result()
                    if truncated:
                        ctx.log.warning("cron_result_truncated", limit=RESULT_MAX_CHARS)
                        reply += msg("cron_result_truncated", ctx.locale, limit=RESULT_MAX_CHARS)
                    status = "success"
            finally:
                stop.cancel()
                consume.cancel()
                await asyncio.gather(stop, consume, return_exceptions=True)
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
        grant = await session.get(WecomPersonalGrant, (bot.id, actor.id), populate_existing=True)
        if (
            node is None
            or not node.is_active
            or not wecom_policy.runtime_supported(node.capabilities, backend)
            or grant is None
            or grant.status != "connected"
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
            context_epoch=grant.context_epoch,
            base_session_id=None,
        )
        return wecom_guidance(grant, scheduled=True)

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
        async with ctx.session_factory() as session:
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
                session.add(
                    ChatLog(
                        bot_id=bot.id,
                        bot_key=bot.bot_key,
                        platform=bot.platform,
                        chat_type="cron",
                        message_type="text",
                        task_id=task.id,
                        user_id=task.user_id,
                        user_login=actor.login_name if actor else None,
                        user_name=actor.display_name if actor else None,
                        request_at=run.started_at,
                        response_at=now,
                        relay_session_id=relay_session_id,
                        relay_server_id=execution_relay_id,
                        model=execution_model,
                        message_content=run.prompt[:10000],
                        response_content=reply[:50000],
                        tools_used=tools,
                        status="success" if status in {"success", "skipped"} else "error",
                        error_code=error,
                        error_message=detail,
                        input_tokens=run.input_tokens,
                        output_tokens=run.output_tokens,
                        cache_read_tokens=run.cache_read_tokens,
                        cache_creation_tokens=run.cache_creation_tokens,
                        cost_usd=run.cost_usd,
                    )
                )
            await session.commit()
