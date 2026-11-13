"""审批单的摘要与正文：由平台从实际参数生成，助理写的说明单独放在后面并标明。

正文是纯文本（不以 Tower 的富文本标记开头）：被回复的原话、要发出的内容都可能含标记，纯文本
下它们不会被渲染。原话、要执行的内容、助理的说明每行前都加竖线，一段内容伪造不出别的小节。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from coreman.core.assistants.approvals import attachments
from coreman.core.assistants.approvals.catalog import Template

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
    """

    channel: str
    target: str = ""
    quoted: str = ""
    anchor_message_id: str | None = None


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


def abstract(template: Template, facts: list[Facts]) -> str:
    """单行摘要，会出现在锁屏推送里：只有渠道和对象（非消息类是渠道和动作名）。"""
    label = CHANNELS[facts[0].channel]
    names = [" ".join(item.target.split()) for item in facts if item.target.strip()]
    if not names:
        text = f"{label} · {template.action}"
    elif len(facts) == 1:
        text = f"{label} → {names[0]}"
    else:
        text = f"{label} → {names[0]} 等 {len(facts)} 个会话"
    # Tower 要求摘要是单行：会话名里的换行与连续空白压成一个空格。
    text = " ".join(text.split())
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
    if background or research:
        lines += ["", NOTE]
        if background:
            lines += ["背景：", _bar(background)]
        if research:
            lines += ["调研：", _bar(research)]
    return lines


def render(
    template: Template,
    calls: list[dict[str, Any]],
    facts: list[Facts],
    *,
    background: str = "",
    research: str = "",
    links: dict[str, str] | None = None,
) -> str:
    """审批单正文。超过上限时只截断原话；不算原话仍超出则抛 `TooLong`。

    Args:
        links: 临时桶对象名 → 预签名下载链接；参数里的文件引用按它附上查看链接
    """
    head = "\n".join(_head(template, calls, facts))
    rest = "\n".join(_rest(template, calls, background, research, links or {}))
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
