"""从文本里认出飞书云文档链接。工具只收链接：token 一律从这里解析，不收模型拼的裸 token。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qs, urlsplit

Kind = Literal["docx", "wiki", "sheets", "base"]
# 链接路径里的类型 → 统一的种类；docs（旧版文档）、思维笔记、幻灯片、文件暂不支持。
_KINDS: dict[str, Kind] = {"docx": "docx", "wiki": "wiki", "sheets": "sheets", "base": "base"}
UNSUPPORTED = frozenset({"docs", "mindnotes", "slides", "file", "minutes"})
# 协作者、公共设置接口里的 type。
PERMISSION_TYPES: dict[Kind, str] = {
    "docx": "docx",
    "wiki": "wiki",
    "sheets": "sheet",
    "base": "bitable",
}
_HOSTS = (".feishu.cn", ".larksuite.com", ".larkoffice.com", ".feishu.net")
_URL = re.compile(r"https://[A-Za-z0-9.-]+/[A-Za-z]+/[A-Za-z0-9]+[^\s<>\"'()\[\]）】，。]*")
_TOKEN = re.compile(r"[A-Za-z0-9]{8,64}\Z")
_PARAM = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
MAX_LINKS = 20


@dataclass(frozen=True)
class Link:
    kind: Kind
    token: str
    url: str
    # 多维表格链接里的 ?table= / ?view=，电子表格的 ?sheet=。
    table: str | None = None
    view: str | None = None
    sheet: str | None = None

    @property
    def permission_type(self) -> str:
        return PERMISSION_TYPES[self.kind]


class LinkError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _param(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name) or []
    return values[0] if values and _PARAM.fullmatch(values[0]) else None


def parse(url: str) -> Link:
    """一条链接 → Link；不是飞书文档链接抛 invalid_link，类型不支持抛 unsupported_type。"""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        raise LinkError("invalid_link") from None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host.endswith(_HOSTS) or parts.username or parts.port:
        raise LinkError("invalid_link")
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2:
        raise LinkError("invalid_link")
    kind, token = segments[0].lower(), segments[1]
    if kind in UNSUPPORTED:
        raise LinkError("unsupported_type")
    if kind not in _KINDS or not _TOKEN.fullmatch(token):
        raise LinkError("invalid_link")
    query = parse_qs(parts.query)
    canonical = f"https://{host}/{kind}/{token}"
    return Link(
        _KINDS[kind],
        token,
        canonical,
        table=_param(query, "table"),
        view=_param(query, "view"),
        sheet=_param(query, "sheet"),
    )


def find(text: str) -> list[Link]:
    """文本里出现的飞书文档链接，按出现顺序去重（按 token）；认不出的跳过。"""
    found: dict[str, Link] = {}
    for match in _URL.finditer(text or ""):
        try:
            link = parse(match.group(0))
        except LinkError:
            continue
        found.setdefault(link.token, link)
        if len(found) >= MAX_LINKS:
            break
    return list(found.values())
