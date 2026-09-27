"""回复按钮：提问人点一下，就把按钮上写的字作为他的下一句话发给机器人。

约定（渲染器生成、网关解析，两边共用这里）：

- 发出去的就是按钮上看得见的文字，没有隐藏内容：块里 `reply` 只是「这是回复按钮」的标记；
- 只有提问人能点：回调 value 里写着这一轮提问人的飞书 user_id，别人点了网关直接拒绝。
  value 由渲染器生成，raw 块里的回调在裁剪时去掉，模型伪造不了；
- value 还带按钮行与行内回复按钮的 element_id，这一轮真正开始后据此把这一行置灰；
- 私聊还是群不写进 value，由服务端按真实私聊记录判定（见 `coreman/core/chat/card_replies`）。

卡片是共享的（2.0 只有共享卡片），置灰和「已选择」群里所有人都能看到。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from coreman.core.feishu_cards import style
from coreman.core.feishu_cards.sanitize import valid_element_id

ACTION = "reply"
# 与块模型的按钮文字上限一致：超过的不是我们渲染的，按畸形丢掉。
MAX_LABEL_CHARS = 40
MAX_BUTTONS = 6
USED_TIPS = "这组按钮已经用过了"
USER_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")

ChatType = Literal["single", "group"]


@dataclass(frozen=True)
class ReplyClick:
    """回调 value 里的一次回复按钮点击。"""

    text: str
    requester: str
    row: str | None
    buttons: tuple[str, ...]


def reply_value(text: str, *, requester: str, row: str, buttons: list[str]) -> dict[str, Any]:
    """按钮的回调 value：要发的话就是按钮文字本身。"""
    return {
        "action": ACTION,
        "text": text,
        "requester": requester,
        "row": row,
        "buttons": list(buttons),
    }


def _ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > MAX_BUTTONS:
        return ()
    return tuple(v for v in value if isinstance(v, str) and valid_element_id(v))


def parse_reply(value: Any) -> ReplyClick | None:
    """回调 value → ReplyClick；不是回复按钮或字段不合约定返回 None。"""
    if not isinstance(value, dict) or value.get("action") != ACTION:
        return None
    text, requester = value.get("text"), value.get("requester")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_LABEL_CHARS:
        return None
    if not isinstance(requester, str) or not USER_ID.fullmatch(requester):
        return None
    row = value.get("row")
    return ReplyClick(
        text=text.strip(),
        requester=requester,
        row=row if isinstance(row, str) and valid_element_id(row) else None,
        buttons=_ids(value.get("buttons")),
    )


@dataclass(frozen=True)
class UsedRow:
    """点过的一行：置灰要用的全部信息，从入站经 worker 到出站一路带过去。"""

    row: str
    buttons: tuple[str, ...]
    label: str
    chat_type: ChatType

    def dump(self) -> dict[str, Any]:
        return {
            "row": self.row,
            "buttons": list(self.buttons),
            "label": self.label,
            "chat_type": self.chat_type,
        }

    @classmethod
    def parse(cls, value: Any) -> UsedRow | None:
        if not isinstance(value, dict):
            return None
        row, label = value.get("row"), value.get("label")
        if not isinstance(row, str) or not valid_element_id(row):
            return None
        if not isinstance(label, str) or not label.strip() or len(label) > MAX_LABEL_CHARS:
            return None
        chat: ChatType = "single" if value.get("chat_type") == "single" else "group"
        return cls(row, _ids(value.get("buttons")), label.strip(), chat)


def note_id(row: str) -> str | None:
    """「已选择」那一行的 element_id：跟着按钮行走，重放时撞 ID 就知道已经加过了。"""
    candidate = f"{row}_used"
    return candidate if valid_element_id(candidate) else None


def used_note(used: UsedRow, user_id: str) -> dict[str, Any] | None:
    """按钮行下面的灰色小字：群里写明是谁点的（人员标签只展示、不通知），私聊只写选了什么。"""
    element_id = note_id(used.row)
    if element_id is None:
        return None
    label = used.label.replace("<", "&#60;").replace("\r", "").replace("\n", " ")
    content = f"<font color='grey'>已选择：{label}</font>"
    if used.chat_type == "group" and USER_ID.fullmatch(user_id):
        content = f"<person id='{user_id}' show_avatar=false></person> {content}"
    node: dict[str, Any] = {
        "tag": "markdown",
        "element_id": element_id,
        "content": content,
        "text_size": style.TEXT_NOTE,
    }
    icon = style.icon("done_outlined", "grey")
    if icon:
        node["icon"] = icon
    return node


def _disabled() -> dict[str, Any]:
    return {"disabled": True, "disabled_tips": {"tag": "plain_text", "content": USED_TIPS}}


def batch_actions(used: UsedRow, user_id: str) -> list[dict[str, Any]]:
    """CardKit batch_update 的动作：行内回复按钮全部置灰，行下加一行「已选择」。

    链接按钮不动，点过回复之后链接照样能打开。
    """
    actions: list[dict[str, Any]] = [
        {
            "action": "partial_update_element",
            "params": {"element_id": button, "partial_element": _disabled()},
        }
        for button in used.buttons
    ]
    note = used_note(used, user_id)
    if note is not None:
        actions.append(
            {
                "action": "add_elements",
                "params": {
                    "type": "insert_after",
                    "target_element_id": used.row,
                    "elements": [note],
                },
            }
        )
    return actions


def mark_card(card: dict[str, Any], used: UsedRow, user_id: str) -> bool:
    """在一张卡片 JSON 上原地做同样的标记（续卡没有卡片实体，只能整卡 PATCH）。

    找不到按钮行返回 False；已经标记过的不重复加「已选择」。
    """
    found = _find_parent((card.get("body") or {}).get("elements"), used.row)
    if found is None:
        return False
    siblings, index = found
    _disable(siblings[index], set(used.buttons))
    note = used_note(used, user_id)
    if note is not None and not any(
        isinstance(s, dict) and s.get("element_id") == note["element_id"] for s in siblings
    ):
        siblings.insert(index + 1, note)
    return True


def _find_parent(elements: Any, element_id: str) -> tuple[list[Any], int] | None:
    if not isinstance(elements, list):
        return None
    for index, node in enumerate(elements):
        if not isinstance(node, dict):
            continue
        if node.get("element_id") == element_id:
            return elements, index
        for key in ("elements", "columns"):
            found = _find_parent(node.get(key), element_id)
            if found is not None:
                return found
    return None


def _disable(node: Any, wanted: set[str]) -> None:
    if isinstance(node, dict):
        if node.get("tag") == "button" and node.get("element_id") in wanted:
            node.update(_disabled())
        for value in node.values():
            _disable(value, wanted)
    elif isinstance(node, list):
        for item in node:
            _disable(item, wanted)
