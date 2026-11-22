"""消息列表里的提醒：长任务和等本人确认的事项，不打开会话也看得到。

- 应用消息流卡片（`im/v2/app_feed_card`，权限 `im:app_feed_card:write`）：跑满 1 分钟的对话回复
  建一张「进行中」卡片，结束时删掉；正常做完的再建一张「已完成」卡片（会提醒，代替往会话里另发一条
  完成消息，见 worker 的 `_notify_long_task`），24 小时后删掉；等本人点的确认卡建一张「待确认」
  卡片，点过（卡片被更新）后删掉。卡片点开经平台跳转进对应会话（core/feishu_open）。
- 机器人单聊即时提醒（`im/v2/feed_cards/bot_time_sentive`，权限
  `im:datasync.feed_card.time_sensitive:write`）：私聊里的长任务做完，把这个会话在消息列表临时置顶；
  本人下一次发消息、或 6 小时后取消置顶。不加急（用户决定）。

都是锦上添花：缺权限、接口报错只记一笔，不影响回复本身。
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from coreman.core import feishu_open
from coreman.core.config import get_settings
from coreman.core.logging import get_logger
from coreman.core.platforms.feishu import FeishuError

if TYPE_CHECKING:
    from coreman.runtime.gateway_feishu.transport import FeishuTransport

log = get_logger(__name__)

FEED_PATH = "/open-apis/im/v2/app_feed_card"
PIN_PATH = "/open-apis/im/v2/feed_cards/bot_time_sentive"
# 对话回复跑满这么久才算长任务：建「进行中」卡片、做完置顶（和 worker 的完成提醒同一门槛）。
LONG_TASK_SECONDS = 60
# 「已完成」卡片保留这么久，之后删掉，免得消息列表越积越多。
DONE_SECONDS = 24 * 60 * 60
# 置顶最多保留这么久（本人没来看也取消）。
PIN_SECONDS = 6 * 60 * 60
# 批量更新接口 update_fields 里「预览」的编号。
FIELD_PREVIEW = "3"
TITLE_CHARS = 60
PREVIEW_CHARS = 120


def open_link(transport: FeishuTransport, chat_id: str | None) -> str:
    """点开进这个会话；不知道会话时进和这个机器人的私聊。"""
    app_id = str(getattr(transport.client, "app_id", "") or "")
    return feishu_open.link(get_settings().public_base_url, chat_id=chat_id, app_id=app_id)


def task_biz(task_id: int) -> str:
    return f"coreman-task-{task_id}"


def done_biz(task_id: int) -> str:
    return f"coreman-task-{task_id}-done"


def confirm_biz(key: str) -> str:
    return "coreman-confirm-" + hashlib.sha1(key.encode()).hexdigest()[:24]


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def create(
    transport: FeishuTransport,
    *,
    biz_id: str,
    user_id: str,
    title: str,
    preview: str,
    status: tuple[str, str],
    chat_id: str | None,
    notify: bool = False,
) -> bool:
    """建一张应用消息流卡片；`status` 是（标签文字, primary|secondary|success|danger）。

    `notify` 为真时像新消息一样提醒本人，否则静默出现在消息列表里。
    """
    card: dict[str, Any] = {
        "biz_id": biz_id,
        "title": _clip(title, TITLE_CHARS),
        "preview": _clip(preview, PREVIEW_CHARS),
        "status_label": {"text": status[0], "type": status[1]},
        "link": {"link": open_link(transport, chat_id)},
        "notify": {"close_notify": not notify},
    }
    try:
        await transport.call(
            "POST",
            FEED_PATH,
            params={"user_id_type": "user_id"},
            json={"app_feed_card": card, "user_ids": [user_id]},
        )
    except FeishuError as exc:
        log.info("feishu_feed_card_failed", code=exc.code, action="create")
        return False
    return True


async def update_preview(
    transport: FeishuTransport, *, biz_id: str, user_id: str, preview: str
) -> None:
    try:
        await transport.call(
            "PUT",
            f"{FEED_PATH}/batch",
            params={"user_id_type": "user_id"},
            json={
                "feed_cards": [
                    {
                        "app_feed_card": {
                            "biz_id": biz_id,
                            "preview": _clip(preview, PREVIEW_CHARS),
                        },
                        "user_id": user_id,
                        "update_fields": [FIELD_PREVIEW],
                    }
                ]
            },
        )
    except FeishuError as exc:
        log.info("feishu_feed_card_failed", code=exc.code, action="update")


async def delete(transport: FeishuTransport, *, biz_id: str, user_id: str) -> None:
    try:
        await transport.call(
            "DELETE",
            f"{FEED_PATH}/batch",
            params={"user_id_type": "user_id"},
            json={"feed_cards": [{"biz_id": biz_id, "user_id": user_id}]},
        )
    except FeishuError as exc:
        log.info("feishu_feed_card_failed", code=exc.code, action="delete")


async def pin(transport: FeishuTransport, user_id: str, on: bool) -> bool:
    """把本人和这个机器人的私聊在消息列表临时置顶（或取消）。"""
    try:
        await transport.call(
            "PATCH",
            PIN_PATH,
            params={"user_id_type": "user_id"},
            json={"time_sensitive": on, "user_ids": [user_id]},
        )
    except FeishuError as exc:
        log.info("feishu_pin_failed", code=exc.code, on=on)
        return False
    return True


def confirm_card(card: Any) -> str | None:
    """这张卡是不是「等发起人确认」的写操作 / 定时任务确认卡；是就返回卡片标题（可能为空串）。"""
    from coreman.runtime.gateway_feishu.reply_buttons import SCHEDULE_EVENT, WRITE_CARD_EVENTS

    if not isinstance(card, dict):
        return None
    found = False
    stack: list[Any] = [card]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for behavior in node.get("behaviors") or []:
                value = behavior.get("value") if isinstance(behavior, dict) else None
                if (
                    isinstance(value, dict)
                    and value.get("event_key") in WRITE_CARD_EVENTS | {SCHEDULE_EVENT}
                    and str(value.get("level") or "").startswith("confirm")
                ):
                    found = True
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    if not found:
        return None
    header = card.get("header") if isinstance(card.get("header"), dict) else {}
    title = (header.get("title") or {}).get("content") if isinstance(header, dict) else ""
    return str(title or "")
