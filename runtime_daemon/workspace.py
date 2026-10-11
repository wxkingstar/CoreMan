"""Bounded workspace administration over the authenticated daemon channel."""

from __future__ import annotations

import base64
import configparser
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

CHUNK = 256 * 1024
MAX_TEXT = 1024 * 1024
MAX_TOTAL = 1024**3
MARKER = ".coreman-workspace.json"
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


def forbidden(name):
    return any(
        p in EXCLUDED
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


def exclude_marker(directory):
    """Git 仓库里把所有权标记列入本地排除：共用目录的另一套系统部署时可能 git clean -fd，
    不排除就会删掉标记；也免得它出现在 git status 里、被误提交。"""
    if not (directory / ".git").is_dir() or (directory / ".git").is_symlink():
        return
    current = git_exclude_file(directory)
    previous = current.read_text() if current.is_file() else ""
    if "/" + MARKER not in previous.splitlines():
        current.parent.mkdir(parents=True, exist_ok=True)
        text = previous + ("\n" if previous and not previous.endswith("\n") else "")
        replace_file(current, (text + "/" + MARKER + "\n").encode())


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
        self.bot = str(data.get("bot_id") or "")
        if not self.bot:
            raise self.error("缺少员工标识")
        marker = self.root / MARKER
        if (
            data.get("type") != "workspace-info"
            and (marker.exists() or marker.is_symlink())
            and not self.owned()
        ):
            raise self.error("工作目录属于其他员工或所有权标记无效")

    def path(self, name, root=None, allow_link=False):
        root = root or self.root
        if not isinstance(name, str) or "\x00" in name or "\\" in name:
            raise self.error("文件路径无效")
        p = PurePosixPath(name)
        if p.is_absolute() or ".." in p.parts or forbidden(name):
            raise self.error("该文件路径不允许访问")
        target = root / p
        resolved = target.resolve()
        if not resolved.is_relative_to(root.resolve()):
            raise self.error("文件超出工作目录")
        if forbidden(resolved.relative_to(root.resolve()).as_posix()):
            raise self.error("链接目标不允许访问")
        current = root
        for part in p.parts:
            current = current / part
            if current.is_symlink() and not (allow_link and current == target):
                raise self.error("不允许通过链接访问文件")
        return target

    def owned(self, root=None):
        marker = (root or self.root) / MARKER
        if marker.is_symlink():
            return False
        try:
            return json.loads(marker.read_text()).get("bot_id") == self.bot
        except (OSError, ValueError):
            return False

    def init(self, root=None):
        root = root or self.root
        root.mkdir(parents=True, exist_ok=True)
        marker = root / MARKER
        if marker.exists() or marker.is_symlink():
            if not self.owned(root):
                raise self.error("工作目录属于其他员工或所有权标记无效")
        self.instructions(root)
        link_skills(root)
        remove_legacy_instruction_excludes(root)
        exclude_marker(root)
        marker.write_text(
            json.dumps(
                {
                    "bot_id": self.bot,
                    "workspace_protocol": 1,
                    "transfer_id": self.data.get("transfer_id"),
                }
            )
        )
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
        path = self.path(self.data["path"], allow_link=True)
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
            "hash": digest(path),
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
        manifest, excluded, total = [], [], 0
        for parent, dirs, files in os.walk(self.root, followlinks=False):
            for name in list(dirs) + files:
                path = Path(parent) / name
                rel = path.relative_to(self.root).as_posix()
                if forbidden(rel):
                    excluded.append(rel)
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
                        or forbidden(path.resolve().relative_to(self.root).as_posix())
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
                            "hash": digest(path),
                            "mode": stat.S_IMODE(path.stat().st_mode) & 0o777,
                        }
                    )
                elif name in files:
                    excluded.append(rel)
                if len(manifest) > 10000 or total > MAX_TOTAL:
                    raise self.error("工作目录超过迁移大小限制")
        return {"manifest": manifest, "excluded": excluded[:1000], "total_size": total}

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

    def validate_manifest(self, manifest, root):
        if not isinstance(manifest, list) or len(manifest) > 10000:
            raise self.error("迁移清单无效")
        seen, total = set(), 0
        for entry in manifest:
            name = entry["path"]
            p = self.path(name, root, allow_link="link" in entry)
            if (
                not name
                or str(PurePosixPath(name)) != name
                or name in seen
                or any(PurePosixPath(name).is_relative_to(PurePosixPath(x)) for x in seen)
            ):
                raise self.error("迁移清单路径冲突")
            seen.add(name)
            if "link" in entry:
                link = entry["link"]
                target = (p.parent / link).resolve()
                if (
                    os.path.isabs(link)
                    or not target.is_relative_to(root.resolve())
                    or forbidden(target.relative_to(root.resolve()).as_posix())
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
        if not stage.exists():
            manifest = self.data.get("manifest")
            self.validate_manifest(manifest, files)
            stage.mkdir(mode=0o700, parents=True)
            files.mkdir()
            meta.write_text(
                json.dumps({"bot_id": self.bot, "target": str(self.root), "manifest": manifest})
            )
            for entry in manifest:
                if "link" not in entry and entry["size"] == 0:
                    p = self.path(entry["path"], files)
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
        p = self.path(entry["path"], files)
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

    def install(self, files):
        if self.root.exists():
            names = [p.name for p in self.root.iterdir()]
            if names and not (names == [MARKER] and self.owned()):
                raise self.error("目标工作目录非空，未覆盖已有文件")
            if names:
                (self.root / MARKER).unlink()
            self.root.rmdir()
        self.init(files)
        files.rename(self.root)

    def finish(self):
        if self.owned():
            marker = json.loads((self.root / MARKER).read_text())
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
            p = self.path(entry["path"], files)
            if not p.is_file() or p.stat().st_size != entry["size"] or digest(p) != entry["hash"]:
                raise self.error("迁移文件不完整或校验失败")
            p.chmod(int(entry.get("mode", 0o644)) & 0o777)
        for entry in record["manifest"]:
            if "link" in entry:
                p = self.path(entry["path"], files, allow_link=True)
                p.parent.mkdir(parents=True, exist_ok=True)
                if p.is_symlink():
                    if os.readlink(p) != entry["link"]:
                        raise self.error("迁移链接发生变化")
                else:
                    p.symlink_to(entry["link"])
        self.install(files)
        shutil.rmtree(stage)
        return {"installed": True}

    def git(self, args):
        """只读查看工作目录的 Git 状态：不带凭证、不联网，仓库自带的执行类配置一律拒绝。"""
        repo = self.root
        gitdir = repo / ".git"
        if gitdir.exists() or gitdir.is_symlink():
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

    def optional(self, args):
        try:
            return self.git(args)
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
            "git_status",
            "git_diff",
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
