"""员工在私聊里引用 API 任务推来的结果回复：把原任务带进当前会话，接着处理。

业务系统发起的任务跑在独立会话里，不在员工和 AI 员工私聊的上下文中。员工引用那条结果
（或「在某系统里发起」那句）回复时，这一轮带上原任务的请求、图片、回复和写操作记录；同一个
运行时会话里已经带过的不再重复，会话换新后再引用会重新带。只认发起人本人在他自己的私聊里引用。
见 docs/superpowers/specs/2026-11-24-api-tasks-system-clients.md 第 7 节。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.assistants.approvals.attachments import Store
from coreman.core.db.models import (
    ApiClient,
    ApiTask,
    ApiTaskUpload,
    ChatLog,
    ChatSession,
    FeishuDelivery,
    OutboxItem,
    Task,
)
from coreman.core.wecom.media import Media

REPLY_MAX = 6000
_OUTBOX_KEY = re.compile(r"^api-task:([0-9a-f-]{36}):")
_SESSION_KEY = re.compile(r"^api:([0-9a-f-]{36})$")


@dataclass(frozen=True)
class Recalled:
    """要带进这一轮的原任务：一段说明文字，和直接给模型看的图片 part。"""

    text: str
    parts: list[dict[str, Any]]


async def _task_of(session: AsyncSession, bot_id: uuid.UUID, parent_id: str) -> uuid.UUID | None:
    """被引用的消息是哪个 API 任务的：结果卡片（流式投递）或触发、拒绝等出站消息。"""
    key = await session.scalar(
        select(Task.session_key)
        .join(FeishuDelivery, FeishuDelivery.task_id == Task.id)
        .where(FeishuDelivery.message_id == parent_id, Task.bot_id == bot_id)
    )
    if key is not None:
        match = _SESSION_KEY.match(key)
        return uuid.UUID(match.group(1)) if match else None
    keys = await session.scalars(
        select(OutboxItem.dedupe_key).where(
            OutboxItem.bot_id == bot_id,
            OutboxItem.payload["_feishu_message_id"].astext == parent_id,
        )
    )
    for key in keys:
        match = _OUTBOX_KEY.match(key or "")
        if match:
            return uuid.UUID(match.group(1))
    return None


def _loaded(row: ChatSession | None, relay_session_id: uuid.UUID) -> list[str]:
    state = (row.api_tasks_loaded if row is not None else None) or {}
    if state.get("relay_session_id") != str(relay_session_id):
        return []
    return [str(item) for item in state.get("ids") or []]


async def _replies(session: AsyncSession, bot_id: uuid.UUID, key: str) -> list[str]:
    """原任务会话里第一轮和最后一轮的回复（续接、重跑之后最后一轮可能不同）。"""
    from coreman.core.api_tasks.service import clean_reply

    found: list[str] = []
    for order in (ChatLog.id.asc(), ChatLog.id.desc()):
        log = await session.scalar(
            select(ChatLog)
            .where(
                ChatLog.bot_id == bot_id,
                ChatLog.session_key == key,
                ChatLog.status != "running",
            )
            .order_by(order)
            .limit(1)
        )
        text = clean_reply(log.response_content) if log is not None else None
        if text and text not in found:
            found.append(text[:REPLY_MAX] + ("…（已截断）" if len(text) > REPLY_MAX else ""))
    return found


async def recall(
    session: AsyncSession,
    *,
    bot_id: uuid.UUID,
    chat_id: str,
    user_id: uuid.UUID | None,
    parent_id: str,
    session_key: str,
    relay_session_id: uuid.UUID,
    cipher: Any,
    base_url: str,
    now: datetime,
    store: Store | None = None,
) -> Recalled | None:
    """被引用的是本人的 API 任务、而且这个会话里还没带过：返回要带进来的内容，并记下已带过。"""
    from coreman.core.api_tasks import uploads, writes
    from coreman.core.api_tasks.service import client_session_key

    if user_id is None:
        return None
    api_task_id = await _task_of(session, bot_id, parent_id)
    if api_task_id is None:
        return None
    row = await session.get(ApiTask, api_task_id)
    if (
        row is None
        or row.client_key is None
        or row.bot_id != bot_id
        or row.user_id != user_id
        or row.chat_id != chat_id
    ):
        return None
    current = await session.get(ChatSession, (bot_id, session_key), with_for_update=True)
    loaded = _loaded(current, relay_session_id)
    if str(row.id) in loaded:
        return None
    client = await session.get(ApiClient, row.client_key)
    system = client.name if client is not None else row.client_key
    lines = [
        f"- 引用的是在业务系统「{system}」里发起、经 API 交给你的一条任务"
        "（当时在独立会话里处理，不在这个对话的上下文中）。下面是原任务的内容，供你接着处理：",
        "任务内容：",
        row.prompt,
    ]
    present = {
        upload_id
        for upload_id in await session.scalars(
            select(ApiTaskUpload.id).where(ApiTaskUpload.id.in_(row.image_ids or []))
        )
    }
    images = [i for i in row.image_ids or [] if i in present]
    parts = [
        {"type": "image", "ref": {"message_id": f"api:{row.id}", "file_key": str(identity)}}
        for identity in images[: uploads.INLINE]
    ]
    if row.image_ids:
        lines.append(f"图片：原任务共 {len(row.image_ids)} 张。")
        if len(present) < len(row.image_ids):
            lines.append(f"其中 {len(row.image_ids) - len(present)} 张已过保留期，看不到了。")
        if parts:
            lines.append(f"前 {len(parts)} 张附在这条消息里。")
        rest = images[uploads.INLINE :]
        if rest:
            urls, ttl = await uploads.links(
                session, rest, store=store, cipher=cipher, base_url=base_url, now=now
            )
            lines.append(f"其余的用下面的链接下载到工作目录 api-inputs/ 下查看（{ttl}内有效）：")
            lines += [f"{index}. {url}" for index, url in enumerate(urls, uploads.INLINE + 1)]
    replies = await _replies(session, bot_id, client_session_key(row.id))
    if replies:
        lines.append("原任务的回复：" if len(replies) == 1 else "原任务第一轮和最后一轮的回复：")
        lines += replies
    done = await writes.view(session, row)
    if done:
        lines.append("原任务提出的业务系统写操作：")
        lines += [f"{w['system']}.{w['operation']}：{w['status']}" for w in done]
    if current is not None:
        current.api_tasks_loaded = {
            "relay_session_id": str(relay_session_id),
            "ids": [*loaded, str(row.id)],
        }
    return Recalled("\n".join(lines), parts)


class RecallFetcher:
    """引用回复里带进来的原任务图片从上传记录取，其余照旧交给平台的下载器。"""

    def __init__(self, inner: Any, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.inner = inner
        self.session_factory = session_factory

    def _own(self, ref: dict[str, Any]) -> str | None:
        message_id = str(ref.get("message_id") or "")
        return message_id.removeprefix("api:") if message_id.startswith("api:") else None

    async def fetch_image(self, ref: dict[str, Any]) -> Media:
        own = self._own(ref)
        if own is None:
            return await self.inner.fetch_image(ref)  # type: ignore[no-any-return]
        from coreman.core.api_tasks.uploads import UploadFetcher

        return await UploadFetcher(self.session_factory, own).fetch_image(ref)

    async def fetch_file(self, ref: dict[str, Any], *, filename: str | None) -> Media:
        return await self.inner.fetch_file(ref, filename=filename)  # type: ignore[no-any-return]

    async def aclose(self) -> None:
        await self.inner.aclose()
