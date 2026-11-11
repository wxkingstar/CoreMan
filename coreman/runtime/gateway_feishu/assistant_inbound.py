"""个人助理应用的入站：一个应用一条长连接，进程内按人分发到各自的助理记录。

只接私聊：私聊消息、进入私聊、卡片回调按当事人的 user_id 找到他本人的助理记录，再走与普通
机器人相同的归一化与落库；群消息在归一化之前丢弃、不入库；被拉进群就退群并私信拉群的人。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


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
