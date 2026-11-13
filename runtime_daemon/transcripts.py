"""助理轮次的 CLI 转录：一轮结束时打包上传，续聊前还原到沙箱里。

转录只取续聊必需的部分，路径都相对本轮目录（沙箱里的 /turn）：
- Claude：`config/projects/`（会话 jsonl，工作目录在沙箱里固定是 /work，所以项目目录名固定）；
- Codex：`config/sessions/`（rollout），加上驱动记下的会话 → 线程对应
  `driver/<会话>/codex_thread.txt`。
打成 tar.gz；超过 `MAX_BYTES` 的不上传，平台据此结束这个会话、下一轮开新会话。

还原时逐个成员检查：只接受普通文件与目录、相对路径、落在上面这几个位置之内，解压后总量有
上限、成员数有上限；不认识的成员整个包作废（`BadBundle`），已写出的内容也会清掉，这一轮从新会话
开始，不会把东西写到别处。

本轮目录里的一切都是沙箱里可能被提示词注入的助理写的，所以打包与还原都从目录描述符出发、
逐级用 O_NOFOLLOW 打开，从不跟随任何链接；异常只带固定代码，不带成员名、路径或系统报错。
"""

from __future__ import annotations

import io
import os
import posixpath
import re
import stat
import tarfile
import zlib
from pathlib import Path

MAX_BYTES = 50 * 1024 * 1024
# 解压后的上限：防一个很小的压缩包展开成巨量文件。
MAX_EXPANDED = 512 * 1024 * 1024
MAX_MEMBERS = 20000
MAX_DEPTH = 64
TREES = {"claude": ("config/projects",), "codex": ("config/sessions",)}
THREAD_FILE = "codex_thread.txt"
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW


class TooLarge(Exception):
    """打包后超过上限，或读不出来；只带固定代码（too_large / io）。"""


class BadBundle(Exception):
    """下发的转录包不合规，整个作废；只带固定代码（path/type/duplicate/too_many/too_large/io）。"""


def _open_dir_path(root_fd: int, parts: tuple[str, ...]) -> int:
    """从 root_fd 起逐级以 O_NOFOLLOW 打开目录；任何一级是链接或不是目录都抛 OSError。"""
    current = os.dup(root_fd)
    try:
        for part in parts:
            nxt = os.open(part, _DIR_FLAGS, dir_fd=current)
            os.close(current)
            current = nxt
    except BaseException:
        os.close(current)
        raise
    return current


class _Budget:
    """打包时见过的目录项计数：一过 MAX_MEMBERS 立刻放弃，不把整个目录列出来再排序。"""

    def __init__(self) -> None:
        self.seen = 0

    def take(self) -> None:
        self.seen += 1
        if self.seen > MAX_MEMBERS:
            raise TooLarge("too_large")


def _walk(
    dir_fd: int,
    prefix: tuple[str, ...],
    out: list[tuple[tuple[str, ...], int]],
    depth: int,
    budget: _Budget,
):
    if depth > MAX_DEPTH:
        return
    kept: list[tuple[str, bool]] = []
    try:
        with os.scandir(dir_fd) as entries:
            for entry in entries:
                budget.take()
                try:
                    entry.name.encode("utf-8")
                    if entry.is_dir(follow_symlinks=False):
                        kept.append((entry.name, True))
                    elif entry.is_file(follow_symlinks=False):
                        kept.append((entry.name, False))
                except (OSError, UnicodeEncodeError):
                    continue
    except OSError:
        return
    for name, is_dir in sorted(kept):
        try:
            if is_dir:
                child = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
                try:
                    _walk(child, (*prefix, name), out, depth + 1, budget)
                finally:
                    os.close(child)
            else:
                size = _file_size(dir_fd, name)
                if size is not None:
                    out.append(((*prefix, name), size))
        except OSError:
            continue


def _file_size(dir_fd: int, name: str) -> int | None:
    fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
    finally:
        os.close(fd)
    return st.st_size if _is_regular(st.st_mode) else None


def _is_regular(mode: int) -> bool:
    return stat.S_ISREG(mode)


def _collect(root_fd: int, provider: str, session_id: str) -> list[tuple[tuple[str, ...], int]]:
    found: list[tuple[tuple[str, ...], int]] = []
    budget = _Budget()
    for tree in TREES.get(provider, ()):
        parts = tuple(tree.split("/"))
        try:
            base = _open_dir_path(root_fd, parts)
        except OSError:
            continue
        try:
            _walk(base, parts, found, 0, budget)
        finally:
            os.close(base)
    if provider == "codex" and SESSION_ID.fullmatch(session_id):
        try:
            parent = _open_dir_path(root_fd, ("driver", session_id))
        except OSError:
            return found
        try:
            size = _file_size(parent, THREAD_FILE)
            if size is not None:
                found.append((("driver", session_id, THREAD_FILE), size))
        except OSError:
            pass
        finally:
            os.close(parent)
    return found


class _Capped:
    """写入即计数的输出：压缩结果一越过 MAX_BYTES 就中止，不等整包生成。"""

    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def write(self, chunk: bytes) -> int:
        self.buffer.write(chunk)
        if self.buffer.tell() > MAX_BYTES:
            raise TooLarge("too_large")
        return len(chunk)

    def flush(self) -> None:
        pass


class _Bounded:
    """最多给出 size 字节：文件之后长大的部分不读。

    文件缩短时，读到的字节少于 tar 要的块，tarfile 报读不完整，打包以 TooLarge("io") 放弃；
    只有恰好在块边界读到文件末尾时才补零凑满 tar 头里的大小。
    """

    def __init__(self, fd: int, size: int) -> None:
        self.fd = fd
        self.left = size

    def read(self, n: int = -1) -> bytes:
        if self.left <= 0:
            return b""
        n = self.left if n < 0 else min(n, self.left)
        n = min(n, 1024 * 1024)
        chunk = os.read(self.fd, n)
        if not chunk:
            chunk = b"\0" * n
        self.left -= len(chunk)
        return chunk


def pack(root: Path, provider: str, session_id: str) -> bytes | None:
    """本轮的转录包；没有任何转录文件时返回 None。

    全程不跟随链接：是链接或不是普通文件/目录的一律跳过，打不开的文件也跳过。

    Raises:
        TooLarge: 目录项超过 MAX_MEMBERS、展开总量超过 MAX_EXPANDED、压缩后超过 MAX_BYTES，
            或打包中读文件出错（code io）
    """
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return None
    try:
        files = _collect(root_fd, provider, session_id)
        if not files:
            return None
        if sum(size for _, size in files) > MAX_EXPANDED:
            raise TooLarge("too_large")
        out = _Capped()
        try:
            with tarfile.open(fileobj=out, mode="w:gz") as tar:  # type: ignore[call-overload]
                for parts, size in files:
                    _add(tar, root_fd, parts, size)
        except (OSError, tarfile.TarError, zlib.error, ValueError):
            raise TooLarge("io") from None
        return out.buffer.getvalue()
    finally:
        os.close(root_fd)


def _add(tar: tarfile.TarFile, root_fd: int, parts: tuple[str, ...], size: int) -> None:
    parent = _open_dir_path(root_fd, parts[:-1])
    try:
        fd = os.open(parts[-1], _FILE_FLAGS, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        st = os.fstat(fd)
        if not _is_regular(st.st_mode):
            raise OSError
        info = tarfile.TarInfo("/".join(parts))
        info.size = size
        info.mtime = int(st.st_mtime)
        info.mode = 0o600
        tar.addfile(info, _Bounded(fd, size))  # type: ignore[arg-type]
    finally:
        os.close(fd)


def _allowed(name: str, provider: str, session_id: str, is_dir: bool) -> bool:
    if name.startswith("/") or name != posixpath.normpath(name) or ".." in name.split("/"):
        return False
    for tree in TREES.get(provider, ()):
        if name.startswith(tree + "/") or (is_dir and name == tree):
            return True
    return provider == "codex" and not is_dir and name == f"driver/{session_id}/{THREAD_FILE}"


class _Restore:
    """还原时的创建记录：失败时按相反顺序只清自己建的东西，不跟随链接。"""

    def __init__(self, root_fd: int) -> None:
        self.root_fd = root_fd
        self.created: list[tuple[bool, tuple[str, ...]]] = []

    def dir_fd(self, parts: tuple[str, ...]) -> int:
        current = os.dup(self.root_fd)
        try:
            for i, part in enumerate(parts):
                try:
                    nxt = os.open(part, _DIR_FLAGS, dir_fd=current)
                except FileNotFoundError:
                    os.mkdir(part, 0o700, dir_fd=current)
                    self.created.append((True, parts[: i + 1]))
                    nxt = os.open(part, _DIR_FLAGS, dir_fd=current)
                    os.fchmod(nxt, 0o700)
                except OSError:
                    raise BadBundle("path") from None
                os.close(current)
                current = nxt
        except BaseException:
            os.close(current)
            raise
        return current

    def write(self, parts: tuple[str, ...], source: tarfile.ExFileObject) -> None:
        parent = self.dir_fd(parts[:-1])
        try:
            try:
                fd = os.open(
                    parts[-1],
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent,
                )
            except FileExistsError:
                raise BadBundle("duplicate") from None
        finally:
            os.close(parent)
        self.created.append((False, parts))
        try:
            os.fchmod(fd, 0o600)
            with source, os.fdopen(fd, "wb", closefd=False) as handle:
                while chunk := source.read(1024 * 1024):
                    handle.write(chunk)
        finally:
            os.close(fd)

    def rollback(self) -> None:
        for is_dir, parts in reversed(self.created):
            try:
                parent = _open_dir_path(self.root_fd, parts[:-1])
            except OSError:
                continue
            try:
                if is_dir:
                    os.rmdir(parts[-1], dir_fd=parent)
                else:
                    os.unlink(parts[-1], dir_fd=parent)
            except OSError:
                pass
            finally:
                os.close(parent)


def unpack(root: Path, provider: str, session_id: str, data: bytes) -> int:
    """把下发的转录包还原到本轮目录，返回写出的文件数。

    失败时本次调用创建的目录和文件都会清掉，已有内容不动。

    Raises:
        BadBundle: 包坏了、成员类型或位置不对、重复、成员太多、解压后太大（固定代码）
    """
    if len(data) > MAX_BYTES:
        raise BadBundle("too_large")
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        raise BadBundle("io") from None
    restore = _Restore(root_fd)
    written = expanded = count = 0
    try:
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                for member in tar:
                    count += 1
                    if count > MAX_MEMBERS:
                        raise BadBundle("too_many")
                    is_dir = member.isdir()
                    if not _allowed(member.name, provider, session_id, is_dir):
                        raise BadBundle("path")
                    parts = tuple(member.name.split("/"))
                    if is_dir:
                        os.close(restore.dir_fd(parts))
                        continue
                    if not member.isfile():
                        raise BadBundle("type")
                    expanded += member.size
                    if expanded > MAX_EXPANDED:
                        raise BadBundle("too_large")
                    source = tar.extractfile(member)
                    if source is None:
                        raise BadBundle("io")
                    restore.write(parts, source)
                    written += 1
        except (tarfile.TarError, OSError, EOFError, zlib.error, ValueError):
            raise BadBundle("io") from None
    except BadBundle:
        restore.rollback()
        raise
    finally:
        os.close(root_fd)
    return written
