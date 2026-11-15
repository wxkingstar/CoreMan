"""飞书消息内容 → 一段可读的文字（纯函数，轮询每条消息都会跑，必须便宜且永不抛错）。

飞书给的消息是 `{"msg_type": …, "body": {"content": "<JSON 字符串>"}}`。卡片（interactive）带
`card_msg_content_type=user_card_content` 读时是发送时的原始卡片 JSON（1.0、2.0、模板卡片），
不带时是只有占位文字的降级骨架；文字交给平台层已有的 `card_text`，这里只数图、收图片键。
图片和视频的数量写在文字开头的标记里，免得被收件箱的摘录截断。
"""

from __future__ import annotations

import json
import re
from typing import Any

from coreman.core.platforms.feishu_content import card_text

LIMIT = 4000
# 与平台层 `render` 同一条线：再长的内容不解析。
MAX_RAW = 256_000
CARD_EMPTY = "[卡片消息，没有可读的文字]"
CARD_IMAGES_NOTE = "卡片里的图无法下载"
# 找图片键时最多走这么多层、这么多个节点。
_DEPTH = 24
_NODES = 2000
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
_MARKER = re.compile(r"^\[含 (?:\d+ 张图(?:、\d+ 个视频)?|\d+ 个视频)\](?: |$)")


def _cap(text: str, limit: int = LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _content(item: dict[str, Any]) -> Any:
    body = item.get("body")
    raw = body.get("content") if isinstance(body, dict) else None
    if not isinstance(raw, str) or len(raw) > MAX_RAW:
        return None
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        return None


def _images(data: Any) -> tuple[int, list[str]]:
    """卡片里图片的个数与图片键（深度与节点数都有上限）。"""
    count, keys, seen = 0, [], 0
    stack: list[tuple[Any, int]] = [(data, 0)]
    while stack:
        node, depth = stack.pop()
        seen += 1
        if depth > _DEPTH or seen > _NODES:
            continue
        if isinstance(node, list):
            stack.extend((child, depth + 1) for child in reversed(node))
        elif isinstance(node, dict):
            tag = node.get("tag")
            if isinstance(tag, str) and tag in _IMAGE_TAGS:
                count += 1
                key = node.get("image_key") or node.get("img_key")
                if isinstance(key, str) and key:
                    keys.append(key)
                continue
            if tag == "img_combination":
                images = node.get("img_list")
                for one in images if isinstance(images, list) else []:
                    count += 1
                    key = one.get("img_key") if isinstance(one, dict) else None
                    if isinstance(key, str) and key:
                        keys.append(key)
                continue
            stack.extend((child, depth + 1) for child in reversed(list(node.values())))
    return count, keys


def card_image_keys(item: dict[str, Any]) -> list[str]:
    """卡片里的图片键（去重、保序）；不是卡片或读不出时为空。"""
    if item.get("msg_type") != "interactive":
        return []
    try:
        return list(dict.fromkeys(_images(_content(item))[1]))
    except Exception:  # noqa: BLE001 见 `item_text`
        return []


def without_marker(text: str) -> str:
    """去掉 post 摘录开头的 `[含 N 张图…]` 标记（见 `item_text`）。"""
    return _MARKER.sub("", text, count=1)


def _post(data: dict[str, Any]) -> str:
    lines = data.get("content")
    runs = [
        run
        for line in (lines if isinstance(lines, list) else [])
        if isinstance(line, list)
        for run in line
        if isinstance(run, dict)
    ]
    images = sum(1 for run in runs if run.get("tag") == "img")
    videos = sum(1 for run in runs if run.get("tag") == "media")
    parts = [str(data.get("title") or ""), *(str(run.get("text") or "") for run in runs)]
    text = " ".join(part for part in parts if part)
    counts = [f"{images} 张图"] if images else []
    counts += [f"{videos} 个视频"] if videos else []
    marker = f"[含 {'、'.join(counts)}]" if counts else ""
    return _cap(f"{marker} {text}".strip() if marker else text)


def _card(data: Any) -> str:
    if not isinstance(data, dict):
        return CARD_EMPTY
    count, _ = _images(data)
    try:
        text = " ".join(card_text(data).split())
    except RecursionError:
        text = ""
    if not text:
        return CARD_EMPTY
    return _cap(f"[卡片·含 {count} 张图] {text}" if count else f"[卡片] {text}")


def item_text(item: dict[str, Any]) -> str:
    """一条飞书消息的可读文字；任何内容都不抛错，最长 `LIMIT` 个字符。"""
    try:
        return _item_text(item)
    except Exception:  # noqa: BLE001
        # 内容是第三方发来的任意 JSON，轮询的每条消息都会走到这里：抛错会让一位助理的盯消息
        # 卡在同一条上；平台层 `card_text` 对畸形结构本身就会抛 KeyError / TypeError。
        kind = str(item.get("msg_type") or "") if isinstance(item, dict) else ""
        return CARD_EMPTY if kind == "interactive" else f"[{kind or '未知类型'}消息]"


def _item_text(item: dict[str, Any]) -> str:
    kind = str(item.get("msg_type") or "")
    data = _content(item)
    if kind == "text" and isinstance(data, dict):
        return _cap(str(data.get("text") or ""))
    if kind == "post" and isinstance(data, dict):
        return _post(data)
    if kind == "interactive":
        return _card(data)
    if kind in _FILE_KINDS:
        name = data.get("file_name") if isinstance(data, dict) else None
        label = _FILE_KINDS[kind]
        return _cap(f"[{label}：{name}]") if isinstance(name, str) and name else f"[{label}]"
    return _FIXED.get(kind) or f"[{kind or '未知类型'}消息]"
