"""定时任务：独立 Relay 会话、执行者当前授权、原子留痕与结果入队。"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy import select

from coreman.core.auth.system_access import build_system_access
from coreman.core.bus import tasks
from coreman.core.cron.access import require_operator
from coreman.core.cron.delivery import enqueue_result
from coreman.core.cron.precheck import PrecheckError, run_precheck
from coreman.core.db.models import Bot, ChatLog, CronJob, CronRun, RelayServer, Task, User
from coreman.core.errors import ApiError
from coreman.core.knowledge.installation import effective_env
from coreman.core.pricing import estimate
from coreman.core.prompting import (
    build_env,
    build_system_prompt,
    load_segments,
    sanitize_user_input,
)
from coreman.core.relay.client import ChatRequest
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
from coreman.runtime.worker.context import TaskContext


class CronRunHandler:
    kind = "cron_run"

    async def run(self, ctx: TaskContext) -> None:
        heartbeat = asyncio.create_task(self._heartbeat(ctx))
        try:
            await self._run(ctx)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _heartbeat(self, ctx: TaskContext) -> None:
        while True:
            await ctx.heartbeat()
            await asyncio.sleep(10)

    async def _run(self, ctx: TaskContext) -> None:
        config = ctx.task.payload.get("config", {})
        if not isinstance(config, dict):
            config = {}
        meta: dict[str, Any] = {}
        usage: UsageEvent | None = None
        tools: list[str] = []
        reply = ""
        status, error = "failed", None
        session_id = uuid.uuid4()
        execution_model: str | None = None
        execution_relay_id: uuid.UUID | None = None
        try:
            # precheck 只接收非敏感快照，不注入 bot env / token / 任意 Python 对象。
            started = time.monotonic()
            result = run_precheck(
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
                        )
                        + "\n\n这是定时执行。请直接给出可交付的结果；不要等待对话卡片回答。",
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
            consume = asyncio.create_task(self._consume(gen))
            try:
                async with asyncio.timeout(min(bot.sse_timeout_seconds, 7200)):
                    ready, _ = await asyncio.wait(
                        {stop, consume}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if stop in ready:
                        raise asyncio.CancelledError()
                    reply, usage, tools = consume.result()
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
        )

    async def _consume(
        self, gen: AsyncGenerator[SseEvent, None]
    ) -> tuple[str, UsageEvent | None, list[str]]:
        parts: list[str] = []
        size = 0
        usage = None
        finished = False
        tools: list[str] = []
        async for event in gen:
            if isinstance(event, (RelayErrorEvent, AskUserQuestionEvent)):
                raise ValueError("relay_error_or_interactive_answer_required")
            if isinstance(event, FinishEvent):
                finished = event.reason in ("stop", "end_turn", "completed")
            if isinstance(event, TextDelta):
                size += len(event.text)
                if size > 100000:
                    raise ValueError("result_size_limit")
                parts.append(event.text)
            elif isinstance(event, UsageEvent):
                usage = event
            elif isinstance(event, ToolUseStart) and event.name not in tools:
                tools.append(event.name)
        reply = "".join(parts).strip()
        if not finished:
            raise ValueError("incomplete_result")
        if not reply:
            raise ValueError("empty_result")
        return reply, usage, tools

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
                status, error, reply = "failed", "cancelled", ""
            now = utcnow()
            run.status, run.reply, run.error_message = status, reply or None, error
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
                result={"cron_run_id": run.id, "status": status},
                only_active=True,
            )
            bot = await session.get(Bot, task.bot_id)
            actor = await session.get(User, task.user_id) if task.user_id else None
            if bot is not None:
                config = task.payload.get("config", {})
                if status != "skipped" and error != "cancelled":
                    content = (
                        reply
                        if status == "success"
                        else f"**定时任务执行失败**\n{run.job_name}\n{error or status}"
                    )
                    run.delivery = await enqueue_result(
                        session,
                        bot=bot,
                        config=config,
                        run_id=run.id,
                        content=content,
                        cipher=ctx.cipher,
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
                        input_tokens=run.input_tokens,
                        output_tokens=run.output_tokens,
                        cache_read_tokens=run.cache_read_tokens,
                        cache_creation_tokens=run.cache_creation_tokens,
                        cost_usd=run.cost_usd,
                    )
                )
            await session.commit()
