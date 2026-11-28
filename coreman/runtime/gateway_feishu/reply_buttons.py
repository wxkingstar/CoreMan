"""网关这一侧的回复按钮：应答回调前的判定，以及这一轮开始后把按钮行置灰。

应答回调（3 秒窗口）只做两件判定再落库：点击人不是提问人就拒绝；会话类型按真实私聊记录
改判（点击人与本机器人的私聊记录正是这个会话才算私聊，否则按群）。置灰由 worker 在这一轮
真正开始时排出站，这里的出站循环执行：

- 流式主卡是卡片实体：按 message_id 找到投递记录拿 card_id，用 batch_update 局部更新，序号走
  `transport.sequence()`，与这张卡之前的所有操作共用一个严格递增的序号空间。按钮只出现在终稿
  里，终稿之前已经关了流式，不会撞上「流式期间回调不能更新卡片」。投递记录跟着流在完成 1 小时
  后清理，之后的点击照常发出消息，只是不再置灰；
- 续卡是普通卡片消息：出站记录里存着卡片 JSON，改好后整卡 PATCH，并把改过的卡存回去，
  同一张卡的另一行再被点时不会把前一行改回来。
"""

from __future__ import annotations

import copy
import time
import uuid
from typing import TYPE_CHECKING, Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import tasks
from coreman.core.chat import busy_choice, stream_stop
from coreman.core.chat.card_replies import REPLY_BUTTON, clicked
from coreman.core.chat.reachability import private_chat_of
from coreman.core.db.models import (
    FeishuDelivery,
    InboundEvent,
    InteractionState,
    OutboxItem,
    Task,
    TaskStream,
)
from coreman.core.feishu_cards.reply_buttons import (
    UsedRow,
    batch_actions,
    expire_actions,
    expire_card,
    mark_card,
)
from coreman.core.logging import get_logger
from coreman.core.platforms.feishu import FeishuError
from coreman.core.systems_catalog import member_writes
from coreman.core.wecom.messages import InboundMessage
from coreman.runtime.gateway_common.inbound import InboundBot, enqueue_inbound

if TYPE_CHECKING:
    from coreman.runtime.gateway_feishu.transport import FeishuTransport

log = get_logger(__name__)
# 点击的结果，决定回调应答里的 toast。
Outcome = Literal[
    "queued",
    "duplicate",
    "not_requester",
    # 发在群里的任务提问卡：点的人不是被问的人。
    "not_asked",
    "busy_wait",
    "busy_stop",
    "busy_progress",
    "busy_cancel",
    "busy_resume",
    "busy_drop",
    "busy_bg_wait",
    "busy_bg_stop",
    # 个人助理的开通卡片（没有助理记录时，由 AssistantInbound.on_unprovisioned 处理）。
    "provision_started",
    "provision_expired",
    # 其余卡片按钮：只按回调 value 认出点了什么，结果由 worker 处理后更新卡片告知。
    "write_confirm",
    "write_cancel",
    "write_resend",
    "schedule_confirm",
    "schedule_cancel",
    "topic_new",
    "topic_fork",
    "cold_start_answer",
    "cold_start_skip",
    "assistant_stop",
    "gap_request_submit",
    "gap_request_drop",
    "credential_submit",
    "authorize_link",
    "choice_submit",
    "choice_other",
    "switch_runtime",
    "switch_wait",
    # 确认卡（写操作、定时任务）由发起人以外的人点了。
    "not_initiator",
    # 流式卡片上的「停止」：已收下、不是提问人、这条已经结束。
    "stream_stop",
    "stream_stop_denied",
    "stream_stop_late",
    # 停下的这一轮写在后台阶段的进程里：先弹确认（会连带停掉后台任务）。
    "live_stop_confirm",
    # 后台任务区块上的「停止」「停掉全部后台任务」：已收下、没有权限、已经结束。
    "background_stop",
    "background_stop_denied",
    "background_stop_late",
    # 建议追问超过有效期。
    "reply_expired",
]
_BUSY_KEYS = frozenset(
    {
        busy_choice.WAIT,
        busy_choice.STOP,
        busy_choice.PROGRESS,
        busy_choice.CANCEL,
        busy_choice.RESUME,
        busy_choice.DROP,
        busy_choice.BG_WAIT,
        busy_choice.BG_STOP,
    }
)


async def settle(
    session: AsyncSession,
    bot_id: uuid.UUID,
    message: InboundMessage,
    *,
    single: bool | None = None,
) -> bool:
    """应答前的判定：不是回复按钮点击或允许入队返回 True；提问人以外的人点击返回 False。

    会话类型在这里定下来：按钮 value 里没有它，也不信任回调里的任何说法。

    Args:
        single: 调用方已经确认过这是不是私聊（个人助理只有私聊）；不给就按私聊记录判断
    """
    meta = clicked(message.reply_context)
    if meta is None or message.kind != "message":
        return True
    if meta.get("requester") != message.sender.platform_user_id:
        return False
    if single is None:
        single = await private_chat_of(
            session, bot_id, "feishu", message.sender.platform_user_id, message.chat_id
        )
    chat_type: Literal["single", "group"] = "single" if single else "group"
    message.chat_type = chat_type
    message.reply_context = {**message.reply_context, "chat_type": chat_type}
    return True


def is_reply_click(message: InboundMessage) -> bool:
    return message.kind == "message" and REPLY_BUTTON in message.reply_context


def _busy_click(message: InboundMessage) -> Outcome | None:
    action = message.card_action or {}
    if (
        message.kind != "card_action"
        or busy_choice.parse_card_task_id(str(action.get("task_id") or "")) is None
    ):
        return None
    key = action.get("event_key")
    return cast("Outcome", key) if key in _BUSY_KEYS else None


# 卡片回调 value 里的 event_key。网关不引入这些卡片所在的模块（有的在 worker 里），
# 写成字面值；测试核对它们与各模块的常量一致。
WRITE_CARD_EVENTS = frozenset(
    {
        "feishu_group_write",
        "assistant_write",
        "cloud_write",
        "systems_write",
        "systems_batch",
        "member_write",
    }
)
SCHEDULE_EVENT = "personal_schedule"
# 任务平台的提问卡（core/work_tasks/cards.EVENT_KEY）。
WORK_TASK_ASK_EVENT = "work_task_ask"
TOPIC_EVENTS: dict[str, Outcome] = {"topic_new": "topic_new", "topic_fork": "topic_fork"}
COLD_START_EVENT = "assistant_cold_start_card"
STOP_EVENT = "assistant_stop"
GAP_REQUEST_EVENT = "assistant_gap_request"
GAP_REQUEST_VERBS: dict[str, Outcome] = {
    "submit": "gap_request_submit",
    "drop": "gap_request_drop",
}
AUTHORIZE_EVENTS = frozenset({"personal_authorize", "personal_upgrade"})
CHOICE_SUBMIT = "submit_choice"
CHOICE_OTHER = "opt_other"
SWITCH_SUBMIT = "ratelimit_switch_submit"
SWITCH_OPTIONS: dict[str, Outcome] = {"opt_switch": "switch_runtime", "opt_wait": "switch_wait"}
_WRITE_VERBS: dict[str, Outcome] = {"confirm": "write_confirm", "cancel": "write_cancel"}
_SCHEDULE_VERBS: dict[str, Outcome] = {"confirm": "schedule_confirm", "cancel": "schedule_cancel"}


def _not_asked(message: InboundMessage) -> bool:
    """任务提问卡带着能回答的人的名单（群里的卡才有），点的人不在里面。"""
    action = message.card_action or {}
    allowed = action.get("allowed")
    return (
        message.kind == "card_action"
        and action.get("event_key") == WORK_TASK_ASK_EVENT
        and isinstance(allowed, list)
        and bool(allowed)
        and message.sender.platform_user_id not in allowed
    )


def _card_click(message: InboundMessage) -> Outcome | None:
    """其余卡片按钮点的是什么，只看回调 value，不查库（3 秒应答窗口）。认不出返回 None。"""
    action = message.card_action or {}
    if message.kind != "card_action":
        return None
    if action.get("card_type") == "credential":
        return "credential_submit"
    key = str(action.get("event_key") or "")
    verb = str(action.get("level") or "").partition(":")[0]
    picked = {v for values in (action.get("selected") or {}).values() for v in values}
    if verb == "resend":
        return "write_resend"
    if key in WRITE_CARD_EVENTS:
        return _WRITE_VERBS.get(verb)
    if key == SCHEDULE_EVENT:
        return _SCHEDULE_VERBS.get(verb)
    if key in TOPIC_EVENTS:
        return TOPIC_EVENTS[key]
    if key == COLD_START_EVENT:
        # 令牌是「题.answer|skip.签名」。
        parts = str(action.get("level") or "").split(".")
        return "cold_start_skip" if parts[1:2] == ["skip"] else "cold_start_answer"
    if key == STOP_EVENT:
        return "assistant_stop"
    if key == GAP_REQUEST_EVENT:
        # 令牌是「submit|drop.到期.签名」。
        return GAP_REQUEST_VERBS.get(str(action.get("level") or "").partition(".")[0])
    if key in AUTHORIZE_EVENTS:
        return "authorize_link"
    if key == CHOICE_SUBMIT:
        return "choice_other" if CHOICE_OTHER in picked else "choice_submit"
    if key == SWITCH_SUBMIT:
        return next((SWITCH_OPTIONS[p] for p in picked if p in SWITCH_OPTIONS), None)
    return None


# 只有发起人能点的确认卡：按钮 value 的 task_id 是 `personal:<发起那条消息的任务>`；
# 成员本机发起的写操作是 `personal:member-write:<待确认记录>`，发起人记在那条记录里。
_INITIATOR_ONLY = frozenset(
    {"write_confirm", "write_cancel", "write_resend", "schedule_confirm", "schedule_cancel"}
)


async def _initiator_clicked(
    session: AsyncSession, bot_id: uuid.UUID, message: InboundMessage
) -> bool | None:
    """点的人是不是发起那条消息的人。只为选 toast：真正的校验在 worker，这里认不出返回 None。"""
    task_id = str((message.card_action or {}).get("task_id") or "")
    state_id = member_writes.parse_card_ref(task_id)
    if state_id is not None:
        state = await session.get(InteractionState, state_id)
        if state is None or state.bot_id != bot_id or state.kind != member_writes.KIND:
            return None
        owner = state.state.get("platform_user_id")
        if not owner:
            return None
        return bool(owner == message.sender.platform_user_id)
    kind, _, raw_id = task_id.partition(":")
    if kind != "personal" or not raw_id.isdigit():
        return None
    origin = await session.get(Task, int(raw_id))
    if origin is None or origin.bot_id != bot_id or origin.inbound_event_id is None:
        return None
    event = await session.get(InboundEvent, origin.inbound_event_id)
    if event is None or not event.sender_platform_user_id:
        return None
    return event.sender_platform_user_id == message.sender.platform_user_id


async def _stop_click(
    session: AsyncSession, bot_id: uuid.UUID, message: InboundMessage
) -> Outcome | None:
    """流式卡片上的「停止」：只为选 toast，真正的核对和停止在 worker。"""
    action = message.card_action or {}
    target_id = stream_stop.parse_card_task_id(str(action.get("task_id") or ""))
    if message.kind != "card_action" or target_id is None:
        return None
    target = await session.get(Task, target_id)
    if target is None or target.bot_id != bot_id:
        return None
    if str(target.payload.get("platform_user_id") or "") != message.sender.platform_user_id:
        return "stream_stop_denied"
    if target.status not in tasks.OPEN or target.cancel_requested_at is not None:
        return "stream_stop_late"
    if target.payload.get("live_join") and action.get("level") != stream_stop.CONFIRMED:
        from coreman.core.chat import background_runs
        from coreman.core.db.models import BackgroundRun

        run = await session.get(BackgroundRun, int(target.payload["live_join"]))
        if run is not None and run.state == "running" and background_runs.running(run.tasks or []):
            return "live_stop_confirm"
    return "stream_stop"


async def _background_click(
    session: AsyncSession, bot_id: uuid.UUID, message: InboundMessage
) -> Outcome | None:
    """后台任务区块上的按钮：只为选 toast，真正的核对和停止在 worker。"""
    from coreman.core.chat import background_runs
    from coreman.core.db.models import BackgroundRun

    action = message.card_action or {}
    run_id = background_runs.parse_card_task_id(str(action.get("task_id") or ""))
    if message.kind != "card_action" or run_id is None:
        return None
    run = await session.get(BackgroundRun, run_id)
    if run is None or run.bot_id != bot_id:
        return None
    if run.state != "running":
        return "background_stop_late"
    if not await background_runs.can_stop(session, run, message.sender.platform_user_id or ""):
        return "background_stop_denied"
    return "background_stop"


async def admit(
    session: AsyncSession,
    bot: InboundBot,
    message: InboundMessage,
    *,
    lease_generation: int,
    single: bool | None = None,
) -> Outcome | None:
    """应答窗口里的入站落库：回复按钮先判定提问人与会话类型。返回回复按钮点击的结果，
    其他卡片按钮的点击返回点的是哪类按钮（认不出为 None），其余事件返回 None。调用方负责提交。"""
    if not await settle(session, bot.id, message, single=single):
        return "not_requester"
    meta = clicked(message.reply_context) if message.kind == "message" else None
    expires = meta.get("expires") if meta else None
    if isinstance(expires, int) and time.time() > expires:
        # 建议追问过期了：不入队，告诉他直接发消息。
        return "reply_expired"
    if _not_asked(message):
        # 群里的任务提问卡只有被问的人（和代表人）能回答：别人点了不入队，卡片也不动。
        return "not_asked"
    task = await enqueue_inbound(session, bot, message, lease_generation=lease_generation)
    if not is_reply_click(message):
        outcome = (
            _busy_click(message)
            or _card_click(message)
            or await _stop_click(session, bot.id, message)
            or await _background_click(session, bot.id, message)
        )
        if outcome in _INITIATOR_ONLY:
            initiator = await _initiator_clicked(session, bot.id, message)
            if initiator is None:
                return None  # 认不出发起人：不说「已确认」，只回兜底的「已收到」
            if not initiator:
                return "not_initiator"
        return outcome
    return "queued" if task is not None else "duplicate"


async def mark_used(transport: FeishuTransport, item: OutboxItem) -> None:
    """出站循环里执行置灰。卡片不认改动（字段、ID 冲突）只记一笔，这是锦上添花，不重试。"""
    from coreman.runtime.gateway_feishu.transport import CARD_CONTENT_ERRORS, api_id, card_json

    used = UsedRow.parse(item.payload.get("_reply_used"))
    user = str(item.payload.get("_operator") or "")
    mid = api_id(item.target.get("message_id"))
    if used is None:
        return
    async with transport.factory() as session:
        delivery = await session.scalar(
            select(FeishuDelivery)
            .join(TaskStream, TaskStream.task_id == FeishuDelivery.task_id)
            .where(TaskStream.bot_id == transport.bot_id, FeishuDelivery.message_id == mid)
        )
    try:
        if delivery is not None:
            if delivery.card_id and delivery.is_static:
                card_id = api_id(delivery.card_id)
                await transport.call(
                    "POST",
                    f"/open-apis/cardkit/v1/cards/{card_id}/batch_update",
                    json={
                        "sequence": await transport.sequence(delivery.task_id),
                        "actions": card_json(batch_actions(used, user)),
                    },
                )
            return
        async with transport.factory() as session:
            sent = await session.scalar(
                select(OutboxItem)
                .where(
                    OutboxItem.bot_id == transport.bot_id,
                    OutboxItem.status == "sent",
                    OutboxItem.payload["_feishu_message_id"].astext == mid,
                )
                .order_by(OutboxItem.id.desc())
                .limit(1)
            )
            card = sent.payload.get("card") if sent is not None else None
            if sent is None or not isinstance(card, dict) or card.get("schema") != "2.0":
                return
            updated = copy.deepcopy(card)
            if not mark_card(updated, used, user):
                return
            await transport.call(
                "PATCH",
                f"/open-apis/im/v1/messages/{mid}",
                json={"content": card_json({k: v for k, v in updated.items() if k != "task_id"})},
            )
            sent.payload = {**sent.payload, "card": updated}
            await session.commit()
    except FeishuError as exc:
        if exc.code not in CARD_CONTENT_ERRORS:
            raise
        log.warning("feishu_reply_used_rejected", code=exc.code, item_id=item.id)


async def expire_followups(transport: FeishuTransport, item: OutboxItem) -> None:
    """建议追问到期：把那组按钮置灰。主卡按投递记录局部更新，续卡整卡 PATCH。

    续卡的消息 ID 到点才查（排这条时续卡还没发出）；续卡被拒改发了普通消息、投递记录已经
    清理时什么也不做。只是外观：过期由应答回调时按按钮里的时刻判定。
    """
    from coreman.runtime.gateway_feishu.transport import CARD_CONTENT_ERRORS, api_id, card_json

    raw = item.payload.get("_followups_expire")
    buttons = [b for b in raw if isinstance(b, str)] if isinstance(raw, list) else []
    if not buttons:
        return
    async with transport.factory() as session:
        mid = item.target.get("message_id")
        dedupe = item.target.get("outbox_dedupe")
        if not mid and isinstance(dedupe, str):
            sent = await session.scalar(
                select(OutboxItem).where(
                    OutboxItem.bot_id == transport.bot_id,
                    OutboxItem.dedupe_key == dedupe,
                    OutboxItem.status == "sent",
                )
            )
            mid = sent.payload.get("_feishu_message_id") if sent is not None else None
        if not isinstance(mid, str) or not mid:
            return
        mid = api_id(mid)
        delivery = await session.scalar(
            select(FeishuDelivery)
            .join(TaskStream, TaskStream.task_id == FeishuDelivery.task_id)
            .where(TaskStream.bot_id == transport.bot_id, FeishuDelivery.message_id == mid)
        )
    try:
        if delivery is not None:
            if delivery.card_id and delivery.is_static:
                await transport.call(
                    "POST",
                    f"/open-apis/cardkit/v1/cards/{api_id(delivery.card_id)}/batch_update",
                    json={
                        "sequence": await transport.sequence(delivery.task_id),
                        "actions": card_json(expire_actions(buttons)),
                    },
                )
            return
        async with transport.factory() as session:
            sent = await session.scalar(
                select(OutboxItem)
                .where(
                    OutboxItem.bot_id == transport.bot_id,
                    OutboxItem.status == "sent",
                    OutboxItem.payload["_feishu_message_id"].astext == mid,
                )
                .order_by(OutboxItem.id.desc())
                .limit(1)
            )
            card = sent.payload.get("card") if sent is not None else None
            if sent is None or not isinstance(card, dict) or card.get("schema") != "2.0":
                return
            updated = copy.deepcopy(card)
            if not expire_card(updated, buttons):
                return
            await transport.call(
                "PATCH",
                f"/open-apis/im/v1/messages/{mid}",
                json={"content": card_json({k: v for k, v in updated.items() if k != "task_id"})},
            )
            sent.payload = {**sent.payload, "card": updated}
            await session.commit()
    except FeishuError as exc:
        if exc.code not in CARD_CONTENT_ERRORS:
            raise
        log.warning("feishu_followups_expire_rejected", code=exc.code, item_id=item.id)
