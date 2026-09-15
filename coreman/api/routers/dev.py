"""开发期注入（只在 COREMAN_ENV=dev 时挂载）：不连企微也能把一条消息灌进总线。

走的是正经链路——`enqueue_inbound` 落 `inbound_events` + 建 task，worker 照常处理；
只有「消息从哪来」是假的。此接口不解密 bot 凭证，但 worker / gateway 仍会使用已登记
配置；请使用测试 bot 和测试会话，结果可在 `GET /tasks/{id}` 里查看。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.permissions import require_roles
from coreman.api.routers.chat_logs import chat_log_out
from coreman.api.security import verify_csrf
from coreman.core.db.models import Bot, ChatLog, InboundEvent, Task, TaskStream, User
from coreman.core.wecom.messages import InboundMessage, Part, Sender, TextPart
from coreman.core.wecom.stream_render import StreamView, render_wecom_stream
from coreman.runtime.gateway_common.inbound import enqueue_inbound
from coreman.runtime.gateway_wecom.runner import BotInfo

router = APIRouter(prefix="/api/dev", tags=["dev"], dependencies=[Depends(verify_csrf)])
# B008：同 platform_apps，require_roles(...) 不能写进参数默认值里，挪成模块级单例。
_ADMINS = require_roles("platform_admin")


class InjectIn(BaseModel):
    bot_key: str = Field(max_length=50)
    text: str = Field(default="", max_length=10000)
    parts: list[Part] = Field(default_factory=list, max_length=20)
    platform_user_id: str = Field(default="dev-user", max_length=100)
    chat_type: Literal["single", "group"] = "single"
    chat_id: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_content(self) -> InjectIn:
        if not self.text and not self.parts:
            raise ValueError("请提供 text 或 parts")
        if (
            len(json.dumps([part.model_dump() for part in self.parts], ensure_ascii=False).encode())
            > 65536
        ):
            raise ValueError("媒体引用和内容总量不能超过 64 KiB")
        return self


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def task_out(row: Task) -> dict[str, Any]:
    # tasks 没有 created_at 列：入队时间看 run_after（立即入队时就是 now()）。
    return {
        "id": row.id,
        "status": row.status,
        "kind": row.kind,
        "lane": row.lane,
        "run_after": _iso(row.run_after),
        "claimed_at": _iso(row.claimed_at),
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at),
        "error_code": row.error_code,
        "error_message": row.error_message,
    }


def stream_out(row: TaskStream, now: datetime) -> dict[str, Any]:
    return {
        "rendered": render_wecom_stream(StreamView.from_row(row), now),
        "thinking_md": row.thinking_md,
        "pending_text": row.pending_text,
        "final_text": row.final_text,
        "is_complete": row.is_complete,
        "delivery_mode": row.delivery_mode,
    }


@router.post("/inject-message")
async def inject_message(
    body: InjectIn,
    _: User = Depends(_ADMINS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = (
        await session.execute(select(Bot).where(Bot.bot_key == body.bot_key))
    ).scalar_one_or_none()
    if bot is None:
        raise not_found("机器人不存在")
    if not bot.enabled:
        raise ApiError(409, 409, "机器人已停用")
    if bot.platform != "wecom":
        raise ApiError(409, 409, "只支持企微机器人")
    now = datetime.now(UTC)
    message = InboundMessage(
        platform="wecom",
        bot_id=bot.id,
        kind="message",
        chat_type=body.chat_type,
        chat_id=body.chat_id or body.platform_user_id,
        sender=Sender(platform_user_id=body.platform_user_id),
        message_id=f"dev-{uuid.uuid4()}",
        mentions_bot=False,
        parts=([TextPart(text=body.text)] if body.text else []) + body.parts,
        reply_context={
            "gateway_instance": "dev",
            "req_id": f"dev-{uuid.uuid4()}",
            "received_at": now.isoformat(),
        },
        raw={},
    )
    info = BotInfo(
        id=bot.id,
        bot_key=bot.bot_key,
        name=bot.name,
        welcome_message=bot.welcome_message,
        wecom_bot_id="dev",
        secret="",
        credentials_fingerprint="",
    )
    task = await enqueue_inbound(session, info, message, lease_generation=0)
    event_id = (
        await session.execute(
            select(InboundEvent.id).where(
                InboundEvent.bot_id == bot.id, InboundEvent.platform_msg_id == message.message_id
            )
        )
    ).scalar_one()
    # 提交之后 tasks_queued 才发出去，worker 才会来抢。
    await session.commit()
    return {"code": 0, "data": {"task_id": task.id if task else None, "inbound_event_id": event_id}}


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: int,
    _: User = Depends(_ADMINS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    task = await session.get(Task, task_id, populate_existing=True)
    if task is None:
        raise not_found("任务不存在")
    stream = await session.get(TaskStream, task_id, populate_existing=True)
    log = (
        await session.execute(
            select(ChatLog).where(ChatLog.task_id == task_id).order_by(ChatLog.id.desc()).limit(1)
        )
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    return {
        "code": 0,
        "data": {
            "task": task_out(task),
            "stream": stream_out(stream, now) if stream else None,
            "chat_log": chat_log_out(log) if log else None,
        },
    }
