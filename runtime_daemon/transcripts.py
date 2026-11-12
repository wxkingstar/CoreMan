"""助理轮次的 CLI 转录：一轮结束时打包上传，续聊前还原到沙箱里。

转录只取续聊必需的部分，路径都相对本轮目录（沙箱里的 /turn）：
- Claude：`config/projects/`（会话 jsonl，工作目录在沙箱里固定是 /work，所以项目目录名固定）；
- Codex：`config/sessions/`（rollout），加上驱动记下的会话 → 线程对应
  `driver/<会话>/codex_thread.txt`。
打成 tar.gz；超过 `MAX_BYTES` 的不上传，平台据此结束这个会话、下一轮开新会话。

还原时逐个成员检查：只接受普通文件与目录、相对路径、落在上面这几个位置之内，解压后总量有
上限；不认识的成员整个包作废（`BadBundle`），这一轮从新会话开始，不会把东西写到别处。
"""

from __future__ import annotations

import io
import posixpath
import re
import tarfile
from pathlib import Path

MAX_BYTES = 50 * 1024 * 1024
# 解压后的上限：防一个很小的压缩包展开成巨量文件。
MAX_EXPANDED = 512 * 1024 * 1024
TREES = {"claude": ("config/projects",), "codex": ("config/sessions",)}
THREAD_FILE = "codex_thread.txt"
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")


class TooLarge(Exception):
    """打包后超过上限。"""


class BadBundle(Exception):
    """下发的转录包不合规，整个作废。"""


def _members(root: Path, provider: str, session_id: str) -> list[Path]:
    found: list[Path] = []
    for tree in TREES.get(provider, ()):
        base = root / tree
        if base.is_dir():
            found += sorted(p for p in base.rglob("*") if p.is_file() and not p.is_symlink())
    if provider == "codex" and SESSION_ID.fullmatch(session_id):
        thread = root / "driver" / session_id / THREAD_FILE
        if thread.is_file() and not thread.is_symlink():
            found.append(thread)
    return found


def pack(root: Path, provider: str, session_id: str) -> bytes | None:
    """本轮的转录包；没有任何转录文件时返回 None。

    Raises:
        TooLarge: 压缩后超过 MAX_BYTES
    """
    files = _members(root, provider, session_id)
    if not files:
        return None
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path in files:
            info = tar.gettarinfo(str(path), arcname=path.relative_to(root).as_posix())
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o600
            with path.open("rb") as handle:
                tar.addfile(info, handle)
            if buffer.tell() > MAX_BYTES:
                raise TooLarge
    data = buffer.getvalue()
    if len(data) > MAX_BYTES:
        raise TooLarge
    return data


def _allowed(name: str, provider: str, session_id: str) -> bool:
    if name.startswith("/") or name != posixpath.normpath(name) or ".." in name.split("/"):
        return False
    for tree in TREES.get(provider, ()):
        if name == tree or name.startswith(tree + "/"):
            return True
    return provider == "codex" and name == f"driver/{session_id}/{THREAD_FILE}"


def unpack(root: Path, provider: str, session_id: str, data: bytes) -> int:
    """把下发的转录包还原到本轮目录，返回写出的文件数。

    Raises:
        BadBundle: 包坏了、成员类型或位置不对、解压后太大
    """
    written = expanded = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar:
                if not _allowed(member.name, provider, session_id):
                    raise BadBundle(member.name)
                if member.isdir():
                    continue
                if not member.isfile():
                    raise BadBundle("not a regular file")
                expanded += member.size
                if expanded > MAX_EXPANDED:
                    raise BadBundle("too large")
                source = tar.extractfile(member)
                if source is None:
                    raise BadBundle("unreadable member")
                target = root / member.name
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with source, target.open("wb") as handle:
                    while chunk := source.read(1024 * 1024):
                        handle.write(chunk)
                target.chmod(0o600)
                written += 1
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise BadBundle(type(exc).__name__) from None
    return written
