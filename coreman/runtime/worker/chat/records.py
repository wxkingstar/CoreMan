"""chat_logs 记录的拼装。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from coreman.core.chat.chat_logs import ChatLogEntry
from coreman.core.chat.content import BuiltContent
from coreman.core.relay.sse import UsageEvent
from coreman.runtime.worker.chat.models import Intake
from coreman.runtime.worker.context import TaskContext


def log_entry(
    ctx: TaskContext,
    intake: Intake,
    *,
    status: str,
    relay_session_id: uuid.UUID | None = None,
    response_content: str | None = None,
    tools_used: list[str] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    usage: UsageEvent | None = None,
    content: BuiltContent | None = None,
) -> ChatLogEntry:
    """拼一条 chat_logs。耗时从认领时刻起算，`response_at` 只有成功才填。

    `content` 非空时消息四列以组装结果为准：入站 parts 只认得出「有张图」，组装完才知道
    真正发给模型的是哪句提示词、引用了什么、附件多大。
    """
    now = datetime.now(UTC)
    request_at = ctx.task.claimed_at or now
    speaker = intake.speaker
    return ChatLogEntry(
        bot_id=intake.bot.id,
        bot_key=intake.bot.bot_key,
        platform=intake.bot.platform,
        chat_type=intake.chat_type,
        message_type=content.message_type if content else intake.message_type,
        status=status,
        request_at=request_at,
        user_id=speaker.user_id,
        platform_user_id=speaker.platform_user_id,
        user_login=speaker.login_name,
        user_name=speaker.display_name,
        chat_id=intake.chat_id,
        session_key=intake.session_key,
        relay_session_id=relay_session_id,
        relay_server_id=intake.relay_id,
        model=intake.bot.model,
        stream_id=ctx.stream_id,
        task_id=ctx.task.id,
        private=ctx.private_turn,
        message_content=content.text if content else intake.text,
        quoted_content=content.quoted_content if content else None,
        file_info=content.file_info if content else None,
        response_content=response_content,
        tools_used=list(tools_used or []),
        error_code=error_code,
        error_message=error_message,
        latency_ms=int((now - request_at).total_seconds() * 1000),
        input_tokens=usage.input_tokens if usage else None,
        output_tokens=usage.output_tokens if usage else None,
        cache_read_tokens=usage.cache_read_tokens if usage else None,
        cache_creation_tokens=usage.cache_creation_tokens if usage else None,
        cost_usd=None,
        response_at=now if status == "success" else None,
    )
