"""Bounded workspace administration over the authenticated daemon channel."""

from __future__ import annotations

import base64
import configparser
import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

# 换机分块：平台按两端 ping 报的 workspace_chunk 取小，旧平台仍按 256KB 读写。
CHUNK = 2 * 1024 * 1024
MAX_TEXT = 1024 * 1024
MAX_TOTAL = 4 * 1024**3
MAX_FILES = 50000
# 未推送提交随描述内联传过去；超过这个大小就整仓复制。
BUNDLE_LIMIT = 8 * 1024 * 1024
# 换机暂存目录超过这个时长还在，一定是中断的迁移留下的（单次迁移最长 30 分钟）。
STALE_STAGE = 6 * 3600
SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
PROBE_TOKEN = re.compile(r"[0-9a-f]{32}")
MARKER = ".coreman-workspace.json"
# 员工记忆：Claude 与 Codex 共用；`.coreman-` 前缀让文件管理看不到、换机复制不带（经平台库部署）。
MEMORY_DIR = ".coreman-memory"
EXCLUDED = {
    ".git",
    ".ssh",
    ".aws",
    ".config",
    ".cache",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".codex",
    MARKER,
}


MAX_DIFF = 256 * 1024
# 所有权标记由守护进程写、不到 1 KB；读它时最多读这么多（目录里的东西可能被助理换掉）。
MARKER_LIMIT = 64 * 1024
# 工作目录根下守护进程自己用的目录：健康探测目录、个人助理工作目录的回收站。助理专用实例拒绝把它们
# （及其下的任何目录）当作工作目录。
HEALTH_DIR = ".coreman-health"
TRASH_DIR = ".coreman-trash"
# 助理专用实例的归属登记：每个助理工作目录一条（文件名是目录真实路径的 sha256），记着员工与目录的
# inode。它在工作目录之外，沙箱里改不到；删目录时据此认归属，不看助理写得到的标记。删掉之后这条
# 换成墓碑（{"bot_id": ..., "removed": true}），平台重发时据此答「已删除」；墓碑留
# TOMBSTONE_SECONDS。
OWNERS_DIR = ".coreman-owners"
TOMBSTONE_SECONDS = 30 * 24 * 3600
RESERVED_DIRS = (HEALTH_DIR, TRASH_DIR, OWNERS_DIR)
# Workspace.remove 把目录改名放进回收站时用的名字（secrets.token_hex(16)）：32 位小写十六进制。
TRASH_ENTRY_BYTES = 16
TRASH_ENTRY_NAME = re.compile(f"[0-9a-f]{{{2 * TRASH_ENTRY_BYTES}}}")


def read_small(path, limit=MARKER_LIMIT):
    """读一个别人可能换掉的小文件，读不了或不像样一律返回 None。

    不跟随链接（O_NOFOLLOW），不在 FIFO、设备上等待（O_NONBLOCK 打开、fstat 只认普通文件），
    超过 `limit` 字节不读。打开之后核对的是同一个文件描述符，没有先查后用的窗口。
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, min(65536, limit + 1 - total))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                return None
    except OSError:
        return None
    finally:
        os.close(fd)


def marker_data(root):
    """工作目录里所有权标记的内容；没有、读不了、不是 JSON 对象都返回空字典。"""
    return parse_object(read_small(Path(root) / MARKER))


def parse_object(raw):
    """JSON 对象；读不了、不是 JSON 对象（含嵌套过深）都返回空字典。"""
    if raw is None:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def reserved(root, raw):
    """`raw`（相对工作目录根或绝对路径）按真实位置算，是不是落在保留目录里（含保留目录本身）。"""
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        return False
    base = Path(root).resolve()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base / path
    resolved = path.resolve()
    return any(resolved.is_relative_to(base / name) for name in RESERVED_DIRS)


def private_dir(root, name, create=True):
    """工作目录根下守护进程自己的目录：没有就建（0700）；必须是本用户的真实目录，不是链接，
    权限不是 0700 就收紧。核对与收紧都在同一个不跟随链接打开的描述符上做。"""
    path = Path(root) / name
    if create:
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise OSError("private directory is not ours")
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.fchmod(fd, 0o700)
    finally:
        os.close(fd)
    return path


def trash_dir(root):
    """工作目录根下的回收站（见 private_dir）。"""
    return private_dir(root, TRASH_DIR)


def owners_dir(root, create=True):
    """工作目录根下的归属登记目录（见 private_dir）。`create=False` 时没有就返回 None。"""
    try:
        return private_dir(root, OWNERS_DIR, create=create)
    except FileNotFoundError:
        if create:
            raise
        return None


def record_name(path):
    """归属登记的文件名：目录真实路径的 sha256，不取目录里的任何东西。"""
    return hashlib.sha256(os.fsencode(str(path))).hexdigest()


def read_record(record):
    """归属登记的内容：没有登记返回 None；有但读不了、不像样返回空字典（谁的都不算）。"""
    if not os.path.lexists(record):
        return None
    return parse_object(read_small(record))


def write_record(record, data):
    """原子写一条归属登记（0600，同目录临时文件再改名）。"""
    temporary = record.parent / f".tmp-{secrets.token_hex(8)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(json.dumps(data).encode())
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, record)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def live_record(data):
    """像样的归属登记：有员工编号与 inode，不是墓碑。"""
    bot = data.get("bot_id")
    return (
        isinstance(bot, str)
        and bool(bot)
        and type(data.get("ino")) is int
        and "removed" not in data
    )


def tombstone_of(data, bot):
    """这个员工的墓碑：本登记删掉过这个员工在这里的目录。"""
    return data.get("bot_id") == bot and data.get("removed") is True and "ino" not in data


def registered_to(data, bot, info):
    """登记是不是记着这个员工、且就是眼前这个目录。

    只比 inode、不比设备号：工作目录根可以放在几台节点共同挂载的网络存储上，网络挂载的设备号
    换一台机器、重启或重新挂载都可能变，比设备号会把本助理的目录认成别人的。
    """
    return live_record(data) and data.get("bot_id") == bot and data.get("ino") == info.st_ino


def purge_tombstones(root, now=None):
    """删掉归属登记里超过 TOMBSTONE_SECONDS 的墓碑。

    别的一概不动，不跟随链接，没有登记目录就不建。
    """
    owners = owners_dir(root, create=False)
    if owners is None:
        return
    cutoff = (time.time() if now is None else now) - TOMBSTONE_SECONDS
    with os.scandir(owners) as entries:
        found = list(entries)
    for entry in found:
        try:
            info = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_mtime >= cutoff:
                continue
            data = parse_object(read_small(entry.path))
            if data.get("removed") is True and "ino" not in data:
                os.unlink(entry.path)
        except OSError:
            continue


def redact(url):
    """远端地址里可能带着令牌（https://user:token@host/...），只展示主机与路径。"""
    parts = urlsplit(url)
    if parts.scheme and (parts.username or parts.password):
        return parts._replace(
            netloc=parts.hostname + (f":{parts.port}" if parts.port else "")
        ).geturl()
    return url


def untracked_diff(name, path):
    header = f"--- /dev/null\n+++ b/{name}\n"
    if path.is_symlink():
        return header + f"+{os.readlink(path)}\n\\ 符号链接\n"
    if path.is_dir():
        return header + "\\ 未跟踪的目录\n"
    with path.open("rb") as f:
        raw = f.read(MAX_DIFF + 1)
    if b"\0" in raw:
        return header + "\\ 二进制文件\n"
    lines = raw[:MAX_DIFF].decode(errors="replace").splitlines()
    return header + "".join(f"+{line}\n" for line in lines)


def forbidden(name, transfer=False):
    """transfer=True 用于换机复制：.git 随工作目录一起迁移，文件管理里仍然看不到它。"""
    return any(
        (p in EXCLUDED and not (transfer and p == ".git"))
        or p.startswith(".env")
        or p.startswith(".coreman-")
        or p.lower().endswith((".pem", ".key", ".p12", ".pfx"))
        or p.lower()
        in {"credentials.json", "auth.json", "id_rsa", "id_ed25519", ".netrc", ".npmrc", ".pypirc"}
        for p in PurePosixPath(name).parts
    )


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def cached_digest(cache, path):
    """换机按块读取时每块都要带整文件哈希；按 inode、大小与修改时间缓存，大文件不再每块重算。"""
    st = path.stat()
    key = (str(path), st.st_ino, st.st_size, st.st_mtime_ns)
    if key not in cache:
        if len(cache) > 20000:
            cache.clear()
        cache[key] = digest(path)
    return cache[key]


def sweep_stages(parent):
    cutoff = time.time() - STALE_STAGE
    for stage in parent.glob(".coreman-transfer-*"):
        if stage.is_dir() and not stage.is_symlink() and stage.stat().st_mtime < cutoff:
            shutil.rmtree(stage, ignore_errors=True)


def merge_tree(overlay, base):
    """把 overlay 里的条目挪到 base 上，同名的以 overlay 为准；两边都是目录时逐层合并。"""
    for entry in list(overlay.iterdir()):
        target = base / entry.name
        if (
            entry.is_dir()
            and not entry.is_symlink()
            and target.is_dir()
            and not target.is_symlink()
        ):
            merge_tree(entry, target)
            continue
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif target.exists() or target.is_symlink():
            target.unlink()
        entry.rename(target)


def clean_in_repo(rel, repos):
    """rel 是否落在某个可克隆仓库里、且与 HEAD 一致（目标节点克隆即可还原，不必复制）。"""
    parts = PurePosixPath(rel).parts
    for i in range(len(parts) - 1, -1, -1):
        key = "/".join(parts[:i])
        if key in repos:
            return "/".join(parts[i:]) in repos[key]
    return False


def instruction_alias(root, name):
    """name 是指向另一份指令文件的相对链接时，返回那份文件名。

    AGENTS.md 与 CLAUDE.md 是同一份指令：一个是普通文件，另一个链接过去。CoreMan 新建的目录
    以 AGENTS.md 为正本；其他机器人系统已有的目录常以 Git 里跟踪的 CLAUDE.md 为正本、AGENTS.md
    链接过去。两种方向都认，不改写已有目录的布局。
    """
    if name not in ("AGENTS.md", "CLAUDE.md"):
        return None
    partner = "CLAUDE.md" if name == "AGENTS.md" else "AGENTS.md"
    path = root / name
    if (
        path.is_symlink()
        and os.readlink(path) == partner
        and (root / partner).is_file()
        and not (root / partner).is_symlink()
    ):
        return partner
    return None


def replace_file(path, raw):
    """原子替换文件内容，保留原文件的权限和属组；新文件按 umask / 目录默认 ACL 取权限。

    工作目录可能与其它实例用户共用（如多个实例用户共用的机器人目录），
    不能像私有文件那样收成 0600，否则别的实例读不到。
    """
    temporary = path.parent / f".coreman-tmp-{os.urandom(8).hex()}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        if path.is_file() and not path.is_symlink():
            st = path.stat()
            os.chmod(temporary, stat.S_IMODE(st.st_mode))
            try:
                os.chown(temporary, -1, st.st_gid)
            except PermissionError:
                pass
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def git_exclude_file(directory):
    from .agent import OperationError

    current = directory
    for part in (".git", "info", "exclude"):
        current = current / part
        if current.is_symlink():
            raise OperationError("Git 排除配置不能是符号链接")
    return current


def remove_legacy_instruction_excludes(directory):
    current = git_exclude_file(directory)
    if current.is_file():
        previous = current.read_text()
        updated = "".join(
            line
            for line in previous.splitlines(keepends=True)
            if line.rstrip("\r\n") not in {"/AGENTS.md", "AGENTS.md"}
        )
        if updated != previous:
            replace_file(current, updated.encode())


def exclude_private(directory):
    """Git 仓库里把所有权标记和记忆目录列入本地排除：共用目录的另一套系统部署时可能
    git clean -fd，不排除就会删掉它们；也免得它们出现在 git status 里、被误提交。"""
    if not (directory / ".git").is_dir() or (directory / ".git").is_symlink():
        return
    current = git_exclude_file(directory)
    previous = current.read_text() if current.is_file() else ""
    missing = [
        line for line in ("/" + MARKER, "/" + MEMORY_DIR + "/") if line not in previous.splitlines()
    ]
    if missing:
        current.parent.mkdir(parents=True, exist_ok=True)
        text = previous + ("\n" if previous and not previous.endswith("\n") else "")
        replace_file(current, (text + "".join(line + "\n" for line in missing)).encode())


def link_skills(directory):
    """让 .claude/skills 整个链接到 .agents/skills，Claude 与 Codex 读同一份技能。

    已有的 .claude/skills 目录里只有指回 .agents/skills 同名技能的链接时，换成整目录链接；
    放着别的内容就原样保留，安装技能时仍逐个补链接。Git 仓库的布局归仓库管，不动。
    """
    canonical, alias = directory / ".agents/skills", directory / ".claude/skills"
    if (directory / ".git").exists() or alias.is_symlink():
        return
    for path in (directory / ".agents", canonical, directory / ".claude"):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            return
    if alias.exists():
        if not alias.is_dir() or any(
            not entry.is_symlink() or os.readlink(entry) != f"../../.agents/skills/{entry.name}"
            for entry in alias.iterdir()
        ):
            return
        for entry in alias.iterdir():
            entry.unlink()
        alias.rmdir()
    canonical.mkdir(parents=True, exist_ok=True)
    alias.parent.mkdir(exist_ok=True)
    alias.symlink_to("../.agents/skills", target_is_directory=True)


GITIGNORE = f"""# 技能（.agents、.claude）与 MCP 配置（.mcp.json）随仓库提交，这里只排除本机文件。
/{MARKER}
/{MEMORY_DIR}/
.env
.env.*
.claude/settings.local.json
"""


def write_gitignore(directory):
    """新目录写一份 .gitignore，AI 员工以后 git init 时技能与 MCP 配置一并提交。
    已有 .gitignore 或已是 Git 仓库的目录归仓库管，不动。"""
    target = directory / ".gitignore"
    if (directory / ".git").exists() or target.exists() or target.is_symlink():
        return
    replace_file(target, GITIGNORE.encode())


class InstructionsConflict(Exception):
    """Canonical instruction files require a user merge before initialization."""


class Workspace:
    def __init__(self, agent, data):
        self.agent, self.data = agent, data
        from .agent import OperationError

        self.error = OperationError
        raw = Path(str(data.get("working_dir") or "")).expanduser()
        if not raw.is_absolute():
            raw = agent.root / raw
        current, inside = Path(raw.anchor), False
        for part in raw.parts[1:]:
            current = current / part
            if inside and current.is_symlink():
                raise self.error("工作目录不能包含符号链接")
            if current.resolve() == agent.root:
                inside = True
        self.root = agent.workspace(str(data.get("working_dir") or ""))
        self.checked_repos = set()
        self.bot = str(data.get("bot_id") or "")
        if not self.bot:
            raise self.error("缺少员工标识")
        marker = self.root / MARKER
        if (
            data.get("type") not in ("workspace-info", "workspace-remove")
            and (marker.exists() or marker.is_symlink())
            and not self.owned()
        ):
            raise self.error("工作目录属于其他员工或所有权标记无效")

    def path(self, name, root=None, allow_link=False, transfer=False):
        root = root or self.root
        if not isinstance(name, str) or "\x00" in name or "\\" in name:
            raise self.error("文件路径无效")
        p = PurePosixPath(name)
        if p.is_absolute() or ".." in p.parts or forbidden(name, transfer):
            raise self.error("该文件路径不允许访问")
        target = root / p
        resolved = target.resolve()
        if not resolved.is_relative_to(root.resolve()):
            raise self.error("文件超出工作目录")
        if forbidden(resolved.relative_to(root.resolve()).as_posix(), transfer):
            raise self.error("链接目标不允许访问")
        current = root
        for part in p.parts:
            current = current / part
            if current.is_symlink() and not (allow_link and current == target):
                raise self.error("不允许通过链接访问文件")
        return target

    def owned(self, root=None):
        return marker_data(root or self.root).get("bot_id") == self.bot

    @property
    def assistant_only(self):
        return getattr(self.agent, "assistant_only", False) is True

    def init(self, root=None):
        root = root or self.root
        if self.assistant_only and root == self.root and root.exists():
            # 助理专用实例只在全新的目录上初始化，绝不读写助理在沙箱里写过的文件：标记是本员工的说明
            # 上一次初始化已经做完（标记最后写），原样返回；没有标记的非空目录一律拒绝。
            if self.owned(root):
                return {"initialized": True, "owned": True, "workspace_protocol": 1}
            if root.is_symlink() or not root.is_dir() or any(root.iterdir()):
                raise self.error("工作目录已存在，个人助理只能使用全新的目录")
        record = None
        if self.assistant_only and root == self.root:
            # 全新目录：在目录之外登记归属，删除时认这条登记。登记着别的员工、且就是眼前这个空目录
            # 的，不接管；墓碑不挡（那个目录已经删掉了）。
            record = owners_dir(self.agent.root) / record_name(self.root)
            data = read_record(record)
            if (
                data is not None
                and data.get("bot_id") != self.bot
                and os.path.lexists(root)
                and registered_to(data, data.get("bot_id"), os.lstat(root))
            ):
                raise self.error("工作目录属于其他员工或所有权标记无效")
        root.mkdir(parents=True, exist_ok=True)
        if record is not None:
            # 建好目录马上登记，之后才往里写东西：中途崩了，留下的目录删除时也认得出。
            write_record(record, {"bot_id": self.bot, "ino": os.lstat(root).st_ino})
        marker = root / MARKER
        if marker.exists() or marker.is_symlink():
            if not self.owned(root):
                raise self.error("工作目录属于其他员工或所有权标记无效")
        self.instructions(root)
        link_skills(root)
        write_gitignore(root)
        remove_legacy_instruction_excludes(root)
        exclude_private(root)
        marker.write_text(
            json.dumps(
                {
                    "bot_id": self.bot,
                    "workspace_protocol": 1,
                    "transfer_id": self.data.get("transfer_id"),
                }
            )
        )
        # 暂存目录还要整体挪到目标位置，链接要指向最终路径：只在正式目录上建。
        if root == self.root:
            self.agent.ensure_memory_layout(str(root))
        return {"initialized": True, "owned": True, "workspace_protocol": 1}

    def instructions(self, root):
        """确保 AGENTS.md 与 CLAUDE.md 是一份正本加一个指向它的链接；已有的正本原样沿用。"""
        agents, alias = root / "AGENTS.md", root / "CLAUDE.md"
        if instruction_alias(root, "AGENTS.md"):
            return
        if (
            alias.is_file()
            and not alias.is_symlink()
            and not (agents.exists() or agents.is_symlink())
        ):
            # 只有 CLAUDE.md（Git 仓库、其他机器人系统的目录）：它可能被 Git 跟踪、与别人共用，
            # 不改动它，只补一个 AGENTS.md 链接让 Codex 也读到同一份。
            agents.symlink_to("CLAUDE.md")
            return
        canonical = self.path("AGENTS.md", root)
        if (
            alias.exists()
            and not alias.is_symlink()
            and canonical.exists()
            and alias.read_bytes() != canonical.read_bytes()
        ):
            raise InstructionsConflict("AGENTS.md 与 CLAUDE.md 内容冲突，请统一内容后重试")
        if not canonical.exists():
            canonical.write_text(
                alias.read_text()
                if alias.is_file() and not alias.is_symlink()
                else str(self.data.get("content") or "# 工作区说明\n"),
                encoding="utf-8",
            )
        if not (alias.is_symlink() and os.readlink(alias) == "AGENTS.md"):
            if alias.exists() or alias.is_symlink():
                n = 1
                while (root / f"CLAUDE.md.preserved-{n}").exists() or (
                    root / f"CLAUDE.md.preserved-{n}"
                ).is_symlink():
                    n += 1
                alias.rename(root / f"CLAUDE.md.preserved-{n}")
            alias.symlink_to("AGENTS.md")

    def info(self):
        marker = self.root / MARKER
        return {
            "exists": self.root.is_dir(),
            "empty": not self.root.exists() or not any(self.root.iterdir()),
            "owned": self.owned(),
            # 有标记但不属于本员工 = 其他员工的目录；没有标记的已有目录（如另一套机器人系统在用的）
            # 可以接管。
            "marked": marker.exists() or marker.is_symlink(),
            "workspace_protocol": 1,
        }

    def probe(self):
        """换机前判断两台节点是不是共用同一个目录：源节点建一个随机名文件，目标节点在它的目标路径找。

        文件名每次都是新的，NFS 客户端不可能缓存过它，目标查得到就一定是同一份目录。目标只查不删，
        由建文件的源节点删除：两台节点的用户对目录的写权限可能不同。
        """
        token = self.data.get("token")
        if not isinstance(token, str) or not PROBE_TOKEN.fullmatch(token):
            raise self.error("探测令牌无效")
        path = self.root / f".coreman-probe-{token}"
        action = self.data.get("action")
        if action == "create":
            if not self.root.is_dir():
                raise self.error("工作目录不存在")
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            return {"created": True}
        if action == "check":
            return {"found": path.is_file() and not path.is_symlink()}
        if action == "remove":
            path.unlink(missing_ok=True)
            return {"removed": True}
        raise self.error("不支持的探测操作")

    def remove(self):
        """个人助理停用或换走之后删掉它的工作目录。只在助理专用实例上做，只删本员工的目录。

        归属按守护进程自己保管的登记认（见 OWNERS_DIR），只比 inode（见 registered_to）。平台见到
        「已删除」或「不是这位助理的」就不再来，所以这两种回答只在能确认时给；确认不了一律答
        `unverified`（平台留着登记、隔一阵再来），什么都不动。依次是：

        - 登记是本员工的墓碑：上次已经删了，答「已删除」，眼前这里有什么都不碰；
        - 登记记着别的员工：答「不是这位助理的」；
        - 目录不在：登记记着本员工（运维手工删了，或上次改名进回收站后没来得及写墓碑）就写墓碑、
          答「已删除」；没有登记（例如共享存储没挂上）认不准；
        - 目录在：登记记着本员工且 inode 对得上，或没有登记（登记出现之前初始化的目录）而目录里的
          标记写着本员工，才删；登记读不懂、inode 对不上、既无登记又无标记都认不准。

        判断时只读不建：`.coreman-owners` 与回收站只在真要删时才建。

        删的时候先把目录原子地改名进工作目录根下的回收站（同一文件系统，一步完成），把登记换成墓碑，
        再在后台线程里删：不跟随任何链接，删不掉的留给助理侧的清扫（启动时与之后每 10 分钟）。还有
        在跑的沙箱轮次用着它时不动（`busy`），平台稍后再来。`in_use` 核对与改名都在轮次准入的锁里
        （见 AssistantTurns.admission）：准入在同一把锁里登记工作目录并核对它还在原处，所以改名之后
        不会再有轮次挂上这棵树，后台删除重试时改权限、按路径删也就碰不到沙箱写得到的东西。
        """
        if not self.assistant_only:
            raise self.error("不支持的工作区操作")
        owners = owners_dir(self.agent.root, create=False)
        name = record_name(self.root)
        data = read_record(owners / name) if owners is not None else None
        exists = os.path.lexists(self.root)
        unverified = {"removed": False, "existed": exists, "unverified": True}
        if data is not None and tombstone_of(data, self.bot):
            return {"removed": True, "existed": False}
        if data is not None and live_record(data) and data["bot_id"] != self.bot:
            return {"removed": False, "existed": exists}
        if not exists:
            if data is not None and live_record(data):
                with contextlib.suppress(OSError):
                    write_record(owners / name, {"bot_id": self.bot, "removed": True})
                return {"removed": True, "existed": False}
            return unverified
        info = os.lstat(self.root)
        if not stat.S_ISDIR(info.st_mode):
            return unverified
        if data is None:
            owned = self.owned()
        else:
            owned = registered_to(data, self.bot, info)
        if not owned:
            return unverified
        from .sandbox import remove_tree

        target = trash_dir(self.agent.root) / secrets.token_hex(TRASH_ENTRY_BYTES)
        turns = getattr(getattr(self.agent, "daemon", None), "assistant", None)
        with turns.admission if turns is not None else contextlib.nullcontext():
            if turns is not None and turns.in_use(self.root):
                return {"removed": False, "existed": True, "busy": True}
            os.rename(self.root, target)
        # 换成墓碑写不进去也已经删了：如实答「已删除」。回执丢了再来时没有墓碑，按上面的规则作答。
        with contextlib.suppress(OSError):
            write_record(owners_dir(self.agent.root) / name, {"bot_id": self.bot, "removed": True})
        threading.Thread(target=remove_tree, args=(target,), daemon=True).start()
        return {"removed": True, "existed": True}

    def list(self):
        parent = self.path(self.data.get("path", ""))
        entries = []
        for child in parent.iterdir():
            rel = child.relative_to(self.root).as_posix()
            if forbidden(rel):
                continue
            st = child.lstat()
            entries.append(
                {
                    "name": child.name,
                    "path": rel,
                    "type": "link"
                    if child.is_symlink()
                    else "directory"
                    if child.is_dir()
                    else "file",
                    "size": st.st_size,
                }
            )
            if len(entries) > 1000:
                break
        entries.sort(key=lambda entry: entry["name"])
        return {"entries": entries[:1000], "truncated": len(entries) > 1000}

    def read(self):
        path = self.path(
            self.data["path"], allow_link=True, transfer=bool(self.data.get("transfer_id"))
        )
        if not path.is_file() or path.stat().st_size > MAX_TOTAL:
            raise self.error("文件类型或大小不支持")
        offset = int(self.data.get("offset", 0))
        length = int(self.data.get("length", CHUNK))
        if offset < 0 or length < 1 or length > CHUNK:
            raise self.error("读取范围无效")
        size = path.stat().st_size
        with path.open("rb") as f:
            f.seek(offset)
            raw = f.read(length)
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            content = None
        return {
            "data": base64.b64encode(raw).decode(),
            "content": content,
            "size": size,
            "hash": cached_digest(self.agent.digests, path),
            "editable": size <= MAX_TEXT
            and content is not None
            and (not path.is_symlink() or bool(instruction_alias(self.root, self.data["path"]))),
            "offset": offset,
            "eof": offset + len(raw) >= size,
        }

    def write(self):
        name = self.data["path"]
        path = self.path(instruction_alias(self.root, name) or name)
        raw = self.data["content"].encode("utf-8")
        if len(raw) > MAX_TEXT or "expected_hash" not in self.data:
            raise self.error("文件过大或缺少版本校验")
        actual = digest(path) if path.is_file() else None
        if actual != self.data["expected_hash"]:
            return {
                "success": False,
                "code": "conflict",
                "message": "文件已发生变化，请重新读取后保存",
            }
        path.parent.mkdir(parents=True, exist_ok=True)
        replace_file(path, raw)
        return {"hash": hashlib.sha256(raw).hexdigest(), "size": len(raw)}

    def export(self):
        """换机复制的清单。

        git=True 时，有远端、能在目标节点重新克隆的仓库只带描述（远端、分支、未推送提交的 bundle），
        仓库里与 HEAD 一致的已跟踪文件不进清单，由目标节点克隆还原；expand 里的仓库仍整仓复制。
        """
        git_aware = self.data.get("git") is True
        expand = self.data.get("expand") or []
        if not isinstance(expand, list):
            raise self.error("迁移参数无效")
        manifest, excluded, total = [], [], 0
        repos, descriptors = {}, []
        for parent, dirs, files in os.walk(self.root, followlinks=False):
            here = Path(parent)
            rel_dir = here.relative_to(self.root).as_posix()
            rel_dir = "" if rel_dir == "." else rel_dir
            if git_aware and ".git" in dirs and rel_dir not in expand:
                described = self.describe_repo(here, rel_dir)
                if described is not None:
                    descriptors.append(described[0])
                    repos[rel_dir] = described[1]
                    dirs.remove(".git")
            for name in list(dirs) + files:
                path = here / name
                rel = path.relative_to(self.root).as_posix()
                if forbidden(rel, transfer=True):
                    excluded.append(rel)
                    if name in dirs:
                        dirs.remove(name)
                    continue
                if repos and clean_in_repo(rel, repos):
                    if name in dirs:
                        dirs.remove(name)
                    continue
                if path.is_symlink():
                    if name in dirs:
                        dirs.remove(name)
                    link = os.readlink(path)
                    if (
                        os.path.isabs(link)
                        or not path.resolve().is_relative_to(self.root)
                        or forbidden(
                            path.resolve().relative_to(self.root).as_posix(), transfer=True
                        )
                    ):
                        excluded.append(rel)
                        continue
                    manifest.append({"path": rel, "link": link})
                elif path.is_file():
                    size = path.stat().st_size
                    total += size
                    manifest.append(
                        {
                            "path": rel,
                            "size": size,
                            "hash": cached_digest(self.agent.digests, path),
                            "mode": stat.S_IMODE(path.stat().st_mode) & 0o777,
                        }
                    )
                elif name in files:
                    excluded.append(rel)
                if len(manifest) > MAX_FILES or total > MAX_TOTAL:
                    raise self.error(
                        "工作目录超过迁移上限（5 万个文件或 4G，可从远端重新克隆的仓库不计）"
                    )
        return {
            "manifest": manifest,
            "excluded": excluded[:1000],
            "total_size": total,
            "repos": descriptors,
        }

    def git_paths(self, args, repo):
        from .agent import MAX_BODY

        out = self.git(args, repo)
        # run_command 只读前 MAX_BODY 字节；截断的列表会把改动当成未改动，宁可整仓复制。
        if len(out.encode()) >= MAX_BODY - 16:
            raise self.error("Git 输出过长")
        return {name for name in out.split("\0") if name}

    def describe_repo(self, repo, rel):
        """能在目标节点重新克隆的仓库：返回 (描述, 与 HEAD 一致的已跟踪文件)。

        否则返回 None，整仓复制。
        """
        gitdir = repo / ".git"
        # 子模块、多工作树、浅克隆与 LFS 都无法靠克隆原样还原。
        if (repo / ".gitmodules").exists() or any(
            (gitdir / name).exists() for name in ("shallow", "worktrees", "modules", "lfs")
        ):
            return None
        try:
            url = self.git(["remote", "get-url", "origin"], repo).strip()
            # 带凭证、非 HTTPS/SSH 的远端不往外传，整仓复制。
            self.agent.git_source(url, any_host=True)
            head = self.git(["rev-parse", "--verify", "HEAD"], repo).strip()
            # 储藏也克隆不回来。
            if not SHA.fullmatch(head) or self.optional(
                ["rev-parse", "--verify", "--quiet", "refs/stash"], repo
            ):
                return None
            branch = self.optional(["symbolic-ref", "--quiet", "--short", "HEAD"], repo)
            branches = []
            listing = self.git(
                [
                    "for-each-ref",
                    "--format=%(refname:short)%00%(objectname)%00%(upstream:short)",
                    "refs/heads",
                ],
                repo,
            )
            for line in listing.splitlines():
                name, sha, upstream = line.split("\0")
                branches.append({"name": name, "sha": sha, "upstream": upstream})
            local = ["--branches"] if branch else ["--branches", "HEAD"]
            unpushed = int(self.git(["rev-list", "--count", *local, "--not", "--remotes"], repo))
            bundle = None
            if unpushed:
                with tempfile.TemporaryDirectory() as tmp:
                    target = Path(tmp) / "repo.bundle"
                    self.git(["bundle", "create", str(target), *local, "--not", "--remotes"], repo)
                    if target.stat().st_size > BUNDLE_LIMIT:
                        return None
                    bundle = base64.b64encode(target.read_bytes()).decode()
            tracked = self.git_paths(["ls-files", "-z"], repo)
            options = ["diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z"]
            changed = self.git_paths([*options, "HEAD"], repo)
            deleted = self.git_paths([*options, "--diff-filter=D", "HEAD"], repo)
        except (self.error, ValueError):
            return None
        descriptor = {
            "path": rel,
            "url": url,
            "head": head,
            "branch": branch,
            "branches": branches,
            "bundle": bundle,
            "deleted": sorted(deleted),
        }
        return descriptor, tracked - changed

    def stage(self):
        transfer = self.data.get("transfer_id", "")
        if not re.fullmatch(r"[a-fA-F0-9-]{16,64}", transfer):
            raise self.error("迁移标识无效")
        stage = self.root.parent / (
            ".coreman-transfer-"
            + hashlib.sha256((str(self.root) + self.bot + transfer).encode()).hexdigest()[:32]
        )
        if stage.is_symlink():
            raise self.error("暂存目录无效")
        return stage

    def repo_stage(self, stage, repo):
        rel = repo.get("path") if isinstance(repo, dict) else None
        if not isinstance(rel, str):
            raise self.error("仓库描述无效")
        if rel:
            self.path(rel, transfer=True)
        return stage / "repos" / hashlib.sha256(rel.encode()).hexdigest()[:16]

    def clone(self):
        """换机：按源仓库描述在暂存区重新克隆，带回未推送的提交，还原分支、上游与 HEAD。

        用节点用户自己的 Git 凭证访问远端；失败时平台改为整仓复制。
        """
        from .agent import run_command

        stage = self.stage()
        repo = self.data.get("repo")
        dest = self.repo_stage(stage, repo)
        url, _ = self.agent.git_source(str(repo.get("url", "")), any_host=True)
        head, branch = str(repo.get("head", "")), str(repo.get("branch") or "")
        branches = repo.get("branches")
        if (
            not SHA.fullmatch(head)
            or not isinstance(branches, list)
            or len(branches) > 1000
            or any(not SHA.fullmatch(str(b.get("sha", ""))) for b in branches)
            or (branch and branch not in [b.get("name") for b in branches])
        ):
            raise self.error("仓库描述无效")
        sweep_stages(self.root.parent)
        stage.mkdir(mode=0o700, parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(exist_ok=True)
        deadline = time.monotonic() + 900
        env = {k: v for k, v in os.environ.items() if not k.startswith(("COREMAN_", "GIT_"))}
        env["GIT_TERMINAL_PROMPT"] = "0"
        base = ["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.file.allow=never"]

        def run(args, *, prefix=base):
            remaining = int(deadline - time.monotonic())
            if remaining <= 0:
                raise self.error("克隆超时")
            return run_command([*prefix, *args], timeout=remaining, env_override=env, git_auth=True)

        def git(*args, prefix=base):
            return run(["-C", str(dest), *args], prefix=prefix)

        # 只拉提交与目录树，文件内容在检出时按需取，历史很大的仓库也快。
        run(["clone", "--quiet", "--no-checkout", "--filter=blob:none", "--", url, str(dest)])
        if repo.get("bundle"):
            raw = base64.b64decode(str(repo["bundle"]), validate=True)
            if len(raw) > BUNDLE_LIMIT:
                raise self.error("仓库描述无效")
            bundle = dest.parent / (dest.name + ".bundle")
            bundle.write_bytes(raw)
            refspecs = ["+refs/*:refs/coreman-transfer/*"]
            if not branch:
                refspecs.append("+HEAD:refs/coreman-transfer/HEAD")
            # 暂存区里我们自己写的 bundle 文件：放开 file 协议只用于这一次读取。
            local = ["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.file.allow=always"]
            git("fetch", "--quiet", "--no-tags", str(bundle), *refspecs, prefix=local)
            bundle.unlink()
        wanted = set()
        for item in branches:
            name, upstream = str(item.get("name", "")), str(item.get("upstream") or "")
            git("check-ref-format", "--branch", name)
            git("update-ref", f"refs/heads/{name}", item["sha"])
            wanted.add(f"refs/heads/{name}")
            if upstream.startswith("origin/"):
                git("config", f"branch.{name}.remote", "origin")
                git("config", f"branch.{name}.merge", "refs/heads/" + upstream[len("origin/") :])
        if branch:
            git("symbolic-ref", "HEAD", f"refs/heads/{branch}")
        else:
            git("update-ref", "--no-deref", "HEAD", head)
        if git("rev-parse", "HEAD").strip() != head:
            raise self.error("克隆后的 HEAD 与源仓库不一致")
        # 克隆自带、源仓库没有的本地分支与 bundle 的临时引用都删掉。
        refs = git("for-each-ref", "--format=%(refname)", "refs/heads", "refs/coreman-transfer")
        for ref in refs.split():
            if ref not in wanted:
                git("update-ref", "-d", ref)
        git("reset", "--quiet", "--hard", "HEAD")
        return {"cloned": True}

    def validate_manifest(self, manifest, root):
        if not isinstance(manifest, list) or len(manifest) > MAX_FILES:
            raise self.error("迁移清单无效")
        seen, total = set(), 0
        for entry in manifest:
            name = entry["path"]
            p = self.path(name, root, allow_link="link" in entry, transfer=True)
            if not name or str(PurePosixPath(name)) != name or name in seen:
                raise self.error("迁移清单路径冲突")
            seen.add(name)
            if "link" in entry:
                link = entry["link"]
                target = (p.parent / link).resolve()
                if (
                    os.path.isabs(link)
                    or not target.is_relative_to(root.resolve())
                    or forbidden(target.relative_to(root.resolve()).as_posix(), transfer=True)
                ):
                    raise self.error("迁移链接越界")
            else:
                size = entry.get("size")
                if (
                    not isinstance(size, int)
                    or size < 0
                    or not re.fullmatch("[a-f0-9]{64}", entry.get("hash", ""))
                ):
                    raise self.error("迁移清单校验无效")
                total += size
        for name in seen:
            if any(str(p) in seen for p in PurePosixPath(name).parents if str(p) != "."):
                raise self.error("迁移清单目录冲突")
        if total > MAX_TOTAL:
            raise self.error("迁移超过大小限制")

    def import_(self):
        stage = self.stage()
        meta = stage / "transfer.json"
        files = stage / "files"
        if not meta.exists():
            manifest = self.data.get("manifest")
            self.validate_manifest(manifest, files)
            if not stage.exists():
                sweep_stages(self.root.parent)
            stage.mkdir(mode=0o700, parents=True, exist_ok=True)
            files.mkdir(exist_ok=True)
            meta.write_text(
                json.dumps({"bot_id": self.bot, "target": str(self.root), "manifest": manifest})
            )
            for entry in manifest:
                if "link" not in entry and entry["size"] == 0:
                    p = self.path(entry["path"], files, transfer=True)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.touch()
        record = json.loads(meta.read_text())
        if (
            record["bot_id"] != self.bot
            or record["target"] != str(self.root)
            or ("manifest" in self.data and self.data["manifest"] != record["manifest"])
        ):
            raise self.error("迁移清单或归属发生变化")
        if not self.data.get("path"):
            return {"received": 0}
        entry = next((e for e in record["manifest"] if e["path"] == self.data["path"]), None)
        if not entry or "link" in entry:
            raise self.error("文件不在迁移清单中")
        if (
            not isinstance(self.data.get("data"), str)
            or len(self.data["data"]) > ((CHUNK + 2) // 3) * 4
        ):
            raise self.error("迁移分块超过大小限制")
        raw = base64.b64decode(self.data["data"], validate=True)
        p = self.path(entry["path"], files, transfer=True)
        received = p.stat().st_size if p.exists() else 0
        if (
            len(raw) > CHUNK
            or int(self.data.get("offset", 0)) != received
            or received + len(raw) > entry["size"]
        ):
            raise self.error("迁移分块大小或顺序不正确")
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("ab") as f:
            f.write(raw)
        return {"received": received + len(raw)}

    def install(self, files, replace=False):
        if self.root.exists() and not replace:
            names = [p.name for p in self.root.iterdir()]
            if names and not (names == [MARKER] and self.owned()):
                raise self.error("目标工作目录非空，未覆盖已有文件")
            if names:
                (self.root / MARKER).unlink()
            self.root.rmdir()
        self.init(files)
        if self.root.exists():
            # 换机「复制当前文件」即完全覆盖：原目录整体挪进暂存目录，随暂存目录一起删除。
            self.root.rename(files.parent / "replaced")
        files.rename(self.root)

    def finish(self):
        if self.owned():
            marker = marker_data(self.root)
            if marker.get("transfer_id") and marker["transfer_id"] == self.data.get("transfer_id"):
                return {"installed": True}
        stage = self.stage()
        files = stage / "files"
        record = json.loads((stage / "transfer.json").read_text())
        if (
            record["bot_id"] != self.bot
            or record["target"] != str(self.root)
            or ("manifest" in self.data and self.data["manifest"] != record["manifest"])
        ):
            raise self.error("迁移归属或清单无效")
        self.validate_manifest(record["manifest"], files)
        for entry in record["manifest"]:
            if "link" in entry:
                continue
            p = self.path(entry["path"], files, transfer=True)
            if not p.is_file() or p.stat().st_size != entry["size"] or digest(p) != entry["hash"]:
                raise self.error("迁移文件不完整或校验失败")
            p.chmod(int(entry.get("mode", 0o644)) & 0o777)
        for entry in record["manifest"]:
            if "link" in entry:
                p = self.path(entry["path"], files, allow_link=True, transfer=True)
                p.parent.mkdir(parents=True, exist_ok=True)
                if p.is_symlink():
                    if os.readlink(p) != entry["link"]:
                        raise self.error("迁移链接发生变化")
                else:
                    p.symlink_to(entry["link"])
        self.restore_repos(stage, files)
        self.install(files, replace=self.data.get("replace") is True)
        # 被替换的旧目录里可能有删不掉的只读目录；文件已经就位，清理失败不影响结果。
        shutil.rmtree(stage, ignore_errors=True)
        return {"installed": True}

    def restore_repos(self, stage, files):
        """把暂存区克隆好的仓库放回清单复制的文件里：复制来的（改动、未跟踪）文件覆盖克隆结果，
        源上删掉的已跟踪文件也删掉。内层仓库先放，外层仓库再整体合并上去。"""
        repos = self.data.get("repos") or []
        if not isinstance(repos, list):
            raise self.error("迁移参数无效")
        for repo in sorted(repos, key=lambda r: -len(PurePosixPath(str(r.get("path"))).parts)):
            clone = self.repo_stage(stage, repo)
            if not (clone / ".git").is_dir() or (clone / ".git").is_symlink():
                raise self.error("仓库尚未克隆完成")
            rel = repo["path"]
            overlay = self.path(rel, files, transfer=True) if rel else files
            if overlay.is_symlink() or (overlay.exists() and not overlay.is_dir()):
                raise self.error("迁移清单目录冲突")
            if overlay.exists():
                merge_tree(overlay, clone)
            for name in repo.get("deleted") or []:
                try:
                    target = self.path(str(name), clone, allow_link=True, transfer=True)
                except self.error:
                    continue
                if target.is_symlink() or target.is_file():
                    target.unlink()
            if overlay.exists():
                shutil.rmtree(overlay)
            overlay.parent.mkdir(parents=True, exist_ok=True)
            clone.rename(overlay)

    def git(self, args, repo=None):
        """只读查看工作目录的 Git 状态：不带凭证、不联网，仓库自带的执行类配置一律拒绝。"""
        repo = repo or self.root
        gitdir = repo / ".git"
        if (gitdir.exists() or gitdir.is_symlink()) and repo not in self.checked_repos:
            if not gitdir.is_dir() or gitdir.is_symlink():
                raise self.error("不支持链接或外部 Git 元数据目录")
            if (gitdir / "commondir").exists():
                raise self.error("不支持外部 Git 元数据目录")
            for parent, dirs, names in os.walk(gitdir):
                if any((Path(parent) / n).is_symlink() for n in dirs + names):
                    raise self.error("Git 元数据包含不安全链接")
            config_path = gitdir / "config"
            if config_path.is_symlink():
                raise self.error("Git 配置路径不安全")
            # Existing repositories can contain local executable filters, includes or
            # alternate SSH/proxy programs. Never run admin Git with such configuration.
            config = configparser.RawConfigParser(strict=False)
            try:
                config.read_string(config_path.read_text())
            except (OSError, configparser.Error) as exc:
                raise self.error("Git 配置无效") from exc
            for section in config.sections():
                base = section.lower().split(" ", 1)[0]
                if base in {"include", "includeif", "filter", "credential", "alias", "url"}:
                    raise self.error("Git 配置含不受支持的执行或凭证设置")
                for key, _value in config.items(section):
                    if key.lower() in {
                        "sshcommand",
                        "gitproxy",
                        "askpass",
                        "worktree",
                        "pager",
                        "editor",
                        "uploadpack",
                        "receivepack",
                        "proxy",
                    }:
                        raise self.error("Git 配置含不受支持的执行设置")
            self.checked_repos.add(repo)
        # Ignore inherited Git config, credential helpers and hooks, including repo config hooks.
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_LITERAL_PATHSPECS": "1",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_SSH_COMMAND": "ssh -oBatchMode=yes",
            }
        )
        from .agent import run_command

        try:
            output = run_command(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "credential.helper=",
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    # 节点用户的全局忽略文件（~/.config/git/ignore）不该左右这里看到的变更。
                    "core.excludesFile=/dev/null",
                    "-c",
                    "protocol.allow=never",
                    *args,
                ],
                cwd=repo,
                env_override=env,
                stderr_to_stdout=False,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise self.error("Git 操作失败或超时") from exc
        return output.rstrip("\n")

    def optional(self, args, repo=None):
        try:
            return self.git(args, repo)
        except self.error:
            return ""

    def is_repo(self):
        gitdir = self.root / ".git"
        return gitdir.is_dir() and not gitdir.is_symlink()

    def git_status(self):
        """只读呈现：远端地址、分支、最近提交、未推送的提交数，以及有变化的文件。

        提交与推送由 AI 员工自己决定，这里只给管理员判断用。
        """
        if not self.is_repo():
            return {"initialized": False}
        # -z 保留中文与特殊字符路径；重命名项后面跟着一个原路径记录。未跟踪目录只列一项。
        raw = self.git(["status", "--porcelain=v1", "-z", "--untracked-files=normal"])
        entries, skip = [], False
        for line in raw.split("\0"):
            if skip:
                skip = False
                continue
            if len(line) < 4:
                continue
            status, name = line[:2], line[3:]
            skip = "R" in status or "C" in status
            if not forbidden(name.rstrip("/")):
                entries.append({"path": name, "status": status})
        head = self.optional(["log", "-1", "--format=%h%x00%s%x00%cI"]).split("\0")
        ahead = self.optional(["rev-list", "--count", "@{upstream}..HEAD"])
        return {
            "initialized": True,
            "remote": redact(self.optional(["remote", "get-url", "origin"])),
            "branch": self.optional(["symbolic-ref", "--quiet", "--short", "HEAD"]),
            "upstream": self.optional(
                ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"]
            ),
            "ahead": int(ahead) if ahead.isdigit() else None,
            "head": {"hash": head[0], "subject": head[1], "date": head[2]}
            if len(head) == 3
            else None,
            "files": entries[:1000],
            "truncated": len(entries) > 1000,
        }

    def git_diff(self):
        """单个文件相对最近一次提交的改动；未跟踪的文件按全部新增呈现。只读、限长。"""
        name = self.data.get("path")
        path = self.path(name, allow_link=True)
        if not self.is_repo():
            raise self.error("工作目录不是 Git 仓库")
        state = self.git(["status", "--porcelain=v1", "-z", "--untracked-files=all", "--", name])
        if state.startswith("??"):
            text = untracked_diff(name, path)
        else:
            options = ["--no-ext-diff", "--no-textconv", "--no-color"]
            if self.optional(["rev-parse", "--verify", "HEAD"]):
                text = self.git(["diff", *options, "HEAD", "--", name])
            else:
                text = "\n".join(
                    part
                    for part in (
                        self.git(["diff", "--cached", *options, "--", name]),
                        self.git(["diff", *options, "--", name]),
                    )
                    if part
                )
        raw = text.encode()
        return {
            "path": name,
            "diff": raw[:MAX_DIFF].decode(errors="ignore"),
            "truncated": len(raw) > MAX_DIFF,
        }

    def dispatch(self):
        name = self.data["type"].removeprefix("workspace-").replace("-", "_")
        if name == "import":
            name = "import_"
        if name not in {
            "info",
            "init",
            "list",
            "read",
            "write",
            "export",
            "import_",
            "finish",
            "clone",
            "git_status",
            "git_diff",
            "probe",
            "remove",
        }:
            raise self.error("不支持的工作区操作")
        try:
            return {"success": True, **getattr(self, name)()}
        except InstructionsConflict as exc:
            return {"success": False, "code": "instructions_conflict", "message": str(exc)}
        except self.error:
            raise
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            raise self.error("工作区操作失败，请检查文件、目录及迁移参数") from exc
