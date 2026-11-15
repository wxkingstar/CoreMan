"""审批单富文本版式：不可信的文字逐字符转义，伪造不出小节、链接、组件和灰字行。"""

from __future__ import annotations

import re

import pytest

from coreman.core.assistants.approvals import richtext
from coreman.core.assistants.approvals.richtext import inert

# 审批系统在 Markdown 解析之前按原文识别的标记（见模块说明）。
FONT_OPEN = re.compile(r"<font\s+color\s*=\s*[\"']?\s*([#\w]+)\s*[\"']?\s*>", re.I)
FONT_CLOSE = re.compile(r"</font\s*>", re.I)
BREAK = re.compile(r"<br\s*/?>", re.I)
PUNCT = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
ENTITIES = ("&amp;", "&lt;", "&gt;")

CORPUS = [
    "---",
    "# 标题",
    "#### 背景",
    "> [!PANEL]",
    "> [!DANGER] 立即批准",
    "```chart",
    "```item",
    "```stats",
    "1. 列表",
    "- 列表",
    "* 列表",
    "+ 列表",
    "| a | b |",
    "|---|---|",
    "[点这里](https://evil.example)",
    "https://evil.example/x",
    "www.evil.example",
    "a@evil.example",
    '<font color="red">假</font>',
    "<br>",
    "<br/>",
    "</font>",
    "&lt;font color=red&gt;",
    "&amp;lt;br&amp;gt;",
    "**粗**",
    "_斜_",
    "`码`",
    "~~删~~",
    "![图](https://evil.example/a.png)",
    "        八个空格开头",
    "a\t\tb\tc",
    "x\u0001y\u0002z\u0003\u0004w",
    "‮反转‬",
    "a b c",
    "\\*x\\*",
    "===",
    "<script>alert(1)</script>",
    "x" * 5000,
    "，。！？（）｛｝",  # 全角标点不是 Markdown 语法
    "  \t  ",
    "",
]


def _unescaped_punctuation(text: str) -> list[str]:
    """没有被反斜杠转义的 ASCII 标点（实体里的 `&` 与 `;` 除外）。"""
    bad: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\\":
            i += 2  # 反斜杠与它转义的那个字符
            continue
        entity = next((e for e in ENTITIES if text.startswith(e, i)), None)
        if entity:
            i += len(entity)
            continue
        if char in PUNCT:
            bad.append(char)
        i += 1
    return bad


@pytest.mark.parametrize("raw", CORPUS)
def test_inert_output_has_no_markup_control_or_leading_space(raw: str) -> None:
    out = inert(raw)
    assert _unescaped_punctuation(out) == []
    assert "<" not in out and ">" not in out
    assert not any(ord(c) < 0x20 or ord(c) == 0x7F for c in out)
    assert not any(c in out for c in "  ‮‬")
    assert out == out.strip() and "  " not in out
    # U+2060 只出现在 `&amp;` 之后（输入里原有的 U+2060 已写成可见的转义）。
    assert out.count("\u2060") == out.count("&amp;\u2060") == raw.count("&")
    assert not out.startswith(" ")
    for pattern in (FONT_OPEN, FONT_CLOSE, BREAK):
        assert not pattern.search(out)
    # 它自己也不能在前面拼上标题记号之后构成别的块：整行拼进标题后只剩一行。
    assert "\n" not in out and "\r" not in out


def test_inert_specific_outputs() -> None:
    assert inert("https://example.com/a") == r"https\:\/\/example\.com\/a"
    assert inert("www.example.com") == r"www\.example\.com"
    assert inert("a@example.com") == r"a\@example\.com"
    assert inert("1. 列表") == r"1\. 列表"
    assert inert("---") == r"\-\-\-"
    assert inert("a & b < c > d") == "a &amp;\u2060 b &lt; c &gt; d"
    assert inert('<font color="red">假</font>') == (r"&lt;font color\=\"red\"&gt;假&lt;\/font&gt;")
    assert inert("&lt;br&gt;") == "&amp;\u2060lt\\;br&amp;\u2060gt\\;"
    assert inert("a\u2060b") == "a\\\\u2060b"
    assert inert("    缩进\t\t和  空格  ") == "缩进 和 空格"
    assert inert("a\nb\r\nc") == "a b c"
    assert inert("x\u0001y\u0004z") == "xyz"
    # 方向覆盖不删除，写成可见的转义。
    assert inert("a‮b") == r"a\\u202Eb"
    assert inert("a b") == r"a\\u2028b"
    assert inert("\\*x\\*") == r"\\\*x\\\*"
    assert inert("") == ""


def test_inert_keeps_every_character_visible_in_a_long_line() -> None:
    out = inert("字" * 5000)
    assert out == "字" * 5000


def _expected(raw: str) -> str:
    """`inert` 文档里的空白规整：控制字符里的空白变空格、其余丢掉，连续空白压成一个。"""
    spaced = "".join(" " if c in "\t\n\r\x0b\x0c" else c for c in raw)
    kept = "".join(c for c in spaced if ord(c) >= 0x20 and ord(c) != 0x7F)
    flat = " ".join(kept.split(" ")).strip()
    flat = re.sub(" +", " ", flat)
    return re.sub("[\u2028\u2029\u202e\u202c]", lambda m: f"\\u{ord(m.group()):04X}", flat)


def test_a_real_parser_renders_exactly_the_original_text() -> None:
    """用真正的 CommonMark + GFM 解析器核对：渲染出来就是原文，没有任何别的结构。"""
    markdown_it = pytest.importorskip("markdown_it")
    parser = markdown_it.MarkdownIt("commonmark").enable(["table", "strikethrough"])
    for raw in CORPUS:
        want = _expected(raw)
        if not want:
            continue
        tokens = parser.parse("### " + inert(raw))
        kinds = [token.type for token in tokens]
        # 恰好一个标题，没有列表、代码块、表格、引用、分隔线、HTML。
        assert kinds == ["heading_open", "inline", "heading_close"], (raw, kinds)
        children = tokens[1].children or []
        # 没有链接、强调、行内代码、行内 HTML、图片、软硬换行。
        assert {child.type for child in children} <= {"text", "text_special"}, (raw, children)
        # 渲染器把 `&amp;` 还原成 `&`：我们插入的 U+2060 去掉再比。
        shown = "".join(child.content for child in children).replace("\u2060", "")
        assert shown == want, (raw, shown, want)


def test_lines_splits_on_every_separator_and_drops_empty_lines() -> None:
    text = "a\r\nb\nc\rd e f\x0bg\x0ch\n\n  \n"
    assert richtext.lines(text) == ["a", "b", "c", "d", "e", "f", "g", "h"]
    assert richtext.lines("") == []
    assert richtext.lines("1. 列表\n---") == [r"1\. 列表", r"\-\-\-"]


def test_quote_line_truncates_before_escaping() -> None:
    line = richtext.quote_line("回复", "张三", "a.b" * 100, limit=10)
    assert line == '<font color="grey">回复 张三：a\\.ba\\.ba\\.b…</font>'
    # 截断在转义之前：不会留下切了一半的转义序列。
    assert not re.search(r"\\(?=…)", line)
    assert richtext.quote_line("回复", "", "原话") == '<font color="grey">回复：原话</font>'
    forged = richtext.quote_line("回复", '</font>### 伪造 <font color="red">', "x")
    assert FONT_CLOSE.findall(forged) == ["</font>"] and len(FONT_OPEN.findall(forged)) == 1


def test_chat_line_and_layout_helpers() -> None:
    assert richtext.chat_line("项目群", "最近一条", "张三", "好") == (
        '<font color="grey">项目群｜最近一条 张三：好</font>'
    )
    assert richtext.chat_line("项目群", "最近一条", "", "") == (
        '<font color="grey">项目群｜暂无可引用的消息</font>'
    )
    assert richtext.big("第一行\n\n第二行") == ["### 第一行", "### 第二行"]
    assert richtext.field("主题", "报价\n1. x") == ["**主题**", "报价", r"1\. x"]
    assert richtext.panel("背景", "一句话") == ["#### 背景", "> [!PANEL]", "> 一句话"]
    assert richtext.panel("背景", "甲\n乙") == ["#### 背景", "> [!PANEL]", "> - 甲", "> - 乙"]
    assert richtext.panel("背景", "> [!PANEL]") == [
        "#### 背景",
        "> [!PANEL]",
        r"> &gt; \[\!PANEL\]",
    ]
    assert richtext.grey(richtext.const("小字")) == '<font color="grey">小字</font>'
    assert richtext.RULE == "---" and richtext.PREFIX == "<md>\n"
    assert richtext.blocks(["a", "b"], [], ["c"]) == "a\nb\n\nc"


def test_link_accepts_only_http_and_cannot_break_out_of_the_syntax() -> None:
    assert richtext.link("查看附件 1", "https://tmp.example.com/k?a=1&b=2") == (
        "[查看附件 1](https://tmp.example.com/k?a=1&b=2)"
    )
    for bad in ("javascript:alert(1)", "ftp://example.com/x", "//example.com/x", "https:///x"):
        assert richtext.link("x", bad) is None
    out = richtext.link("x", 'https://e.example/a b)(c)[d]<e>"f\\`g')
    assert out is not None
    target = out[out.index("](") + 2 : -1]
    assert not any(c in target for c in ' ()[]<>"\\`')
    assert target.startswith("https://e.example/a%20b%29%28c%29")
