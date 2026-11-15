"""审批单的摘要与正文：由平台从实际参数生成，助理写的说明单独放在后面并标明。

`render` 生成纯文本（不以 Tower 的富文本标记开头）：被回复的原话、要发出的内容都可能含标记，
纯文本下它们不会被渲染。原话、要执行的内容、助理的说明每行前都加竖线，一段内容伪造不出别的
小节。飞书里的确认卡用它。

`render_rich` 生成给审批系统的富文本版式（原话灰字、正文放大、助理的说明放进内容框）。富文本
下标记会生效，所以每一段不可信的文字都经 `richtext.inert` 逐字符转义，见 `richtext` 的说明。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from coreman.core.assistants.approvals import attachments, richtext
from coreman.core.assistants.approvals.catalog import ENTRIES, ON_MESSAGE, Template

LIMIT = 10000
ABSTRACT_LIMIT = 120
CHANNELS = {
    "p2p": "私聊",
    "group": "群聊",
    "doc": "文档",
    "approval": "审批",
    "calendar": "日程",
    "task": "任务",
    "mail": "邮件",
    "other": "其他",
}
LABELS = {
    "text": "正文",
    "body": "正文",
    "subject": "主题",
    "to": "收件人",
    "cc": "抄送",
    "bcc": "密送",
    "summary": "标题",
    "description": "说明",
    "content": "内容",
    "comment": "审批意见",
    "title": "标题",
    "name": "名称",
    "member_open_ids": "成员",
    "attendee_open_ids": "参会人",
}
BAR = "│ "
CUT = "…（原话过长，已截断）"
NOTE = "—— 以下由助理撰写，供参考，不是将要执行的内容 ——"


class TooLong(Exception):
    """不算原话也超过上限：要执行的内容不截断，这张单不能提交。"""


@dataclass(frozen=True)
class Facts:
    """平台为一次调用查到的事实。

    Attributes:
        channel: `APPROVAL_CHANNELS` 之一
        target: 会话名或对方姓名；非消息类为空
        quoted: 被回复（或锚点、被操作）的那条消息的原话，带发送人；没有则为空
        anchor_message_id: 实际采用的锚点
        quoted_by: 那条消息的发送人名字（`quoted` 的前半）；富文本版式把它与原话分开排
        quoted_text: 那条消息的原话（`quoted` 的后半）
    """

    channel: str
    target: str = ""
    quoted: str = ""
    anchor_message_id: str | None = None
    quoted_by: str = ""
    quoted_text: str = ""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _bar(text: str) -> str:
    return "\n".join(BAR + line for line in (text.splitlines() or [""]))


def _size(count: int) -> str:
    if count < 1024:
        return f"{count} B"
    if count < 1024 * 1024:
        return f"{count / 1024:.1f} KB"
    return f"{count / 1024 / 1024:.1f} MB"


def _files(calls: list[dict[str, Any]], links: dict[str, str]) -> list[str]:
    """附件小节：文件名、类型、大小，以及给审批人在批准前查看的链接。"""
    found = attachments.references(calls[:1])
    if not found:
        return []
    lines = [f"附件（{len(found)} 个；链接供批准前查看，审批到期后失效）："]
    for item in found:
        kind = " ".join(str(item["mime"]).split())
        lines.append(f"- {_json(item['filename'])}（{kind}，{_size(int(item['size']))}）")
        if item["key"] in links:
            lines.append(f"  {links[item['key']]}")
    return lines


def heading(template: Template, facts: list[Facts]) -> str:
    """渠道与对象（非消息类是渠道和动作名），单行、不截断：摘要与合单里每一项的小标题都用它。"""
    label = CHANNELS[facts[0].channel]
    names = [" ".join(item.target.split()) for item in facts if item.target.strip()]
    if not names:
        text = f"{label} · {template.action}"
    elif len(facts) == 1:
        text = f"{label} → {names[0]}"
    else:
        text = f"{label} → {names[0]} 等 {len(facts)} 个会话"
    # Tower 要求摘要是单行：会话名里的换行与连续空白压成一个空格。
    return " ".join(text.split())


def abstract(template: Template, facts: list[Facts]) -> str:
    """单行摘要，会出现在锁屏推送里：只有渠道和对象（非消息类是渠道和动作名）。"""
    text = heading(template, facts)
    return text if len(text) <= ABSTRACT_LIMIT else text[: ABSTRACT_LIMIT - 1] + "…"


def _head(template: Template, calls: list[dict[str, Any]], facts: list[Facts]) -> list[str]:
    first = calls[0]["arguments"]
    lines = [f"渠道：{CHANNELS[facts[0].channel]}"]
    names = [" ".join(item.target.split()) for item in facts if item.target.strip()]
    if names:
        label = "对象" if len(facts) == 1 else f"对象（{len(facts)} 个会话）"
        lines.append(f"{label}：{'、'.join(names)}")
    lines.append(f"动作：{template.action}")
    for name in template.target:
        if first.get(name) is not None:
            lines.append(f"{LABELS.get(name, name)}：{_json(first[name])}")
    return lines


def _rest(
    template: Template,
    calls: list[dict[str, Any]],
    background: str,
    research: str,
    links: dict[str, str],
    commitments: str = "",
    unverified: str = "",
) -> list[str]:
    first = calls[0]["arguments"]
    lines: list[str] = []
    shown = [name for name in template.body if first.get(name) is not None]
    files = _files(calls, links)
    if shown or files:
        lines.append("将要执行的内容：")
        for name in shown:
            value = first[name]
            lines.append(f"{LABELS.get(name, name)}：")
            lines.append(_bar(value if isinstance(value, str) else _json(value)))
        lines += files
    # 文件参数已经列在附件小节里：对象名不出现在「其他参数」下。
    used = set(template.body) | set(template.target) | attachments.ARGUMENTS
    if first.get("reply_to_message_id"):
        # 回复文件发往被回复消息所在的会话，接收方参数不起作用：不展示，免得误导审批人。
        used |= {"receive_id", "receive_id_type"}
    others = [f"- {key}：{_json(value)}" for key, value in first.items() if key not in used]
    if others:
        lines += ["", "其他参数：", *others]
    if len(calls) > 1:
        # 合并的单：除第一条外，每个会话只有接收方与重试编号不同，逐条列出。
        lines += ["", "其余会话的参数："]
        for call in calls[1:]:
            differing = {k: v for k, v in call["arguments"].items() if first.get(k) != v}
            lines.append(f"- {_json(differing)}")
    notes = [
        ("替你答应的事：", commitments),
        ("没核实的推断：", unverified),
        ("背景：", background),
        ("调研：", research),
    ]
    if any(text for _, text in notes):
        lines += ["", NOTE]
        for label, text in notes:
            if text:
                lines += [label, _bar(text)]
    return lines


def render(
    template: Template,
    calls: list[dict[str, Any]],
    facts: list[Facts],
    *,
    background: str = "",
    research: str = "",
    commitments: str = "",
    unverified: str = "",
    links: dict[str, str] | None = None,
) -> str:
    """审批单正文。超过上限时只截断原话；不算原话仍超出则抛 `TooLong`。

    Args:
        links: 临时桶对象名 → 预签名下载链接；参数里的文件引用按它附上查看链接
    """
    head = "\n".join(_head(template, calls, facts))
    rest = "\n".join(
        _rest(template, calls, background, research, links or {}, commitments, unverified)
    )
    quotes = [item for item in facts if item.quoted]
    title = "被回复的原话：" if len(facts) == 1 else "各会话里最近的一条消息："
    fixed = len(head) + len(rest) + 4
    if fixed > LIMIT:
        raise TooLong
    if not quotes:
        return f"{head}\n\n{rest}"
    # 每段原话的开销：竖线、换行与小节标题。剩下的额度在各段之间平分。
    budget = LIMIT - fixed - len(title) - 2
    each = budget // len(quotes) - len(BAR) - 1
    if each <= len(CUT):
        noted = f"{head}\n\n（原话过长，未附）\n\n{rest}"
        return noted if len(noted) <= LIMIT else f"{head}\n\n{rest}"
    pieces = []
    for item in quotes:
        flat = " ".join(item.quoted.split())
        pieces.append(BAR + (flat if len(flat) <= each else flat[: each - len(CUT)] + CUT))
    return f"{head}\n\n{title}\n" + "\n".join(pieces) + f"\n\n{rest}"


# ---- 富文本版式（给审批系统） ----

# 消息类工具里不给审批人看的参数。只有这些；其余参数一个不少地列在「其他参数」下。
# - uuid：重试用的幂等键，批准与否与它无关
# - receive_id_type / msg_type：接口的编码方式，标题和正文已经说明了发给谁、发什么
# - receive_id：就是这个会话（标题已有会话名）；回复文件时它不起作用，同样不展示
# - message_id / reply_to_message_id：有原话那一行时，原话已经说明了目标消息（没有原话时仍列出）
RICH_TECHNICAL = frozenset({"uuid", "receive_id_type", "msg_type"})
RICH_MESSAGE_IDS = frozenset({"message_id", "reply_to_message_id"})
# 取默认值时等于没传，不展示；取别的值才是审批人要看的。
RICH_DEFAULTS: dict[str, Any] = {"format": "text", "reply_in_thread": False}
# 原话在审批单上最多显示多少个字；额度不够时再往下缩（见 `render_rich`）。
QUOTE_CAP = 400
# 缩到这个长度以下就没有意义，改为不附原话。
QUOTE_MIN = 20
RICH_NOTE = "以下由助理撰写，供参考，不是将要执行的内容"
_REPLY = ENTRIES["feishu_reply_message"].action
_PLAIN = {ENTRIES["feishu_send_message"].action, _REPLY}
_FORWARD = ENTRIES["feishu_forward_message"].action
_ON_MESSAGE = {ENTRIES[name].action for name in ON_MESSAGE}


def _quoted(item: Facts) -> tuple[str, str]:
    """原话的发送人与内容；只有合在一起的旧数据时发送人为空。"""
    if item.quoted_by or item.quoted_text:
        return item.quoted_by, item.quoted_text
    return "", item.quoted


def _quote_label(template: Template, args: dict[str, Any]) -> str:
    if template.action == _REPLY or args.get("reply_to_message_id"):
        return "回复"
    if template.action == _FORWARD:
        return "转发"
    if template.action in _ON_MESSAGE:
        return "针对"
    return "最近一条"


def _text(value: Any) -> str:
    """一个参数值的文字：字符串原样，一串字符串（收件人、参会人等）一行一个，其余按 JSON。"""
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value and all(isinstance(one, str) for one in value):
        return "\n".join(value)
    return _json(value)


def _hidden(template: Template, call: dict[str, Any], quote_shown: bool) -> set[str]:
    """这一次调用里不展示的参数名（消息类才有，见 `RICH_TECHNICAL`）。"""
    if template.channel != "message":
        return set()
    args = call["arguments"]
    hidden = set(RICH_TECHNICAL)
    if quote_shown:
        hidden |= RICH_MESSAGE_IDS
    if args.get("reply_to_message_id") or args.get("receive_id") == call.get("chat_id"):
        hidden.add("receive_id")
    return hidden


def _is_default(key: str, value: Any) -> bool:
    return (
        key in RICH_DEFAULTS
        and type(value) is type(RICH_DEFAULTS[key])
        and value == (RICH_DEFAULTS[key])
    )


def _rich_files(calls: list[dict[str, Any]], links: dict[str, str]) -> list[str]:
    found = attachments.references(calls[:1])
    if not found:
        return []
    out = richtext.field(f"附件（{len(found)} 个；链接供批准前查看，审批到期后失效）", "")
    for number, item in enumerate(found, 1):
        kind = " ".join(str(item["mime"]).split())
        line = str(richtext.inert(f"{item['filename']}（{kind}，{_size(int(item['size']))}）"))
        url = (
            richtext.link(f"查看附件 {number}", links[item["key"]])
            if item["key"] in links
            else None
        )
        out.append(f"{line} {url}" if url else line)
    return out


def _rich(
    template: Template,
    calls: list[dict[str, Any]],
    facts: list[Facts],
    quote_limit: int,
    notes: list[tuple[str, str]],
    links: dict[str, str],
) -> str:
    first = calls[0]["arguments"]
    head: list[str] = []
    quote_shown = False
    quoted = [_quoted(item) for item in facts]
    if len(calls) > 1:
        # 合并发送：每个会话一行，先说会话名。
        if quote_limit:
            for item, (who, text) in zip(facts, quoted, strict=True):
                head.append(
                    richtext.chat_line(item.target or "会话", "最近一条", who, text, quote_limit)
                )
    elif any(quoted[0]) and quote_limit:
        label = _quote_label(template, first)
        head.append(richtext.quote_line(label, *quoted[0], limit=quote_limit))
        quote_shown = True
    elif any(quoted[0]):
        # 额度不够放原话：如实说明。目标消息仍由这条说明代表，编号不再重列。
        head.append(richtext.grey(richtext.const("原话过长，未附")))
        quote_shown = True
    if template.action not in _PLAIN:
        head.append(richtext.grey(richtext.inert(f"动作：{template.action}")))
    body: list[list[str]] = []
    shown = [name for name in template.body if first.get(name) is not None]
    if template.channel == "message" and shown == ["text"] and isinstance(first["text"], str):
        body.append(richtext.big(first["text"]))
    else:
        for name in [n for n in template.target if first.get(n) is not None] + shown:
            body.append(richtext.field(LABELS.get(name, name), _text(first[name])))
    body.append(_rich_files(calls, links))
    used = set(template.body) | set(template.target) | attachments.ARGUMENTS
    hidden = _hidden(template, calls[0], quote_shown) | used
    params = [
        richtext.grey(richtext.const(f"{richtext.inert(key)}：{richtext.inert(_json(value))}"))
        for key, value in first.items()
        if key not in hidden and not _is_default(key, value)
    ]
    for call, item in zip(calls[1:], facts[1:], strict=True):
        # 合并的单：除第一条外，每个会话只列与第一条不同、又不属于技术参数的那些。
        skip = _hidden(template, call, quote_shown) | used
        name = richtext.inert(item.target or "会话")
        params += [
            richtext.grey(
                richtext.const(f"{name}｜{richtext.inert(key)}：{richtext.inert(_json(value))}")
            )
            for key, value in call["arguments"].items()
            if key not in skip and first.get(key) != value and not _is_default(key, value)
        ]
    if params:
        params.insert(0, richtext.grey(richtext.const("其他参数")))
    tail: list[list[str]] = []
    if any(richtext.lines(text) for _, text in notes):
        tail = [[richtext.RULE], [richtext.grey(richtext.const(RICH_NOTE))]]
        tail += [richtext.panel(title, text) for title, text in notes if richtext.lines(text)]
    return richtext.blocks(head, *body, params, *tail)


def render_rich(
    template: Template,
    calls: list[dict[str, Any]],
    facts: list[Facts],
    *,
    background: str = "",
    research: str = "",
    commitments: str = "",
    unverified: str = "",
    links: dict[str, str] | None = None,
) -> str:
    """审批系统用的富文本正文（以 `richtext.PREFIX` 开头）；版式见 `richtext` 与设计文档。

    不可信的文字（原话、会话名、要发出的内容、助理的说明、参数值、文件名）一律经
    `richtext.inert` 进入版式，不会冒出平台没写的小节、链接或组件。超过上限时与 `render` 一样
    只缩短原话（在转义之前缩短原文）；不算原话仍超出则抛 `TooLong`：要执行的内容不截断。

    Args:
        links: 临时桶对象名 → 预签名下载链接；是版式里唯一可点击的链接
    """
    notes = [
        ("替你答应的事", commitments),
        ("没核实的推断", unverified),
        ("背景", background),
        ("做过的调研", research),
    ]

    def build(limit: int) -> str:
        return richtext.PREFIX + _rich(template, calls, facts, limit, notes, links or {})

    full = build(QUOTE_CAP)
    if len(full) <= LIMIT:
        return full
    if len(build(QUOTE_MIN)) > LIMIT:
        dropped = build(0)
        if len(dropped) > LIMIT:
            raise TooLong
        return dropped
    low, high = QUOTE_MIN, QUOTE_CAP  # low 放得下，high 放不下
    while high - low > 1:
        mid = (low + high) // 2
        if len(build(mid)) <= LIMIT:
            low = mid
        else:
            high = mid
    return build(low)
