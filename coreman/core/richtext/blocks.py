"""把一条回复切成有序片段：普通 Markdown 与 ```` ```card:<类型> ```` 富内容块交替出现。

围栏按 CommonMark 识别：行首 0–3 个空格加至少 3 个 ` 或 ~；关闭围栏同字符、不短于开启围栏、
后面只有空白。普通代码块整段当 Markdown 原样保留，模型在代码块里演示块语法时不会被当成块。

流式时文本随时会在任意位置截断，这里对任何前缀都不能抛异常；末尾没写完的块给 pending，
由渲染器显示占位，不把半截 JSON 露给用户。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from coreman.core.richtext.schema import BLOCK_KINDS, validate_block

SegmentKind = Literal["markdown", "block", "pending", "invalid"]


@dataclass(frozen=True)
class Segment:
    """一个片段。markdown 的 text 是原文；其余三种的 text 是块内的原始 JSON 文本。"""

    kind: SegmentKind
    text: str
    # 块类型名（card: 后面的部分，小写）；markdown 片段为 None。
    block: str | None = None
    # kind == "block" 时为 validate_block 的结果。模型里有列表，不参与哈希。
    model: BaseModel | None = field(default=None, hash=False)
    # kind == "invalid" 时的原因，给日志看。
    error: str | None = None


# 只认 \n 断行：str.splitlines 还会在  、\x0c 等处断开，和 Markdown 的行不一致。
_LINE = re.compile(r"[^\n]*\n|[^\n]+\Z")
_FENCE_OPEN = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
_FENCE_CLOSE = re.compile(r" {0,3}(`{3,}|~{3,})[ \t]*")
_CARD_INFO = re.compile(r"card\s*:\s*([^\s`]+)", re.IGNORECASE)
# 流式末尾还没写完的一行，可能长成围栏开头：行首若干个 ` 或 ~，后面可能跟着半截 info。
_FENCE_START = re.compile(r" {0,3}(`+|~+)(.*)")
_CARD_HINT = re.compile(
    r"^ {0,3}(?:`{3,}|~{3,})[^\S\n]*card[^\S\n]*:", re.IGNORECASE | re.MULTILINE
)
_LEADING_BLANK_LINES = re.compile(r"\A(?:[ \t]*\r?\n)+")


@dataclass
class _Fence:
    char: str
    length: int
    # card 块的类型名；普通代码块为 None。
    card: str | None
    body: list[str] = field(default_factory=list)


@dataclass
class _Piece:
    """扫描结果：markdown 原文，或一个 card 块（closed 表示是否见到关闭围栏）。"""

    markdown: str | None = None
    card: str | None = None
    body: str = ""
    closed: bool = False


def _opening(content: str) -> _Fence | None:
    m = _FENCE_OPEN.fullmatch(content)
    if m is None:
        return None
    marks, info = m.group(1), m.group(2)
    # CommonMark：反引号围栏的 info string 里不能再有反引号，否则那是一行行内代码。
    if marks[0] == "`" and "`" in info:
        return None
    card = _CARD_INFO.fullmatch(info.strip())
    return _Fence(marks[0], len(marks), card.group(1).lower() if card else None)


def _closes(content: str, fence: _Fence) -> bool:
    m = _FENCE_CLOSE.fullmatch(content)
    return m is not None and m.group(1)[0] == fence.char and len(m.group(1)) >= fence.length


def _maybe_card_opening(content: str) -> bool:
    """流式末尾半行：再多几个字就可能成为 card 围栏的开头（``、```、```ca、```card: 等）。

    这种半行先不显示；否则它先作为正文出现，下一帧又被块吃掉，已显示的文字会倒退。
    """
    m = _FENCE_START.fullmatch(content)
    if m is None:
        return False
    marks, info = m.group(1), m.group(2)
    if len(marks) < 3:
        return info == ""
    if marks[0] == "`" and "`" in info:
        return False
    return "card:".startswith(re.sub(r"\s", "", info).lower())


def _scan(text: str, *, final: bool) -> list[_Piece]:
    pieces: list[_Piece] = []
    markdown: list[str] = []
    fence: _Fence | None = None

    def flush() -> None:
        if markdown:
            pieces.append(_Piece(markdown="".join(markdown)))
            markdown.clear()

    for line in _LINE.findall(text):
        content = line.rstrip("\r\n")
        if fence is None:
            opened = _opening(content)
            if opened is not None and opened.card is not None:
                flush()
                fence = opened
                continue
            if not final and not line.endswith("\n") and _maybe_card_opening(content):
                continue
            markdown.append(line)
            fence = opened
        elif _closes(content, fence):
            if fence.card is None:
                markdown.append(line)
            else:
                pieces.append(_Piece(card=fence.card, body="".join(fence.body), closed=True))
            fence = None
        elif fence.card is None:
            markdown.append(line)
        else:
            fence.body.append(line)

    flush()
    if fence is not None and fence.card is not None:
        pieces.append(_Piece(card=fence.card, body="".join(fence.body)))
    return pieces


def _strip_edges(markdown: str) -> str:
    """去掉首尾空行；首行的缩进保留（四格缩进可能是缩进代码块）。"""
    return _LEADING_BLANK_LINES.sub("", markdown).rstrip()


def _block(kind: str, raw: str) -> Segment:
    if kind not in BLOCK_KINDS:
        return Segment("invalid", raw, block=kind, error=f"未知的块类型 {kind!r}")
    try:
        data = parse_json(raw)
    except ValueError as exc:
        return Segment("invalid", raw, block=kind, error=str(exc))
    # 不只接 BlockError：校验器对奇怪的数据偶尔抛别的异常（如对数字取 .get），同样按坏块处理，
    # 一个块不能拖垮整条回复。
    try:
        model = validate_block(kind, data)
    except Exception as exc:
        return Segment("invalid", raw, block=kind, error=str(exc) or type(exc).__name__)
    return Segment("block", raw, block=kind, model=model)


def split(text: str, *, final: bool = True) -> list[Segment]:
    """把回复切成有序片段。

    final=False 用于流式中途：末尾未闭合的 card 块给 pending，末尾可能长成 card 围栏的半行不输出。
    final=True 用于定稿：未闭合的块按已有内容尝试解析，成功算 block，否则 invalid。
    """
    segments: list[Segment] = []
    for piece in _scan(text, final=final):
        if piece.markdown is not None:
            md = _strip_edges(piece.markdown)
            if not md:
                continue
            last = segments[-1] if segments else None
            if last is not None and last.kind == "markdown":
                segments[-1] = Segment("markdown", f"{last.text}\n\n{md}")
            else:
                segments.append(Segment("markdown", md))
            continue
        kind = piece.card or ""
        raw = piece.body.strip()
        if not piece.closed and not final:
            segments.append(Segment("pending", raw, block=kind))
        elif not piece.closed:
            segments.extend(_unclosed(kind, raw))
        else:
            segments.append(_block(kind, raw))
    return segments


def _unclosed(kind: str, raw: str) -> list[Segment]:
    """定稿时忘了关围栏：开头若是一段完整的 JSON 就当块，后面的文字仍按正文显示。"""
    try:
        _obj, end = _decoder(strict=False).raw_decode(raw)
    except (ValueError, RecursionError):
        return [_block(kind, raw)]
    head, rest = raw[:end], _strip_edges(raw[end:])
    return [_block(kind, head), *([Segment("markdown", rest)] if rest else [])]


def has_blocks(text: str) -> bool:
    """回复里是否有 card 块（含未闭合、格式错误的）；不做 JSON 解析。"""
    if _CARD_HINT.search(text) is None:
        return False
    return any(p.markdown is None for p in _scan(text, final=True))


# 字符串外的全角标点，JSON 里不可能合法出现，只可能是笔误。
_FULLWIDTH = {"，": ",", "：": ":"}
# 中文引号当字符串定界符用：开头是这些，结尾认对应的一组（也认半角双引号，模型常混写）。
_CN_OPEN = {"“": '”“"', "”": '”“"', "‘": "’‘", "’": "’‘"}


def _next_meaningful(raw: str, i: int) -> str:
    """跳过空白和 // 注释，返回下一个有意义的字符（没有则空串）。"""
    n = len(raw)
    while i < n:
        if raw[i].isspace():
            i += 1
        elif raw.startswith("//", i):
            end = raw.find("\n", i)
            i = n if end < 0 else end
        else:
            return raw[i]
    return ""


def _repair(raw: str) -> str:
    """按字符串内外分别修：外面去掉 // 注释和尾逗号、中文引号改成半角、全角逗号冒号改半角；
    里面原样（半角字符串里的中文引号是正文）。
    """
    out: list[str] = []
    i, n = 0, len(raw)
    closers: str | None = None  # 当前在字符串里时，能结束它的字符
    while i < n:
        ch = raw[i]
        if closers is not None:
            if ch == "\\" and i + 1 < n:
                out.append(raw[i : i + 2])
                i += 2
                continue
            if ch in closers:
                out.append('"')
                closers = None
            elif ch == '"':
                out.append('\\"')  # 中文引号括起来的字符串里的半角引号
            else:
                out.append(ch)
            i += 1
            continue
        if ch == '"':
            closers = '"'
            out.append(ch)
        elif ch in _CN_OPEN:
            closers = _CN_OPEN[ch]
            out.append('"')
        elif raw.startswith("//", i):
            end = raw.find("\n", i)
            i = n if end < 0 else end
            continue
        elif ch in ",，" and _next_meaningful(raw, i + 1) in ("}", "]"):
            pass
        else:
            out.append(_FULLWIDTH.get(ch, ch))
        i += 1
    return "".join(out)


# 超出这个量级的数字在报表里没有意义，还会让下游 float()/格式化溢出，按缺失处理。
_MAX_NUMBER = 1e15


def _finite(text: str) -> float | None:
    value = float(text)
    return value if math.isfinite(value) and abs(value) <= _MAX_NUMBER else None


def _bounded_int(text: str) -> int | float | None:
    value = int(text)
    return value if abs(value) <= _MAX_NUMBER else None


def _decoder(*, strict: bool) -> json.JSONDecoder:
    """NaN / Infinity / 超大数字一律解析成 None：它们会让卡片 JSON 非法或让格式化溢出。"""
    return json.JSONDecoder(
        parse_float=_finite,
        parse_int=_bounded_int,
        parse_constant=lambda _name: None,
        strict=strict,
    )


def _scrub(value: Any) -> Any:
    """`"\\ud83d"` 这类孤立代理字符：Python 能装进 str，但编码成 UTF-8 时整条回复都会失败。"""
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return value.encode("utf-8", "replace").decode("utf-8")
        return value
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, dict):
        return {_scrub(k): _scrub(v) for k, v in value.items()}
    return value


def parse_json(raw: str) -> Any:
    """宽松 JSON：先严格解析；失败后修尾逗号、中文引号、// 行注释再试；仍失败抛 ValueError。

    修补分两轮：先按字符串内外区别对待，保住字符串里正当使用的中文引号；
    再把中文双引号一律当定界符，兜住 `"名称”` 这种开合混写。
    """
    try:
        return _scrub(_decoder(strict=True).decode(raw))
    except (ValueError, RecursionError) as exc:
        first = exc
    # 单引号不参与第二轮：英文撇号（it’s）很常见，一律替换反而会弄坏正文。
    for candidate in (_repair(raw), _repair(re.sub(r"[“”]", '"', raw))):
        try:
            return _scrub(_decoder(strict=False).decode(candidate))
        except (ValueError, RecursionError):
            continue
    if isinstance(first, json.JSONDecodeError):
        raise ValueError(f"JSON 解析失败：{first.msg}（第 {first.lineno} 行第 {first.colno} 列）")
    raise ValueError(f"JSON 解析失败：{type(first).__name__}")
