"""个人助理应用的入站：一个应用一条长连接，进程内按人分发到各自的助理记录。

只接私聊：私聊消息、进入私聊、卡片回调按当事人的 user_id 找到他本人的助理记录，再走与普通
机器人相同的归一化与落库；群消息在归一化之前丢弃、不入库；被拉进群就退群并私信拉群的人。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.assistants import sealed
from coreman.core.assistants.routing import assistant_for
from coreman.core.chat.identity import resolve_speaker
from coreman.core.chat.reachability import private_chat_of
from coreman.core.crypto import Cipher
from coreman.core.db.models import FeishuDelivery, OutboxItem
from coreman.core.i18n.messages import msg
from coreman.core.logging import get_logger
from coreman.core.platforms.feishu import FeishuClient, FeishuError
from coreman.runtime.gateway_feishu.child import BotInfo
from coreman.runtime.gateway_feishu.inbound import normalize_event
from coreman.runtime.gateway_feishu.reply_buttons import Outcome, admit

log = get_logger(__name__)


@dataclass(frozen=True)
class Triage:
    """一个事件该怎么处理：ignore 丢弃；leave 退群；private 按 `platform_user_id` 找助理记录。"""

    kind: Literal["ignore", "leave", "private"]
    platform_user_id: str = ""
    chat_id: str = ""
    event_id: str = ""
    # 卡片回调：还要确认卡片所在的会话就是这个人与自己助理的私聊。
    click: bool = False
    reason: str = ""


_IGNORE = Triage("ignore")


def _obj(container: object, key: str) -> dict[str, Any]:
    value = container.get(key) if isinstance(container, dict) else None
    return value if isinstance(value, dict) else {}


def triage(raw: dict[str, Any], *, app_id: str) -> Triage:
    header, event = _obj(raw, "header"), _obj(raw, "event")
    if header.get("app_id") != app_id:
        return _IGNORE
    event_type = header.get("event_type")
    if event_type == "im.chat.member.bot.added_v1":
        chat_id = str(event.get("chat_id") or "")
        if not chat_id:
            return _IGNORE
        return Triage(
            "leave",
            platform_user_id=str(_obj(event, "operator_id").get("user_id") or ""),
            chat_id=chat_id,
            event_id=str(header.get("event_id") or ""),
        )
    if event_type == "im.message.receive_v1":
        if _obj(event, "message").get("chat_type") != "p2p":
            return Triage("ignore", reason="group_message")
        sender = _obj(event, "sender")
        user_id = str(_obj(sender, "sender_id").get("user_id") or "")
        if sender.get("sender_type") != "user" or not user_id:
            return Triage("ignore", reason="no_sender")
        return Triage("private", platform_user_id=user_id)
    if event_type == "im.chat.access_event.bot_p2p_chat_entered_v1":
        user_id = str(_obj(event, "operator_id").get("user_id") or "")
        return Triage("private", platform_user_id=user_id) if user_id else _IGNORE
    if event_type == "card.action.trigger":
        user_id = str(_obj(event, "operator").get("user_id") or "")
        return Triage("private", platform_user_id=user_id, click=True) if user_id else _IGNORE
    return _IGNORE


async def sent_by(session: AsyncSession, bot_id: uuid.UUID, message_id: object) -> bool:
    """这张卡片是不是这条助理记录自己发出去的（流式回复的主卡，或一条已发的出站消息）。

    助理记录只在所有者的私聊里说话，所以「本人的助理发出的卡片」就在本人与助理的私聊里。
    所有者没给助理发过私聊时没有私聊记录可查（比如助理主动发来的卡片），靠它认。
    只按归属与平台消息 id 查，不读内容。
    """
    if not isinstance(message_id, str) or not message_id:
        return False
    streamed = select(FeishuDelivery.task_id).where(
        FeishuDelivery.bot_id == bot_id, FeishuDelivery.message_id == message_id
    )
    queued = select(OutboxItem.id).where(
        OutboxItem.bot_id == bot_id,
        OutboxItem.status == "sent",
        OutboxItem.payload["_feishu_message_id"].astext == message_id,
    )
    return bool(await session.scalar(select(or_(streamed.exists(), queued.exists()))))


class AssistantInbound:
    """一个助理应用连接的入站处理；`accept` 交给 `DurableChannel`，落库提交之后才应答。"""

    def __init__(
        self,
        *,
        factory: async_sessionmaker[AsyncSession],
        client: FeishuClient,
        app: uuid.UUID,
        feishu_app_id: str,
        instance_id: str,
        generation: int,
        cipher: Cipher,
        fence: Callable[[], Awaitable[None]],
    ) -> None:
        self.factory, self.client = factory, client
        # app 是 platform_apps.id；feishu_app_id 是飞书的 App ID（事件头里的 app_id）。
        self.app, self.feishu_app_id = app, feishu_app_id
        self.instance_id, self.generation = instance_id, generation
        self.cipher = cipher
        self._fence = fence
        self._tasks: set[asyncio.Task[None]] = set()

    async def accept(self, raw: dict[str, Any]) -> Outcome | None:
        await self._fence()
        found = triage(raw, app_id=self.feishu_app_id)
        if found.kind == "ignore":
            if found.reason:
                log.info("assistant_event_dropped", reason=found.reason)
            return None
        if found.kind == "leave":
            # 退群与私信是两次平台调用，不占应答窗口。
            self._background(
                self.leave_group(found.chat_id, found.platform_user_id, found.event_id)
            )
            return None
        async with self.factory() as session:
            await session.execute(text("SET LOCAL statement_timeout = 1500"))
            speaker = await resolve_speaker(
                session, platform="feishu", platform_user_id=found.platform_user_id
            )
            bot = (
                None
                if speaker.user_id is None
                else await assistant_for(session, self.app, speaker.user_id)
            )
            if bot is None:
                await self.on_unprovisioned(
                    session,
                    platform_user_id=found.platform_user_id,
                    user_id=speaker.user_id,
                    raw=raw,
                )
                await session.commit()
                return None
            # 之后经手的是这个人的私聊内容：这段处理里的日志不带异常文字。
            await sealed.guard_logs(session, bot.id)
            message = normalize_event(
                raw,
                bot_id=bot.id,
                app_id=self.feishu_app_id,
                bot_open_id="",
                gateway_instance=self.instance_id,
                now=datetime.now(UTC),
                cipher=self.cipher,
            )
            if message is None:
                return None
            if found.click and not (
                await private_chat_of(
                    session, bot.id, "feishu", found.platform_user_id, message.chat_id
                )
                or await sent_by(session, bot.id, message.reply_context.get("message_id"))
            ):
                # 卡片不在操作人与自己助理的私聊里：不是所有者在点。
                return "not_requester"
            info = BotInfo(bot.id, bot.bot_key, bot.welcome_message)
            # 落库走与普通机器人相同的入口：助理的入站事件与任务载荷在那里按所有者封存。
            # 到这里的事件都在所有者与助理的私聊里，回复按钮的会话类型不必再查私聊记录。
            outcome = await admit(
                session, info, message, lease_generation=self.generation, single=True
            )
            await session.commit()
            return outcome

    async def on_unprovisioned(
        self,
        session: AsyncSession,
        *,
        platform_user_id: str,
        user_id: uuid.UUID | None,
        raw: dict[str, Any],
    ) -> None:
        """发送人没有启用的助理记录。本期只记日志并丢弃；开通卡片由开通与停用那一期接在这里。

        `user_id` 为 None 表示通讯录里认不出这个人。调用点在应答窗口内，返回后由调用方提交。
        """
        header = raw.get("header")
        log.info(
            "assistant_unprovisioned",
            app=str(self.app),
            known_user=user_id is not None,
            event_type=header.get("event_type") if isinstance(header, dict) else None,
        )

    async def leave_group(self, chat_id: str, platform_user_id: str, event_id: str) -> None:
        """被拉进群：把自己移出去，退成功了再私信拉群的人说明只支持私聊。

        说明里写着「已经退出刚才的群」，所以退群失败时不发，只记一笔（不带内容）。
        """
        try:
            await self.client.leave_chat(chat_id)
        except FeishuError as exc:
            log.warning("assistant_leave_failed", code=exc.code)
            return
        if not platform_user_id:
            return
        try:
            await self.client.notify_user(
                platform_user_id,
                msg("assistant_private_only"),
                key=f"assistant-leave:{event_id or chat_id}",
            )
        except FeishuError as exc:
            log.info("assistant_leave_notice_failed", code=exc.code)

    def _background(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def aclose(self) -> None:
        """退出前等后台的平台调用做完，最多 5 秒；没做完的取消。"""
        if not self._tasks:
            return
        _, pending = await asyncio.wait(set(self._tasks), timeout=5)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
