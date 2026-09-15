"""组装内容阶段：开流之后下载媒体、拼 content parts；拿不到就把这一轮就地结掉。"""

from __future__ import annotations

import asyncio
from typing import Any

from coreman.core.bus import tasks
from coreman.core.chat.content import BuiltContent, ContentBuilder
from coreman.core.i18n.messages import msg
from coreman.core.prompting import (
    sanitize_parts,
    sanitize_user_input,
)
from coreman.core.wecom.media import MediaFetcher
from coreman.runtime.worker.chat.base import ChatStageBase
from coreman.runtime.worker.chat.models import Outcome, Prepared, Verdict
from coreman.runtime.worker.chat.records import log_entry
from coreman.runtime.worker.context import TaskContext


class ContentStage(ChatStageBase):
    """组装发给模型的内容。"""

    @staticmethod
    def _user_content(built: BuiltContent) -> str | list[dict[str, Any]]:
        """纯文本保持字符串（与既有的纯文本请求体一致），含媒体才用 content parts。"""
        if all(p.get("type") == "text" for p in built.parts):
            return sanitize_user_input("\n".join(str(p["text"]) for p in built.parts))
        return sanitize_parts(built.parts)

    async def _build_content(
        self, ctx: TaskContext, pre: Prepared, parts: list[dict[str, Any]]
    ) -> BuiltContent | None:
        """开流之后再下载：提示行要写进思考区给用户看，流不存在就没处写。

        返回 None = 这一轮到此为止（已经回过用户、结过任务、落过日志）。媒体下载失败记
        失败任务；「暂不支持」与「语音没转写出来」沿用既有口径：答过了就算这轮答完了。
        """
        fetcher: MediaFetcher
        owns_fetcher = pre.intake.bot.platform == "feishu" or ctx.media_fetcher is None
        if pre.intake.bot.platform == "feishu":
            from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json
            from coreman.core.platforms.feishu import FeishuClient
            from coreman.core.platforms.feishu_media import FeishuMediaFetcher

            credentials = decrypt_json(ctx.cipher, pre.intake.bot.credentials_enc, CREDENTIALS_AAD)
            fetcher = FeishuMediaFetcher(
                FeishuClient(credentials.get("app_id", ""), credentials.get("app_secret", "")),
                message_id=pre.intake.inbound.platform_msg_id,
            )
        else:
            fetcher = ctx.media_fetcher or MediaFetcher()

        async def on_hint(text: str) -> None:
            # 一有提示就刷出去：攒到 build 之后再写，用户在下载那十几秒里只能看到空白。
            pre.writer.set_thinking_line(text)
            await pre.writer.flush(force=True)

        try:
            builder = ContentBuilder(
                fetcher, locale=ctx.locale, platform=pre.intake.bot.platform, on_hint=on_hint
            )
            build_task = asyncio.create_task(builder.build(parts, text=pre.intake.text))
            stopping = asyncio.create_task(ctx.cancel_event.wait())
            try:
                while True:
                    ready, _ = await asyncio.wait(
                        {build_task, stopping},
                        timeout=self.TICK_SECONDS,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    reason = (
                        (ctx.cancel_reason or "cancelled") if ctx.cancel_event.is_set() else None
                    )
                    if not reason and await pre.supervisor.tick(ctx.clock()) == "expired":
                        reason = "hard_ttl"
                    if reason:
                        build_task.cancel()
                        await asyncio.gather(build_task, return_exceptions=True)
                        await self._finalize(ctx, pre, Outcome(cancelled=True, reason=reason))
                        return None
                    if build_task in ready:
                        built = build_task.result()
                        break
            finally:
                build_task.cancel()
                stopping.cancel()
                await asyncio.gather(build_task, stopping, return_exceptions=True)
        finally:
            # 自己临时建的那份自己收；WorkerService 传下来的那份归它管，不能在这里关。
            if owns_fetcher:
                await fetcher.aclose()
        if built.failed is None:
            return built
        ctx.log.info("content_rejected", code=built.failed_code, message_type=built.message_type)
        failed = built.failed_code == "media_failed"
        # 凑一份与 `_finalize` 同构的判定，收尾三件事（抢终态、送终稿、落日志）才好照它的
        # 规矩来：媒体下载失败记失败任务，「暂不支持」与「语音没转写出来」沿用既有口径。
        verdict = Verdict(
            "error" if failed else "success",
            "failed" if failed else "succeeded",
            "media_failed" if failed else None,
            built.failed if failed else None,
            built.failed,
        )
        async with ctx.session_factory() as session:
            owned = await tasks.finish(
                session,
                ctx.task.id,
                status=verdict.task_status,
                error_code=verdict.error_code,
                error_message=verdict.error_message,
                only_active=True,
            )
            await session.commit()
        if not owned:
            # reaper 已经替这一轮收过尾、也告诉过用户了，再改终稿、再补日志只会自相矛盾。
            ctx.log.warning("task_already_finalized", status=verdict.task_status)
            return None
        pre.writer.thinking.add_end(msg("thinking_end", ctx.locale))
        done = await pre.writer.complete(verdict.final_text)
        # 下载几十兆要十几秒，这期间网关多半已经排空过这条流：那时没人再跟流，终稿得自己推。
        await self._push_if_proactive(ctx, pre, verdict, done, plain=True)
        ctx.chat_logs.submit(
            log_entry(
                ctx,
                pre.intake,
                status=verdict.log_status,
                relay_session_id=pre.info.relay_session_id,
                response_content=verdict.final_text,
                error_code=verdict.error_code,
                error_message=verdict.error_message,
                content=built,
            )
        )
        return None
