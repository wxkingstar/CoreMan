"""消息列表里的提醒：长任务和等本人确认的事项，不打开会话也看得到。

- 应用消息流卡片（`im/v2/app_feed_card`，权限 `im:app_feed_card:write`）：只给群聊建，一个群一张
  （`feishu_open.chat_biz`）。回复跑满 1 分钟是「进行中」，结束时删掉；正常做完再改成「已完成」
  并提醒一次（代替往会话里另发一条完成消息，见 worker 的 `_notify_long_task`）。点开定位到那一轮
  的回复并删掉卡片，没点开的 24 小时后删。等本人点的确认卡建一张「待确认」卡片，点过（卡片被
  更新）后删掉。批量接口整体成功也可能逐张失败，看 failed_cards。
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
# 「已完成」卡片没点开时保留这么久，之后删掉。
DONE_SECONDS = 24 * 60 * 60
# 置顶最多保留这么久（本人没来看也取消）。
PIN_SECONDS = 6 * 60 * 60
# 批量更新接口 update_fields 的编号：标题、预览、状态标签、跳转链接、展示时间与排序时间刷到当前、
# 提醒一次。
FIELD_TITLE, FIELD_PREVIEW, FIELD_STATUS, FIELD_LINK = "1", "3", "10", "12"
FIELD_SHOWN_AT, FIELD_SORTED_AT, FIELD_NOTIFY = "101", "102", "103"
# failed_cards 的原因：这张卡片还没建（实测返回数字 2）。
NOT_CREATED = "2"
TITLE_CHARS = 60
PREVIEW_CHARS = 120


def open_link(
    transport: FeishuTransport,
    chat_id: str | None,
    *,
    position: str | None = None,
    clear: tuple[int, str] | None = None,
) -> str:
    """点开进这个会话（有位置就定位到那条消息）；不知道会话时进和这个机器人的私聊。

    `clear` 是（任务号, 提问人）：点开时顺带删掉这张卡片。
    """
    cfg = get_settings()
    app_id = str(getattr(transport.client, "app_id", "") or "")
    return feishu_open.link(
        cfg.public_base_url,
        chat_id=chat_id,
        app_id=app_id,
        position=position,
        clear=(cfg.master_key_bytes, str(transport.bot_id), *clear) if clear else None,
    )


def task_biz(task_id: int) -> str:
    """升级前「进行中」卡片按任务建的编号：收尾时还要能删掉。"""
    return f"coreman-task-{task_id}"


def confirm_biz(key: str) -> str:
    return "coreman-confirm-" + hashlib.sha1(key.encode()).hexdigest()[:24]


def _clip(text: str, limit: int) -> str:
    # 飞书把消息流卡片的文字按 HTML 转义后原样显示（「&&」显示成「&amp;&amp;」）：换成全角。
    text = " ".join(text.split()).translate(_HTML_CHARS)
    return text if len(text) <= limit else text[: limit - 1] + "…"


_HTML_CHARS = str.maketrans({"&": "＆", "<": "＜", ">": "＞"})


def _failed(result: dict[str, Any]) -> list[Any]:
    """批量接口整体成功也可能逐张失败（无权限、未创建、重复……），原因在 failed_cards 里。"""
    return list((result.get("data") or {}).get("failed_cards") or [])


async def create(
    transport: FeishuTransport,
    *,
    biz_id: str,
    user_id: str,
    title: str,
    preview: str,
    status: tuple[str, str],
    link: str,
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
        "link": {"link": link},
        "notify": {"close_notify": not notify},
    }
    try:
        result = await transport.call(
            "POST",
            FEED_PATH,
            params={"user_id_type": "user_id"},
            json={"app_feed_card": card, "user_ids": [user_id]},
        )
    except FeishuError as exc:
        log.info("feishu_feed_card_failed", code=exc.code, action="create")
        return False
    if failed := _failed(result):
        log.info("feishu_feed_card_failed", reason=failed[0].get("reason"), action="create")
        return False
    return True


async def upsert(
    transport: FeishuTransport,
    *,
    biz_id: str,
    user_id: str,
    title: str,
    preview: str,
    status: tuple[str, str],
    link: str,
    notify: bool = False,
) -> bool:
    """有这张卡片就整张改写并挪到最前（`notify` 时提醒一次），没有就新建。"""
    fields = [FIELD_TITLE, FIELD_PREVIEW, FIELD_STATUS, FIELD_LINK, FIELD_SHOWN_AT, FIELD_SORTED_AT]
    if notify:
        fields.append(FIELD_NOTIFY)
    card = {
        "biz_id": biz_id,
        "title": _clip(title, TITLE_CHARS),
        "preview": _clip(preview, PREVIEW_CHARS),
        "status_label": {"text": status[0], "type": status[1]},
        "link": {"link": link},
    }
    try:
        result = await transport.call(
            "PUT",
            f"{FEED_PATH}/batch",
            params={"user_id_type": "user_id"},
            json={
                "feed_cards": [{"app_feed_card": card, "user_id": user_id, "update_fields": fields}]
            },
        )
    except FeishuError as exc:
        log.info("feishu_feed_card_failed", code=exc.code, action="upsert")
        return False
    failed = _failed(result)
    if not failed:
        return True
    if str(failed[0].get("reason")) not in (NOT_CREATED, "NOT_CREATED"):
        log.info("feishu_feed_card_failed", reason=failed[0].get("reason"), action="upsert")
        return False
    return await create(
        transport,
        biz_id=biz_id,
        user_id=user_id,
        title=title,
        preview=preview,
        status=status,
        link=link,
        notify=notify,
    )


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
