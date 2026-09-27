"""GFM Markdown → 飞书卡片 JSON 2.0 组件。

模型按常规 GFM 写正文，卡片的 markdown 组件却有自己的方言：列表每层 4 空格、代码块只认 69 种
语言名、HTML 只认少数几个标签、链接只认 http/https、图片只认上传后的 img_key……这里分两步：

- :func:`normalize`：纯文本变换，把 GFM 改写成卡片 markdown 能正确渲染的写法；
- :func:`render_markdown`：按顶层块切开，表格、独占一段的图片、分割线换成原生组件，
  其余相邻的文字合并成一个 ``markdown`` 组件。

块结构交给 markdown-it-py（CommonMark + GFM 表格、删除线）解析，行内部分逐字扫描改写；
代码块和行内代码里的内容一律原样保留。
"""

from __future__ import annotations

import re
import string
from collections.abc import Callable, Mapping, Sequence
from functools import lru_cache
from typing import Any, NamedTuple

from markdown_it import MarkdownIt
from markdown_it.common.utils import normalizeReference
from markdown_it.token import Token

from coreman.core.feishu_cards import style
from coreman.core.feishu_cards.context import RenderContext

# ---------------------------------------------------------------- 常量

_MD = MarkdownIt("commonmark").enable(["table", "strikethrough"])

# 飞书代码块认的语言（大小写不敏感），出自卡片 markdown 组件文档。
CODE_LANGUAGES: frozenset[str] = frozenset(
    """
    plain_text abap ada apache apex assembly bash c_sharp cpp c cmake cobol css coffee_script d
    dart delphi diff django docker_file erlang fortran gherkin go graphql groovy html htmlbars
    http haskell json java javascript julia kotlin latex lisp lua matlab makefile markdown nginx
    objective_c opengl_shading_language php perl powershell prolog properties protobuf python r
    ruby rust sas scss sql scala scheme shell solidity swift toml thrift typescript vbscript
    visual_basic xml yaml
    """.split()
)
# 模型常写的别名 → 飞书的语言名；既不在别名表、也不在 CODE_LANGUAGES 里的语言去掉标记。
_LANG_ALIASES: dict[str, str] = {
    "py": "python",
    "py3": "python",
    "python3": "python",
    "js": "javascript",
    "jsx": "javascript",
    "mjs": "javascript",
    "cjs": "javascript",
    "node": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "mts": "typescript",
    "sh": "bash",
    "zsh": "bash",
    "console": "bash",
    "shell-session": "bash",
    "shellsession": "bash",
    "terminal": "bash",
    "yml": "yaml",
    "c++": "cpp",
    "cc": "cpp",
    "cxx": "cpp",
    "hpp": "cpp",
    "h": "c",
    "c#": "c_sharp",
    "cs": "c_sharp",
    "csharp": "c_sharp",
    "objc": "objective_c",
    "objective-c": "objective_c",
    "objectivec": "objective_c",
    "dockerfile": "docker_file",
    "docker": "docker_file",
    "golang": "go",
    "txt": "plain_text",
    "text": "plain_text",
    "plaintext": "plain_text",
    "plain": "plain_text",
    "log": "plain_text",
    "tf": "plain_text",
    "hcl": "plain_text",
    "md": "markdown",
    "kt": "kotlin",
    "kts": "kotlin",
    "rb": "ruby",
    "rs": "rust",
    "ps1": "powershell",
    "pwsh": "powershell",
    "proto": "protobuf",
    "htm": "html",
    "xhtml": "html",
    "vue": "html",
    "svg": "xml",
    "jsonc": "json",
    "json5": "json",
    "make": "makefile",
    "mk": "makefile",
    "pl": "perl",
    "vb": "visual_basic",
    "vbs": "vbscript",
    "tex": "latex",
    "coffee": "coffee_script",
    "coffeescript": "coffee_script",
    "sass": "scss",
    "less": "css",
    "patch": "diff",
    "ini": "properties",
    "conf": "properties",
    "cfg": "properties",
    "env": "properties",
    "gql": "graphql",
    "erl": "erlang",
    "hs": "haskell",
    "jl": "julia",
    "sol": "solidity",
    "gradle": "groovy",
    "sbt": "scala",
    "asm": "assembly",
    "nasm": "assembly",
    "mysql": "sql",
    "psql": "sql",
    "pgsql": "sql",
    "postgresql": "sql",
    "sqlite": "sql",
}
_LANG_WORD = re.compile(r"[\w#+.\-]+")

# 卡片 markdown 认的 HTML 标签；其余标签和裸露的 `<` 都转成 `&#60;` 按文字显示。
_HTML_TAGS = frozenset(
    {"br", "hr", "font", "text_tag", "a", "at", "person", "local_datetime", "link"}
)
_LT = "&#60;"
_AT_ALL_TEXT = "@所有人"

# 表格：数据行超过这个数、或同一个 markdown 组件里表格达到上限时改用 table 组件。
_MD_TABLE_ROWS = 5
_MD_TABLES_PER_BLOCK = 4
_TABLE_MAX_COLUMNS = 50
_TABLE_PAGE_SIZE = 10
_TABLE_HEADER_STYLE: dict[str, Any] = {
    "background_style": "grey",
    "text_color": "grey",
    "bold": True,
    "text_size": "normal",
}
# 多图混排：张数 → combination_mode；一段最多 9 张，更多的按 9 张一组切开。
_COMBINATION_MODES = {2: "double", 3: "triple", 4: "bisect", 5: "bisect", 6: "bisect"}
_COMBINATION_MAX = 9
_IMAGE_ICON = "🖼️"

# ---------------------------------------------------------------- 行内语法的小零件

_ASCII_PUNCT = frozenset(string.punctuation)
_ESCAPED = re.compile(r"\\([!-/:-@\[-`{-~])")
_AUTOLINK = re.compile(r"<([A-Za-z][A-Za-z0-9+.\-]{1,31}:[^<>\s]*)>")
_EMAIL_AUTOLINK = re.compile(
    r"<([A-Za-z0-9.!#$%&'*+/=?^_`{|}~\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*)>"
)
_ATTR = r"""\s+[A-Za-z_:][\w.:\-]*(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))?"""
_TAG = re.compile(rf"<(/?)([A-Za-z][A-Za-z0-9_\-]*)((?:{_ATTR})*)\s*/?>")
_ATTR_PAIR = re.compile(
    r"""([A-Za-z_:][\w.:\-]*)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?"""
)
_HR_HTML = re.compile(r"<hr\s*/?>", re.IGNORECASE)
# 裸 URL：只认 http/https，URL 字符取 RFC 3986 的 ASCII 集合（遇到中文、空白、引号即结束）。
_BARE_URL = re.compile(r"https?://[A-Za-z0-9\-._~:/?#@!$&()*+,;=%]+", re.IGNORECASE)
_URL_TRAILING = ".,:;!?*_~"
_WEB_URL = re.compile(r"https?://\S", re.IGNORECASE)
# `_斜体_` → `*斜体*`（卡片只写明了 `*` 斜体）；词内的下划线（snake_case）不动。
_UNDERSCORE_EM = re.compile(r"(?<![\w*])_(?![\s_])([^_\n]*?[^\s_\\])_(?![\w])")
# 卡片 markdown 不能连写 4 个 `*`：`**a****b**` 合并成 `**ab**`。
_FOUR_STARS = re.compile(r"(?<=\S)\*{4}(?=\S)")
_TASK = re.compile(r"\[([ xX])\][ \t]+")
_NUMBER = re.compile(r"[+\-−±]?[¥￥$€£]?\s?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s?[%‰]?")
_BLANK_CELLS = frozenset({"", "-", "—", "–", "/", "n/a", "N/A", "NA"})
_PLAIN_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_PLAIN_MARKS = re.compile(r"\*\*|__|~~|`|<[^>]*>")
_CELL_BREAK = re.compile(r"[ \t]*\n[ \t]*")


def _entity(ch: str) -> str:
    return f"&#{ord(ch)};"


def _is_web(url: str) -> bool:
    return bool(_WEB_URL.match(url))


def _clean_url(url: str) -> str:
    """链接地址里的空格和尖括号转义掉，避免把 `[t](url)` 拆坏。"""
    return url.strip().replace(" ", "%20").replace("<", "%3C").replace(">", "%3E")


def _run_length(s: str, i: int, ch: str) -> int:
    j = i
    while j < len(s) and s[j] == ch:
        j += 1
    return j - i


def _code_span_end(s: str, i: int) -> tuple[int, int]:
    """s[i] 起的反引号串：返回 (反引号个数, 行内代码结束位置)；没有闭合时结束位置为 -1。"""
    k = _run_length(s, i, "`")
    j = i + k
    while True:
        j = s.find("`", j)
        if j < 0:
            return k, -1
        m = _run_length(s, j, "`")
        if m == k:
            return k, j + k
        j += m


@lru_cache(maxsize=32)
def _bracket_pairs(s: str) -> Mapping[int, int]:
    """整段文字里配对的方括号位置 `[` → `]`，跳过转义和行内代码。

    一次线性扫描算好，免得每个 `[` 都往后找一遍（大量不配对的 `[` 会退化成平方级）。
    """
    pairs: dict[int, int] = {}
    stack: list[int] = []
    j = 0
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "`":
            k, end = _code_span_end(s, j)
            j = end if end > 0 else j + k
            continue
        if c == "[":
            stack.append(j)
        elif c == "]" and stack:
            pairs[stack.pop()] = j
        j += 1
    return pairs


def _skip_spaces(s: str, i: int, newline: bool = False) -> int:
    chars = " \t\n" if newline else " \t"
    while i < len(s) and s[i] in chars:
        i += 1
    return i


def _parse_destination(s: str, i: int) -> tuple[str, int] | None:
    """s[i] == '(' 时解析 `(url "title")`，返回 (url, 结束位置)；title 丢弃。"""
    n = len(s)
    k = _skip_spaces(s, i + 1, newline=True)
    if k < n and s[k] == "<":
        end = s.find(">", k + 1)
        if end < 0 or "\n" in s[k + 1 : end]:
            return None
        url, k = s[k + 1 : end], end + 1
    else:
        start, depth = k, 0
        while k < n:
            c = s[k]
            if c == "\\" and k + 1 < n:
                k += 2
                continue
            if c in " \t\n" or ord(c) < 0x20:
                break
            if c == "(":
                depth += 1
            elif c == ")":
                if depth == 0:
                    break
                depth -= 1
            k += 1
        url = s[start:k]
    k = _skip_spaces(s, k, newline=True)
    if k < n and s[k] in "\"'(":
        close = ")" if s[k] == "(" else s[k]
        end = k + 1
        while end < n and s[end] != close:
            end += 2 if s[end] == "\\" else 1
        if end >= n:
            return None
        k = _skip_spaces(s, end + 1, newline=True)
    if k < n and s[k] == ")":
        return _ESCAPED.sub(r"\1", url), k + 1
    return None


def _reference(refs: Mapping[str, Any], label: str) -> str | None:
    if not label.strip() or len(label) > 999:
        return None
    ref = refs.get(normalizeReference(label))
    if isinstance(ref, Mapping) and isinstance(ref.get("href"), str):
        return str(ref["href"])
    return None


class _Link(NamedTuple):
    text: str
    url: str
    end: int


def _parse_link(s: str, i: int, refs: Mapping[str, Any]) -> _Link | None:
    """s[i] == '[' 时解析内联链接 `[t](url)` 或引用式链接 `[t][id]`、`[t][]`、`[t]`。"""
    close = _bracket_pairs(s).get(i, -1)
    if close < 0:
        return None
    text, j = s[i + 1 : close], close + 1
    if j < len(s) and s[j] == "(":
        dest = _parse_destination(s, j)
        if dest is not None:
            return _Link(text, dest[0], dest[1])
    if j < len(s) and s[j] == "[":
        end = s.find("]", j + 1)
        if end > 0 and "[" not in s[j + 1 : end]:
            url = _reference(refs, s[j + 1 : end] or text)
            return _Link(text, url, end + 1) if url is not None else None
    url = _reference(refs, text)
    return _Link(text, url, j) if url is not None else None


def _tag_attrs(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _ATTR_PAIR.finditer(raw):
        out[m.group(1).lower()] = next((g for g in m.group(2, 3, 4) if g is not None), "")
    return out


def _plain(md: str) -> str:
    """行内 Markdown 去掉标记只留文字，用于表头、图片说明这类纯文本字段。"""
    text = _PLAIN_LINK.sub(r"\1", md)
    text = _PLAIN_MARKS.sub("", text)
    return _ESCAPED.sub(r"\1", text).replace("\n", " ").strip()


def _cell(text: str) -> str:
    """lark_md 单元格：`<br>` 换成的换行去掉两边空格。"""
    return _CELL_BREAK.sub("\n", text).strip()


# 已经上传过的图片：正文里直接写的就是 img_key（网关内嵌图片、技能上传后都会这样写）。
_UPLOADED = re.compile(r"img_[A-Za-z0-9_-]{1,240}")


def _uploaded(url: str) -> str | None:
    return url if _UPLOADED.fullmatch(url) else None


def _image_link(alt: str, url: str) -> str:
    """没换到 img_key 的图片退成链接；不是网址的只留说明文字。"""
    label = _plain(alt).replace("[", "(").replace("]", ")")
    if _is_web(url):
        return f"[{_IMAGE_ICON} {label or '图片'}]({_clean_url(url)})"
    return f"{_IMAGE_ICON} {label}" if label else ""


# ---------------------------------------------------------------- 行内改写


class _Inline:
    """逐字扫一段行内文本，改写成卡片 markdown。

    行内代码原样保留；链接只留 http/https 和 tel；图片换成 img_key 或退成链接；白名单外的
    HTML 标签和裸 `<` 转义；裸 URL 包成链接；行尾两空格或反斜杠的硬换行改成 `<br>`；
    反斜杠转义改成 HTML 实体。
    """

    def __init__(
        self,
        refs: Mapping[str, Any],
        images: Mapping[str, str],
        *,
        pipes: bool = False,
        lark: bool = False,
        link_text: bool = False,
        found: list[str] | None = None,
    ) -> None:
        self.refs = refs
        self.images = images
        self.pipes = pipes  # markdown 表格的单元格：`|` 要转义
        self.lark = lark  # table 组件的 lark_md 单元格：不支持图片，`<br>` 改成换行
        self.link_text = link_text  # 链接文字里：不再套链接，图片只留说明
        self.found = found  # 收集遇到的图片地址，给 image_urls 用
        self._out: list[str] = []
        self._plain: list[str] = []
        self._stacks: dict[str, list[bool]] = {"font": [], "a": []}
        self._at_all: list[int] = []  # 未闭合的 @所有人 标签：它之后的输出位置

    def run(self, s: str) -> str:
        self._out, self._plain = [], []
        self._stacks = {"font": [], "a": []}
        self._at_all = []
        i, n = 0, len(s)
        while i < n:
            c = s[i]
            if c == "\\" and i + 1 < n:
                i = self._backslash(s, i)
            elif c == "`":
                i = self._code(s, i)
            elif c == "\n":
                i = self._newline(s, i)
            elif c == "<":
                i = self._angle(s, i)
            elif (
                c == "!" and s.startswith("[", i + 1) and (img := _parse_link(s, i + 1, self.refs))
            ):
                self._emit(self._image(img))
                i = img.end
            elif c == "[" and not self.link_text and (link := _parse_link(s, i, self.refs)):
                self._emit(self._link(link))
                i = link.end
            elif c in "hH" and (end := self._bare_url(s, i)) > i:
                i = end
            else:
                self._plain.append("\\|" if c == "|" and self.pipes else c)
                i += 1
        self._flush()
        return "".join(self._out)

    # ------------------------------------------------ 输出缓冲

    def _emit(self, text: str) -> None:
        self._flush()
        self._out.append(text)

    def _flush(self) -> None:
        """普通文字段落盘前做两处改写：`_斜体_` 换成 `*斜体*`，合并连写的 `****`。"""
        if not self._plain:
            return
        text = "".join(self._plain)
        self._plain = []
        self._out.append(_FOUR_STARS.sub("", _UNDERSCORE_EM.sub(r"*\1*", text)))

    # ------------------------------------------------ 各分支

    def _backslash(self, s: str, i: int) -> int:
        nxt = s[i + 1]
        if nxt == "\n":  # 反斜杠硬换行
            self._emit("<br>")
            return _skip_spaces(s, i + 2)
        if nxt in _ASCII_PUNCT:  # 卡片没写明反斜杠转义，只写了 HTML 实体
            self._emit("\\|" if nxt == "|" and self.pipes else _entity(nxt))
            return i + 2
        self._plain.append("\\")
        return i + 1

    def _code(self, s: str, i: int) -> int:
        k, end = _code_span_end(s, i)
        if end < 0:
            self._emit("`" * k)
            return i + k
        code = s[i:end]
        self._emit(code.replace("|", "\\|") if self.pipes else code)
        return end

    def _newline(self, s: str, i: int) -> int:
        buf = "".join(self._plain)
        kept = buf.rstrip(" ")
        hard = len(buf) - len(kept) >= 2
        self._plain = [kept.rstrip("\t")]
        if hard:
            self._emit("<br>")
        else:
            self._plain.append("\n")
        return _skip_spaces(s, i + 1)

    def _angle(self, s: str, i: int) -> int:
        if m := _AUTOLINK.match(s, i):
            self._emit(self._url(m.group(1)))
            return m.end()
        if m := _EMAIL_AUTOLINK.match(s, i):
            self._emit(m.group(1))
            return m.end()
        if m := _TAG.match(s, i):
            self._emit(self._tag(m))
            return m.end()
        self._emit(_LT)
        return i + 1

    def _tag(self, m: re.Match[str]) -> str:
        raw, closing, name = m.group(0), m.group(1) == "/", m.group(2).lower()
        if name not in _HTML_TAGS:
            return _LT + raw[1:]
        if self.lark and name == "br":
            return "\n"
        if name == "at":
            # @所有人：群主没开权限时整张卡片发送失败，一律换成文字，标签里的文字一并去掉。
            if closing:
                if not self._at_all:
                    return raw
                self._flush()
                del self._out[self._at_all.pop() :]
                return ""
            if _tag_attrs(m.group(3)).get("id", "").strip().lower() == "all":
                self._emit(_AT_ALL_TEXT)
                if not raw.endswith("/>"):
                    self._at_all.append(len(self._out))
                return ""
            return raw
        stack = self._stacks.get(name)
        if stack is None:
            return raw
        if closing:  # 与开标签同进退；孤立的闭合标签丢掉
            return raw if stack and stack.pop() else ""
        attrs = _tag_attrs(m.group(3))
        if name == "font":
            keep = attrs.get("color", "").strip().lower() in style.FONT_COLORS
        else:
            keep = _is_web(attrs.get("href", ""))
        stack.append(keep)
        return raw if keep else ""

    def _image(self, img: _Link) -> str:
        if self.link_text:
            return _plain(img.text) or _IMAGE_ICON
        if self.found is not None:
            self.found.append(img.url)
        key = None if self.lark else (self.images.get(img.url) or _uploaded(img.url))
        if key:
            alt = _plain(img.text).replace("[", "(").replace("]", ")")
            return f"![{alt}]({key})"
        return _image_link(img.text, img.url)

    def _link(self, link: _Link) -> str:
        nested = _Inline(self.refs, self.images, pipes=self.pipes, lark=self.lark, link_text=True)
        text = nested.run(link.text).strip()
        url = link.url.strip()
        low = url.lower()
        if _is_web(url):
            return f"[{text or url}]({_clean_url(url)})"
        if low.startswith("tel:"):
            tel = url if low.startswith("tel://") else "tel://" + url[4:]
            return f"[{text or url}]({_clean_url(tel)})"
        if low.startswith("mailto:"):
            addr = url[7:].split("?", 1)[0]
            if not text:
                return addr
            return text if not addr or addr in text else f"{text}（{addr}）"
        return text  # 相对路径、javascript: 等：卡片不认，只留文字

    def _url(self, url: str) -> str:
        """尖括号自动链接 `<scheme:...>`。"""
        low = url.lower()
        if self.link_text:
            return url
        if _is_web(url):
            return f"[{url}]({_clean_url(url)})"
        if low.startswith("tel:"):
            return self._link(_Link(url, url, 0))
        if low.startswith("mailto:"):
            return url[7:]
        return url

    def _bare_url(self, s: str, i: int) -> int:
        """裸 URL 包成 `[url](url)`；返回结束位置，不是 URL 时原样返回 i。"""
        if i and s[i - 1].isascii() and (s[i - 1].isalnum() or s[i - 1] in "/@.-_=&"):
            return i
        m = _BARE_URL.match(s, i)
        if not m:
            return i
        url = m.group(0)
        while url:
            if url[-1] in _URL_TRAILING:
                url = url[:-1]
            elif url[-1] == ")" and url.count("(") < url.count(")"):
                url = url[:-1]
            else:
                break
        if len(url) <= url.index("//") + 2:
            return i
        self._emit(url if self.link_text else f"[{url}]({url})")
        return i + len(url)


# ---------------------------------------------------------------- 块结构


def _prepare(
    md: str, refs: Mapping[str, Any] | None = None
) -> tuple[str, list[Token], dict[str, Any]]:
    """解析成块 token，返回 (规范换行后的原文, token, 引用式链接定义)。

    refs 是整篇的链接定义：切片单独解析时带上，`[t][id]` 才能展开。脚注定义 `[^1]: ...`
    会被 CommonMark 当成链接定义吃掉，这里把那一行的 `[` 转义后重新解析，保住原文。
    """
    src = md.replace("\r\n", "\n").replace("\r", "\n").replace("\0", "\ufffd")
    base = {k: v for k, v in (refs or {}).items() if not k.startswith("^")}
    env: dict[str, Any] = {"references": dict(base)}
    tokens = _MD.parse(src, env)
    notes = [
        ref.get("map")
        for key, ref in env["references"].items()
        if key.startswith("^") and isinstance(ref, Mapping)
    ]
    rows = sorted({m[0] for m in notes if isinstance(m, list) and m})
    if rows:
        lines = src.split("\n")
        for row in rows:
            lines[row] = lines[row].replace("[", "\\[", 1)
        src = "\n".join(lines)
        env = {"references": dict(base)}
        tokens = _MD.parse(src, env)
    return src, tokens, dict(env["references"])


def _split(tokens: Sequence[Token]) -> list[tuple[Token, list[Token]]]:
    """同一层的块：返回 (开标签或自闭合 token, 它包住的子 token)。"""
    out: list[tuple[Token, list[Token]]] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.nesting == 1:
            depth, j = 1, i + 1
            while j < len(tokens) and depth:
                depth += tokens[j].nesting
                j += 1
            out.append((tok, list(tokens[i + 1 : j - 1])))
            i = j
            continue
        if tok.nesting == 0:
            out.append((tok, []))
        i += 1
    return out


def _code_lang(info: str) -> str:
    """代码块语言：别名映射到飞书的名字，认不出的返回空串（去掉标记）。"""
    m = _LANG_WORD.match(info.strip().lstrip("{."))
    if not m:
        return ""
    word = m.group(0).lower()
    for cand in (word, word.rstrip("0123456789.")):
        lang = _LANG_ALIASES.get(cand, cand)
        if lang in CODE_LANGUAGES:
            return lang
    return ""


def _fence(tok: Token) -> list[str]:
    """围栏代码块：统一用反引号围栏，围栏行不带前后空格（流式更新文本接口的要求）。"""
    body = tok.content[:-1] if tok.content.endswith("\n") else tok.content
    lines = body.split("\n") if body else []
    longest = max((_run_length(line.lstrip(" "), 0, "`") for line in lines), default=0)
    fence = "`" * (longest + 1) if longest >= 3 else "```"
    return [fence + _code_lang(tok.info), *lines, fence]


def _task_box(text: str) -> str:
    """任务列表 `[ ] x` / `[x] x` → `☐ x` / `☑ x`（卡片没写明任务列表）。"""
    m = _TASK.match(text)
    if not m:
        return text
    return ("☐ " if m.group(1) == " " else "☑ ") + text[m.end() :]


def _is_tight(items: Sequence[tuple[Token, list[Token]]]) -> bool:
    """markdown-it 把紧凑列表里的段落标成 hidden。"""
    for _, inner in items:
        for child, _ in _split(inner):
            if child.type == "paragraph_open":
                return child.hidden
    return True


def _bullet(marker: str, body: list[str]) -> list[str]:
    """列表项：首行接在标记后面，其余行每层缩进 4 空格。"""
    if not body:
        return [marker]
    pad = " " * max(4, len(marker) + 1)
    rest = [pad + line if line else "" for line in body[1:]]
    if body[0].startswith("```"):  # 项里第一块就是代码：标记单独一行，免得多出缩进
        return [marker, pad + body[0], *rest]
    return [f"{marker} {body[0]}", *rest]


def _table_cells(inner: Sequence[Token]) -> tuple[list[str], list[str], list[list[str]]]:
    """GFM 表格 → (表头, 每列对齐 left/right/center/空串, 数据行)；数据行按表头补齐列数。"""
    header: list[str] = []
    aligns: list[str] = []
    rows: list[list[str]] = []
    row: list[str] = []
    in_head = False
    for tok in inner:
        if tok.type == "thead_open":
            in_head = True
        elif tok.type == "tbody_open":
            in_head = False
        elif tok.type == "tr_open":
            row = []
        elif tok.type == "th_open":
            aligns.append(str(tok.attrs.get("style", "")).removeprefix("text-align:"))
        elif tok.type == "inline":
            row.append(tok.content)
        elif tok.type == "tr_close":
            if in_head:
                header = row
            else:
                rows.append(row)
    width = len(header)
    return header, aligns[:width], [(r + [""] * width)[:width] for r in rows]


_DELIMITERS = {"left": ":---", "right": "---:", "center": ":---:"}


class _Writer:
    """把 markdown-it 的块 token 重新写成卡片 markdown（列表、代码块、分割线、标题都改写法）。"""

    def __init__(self, refs: Mapping[str, Any], images: Mapping[str, str]) -> None:
        self.refs = refs
        self.images = images

    def inline(self, text: str, *, pipes: bool = False) -> str:
        return _Inline(self.refs, self.images, pipes=pipes).run(text)

    def blocks(self, tokens: Sequence[Token], *, tight: bool = False) -> list[str]:
        out: list[str] = []
        for tok, inner in _split(tokens):
            lines = self.block(tok, inner)
            if not lines:
                continue
            if out and not tight:
                out.append("")
            out.extend(lines)
        return out

    def block(self, tok: Token, inner: list[Token]) -> list[str]:
        kind = tok.type
        content = inner[0].content if inner and inner[0].type == "inline" else ""
        if kind == "paragraph_open":
            return self.paragraph(content)
        if kind == "heading_open":
            # Setext 标题（下划线 === / ---）一律改写成 ATX，层级不变。
            level = {"=": 1, "-": 2}.get(tok.markup, len(tok.markup))
            text = self.inline(content).replace("\n", " ").strip()
            return [f"{'#' * max(1, min(6, level))} {text}"] if text else []
        if kind in ("bullet_list_open", "ordered_list_open"):
            return self.list_block(tok, inner)
        if kind == "blockquote_open":
            return [f"> {line}" if line else ">" for line in self.blocks(inner)]
        if kind == "fence":
            return _fence(tok)
        if kind == "hr" or (kind == "html_block" and _HR_HTML.fullmatch(tok.content.strip())):
            return ["<hr>"]  # 独占一行；块之间本来就隔空行，不会被当成 Setext 标题
        if kind in ("html_block", "code_block"):
            # HTML 块卡片不支持，按文字显示；缩进 ≥4 空格的「代码」几乎都是误缩进的文字。
            return self.paragraph(tok.content)
        if kind == "table_open":
            return self.table(inner)
        return self.paragraph(tok.content) if tok.content else self.blocks(inner)

    def paragraph(self, text: str) -> list[str]:
        out = self.inline(text.strip("\n").lstrip(" \t")).strip()
        return out.split("\n") if out else []

    def list_block(self, tok: Token, inner: list[Token]) -> list[str]:
        ordered = tok.type == "ordered_list_open"
        items = [(item, sub) for item, sub in _split(inner) if item.type == "list_item_open"]
        tight = _is_tight(items)
        out: list[str] = []
        for n, (item, sub) in enumerate(items):
            marker = f"{item.info if item.info.isdigit() else n + 1}." if ordered else "-"
            if n and not tight:
                out.append("")
            out.extend(_bullet(marker, self.item(sub, tight=tight)))
        return out

    def item(self, inner: list[Token], *, tight: bool) -> list[str]:
        children = _split(inner)
        if children and children[0][0].type == "paragraph_open" and children[0][1]:
            para = children[0][1][0]
            para.content = _task_box(para.content)
        return self.blocks(inner, tight=tight)

    def table(self, inner: list[Token]) -> list[str]:
        header, aligns, rows = _table_cells(inner)
        if not header:
            return []

        def row(cells: list[str]) -> str:
            return "| " + " | ".join(self.inline(c, pipes=True).strip() for c in cells) + " |"

        delimiter = "| " + " | ".join(_DELIMITERS.get(a, "---") for a in aligns) + " |"
        return [row(header), delimiter, *(row(r) for r in rows)]


def _render_text(
    tokens: Sequence[Token], refs: Mapping[str, Any], images: Mapping[str, str]
) -> str:
    return "\n".join(_Writer(refs, images).blocks(tokens)).strip("\n")


def normalize(md: str, images: Mapping[str, str] | None = None) -> str:
    """GFM → 飞书卡片 markdown 组件能正确渲染的写法（纯文本变换，不拆组件）。

    images：图片 URL → img_key；有 key 的图片写成 `![alt](img_key)`，没有的退成链接。
    """
    _, tokens, refs = _prepare(md)
    return _render_text(tokens, refs, images or {})


# ---------------------------------------------------------------- 组件


def _numeric(values: Sequence[str]) -> bool:
    """一列除表头外全是数字（可带千分位、%、货币符号；空单元格和 `-` 不算）。"""
    seen = False
    for value in values:
        text = value.strip().strip("*_").strip()
        if text in _BLANK_CELLS:
            continue
        if not _NUMBER.fullmatch(text):
            return False
        seen = True
    return seen


def _only_images(text: str, refs: Mapping[str, Any]) -> list[tuple[str, str]] | None:
    """段落里只有图片（和空白）时返回 [(alt, url)]，否则 None。"""
    found: list[tuple[str, str]] = []
    i = 0
    while i < len(text):
        if text[i] in " \t\n":
            i += 1
            continue
        if text.startswith("![", i) and (img := _parse_link(text, i + 1, refs)):
            found.append((img.text, img.url))
            i = img.end
            continue
        return None
    return found or None


_Part = tuple[int, int] | str


class _Blocks:
    """render_markdown 的累加器：相邻的文字块攒成一个 markdown 组件，遇到原生组件先落盘。"""

    def __init__(self, ctx: RenderContext, refs: Mapping[str, Any], lines: list[str]) -> None:
        self.ctx = ctx
        self.refs = refs
        self.lines = lines
        self.out: list[dict[str, Any]] = []
        # 待合并的文字：(起, 止) 是原文行区间，落盘时再规范化；str 是已经写好的卡片 markdown。
        self._parts: list[_Part] = []
        self._tables = 0  # 当前 markdown 组件里留下的表格数

    def text(self, span: Sequence[int] | None) -> None:
        if not span:
            return
        last = self._parts[-1] if self._parts else None
        if isinstance(last, tuple):
            self._parts[-1] = (last[0], span[1])
        else:
            self._parts.append((span[0], span[1]))

    def ready(self, md: str) -> None:
        if md:
            self._parts.append(md)

    def add(self, build: Callable[[], dict[str, Any]]) -> None:
        self.flush()
        self.out.append(build())

    def flush(self) -> None:
        chunks: list[str] = []
        for part in self._parts:
            if isinstance(part, str):
                chunks.append(part)
                continue
            _, tokens, refs = _prepare("\n".join(self.lines[part[0] : part[1]]), self.refs)
            chunks.append(_render_text(tokens, refs, self.ctx.images))
        self._parts, self._tables = [], 0
        content = "\n\n".join(c for c in chunks if c.strip())
        if content:
            self.out.append(
                {"tag": "markdown", "element_id": self.ctx.new_id("md"), "content": content}
            )

    # ------------------------------------------------ 表格

    def table(self, tok: Token, inner: list[Token]) -> None:
        header, aligns, rows = _table_cells(inner)
        numeric = [_numeric([row[c] for row in rows]) for c in range(len(header))]
        wanted = (
            len(rows) > _MD_TABLE_ROWS
            or any(aligns)
            or any(numeric)
            or self._tables >= _MD_TABLES_PER_BLOCK
        )
        if wanted and 0 < len(header) <= _TABLE_MAX_COLUMNS and self.ctx.take_table():
            self.add(lambda: self._table(header, aligns, rows, numeric))
            return
        if self._tables >= _MD_TABLES_PER_BLOCK:  # 一个 markdown 组件最多 4 个表格
            self.flush()
        self.text(tok.map)
        self._tables += 1

    def _table(
        self, header: list[str], aligns: list[str], rows: list[list[str]], numeric: list[bool]
    ) -> dict[str, Any]:
        cell = _Inline(self.refs, {}, lark=True)
        columns: list[dict[str, Any]] = []
        for c, title in enumerate(header):
            column: dict[str, Any] = {
                "name": f"c{c}",
                "display_name": _plain(title),
                "data_type": "lark_md",
            }
            align = aligns[c] if c < len(aligns) else ""
            if align or numeric[c]:
                column["horizontal_align"] = align or "right"
            columns.append(column)
        return {
            "tag": "table",
            "element_id": self.ctx.new_id("tbl"),
            "page_size": max(1, min(_TABLE_PAGE_SIZE, len(rows))),
            "row_height": "auto",
            "header_style": dict(_TABLE_HEADER_STYLE),
            "columns": columns,
            "rows": [{f"c{c}": _cell(cell.run(v)) for c, v in enumerate(row)} for row in rows],
        }

    # ------------------------------------------------ 图片

    def images(self, pairs: list[tuple[str, str]]) -> None:
        """独占一段的图片：有 key 的连续几张合成 img / img_combination，没 key 的退成链接。"""
        run: list[tuple[str, str]] = []
        for alt, url in pairs:
            key = self.ctx.image_key(url) or _uploaded(url)
            if key:
                run.append((_plain(alt), key))
                continue
            self._image_run(run)
            run = []
            self.ready(_image_link(alt, url))
        self._image_run(run)

    def _image_run(self, run: list[tuple[str, str]]) -> None:
        for start in range(0, len(run), _COMBINATION_MAX):
            chunk = run[start : start + _COMBINATION_MAX]
            self.flush()
            self.out.append(self._img(*chunk[0]) if len(chunk) == 1 else self._combination(chunk))

    def _img(self, alt: str, key: str) -> dict[str, Any]:
        return {
            "tag": "img",
            "element_id": self.ctx.new_id("img"),
            "img_key": key,
            "alt": {"tag": "plain_text", "content": alt},
            "scale_type": "fit_horizontal",
            "corner_radius": style.RADIUS,
            "preview": True,
        }

    def _combination(self, chunk: list[tuple[str, str]]) -> dict[str, Any]:
        return {
            "tag": "img_combination",
            "element_id": self.ctx.new_id("img"),
            "combination_mode": _COMBINATION_MODES.get(len(chunk), "trisect"),
            "corner_radius": style.RADIUS,
            "img_list": [{"img_key": key} for _, key in chunk],
        }


def render_markdown(md: str, ctx: RenderContext) -> list[dict[str, Any]]:
    """顶层切块后转成组件：markdown / table / img / img_combination / hr。

    按顶层块 token 的行号范围从原文切片，相邻的段落、标题、列表、引用、代码块合并成一个
    markdown 组件；GFM 表格满足条件时换成 table 组件（名额由 ctx 记账），独占一段的图片换成
    img / img_combination，分割线换成 hr。空内容不产出组件。
    """
    src, tokens, refs = _prepare(md)
    acc = _Blocks(ctx, refs, src.split("\n"))
    for tok, inner in _split(tokens):
        content = inner[0].content if inner and inner[0].type == "inline" else ""
        if tok.type == "table_open":
            acc.table(tok, inner)
        elif tok.type == "hr" or (
            tok.type == "html_block" and _HR_HTML.fullmatch(tok.content.strip())
        ):
            acc.add(lambda: {"tag": "hr", "element_id": ctx.new_id("hr")})
        elif tok.type == "paragraph_open" and (pairs := _only_images(content, refs)):
            acc.images(pairs)
        else:
            acc.text(tok.map)
    acc.flush()
    return acc.out


def image_urls(md: str) -> list[str]:
    """正文里所有 http(s) 图片地址（去重保序），供调用方先上传换 img_key。

    代码块和行内代码里的不算；地址与 render_markdown 查 ctx.images 时用的写法一致。
    """
    _, tokens, refs = _prepare(md)
    found: list[str] = []
    scanner = _Inline(refs, {}, found=found)
    for tok in tokens:
        if tok.type in ("inline", "html_block", "code_block"):
            scanner.run(tok.content)
    return list(dict.fromkeys(url for url in found if _is_web(url)))
