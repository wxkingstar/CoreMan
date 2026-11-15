"""飞书消息内容 → 一段可读的文字（纯函数，轮询每条消息都会跑，必须便宜且永不抛错）。

飞书给的消息是 `{"msg_type": …, "body": {"content": "<JSON 字符串>"}}`。卡片（interactive）带
`card_msg_content_type=user_card_content` 读时是发送时的原始卡片 JSON（1.0 的 `elements`、2.0 的
`body.elements`），不带时是只有占位文字的降级骨架：
`{"title", "elements": [[{tag, text|image_key}]]}`。
"""

from __future__ import annotations

import json
from typing import Any

LIMIT = 4000
CARD_PREFIX = "[卡片] "
CARD_EMPTY = "[卡片消息，没有可读的文字]"
CARD_IMAGES_NOTE = "卡片里的图无法下载"
# 结构上的上限：再大的卡片也只走这么多层、这么多个节点。
_DEPTH = 24
_NODES = 2000
# 这些键下面是配置或提示，不是正文。
_SKIP_KEYS = frozenset({"config", "card_link", "alt", "hover_tips", "tooltip", "behaviors"})
_CONTENT_TAGS = frozenset(
    {"markdown", "plain_text", "lark_md", "div", "note", "button", "text", "a"}
)
_IMAGE_TAGS = frozenset({"img", "image"})
_FILE_KINDS = {"file": "文件", "media": "视频"}
_FIXED = {
    "image": "[图片]",
    "audio": "[语音]",
    "sticker": "[表情包]",
    "share_chat": "[分享的群名片]",
    "share_user": "[分享的个人名片]",
    "merge_forward": "[合并转发的聊天记录]",
}


def _cap(text: str, limit: int = LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _content(item: dict[str, Any]) -> Any:
    body = item.get("body")
    raw = body.get("content") if isinstance(body, dict) else None
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


class _Card:
    """顺着卡片 JSON 收集可读的文字（文档顺序、相邻重复只留一条）与图片键。"""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.keys: list[str] = []
        self.images = 0
        self._nodes = 0

    def add(self, value: Any) -> None:
        text = " ".join(value.split()) if isinstance(value, str) else ""
        if text and (not self.texts or self.texts[-1] != text):
            self.texts.append(text)

    def walk(self, node: Any, depth: int = 0) -> None:
        self._nodes += 1
        if depth > _DEPTH or self._nodes > _NODES:
            return
        if isinstance(node, list):
            for child in node:
                self.walk(child, depth + 1)
            return
        if not isinstance(node, dict):
            return
        tag = node.get("tag")
        if tag in _IMAGE_TAGS:
            self.images += 1
            key = node.get("image_key") or node.get("img_key")
            if isinstance(key, str) and key:
                self.keys.append(key)
            return
        if tag == "img_combination":
            for one in node.get("img_list") or []:
                self.images += 1
                key = one.get("img_key") if isinstance(one, dict) else None
                if isinstance(key, str) and key:
                    self.keys.append(key)
            return
        if tag in _CONTENT_TAGS:
            # `content` 是卡片里的文本组件，`text` 是降级骨架里的文本片段；
            # 嵌套的 `text` 对象（div、button）交给下面的递归。
            self.add(node.get("content"))
            self.add(node.get("text"))
        elif isinstance(node.get("title"), str):
            self.add(node.get("title"))
        for key, child in node.items():
            if key not in _SKIP_KEYS and isinstance(child, dict | list):
                self.walk(child, depth + 1)


def _card(data: Any) -> _Card:
    found = _Card()
    if isinstance(data, dict):
        # 降级骨架的 title 是字符串；原始卡片的标题在 header 里，由递归收集。
        found.add(data.get("title") if isinstance(data.get("title"), str) else None)
        found.walk({k: v for k, v in data.items() if k != "title"})
    return found


def card_image_keys(item: dict[str, Any]) -> list[str]:
    """卡片里的图片键（去重、保序）；不是卡片或读不出时为空。"""
    if item.get("msg_type") != "interactive":
        return []
    return list(dict.fromkeys(_card(_content(item)).keys))


def item_text(item: dict[str, Any]) -> str:
    """一条飞书消息的可读文字；任何内容都不抛错，最长 `LIMIT` 个字符。"""
    kind = str(item.get("msg_type") or "")
    data = _content(item)
    if kind == "text" and isinstance(data, dict):
        return _cap(str(data.get("text") or ""))
    if kind == "post" and isinstance(data, dict):
        runs = [
            run
            for line in data.get("content") or []
            if isinstance(line, list)
            for run in line
            if isinstance(run, dict)
        ]
        images = sum(1 for run in runs if run.get("tag") in ("img", "media"))
        parts = [str(data.get("title") or ""), *(str(run.get("text") or "") for run in runs)]
        suffix = f"（含 {images} 张图）" if images else ""
        return _cap(" ".join(part for part in parts if part), LIMIT - len(suffix)) + suffix
    if kind == "interactive":
        found = _card(data)
        suffix = f"（含 {found.images} 张图，{CARD_IMAGES_NOTE}）" if found.images else ""
        if not found.texts:
            return CARD_EMPTY + suffix
        return _cap(CARD_PREFIX + " ".join(found.texts), LIMIT - len(suffix)) + suffix
    if kind in _FILE_KINDS:
        name = data.get("file_name") if isinstance(data, dict) else None
        label = _FILE_KINDS[kind]
        return _cap(f"[{label}：{name}]") if isinstance(name, str) and name else f"[{label}]"
    return _FIXED.get(kind) or f"[{kind or '未知类型'}消息]"
