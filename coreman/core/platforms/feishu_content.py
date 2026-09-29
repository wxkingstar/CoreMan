"""飞书消息内容（`content` JSON）→ 模型读得懂的文字；引用补全与发出消息存档共用。

只读官方结构里的文字节点。图片只记下 `image_key`，由调用方按「消息 ID + key」走官方资源
接口下载；链接地址不访问，按钮回调值、表单值、人员 ID 一概不碰。遍历有深度与节点上限，
畸形或超大的内容只会少读，不会拖垮调用方。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

MAX_TEXT = 12_000
_MAX_NODES = 5_000
_MAX_DEPTH = 12
_MAX_ROWS = 100
_MAX_CHART_ROWS = 50
# 不带 card_msg_content_type 读 Card JSON 2.0 时飞书只给这一句，它不是消息内容。
UPGRADE_PLACEHOLDER = "请升级至最新版本客户端，以查看内容"
# 我们自己卡片上的思考面板与会话链接：不是回复正文，绝不进引用。
_PRIVATE_ELEMENTS = frozenset({"thinking_panel", "thinking", "session_link"})
_LABELS = ("title", "subtitle", "text")
_CHILDREN = ("elements", "columns", "actions", "fields")
_SILENT = frozenset({"person", "person_list", "avatar", "hr", "standard_icon", "date_picker"})


@dataclass(frozen=True)
class Image:
    key: str


@dataclass
class Rendered:
    """一条消息的可读内容：按原顺序的文字与图片，外加（文件消息的）文件。"""

    segments: list[str | Image] = field(default_factory=list)
    file_key: str | None = None
    file_name: str | None = None

    def text(self, image: str = "[图片]") -> str:
        """纯文字形态：图片换成占位，文件写成一行说明。"""
        pieces = [s if isinstance(s, str) else image for s in self.segments]
        if self.file_key:
            pieces.append(f"[文件: {self.file_name or '未知文件名'}]")
        return bounded("\n".join(p for p in pieces if p.strip()))

    @property
    def images(self) -> list[str]:
        return [s.key for s in self.segments if isinstance(s, Image)]


def bounded(text: str) -> str:
    return text.strip()[:MAX_TEXT]


def mention_names(mentions: object) -> dict[str, str]:
    """`@_user_N` → 名字；事件与「获取消息」接口的 mentions 结构都认。"""
    names: dict[str, str] = {}
    for item in mentions if isinstance(mentions, list) else []:
        if isinstance(item, dict):
            key, name = item.get("key"), item.get("name")
            if isinstance(key, str) and key and isinstance(name, str) and name:
                names[key] = name
    return names


def _with_mentions(text: str, names: Mapping[str, str]) -> str:
    # 长的先换：@_user_10 不能被 @_user_1 吃掉一截。
    for key in sorted(names, key=len, reverse=True):
        text = text.replace(key, f"@{names[key]}")
    return text


def _ms_time(value: object) -> str:
    try:
        stamp = int(str(value)) / 1000
        return datetime.fromtimestamp(stamp, UTC).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _at(node: Mapping[str, Any], names: Mapping[str, str]) -> str:
    name = _str(node.get("user_name")) or names.get(_str(node.get("user_id")), "")
    return f"@{name}" if name else "@"


# ---------------------------------------------------------------- 富文本 post


def _post_body(content: Mapping[str, Any]) -> Mapping[str, Any]:
    """富文本可能按语言包一层（zh_cn / en_us / ja_jp）；中文优先，否则取第一种。"""

    def usable(value: object) -> bool:
        return isinstance(value, dict) and ("content" in value or "content_v2" in value)

    if usable(content):
        return content
    zh = content.get("zh_cn")
    if isinstance(zh, dict) and usable(zh):
        return zh
    return next((v for v in list(content.values())[:4] if usable(v)), {})


def _post_segments(content: Mapping[str, Any], names: Mapping[str, str]) -> list[str | Image]:
    post = _post_body(content)
    # content 里图片是独立的 img 节点，content_v2 把图揉进 md 文本：优先前者才拿得到图。
    rows = post.get("content")
    if not isinstance(rows, list):
        rows = post.get("content_v2")
    segments: list[str | Image] = []
    title = _str(post.get("title")).strip()
    if title:
        segments.append(title)
    line: list[str] = []

    def flush() -> None:
        if "".join(line).strip():
            segments.append("".join(line))
        line.clear()

    for row in rows[:500] if isinstance(rows, list) else []:
        for node in row[:100] if isinstance(row, list) else []:
            if not isinstance(node, dict):
                continue
            tag = node.get("tag")
            if tag in {"text", "md", "code_block"}:
                line.append(_with_mentions(_str(node.get("text")), names))
            elif tag == "a":
                text, href = _str(node.get("text")), _str(node.get("href"))
                line.append(f"[{text}]({href})" if text and href and text != href else text or href)
            elif tag == "at":
                line.append(_at(node, names))
            elif tag == "emotion":
                line.append(f"[{_str(node.get('emoji_type')) or '表情'}]")
            elif tag == "hr":
                line.append("---")
            elif tag == "img" and _str(node.get("image_key")):
                flush()
                segments.append(Image(node["image_key"]))
            elif tag == "media":
                line.append("[视频]")
        flush()
    return segments


# ---------------------------------------------------------------- 卡片 interactive


def _plain(value: object) -> str:
    """卡片里的文本对象：字符串、{content}、{text: {content}}，以及 i18n_content。"""
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    for key in ("content", "text"):
        inner = value.get(key)
        if isinstance(inner, str) and inner:
            return inner
        if isinstance(inner, dict):
            return _plain(inner)
    i18n = value.get("i18n_content")
    if isinstance(i18n, dict):
        return _str(i18n.get("zh_cn")) or next((v for v in i18n.values() if isinstance(v, str)), "")
    return ""


def _localized(value: object) -> Any:
    """i18n_header / i18n_elements：中文优先，否则取第一种语言。"""
    if isinstance(value, dict) and value:
        return value.get("zh_cn") or next(iter(value.values()))
    return None


def _rendered(node: object, depth: int = 0) -> str:
    """飞书渲染后的内部结构：`{tag, property: {content | elements}}`，逐层取文字按原顺序拼起来。"""
    if depth > 6:
        return ""
    if isinstance(node, list):
        return "".join(_rendered(n, depth + 1) for n in node[:50])
    prop = node.get("property") if isinstance(node, dict) else None
    if not isinstance(prop, dict):
        return ""
    content = prop.get("content")
    if isinstance(content, str):
        return content
    return _rendered(prop.get("elements"), depth + 1)


def _cell(value: object) -> str:
    if isinstance(value, bool | int | float):
        return str(value)
    if isinstance(value, str):
        return value.replace("\n", " ")
    if isinstance(value, dict):
        # 按 user_card_content 读回的卡片，表格单元格不是发送时的字符串，而是渲染后的结构。
        return (_plain(value) or _rendered(value)).replace("\n", " ")
    if isinstance(value, list):
        return "、".join(t for t in (_cell(v) for v in value[:20]) if t)
    return ""


def _table(element: Mapping[str, Any]) -> list[str]:
    columns = [c for c in element.get("columns") or [] if isinstance(c, dict)][:20]
    # 人员列只有 ID，读不出名字，整列略过。
    keep = [c for c in columns if c.get("data_type") != "persons"]
    if not keep:
        return []
    names = [_str(c.get("name")) for c in keep]
    head = [_plain(c.get("display_name")) or n for c, n in zip(keep, names, strict=True)]
    lines = ["| " + " | ".join(head) + " |"]
    for row in (element.get("rows") or [])[:_MAX_ROWS]:
        if isinstance(row, dict):
            lines.append("| " + " | ".join(_cell(row.get(n)) for n in names) + " |")
    return lines


def _chart(element: Mapping[str, Any]) -> list[str]:
    spec = element.get("chart_spec")
    data = spec.get("data") if isinstance(spec, dict) else None
    lines = ["[图表数据]"]
    for dataset in (data if isinstance(data, list) else [data])[:5]:
        values = dataset.get("values") if isinstance(dataset, dict) else None
        for record in values[:_MAX_CHART_ROWS] if isinstance(values, list) else []:
            if isinstance(record, dict):
                lines.append(json.dumps(record, ensure_ascii=False)[:500])
    return lines if len(lines) > 1 else ["[图表]"]


def _inline(node: Mapping[str, Any], names: Mapping[str, str]) -> str:
    tag = node.get("tag")
    if tag == "at":
        return _at(node, names)
    if tag == "img":
        return "[图片]"
    if tag == "button":
        return f"[按钮: {_plain(node.get('text'))}]"
    if tag in _SILENT or tag in {"select_static", "overflow"}:
        return ""
    return _with_mentions(_plain(node), names)


class _CardReader:
    def __init__(self, names: Mapping[str, str]) -> None:
        self.names = names
        self.lines: list[str] = []
        self.left = _MAX_NODES

    def walk(self, value: object, depth: int = 0) -> None:
        self.left -= 1
        if depth > _MAX_DEPTH or self.left < 0:
            return
        if isinstance(value, list):
            for item in value[:200]:
                if isinstance(item, list):
                    # 默认（降级）格式：每行是一串行内节点。
                    row = "".join(_inline(n, self.names) for n in item[:100] if isinstance(n, dict))
                    self.add(row)
                else:
                    self.walk(item, depth + 1)
            return
        if not isinstance(value, dict) or value.get("element_id") in _PRIVATE_ELEMENTS:
            return
        tag = value.get("tag")
        if tag in {"markdown", "lark_md", "plain_text", "text", "a", "at"}:
            self.add(_inline(value, self.names))
        elif tag in {"img", "img_combination", "image"}:
            self.add("[图片]")
        elif tag == "button":
            label = _plain(value.get("text"))
            if label:
                self.add(f"[按钮: {label}]")
        elif tag == "table":
            self.lines.extend(_table(value))
        elif tag == "chart":
            self.lines.extend(_chart(value))
        elif tag in {"select_static", "multi_select_static", "overflow"}:
            options = [
                _plain(o.get("text")) if isinstance(o, dict) else _str(o)
                for o in (value.get("options") or [])[:50]
            ]
            hint = _plain(value.get("placeholder")) or "选项"
            self.add(f"[{hint}: {' / '.join(o for o in options if o)}]")
        elif tag in _SILENT:
            return
        else:
            # div、note、column_set、column、form、collapsible_panel、1.0 的 action 等容器，
            # 以及 fields 里不带 tag 的字段：先读标题/文字，再读子元素。
            header = value.get("header")
            if isinstance(header, dict):
                self.add(_plain(header.get("title")))
            for key in _LABELS:
                if key in value:
                    self.add(_with_mentions(_plain(value[key]), self.names))
            for key in _CHILDREN:
                if key in value:
                    self.walk(value[key], depth + 1)

    def add(self, text: str) -> None:
        if text.strip():
            self.lines.append(text)


def card_text(card: Mapping[str, Any], names: Mapping[str, str] | None = None) -> str:
    """卡片（1.0 / 2.0 原始 JSON、模板卡片、默认降级结构）里给人看的文字。"""
    reader = _CardReader(names or {})
    if card.get("type") == "template":
        data = card.get("data")
        variables = data.get("template_variable") if isinstance(data, dict) else None
        if isinstance(variables, dict):
            for value in list(variables.values())[:100]:
                reader.add(_str(value))
        return bounded("\n".join(reader.lines))
    header = card.get("header")
    if not isinstance(header, dict):
        header = _localized(card.get("i18n_header"))
    if isinstance(header, dict):
        reader.add(_plain(header.get("title")))
        reader.add(_plain(header.get("subtitle")))
    elif isinstance(card.get("title"), str):
        reader.add(card["title"])
    body = card.get("body")
    source: Mapping[str, Any] = body if isinstance(body, dict) else card
    elements = source.get("elements")
    reader.walk(elements if isinstance(elements, list) else _localized(source.get("i18n_elements")))
    text = bounded("\n".join(reader.lines))
    return "" if text == UPGRADE_PLACEHOLDER else text


# ---------------------------------------------------------------- 各消息类型


def _calendar(content: Mapping[str, Any]) -> str:
    start, end = _ms_time(content.get("start_time")), _ms_time(content.get("end_time"))
    when = f"（{start} – {end}）" if start and end else ""
    return f"[日程] {_str(content.get('summary'))}{when}"


def _system(content: Mapping[str, Any]) -> str:
    text = _str(content.get("template"))
    for key, value in list(content.items())[:20]:
        if isinstance(value, list):
            filled = "、".join(v for v in value if isinstance(v, str))
        elif isinstance(value, dict):
            filled = _str(value.get("text"))
        else:
            filled = _str(value)
        text = text.replace("{" + str(key) + "}", filled)
    return f"[系统消息] {text}" if text else ""


def _todo(content: Mapping[str, Any], names: Mapping[str, str]) -> str:
    summary = content.get("summary")
    parts = _post_segments(summary, names) if isinstance(summary, dict) else []
    title = " ".join(s for s in parts if isinstance(s, str))
    due = _ms_time(content.get("due_time"))
    return f"[任务] {title}" + (f"（截止 {due}）" if due else "")


def render(
    msg_type: str, raw: object, *, names: Mapping[str, str] | None = None
) -> Rendered | None:
    """一条消息的可读内容；None = 结构不认识或没有任何可读内容。

    `raw` 是消息的 `content`：JSON 字符串或已解析的对象。合并转发的子消息不在内容里，
    由调用方自己拼（见 worker 的引用补全）。
    """
    names = names or {}
    if isinstance(raw, str):
        if len(raw) > 256_000:
            return None
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return None
    if not isinstance(raw, dict):
        return None
    content: dict[str, Any] = raw
    out = Rendered()
    add = out.segments.append
    if msg_type == "text":
        add(_with_mentions(_str(content.get("text")), names))
    elif msg_type == "post":
        out.segments.extend(_post_segments(content, names))
    elif msg_type == "image":
        if _str(content.get("image_key")):
            add(Image(content["image_key"]))
    elif msg_type == "file":
        out.file_key = _str(content.get("file_key")) or None
        out.file_name = _str(content.get("file_name")) or None
    elif msg_type == "folder":
        add(f"[文件夹: {_str(content.get('file_name'))}]")
    elif msg_type == "audio":
        add("[语音消息，飞书不提供文字内容]")
    elif msg_type == "media":
        add(f"[视频: {_str(content.get('file_name')) or '未命名'}]")
    elif msg_type == "sticker":
        add("[表情包]")
    elif msg_type == "interactive":
        add(card_text(content, names))
    elif msg_type == "hongbao":
        add(_str(content.get("text")) or "[红包]")
    elif msg_type in {"share_calendar_event", "calendar", "general_calendar"}:
        add(_calendar(content))
    elif msg_type == "share_chat":
        add("[群名片]")
    elif msg_type == "share_user":
        add("[个人名片]")
    elif msg_type == "system":
        add(_system(content))
    elif msg_type == "location":
        add(f"[位置] {_str(content.get('name'))}")
    elif msg_type == "video_chat":
        add(f"[视频会议] {_str(content.get('topic'))}")
    elif msg_type == "todo":
        add(_todo(content, names))
    elif msg_type == "vote":
        options = [o for o in content.get("options") or [] if isinstance(o, str)]
        add(f"[投票] {_str(content.get('topic'))}：{' / '.join(options)}")
    else:
        return None
    out.segments = [s for s in out.segments if isinstance(s, Image) or s.strip()]
    if not out.segments and not out.file_key:
        return None
    return out


def sent_text(msg_type: str, content: Mapping[str, Any]) -> str | None:
    """机器人自己发出的一条消息的正文，存档后引用直接读；流式卡片（卡片实体引用）没有正文。"""
    if msg_type == "interactive" and content.get("type") == "card":
        return None
    rendered = render(msg_type, dict(content))
    return (rendered.text() if rendered else "") or None
