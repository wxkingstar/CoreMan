"""个人助理导入 / 导出用的压缩包：格式、解析与安全检查。

纯函数：不碰数据库，不往磁盘写任何东西，包只在内存里读。

格式（导入与导出相同）：

- 一个 zip。包里每个 `.md` 文件是一页，页的路径是文件在包里的相对路径去掉结尾的 `.md`
  （`人/李四.md` → `人/李四`）；不剥掉最外层目录。
- 文件开头可以有一段 YAML front matter：第一行是 `---`，到下一个只有 `---` 的行为止，中间
  是一个 YAML 映射。只采用三个键：`title`、`summary`（标量，按字符串用）与 `pinned`（布尔），
  其余键忽略。收尾的 `---` 之后的全部内容是正文；没有 front matter 的文件整篇是正文。
- 包根目录下可选的 `followups.json`：待跟进清单，语法见 `_followups`。
- 系统自带的压缩功能顺手带上的文件（`.DS_Store`、`__MACOSX/` 下的一切、`Thumbs.db`、名字以
  `._` 开头的文件）跳过不读，只计数（`Parsed.ignored`）；除此之外的任何别的文件都让整个包被
  拒绝。

任何一条安全检查不过，整个包都拒绝（`ArchiveError`），见 `parse`。
"""

from __future__ import annotations

import io
import json
import re
import stat
import unicodedata
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

import yaml

from coreman.core.assistants import followups, notes
from coreman.core.db.models import FOLLOWUP_CATEGORIES

MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
# 解包后的合计：知识库正文的 20MB 上限，加上 front matter 与待跟进清单的余量。正文合计是否
# 放得进知识库由 `transfer.plan` 按 `notes.MAX_TOTAL_BYTES` 另算。
MAX_UNPACKED_BYTES = notes.MAX_TOTAL_BYTES + 12 * 1024 * 1024
MAX_PAGE_FILES = notes.MAX_PAGES
# 包里条目总数（含目录条目）的硬上限：读中央目录之后、读任何内容之前就判断。
MAX_RAW_ENTRIES = 4096
MAX_HEADER_CHARS = 4096
MAX_FOLLOWUP_ITEMS = followups.MAX_OPEN
MAX_SHOWN_NAME = 200
FOLLOWUPS_FILE = "followups.json"
FORMAT_VERSION = 1
PAGE_SUFFIX = ".md"
NESTED_SUFFIXES = (".zip", ".jar", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".zst", ".7z", ".rar")
# 系统自带的压缩功能带上的文件：跳过不读，不因为它们拒绝整个包。
IGNORED_NAMES = frozenset({".ds_store", "thumbs.db"})
IGNORED_PREFIX = "._"
IGNORED_FOLDER = "__MACOSX"
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_FENCE = "---"
_BOM = "﻿"
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ArchiveError(Exception):
    """整个包被拒绝；`code` 是原因代码，`detail` 是所有者改正时用得上的数或文件名。"""

    def __init__(self, code: str, **detail: Any) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class _BadHeader(Exception):
    """front matter 不是合法的 YAML。"""


@dataclass(frozen=True)
class PageFile:
    """包里的一个页面文件。`problem` 不为空时这一个文件不能导入（其余字段无意义）。"""

    path: str
    title: str | None
    summary: str | None
    pinned: bool | None
    body: str
    problem: str | None = None


@dataclass(frozen=True)
class FollowupItem:
    """`followups.json` 里的一条；`index` 是它在清单里的位置（从 1 起）。"""

    index: int
    category: str
    title: str
    notes: str | None
    review_on: date | None
    due_on: date | None
    snoozed: bool
    problem: str | None = None


@dataclass(frozen=True)
class Parsed:
    pages: list[PageFile]
    followups: list[FollowupItem]
    # 跳过没读的系统文件个数（`.DS_Store` 之类）。
    ignored: int = 0


def _shown(name: str) -> str:
    return name[:MAX_SHOWN_NAME]


def _unsafe(name: str) -> bool:
    """文件名是否可能越出包的根：绝对路径、反斜杠、`.` / `..` 段、空段、控制字符。"""
    parts = name.rstrip("/").split("/")
    return (
        name.startswith("/")
        or "\\" in name
        or any(part in ("", ".", "..") for part in parts)
        or any(unicodedata.category(char)[0] == "C" for char in name)
    )


def _clutter(name: str) -> bool:
    """系统自带的压缩功能带上的文件或目录：`.DS_Store`、`Thumbs.db`、`._*`、`__MACOSX/` 下的。"""
    parts = name.rstrip("/").split("/")
    return (
        IGNORED_FOLDER in parts
        or parts[-1].lower() in IGNORED_NAMES
        or parts[-1].startswith(IGNORED_PREFIX)
    )


def _check(info: zipfile.ZipInfo) -> bool:
    """只看中央目录里的一条，不读内容。返回这一条要不要读：目录与要忽略的系统文件为假。"""
    name = info.filename
    # zipfile 在第一个 NUL 处截断文件名：原始文件名里有 NUL 的一律拒绝。
    if "\x00" in info.orig_filename or _unsafe(name):
        raise ArchiveError("unsafe_path", name=_shown(name))
    kind = stat.S_IFMT(info.external_attr >> 16)
    if kind == stat.S_IFLNK:
        raise ArchiveError("symlink_not_allowed", name=_shown(name))
    if _clutter(name):
        # 名字安全、不是符号链接的系统文件：不读，也就不必再看它的类型与内容。
        return False
    if info.flag_bits & 0x1:
        raise ArchiveError("encrypted_entry", name=_shown(name))
    if info.is_dir():
        return False
    lowered = name.lower()
    if lowered.endswith(NESTED_SUFFIXES):
        raise ArchiveError("nested_archive", name=_shown(name))
    # 设备、管道之类的特殊文件，以及 .md 和根目录的 followups.json 之外的任何文件。
    if kind not in (0, stat.S_IFREG) or (
        name != FOLLOWUPS_FILE and not lowered.endswith(PAGE_SUFFIX)
    ):
        raise ArchiveError("unsupported_file", name=_shown(name))
    return True


def _read(zf: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    """读一个文件，至多 `limit` 字节：中央目录里写的大小不可信，按实际读到的算。"""
    try:
        with zf.open(info) as handle:
            raw = handle.read(limit + 1)
    except Exception:  # noqa: BLE001 坏数据抛什么由压缩算法与 CPython 版本决定
        raise ArchiveError("invalid_archive") from None
    if len(raw) > limit:
        raise ArchiveError("unpacked_too_large", limit_bytes=MAX_UNPACKED_BYTES)
    return raw


def _text(raw: bytes, name: str) -> str:
    if raw[:4] in _ZIP_MAGIC:
        # 改了后缀名的压缩包。
        raise ArchiveError("nested_archive", name=_shown(name))
    if b"\x00" in raw:
        raise ArchiveError("not_utf8", name=_shown(name))
    try:
        return raw.decode("utf-8").removeprefix(_BOM)
    except UnicodeDecodeError:
        raise ArchiveError("not_utf8", name=_shown(name)) from None


def _split(text: str) -> tuple[dict[Any, Any], str]:
    """（front matter 的映射, 正文）。没有 front matter 时映射为空、正文是全文。

    Raises:
        _BadHeader: 有收尾的 `---`，但中间不是合法的 YAML
    """
    first, newline, rest = text.partition("\n")
    if not newline or first.rstrip("\r") != _FENCE:
        return {}, text
    position = 0
    while position <= MAX_HEADER_CHARS:
        end = rest.find("\n", position)
        line = rest[position:] if end < 0 else rest[position:end]
        if line.rstrip("\r") == _FENCE:
            header, body = rest[:position], "" if end < 0 else rest[end + 1 :]
            break
        if end < 0:
            return {}, text
        position = end + 1
    else:
        return {}, text
    try:
        meta = yaml.safe_load(header)
    except (yaml.YAMLError, ValueError, RecursionError, OverflowError):
        raise _BadHeader from None
    if meta is None:
        return {}, body
    if not isinstance(meta, dict):
        # 开头只是一条分隔线，后面碰巧又有一条：整篇都是正文。
        return {}, text
    return meta, body


def _scalar(value: object) -> bool:
    return value is None or (
        isinstance(value, (str, int, float, date)) and not isinstance(value, bool)
    )


def _page(name: str, text: str) -> PageFile:
    path = name[: -len(PAGE_SUFFIX)]
    try:
        meta, body = _split(text)
    except _BadHeader:
        return PageFile(path, None, None, None, "", "invalid_front_matter")
    title, summary, pinned = meta.get("title"), meta.get("summary"), meta.get("pinned")
    if not (_scalar(title) and _scalar(summary) and (pinned is None or isinstance(pinned, bool))):
        return PageFile(path, None, None, None, "", "invalid_front_matter")
    return PageFile(
        path,
        None if title is None else str(title),
        None if summary is None else str(summary),
        pinned,
        body,
    )


def _day(value: object) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str) or _DAY.match(value) is None:
        raise ValueError("not a date")
    return date.fromisoformat(value)


def _followup(index: int, item: object) -> FollowupItem:
    def bad(problem: str) -> FollowupItem:
        return FollowupItem(index, "", "", None, None, None, False, problem)

    if not isinstance(item, dict):
        return bad("invalid_item")
    category, title, note = item.get("category"), item.get("title"), item.get("notes")
    status = item.get("status", "open")
    if not isinstance(category, str) or category not in FOLLOWUP_CATEGORIES:
        return bad("invalid_category")
    if not isinstance(title, str) or not 0 < len(title.strip()) <= followups.MAX_TITLE:
        return bad("invalid_title")
    if note is not None and (not isinstance(note, str) or len(note) > followups.MAX_NOTES):
        return bad("invalid_notes")
    if status not in ("open", "snoozed"):
        return bad("invalid_status")
    try:
        review_on, due_on = _day(item.get("review_on")), _day(item.get("due_on"))
    except ValueError:
        return bad("invalid_date")
    return FollowupItem(
        index, category, title.strip(), note or None, review_on, due_on, status == "snoozed"
    )


def _followups(text: str) -> list[FollowupItem]:
    """`followups.json`：`{"version": 1, "items": [<条目>…]}`，至多 500 条。

    条目是一个对象：`category`（`decide` / `promised` / `waiting` / `remind`）、`title`（非空，
    至多 200 字）必填；`notes`（至多 4000 字）、`review_on`、`due_on`（`YYYY-MM-DD`）、`status`
    （`open` 或 `snoozed`，默认 `open`）可选，可以写 null；其余键忽略。某一条不合规只跳过这
    一条（`problem`）；整个文件不合规则拒绝整个包。
    """
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError):
        raise ArchiveError("invalid_followups") from None
    items = doc.get("items") if isinstance(doc, dict) else None
    version = doc.get("version") if isinstance(doc, dict) else None
    if (
        type(version) is not int
        or version != FORMAT_VERSION
        or not isinstance(items, list)
        or len(items) > MAX_FOLLOWUP_ITEMS
    ):
        raise ArchiveError("invalid_followups")
    return [_followup(index, item) for index, item in enumerate(items, start=1)]


def parse(data: bytes) -> Parsed:
    """解析上传的压缩包。

    依次检查，任何一条不过都拒绝整个包：

    1. 压缩包本身不超过 20MB（`archive_too_large`），是一个读得出中央目录的 zip
       （`invalid_archive`）。
    2. 条目总数不超过 4096，其中页面文件不超过 2000 个（`too_many_entries`）。
    3. 每个条目的名字：不是绝对路径，没有反斜杠、`.` / `..` 段、空段、控制字符或 NUL
       （`unsafe_path`）；不是符号链接（`symlink_not_allowed`）；过了这两条的系统文件
       （`.DS_Store` 之类）跳过不读、只计数，其余的继续查：没有加密
       （`encrypted_entry`）；后缀不是压缩包（`nested_archive`）；是 `.md` 文件或根目录的
       `followups.json`，且是普通文件（`unsupported_file`）。
    4. 中央目录里写的大小合计不超过上限；实际读出来的字节数再核一次（`unpacked_too_large`）。
    5. 每个文件的内容：开头不是 zip 的魔数（`nested_archive`），是不含 NUL 的 UTF-8
       （`not_utf8`）；`followups.json` 的整体结构正确（`invalid_followups`）。

    Raises:
        ArchiveError
    """
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ArchiveError("archive_too_large", limit_bytes=MAX_ARCHIVE_BYTES)
    try:
        opened = zipfile.ZipFile(io.BytesIO(data))
    except Exception:  # noqa: BLE001 坏中央目录抛什么由 CPython 版本决定
        raise ArchiveError("invalid_archive") from None
    with opened as zf:
        infos = zf.infolist()
        if len(infos) > MAX_RAW_ENTRIES:
            raise ArchiveError("too_many_entries", limit=MAX_PAGE_FILES)
        files = [info for info in infos if _check(info)]
        ignored = sum(1 for info in infos if not info.is_dir() and _clutter(info.filename))
        if sum(1 for info in files if info.filename != FOLLOWUPS_FILE) > MAX_PAGE_FILES:
            raise ArchiveError("too_many_entries", limit=MAX_PAGE_FILES)
        if sum(info.file_size for info in files) > MAX_UNPACKED_BYTES:
            raise ArchiveError("unpacked_too_large", limit_bytes=MAX_UNPACKED_BYTES)
        left = MAX_UNPACKED_BYTES
        pages: list[PageFile] = []
        listed: list[FollowupItem] = []
        for info in files:
            raw = _read(zf, info, left)
            left -= len(raw)
            text = _text(raw, info.filename)
            if info.filename == FOLLOWUPS_FILE:
                listed = _followups(text)
            else:
                pages.append(_page(info.filename, text))
    return Parsed(pages, listed, ignored)


def page_text(*, title: str, summary: str, pinned: bool, body: str) -> str:
    """导出的一页：front matter 加正文，`parse` 读回来与原样一致。"""
    header = yaml.safe_dump(
        {"title": title, "summary": summary, "pinned": pinned},
        allow_unicode=True,
        sort_keys=False,
        width=MAX_HEADER_CHARS,
    )
    return f"{_FENCE}\n{header}{_FENCE}\n{body}"


def followups_text(items: Iterable[Mapping[str, Any]]) -> str:
    """导出的 `followups.json`。"""
    return json.dumps(
        {"version": FORMAT_VERSION, "items": list(items)}, ensure_ascii=False, indent=2
    )


def build(pages: Iterable[tuple[str, str]], followups_json: str | None) -> bytes:
    """打成 zip：`pages` 是（页的路径, 文件内容）；`followups_json` 为 None 时不放清单。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, text in pages:
            zf.writestr(path + PAGE_SUFFIX, text)
        if followups_json is not None:
            zf.writestr(FOLLOWUPS_FILE, followups_json)
    return buffer.getvalue()
