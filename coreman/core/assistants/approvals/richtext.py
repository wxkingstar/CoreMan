"""审批单的富文本版式：把别人写的、模型写的文字逐字符转义，再摆进固定的版式里。

审批系统只在正文以 `PREFIX` 开头时按 Markdown 渲染（GFM 扩展，另有 `<font color>` 灰字、
`<br>` 换行，以及 `> [!PANEL]` 内容框、```` ```chart ```` 等组件）。被回复的原话、会话名、要发出
的文字都不是平台写的：若原样放进去，它们能伪造小节（`---`、`#### 背景`）、冒充平台的灰字行、
放可点击的钓鱼链接或组件。所以这里的规则是：**不可信的文字只经 `inert` 进入版式**——它返回
的字符串渲染出来恰好就是原文，不多不少：

- ASCII 标点全部加反斜杠转义（Markdown 的块与行内语法都靠这些字符起头；`.` `:` `/` `@` 也转义，
  这样裸网址、`www.`、邮箱都不会被自动识别成链接，`1.` 也不会变成有序列表）；
- `&` `<` `>` 写成实体（审批系统渲染时才还原实体，而 `<font>`、`<br>` 是在 Markdown 解析之前按
  原文正则识别的，反斜杠挡不住，实体可以）；
- 控制字符丢掉（审批系统拿 U+0001–U+0004 当内部标记），格式字符、方向覆盖与行、段分隔符写成
  可见的 `\\uXXXX`（同 `proxied._visible`：转义而不删除，所有者看到的与要执行的一致）；
- 首尾空白去掉、连续空白压成一个空格（行首四个空格会变成缩进代码块）。

唯一可点击的链接是平台生成的附件查看链接（`link`）。本模块不依赖审批的其他部分。
"""

from __future__ import annotations

import re
import unicodedata
from typing import NewType
from urllib.parse import quote, urlsplit

PREFIX = "<md>\n"
RULE = "---"
# 审批系统的渲染器把它们当内部标记，必须丢掉的是 ASCII 控制字符（U+0000–U+001F、U+007F）。
# 其余控制字符、格式字符（方向覆盖、零宽字符）、行与段分隔符写成可见形式，类别同
# `proxied._HIDDEN`。
_VISIBLE = frozenset({"Cc", "Cf", "Zl", "Zp"})
# CommonMark 里能用反斜杠转义的 ASCII 标点，去掉 `&` `<` `>`（它们写成实体）。
_PUNCT = frozenset("!\"#$%'()*+,-./:;=?@[\\]^_`{|}~")
# `&` 后面紧跟一个 WORD JOINER：审批系统在解析之后把 `&amp;` 还原成 `&`，要是后面接着 `lt;`，
# 还原出来的字符串又会像实体；隔开之后不会。这个字符只会出现在 `&amp;` 之后。
JOINER = "\u2060"
_ENTITY = {"&": "&amp;" + JOINER, "<": "&lt;", ">": "&gt;"}
DROPPED = "（原话过长，未附）"
CUT = "…（已截断）"
_LINE_BREAKS = re.compile("\r\n|[\n\r  \x0b\x0c]")
_SPACES = re.compile(" +")
# 已经转义好的一行：只有 `inert` 能造出来，版式函数只接受它，传不进原文。
Inert = NewType("Inert", str)


def inert(text: str) -> Inert:
    """把不可信的一行文字变成渲染出来恰好是它自己的 Markdown 源码（规则见模块说明）。"""
    spaced: list[str] = []
    for char in text:
        code = ord(char)
        if char in "\n\r\t\x0b\x0c ":
            spaced.append(" ")
        elif code < 0x20 or code == 0x7F:
            continue
        else:
            spaced.append(char)
    flat = _SPACES.sub(" ", "".join(spaced)).strip()
    out: list[str] = []
    for char in flat:
        if char in _ENTITY:
            out.append(_ENTITY[char])
        elif char in _PUNCT:
            out.append("\\" + char)
        elif unicodedata.category(char) in _VISIBLE:
            code = ord(char)
            shown = f"u{code:04X}" if code <= 0xFFFF else f"U{code:08X}"
            out.append("\\\\" + shown)
        else:
            out.append(char)
    return Inert("".join(out))


def const(text: str) -> Inert:
    """平台自己写在源码里的一行（标签、说明）：不含不可信内容，原样放行。"""
    return Inert(text)


def lines(text: str) -> list[Inert]:
    """多行不可信文字：按各种换行拆开，每行 `inert`，丢掉空行。"""
    parts = (inert(part) for part in _LINE_BREAKS.split(text))
    return [part for part in parts if part]


def cut(text: str, limit: int) -> str:
    """在转义之前截断原文（转义之后截会把一个转义序列切成两半）。"""
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + "…"


def cut_plain(text: str, limit: int | None) -> str:
    """不加省略号的截断（省略号由调用方以平台文字补上）。"""
    return text if limit is None or len(text) <= limit else text[:limit]


def grey(line: Inert) -> str:
    """次要的一行小字。"""
    return f'<font color="grey">{line}</font>'


def quote_line(label: str, who: str, text: str, limit: int = 200) -> str:
    """被回复（或最近一条、被操作）那条消息的原话，灰字一行：`回复 张三：原话`。"""
    sender = inert(who)
    body = inert(cut(text, limit)) if limit > 0 else (DROPPED if text.strip() else "")
    head = f"{inert(label)} {sender}" if sender else inert(label)
    return grey(const(f"{head}：{body}" if body else head))


def chat_line(chat: str, label: str, who: str, text: str, limit: int = 200) -> str:
    """合并发送时每个会话一行：`会话名｜最近一条 张三：原话`；没有可引用的消息时只写会话名。"""
    sender = inert(who)
    if not inert(text):
        return grey(const(f"{inert(chat)}｜暂无可引用的消息"))
    if limit <= 0:
        # 会话名是固定的，原话放不下才让位。
        return grey(const(f"{inert(chat)}｜{DROPPED}"))
    body = inert(cut(text, limit))
    head = f"{inert(label)} {sender}" if sender else inert(label)
    return grey(const(f"{inert(chat)}｜{head}：{body}"))


def big(text: str) -> list[str]:
    """要发出的正文：每行一个三级标题（大而粗）。"""
    return [f"### {line}" for line in lines(text)]


def field(label: str, text: str) -> list[str]:
    """带标签的一段：粗体标签（平台写的）加每行一句的内容。"""
    return [f"**{inert(label)}**", *lines(text)]


def panel(title: str, text: str, limit: int | None = None) -> list[str]:
    """内容框：四级标题加 `[!PANEL]`；一行就是一句话，多行变成列表。

    `limit` 给了且文字更长时，在转义之前截到这么多字，末尾加平台写的「…（已截断）」。
    """
    body = lines(cut_plain(text, limit))
    if limit is not None and len(text) > limit and body:
        body[-1] = const(body[-1] + CUT)
    rows = [f"> {line}" for line in body] if len(body) == 1 else [f"> - {line}" for line in body]
    return [f"#### {inert(title)}", "> [!PANEL]", *rows]


def link(label: str, url: str) -> str | None:
    """平台生成的链接（预签名的附件查看地址）：只认 http(s)，把能拆开链接语法的字符都编码。

    返回 None 表示这个地址不能放。这是版式里唯一可点击的链接。
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    safe = quote(url, safe="/:?#@!$&'*+,;=%-._~")
    return f"[{inert(label)}]({safe})"


def blocks(*groups: list[str]) -> str:
    """各块之间空一行；空块跳过。"""
    return "\n\n".join("\n".join(group) for group in groups if group)
