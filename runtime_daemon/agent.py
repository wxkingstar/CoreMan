"""运行时节点上的实例操作：工作区、Git 仓库、记忆、技能安装与额度/健康探测。

由 runtime_daemon 按 AI 类型各持有一个实例，经反向通道接收 CoreMan 的管理命令；
本模块不监听任何端口，也不单独部署。
"""

from __future__ import annotations

import base64
import contextvars
import hashlib
import json
import math
import os
import platform
import re
import select
import shlex
import signal
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

MAX_BODY = 4 * 1024 * 1024
MAX_MEMORY = 256 * 1024
OUTPUT_STYLES = {
    "verbosity-normal.md": (
        "---\nname: 正常人模式\ndescription: 精简回复，避免不必要的填充和重复总结\n"
        "keep-coding-instr"
        "uctions: true\n---\n\nKeep responses focused and to the point. Avoid"
        " unnecessary filler, repetitive summaries, and over-explanation. "
        "Use short paragraphs or bullet points. If a one-sentence answer s"
        "uffices, do not write three.\n"
    ),
    "verbosity-quiet.md": (
        "---\nname: 闷葫芦模式\ndescription: 自然直接，像同事聊天一样\nkeep-coding-instruction"
        "s: true\n---\n\nRespond like a knowledgeable colleague in casual con"
        "versation. Use plain, natural language — no formal tone, no corpo"
        "rate jargon, no unnecessary structure. Skip greetings, pleasantri"
        "es, and meta-commentary (don't say '让我来分析一下'). Go straight to the"
        " answer. Use prose over bullet points when it reads more naturall"
        "y. Keep it concise but always include the complete substantive an"
        "swer.\n"
    ),
    "verbosity-silent.md": (
        "---\nname: 已读不回模式\ndescription: 极简回复，只保留关键信息\nkeep-coding-instructio"
        "ns: true\n---\n\nBe extremely terse. No greetings, no filler, no sum"
        "maries, no sign-offs. Strip all pleasantries and transitions — go"
        " straight to the substance. Prefer bullet points or short paragra"
        "phs over long prose.\n\nIMPORTANT: You MUST still provide the full "
        "substantive answer. 'Minimal' means minimal decoration, NOT minim"
        "al content. Data, analysis results, key findings, and actionable "
        "information must be complete. Never reply with just a status like"
        " '处理完成' or 'Done' — always include the actual result.\n"
    ),
}


COMMAND_CANCEL = contextvars.ContextVar("coreman_command_cancel", default=None)


CLAUDE_PROBE_DISABLED = (
    "未启用 Claude 额度探针：安装链接勾选该选项，"
    "或在 config.json 设置 install_claude_probe 为 true 后重启服务"
)


class OperationError(Exception):
    """Messages are fixed prompts, at most with an exit code, HTTP status or count.

    They never carry command output, paths or credentials, so they may be logged and
    returned to the platform (tests/unit/test_runtime_daemon_errors.py checks this).
    """


def operation_message(exc: OperationError) -> str:
    return str(exc)[:200]


def run_command(
    command: list[str],
    cwd: Path | None = None,
    timeout: int = 300,
    *,
    env_override: dict[str, str] | None = None,
    stderr_to_stdout: bool = True,
) -> str:
    """不通过 shell；超时终止整个进程组，不把命令输出（可能有凭证）放进异常。"""
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT if stderr_to_stdout else subprocess.DEVNULL,
            start_new_session=True,
            # 第三方安装器与探针不需要 Agent 的服务端身份或服务端配置。
            env=env_override
            if env_override is not None
            else {
                key: value for key, value in os.environ.items() if not key.startswith("COREMAN_")
            },
        )
        try:
            deadline = time.monotonic() + timeout
            cancelled = COMMAND_CANCEL.get()
            while True:
                if cancelled is not None and cancelled.is_set():
                    raise subprocess.TimeoutExpired(command, timeout)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    rc = proc.wait(timeout=min(0.2, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
        except subprocess.TimeoutExpired as exc:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=3)
            raise OperationError("操作超时，子进程已停止") from exc
        if rc:
            raise OperationError(f"操作失败（退出码 {rc}）")
        output.seek(0)
        return output.read(MAX_BODY).decode(errors="replace")


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as tmp:
        tmp.write(content)
        temp_path = Path(tmp.name)
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, path)


def status_line_command(value: object) -> str | None:
    """Claude statusLine 只支持 command 类型；无法识别时返回 None。"""
    if not isinstance(value, dict) or value.get("type", "command") != "command":
        return None
    command = value.get("command")
    return command if isinstance(command, str) and command.strip() else None


def saved_status_line(path: Path) -> dict[str, object] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    value = data.get("statusLine") if isinstance(data, dict) else None
    return value if status_line_command(value) is not None else None


def claude_capture_script(target: Path, command: str | None) -> str:
    """statusLine 包装脚本：落盘 Claude 传入的 JSON（订阅账号含 rate_limits），再交给原命令显示。"""
    forward = "/bin/sh -c " + shlex.quote(command) if command else ""
    # 采集失败不能拖垮状态栏：临时文件建不出来时，直接把输入交给原命令。
    fallback = "exec " + forward if command else "cat >/dev/null; exit 0"
    lines = [
        "#!/bin/sh",
        "# CoreMan Claude 额度探针，由 runtime_daemon 生成，重新安装时覆盖。",
        "umask 077",
        "target=" + shlex.quote(str(target)),
        'tmp=$(mktemp "$target.XXXXXX" 2>/dev/null) || { ' + fallback + "; }",
        'cat > "$tmp"',
        "ok=$?",
        # 替换前先打开，原命令读到的一定是本次输入，不会被并发会话的写入换掉。
        'exec 3< "$tmp"',
        'if [ "$ok" -eq 0 ] && [ -s "$tmp" ]; then mv -f "$tmp" "$target"; else rm -f "$tmp"; fi',
        "exec " + forward + " <&3 3<&-" if command else "exit 0",
    ]
    return "\n".join(lines) + "\n"


class Agent:
    def __init__(
        self,
        *,
        root: Path,
        api_url: str,
        token: str,
        relay_id: str,
        provider: str = "claude",
        model: str = "",
        git_hosts: tuple[str, ...] = ("github.com",),
        home: Path | None = None,
    ) -> None:
        if len(token) < 24 or not relay_id:
            raise ValueError("必须配置实例 ID 与 Agent 令牌")
        if urlsplit(api_url).scheme not in {"http", "https"}:
            raise ValueError("COREMAN_API_URL 必须是 HTTP(S) 地址")
        self.root, self.home = root.resolve(), home or Path.home()
        self.api_url, self.token, self.relay_id = api_url.rstrip("/"), token, relay_id
        self.provider, self.model = provider, model
        self.git_hosts = set(git_hosts)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.background: set[str] = set()
        self.background_lock = threading.Lock()
        self.http = requests.Session()
        self.http.trust_env = False

    def workspace(self, raw: str) -> Path:
        if not raw or "\x00" in raw:
            raise OperationError("需要工作目录")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self.root / path
        resolved = path.resolve()
        if resolved == self.root or not resolved.is_relative_to(self.root):
            raise OperationError("工作目录不在允许范围内")
        return resolved

    def git_source(self, raw: str, *, any_host: bool = False) -> tuple[str, str | None]:
        url, sep, skill = raw.partition(".git@")
        if sep:
            url += ".git"
        else:
            url = raw
        if url.startswith("git@"):
            match = re.fullmatch(r"git@([A-Za-z0-9.-]+):([A-Za-z0-9_./-]+)\.git", url)
            if not match:
                raise OperationError("Git 地址格式不正确")
            host = match.group(1)
        else:
            parts = urlsplit(url)
            if (
                parts.scheme != "https"
                or parts.username
                or parts.password
                or parts.query
                or parts.fragment
            ):
                raise OperationError("Git 来源必须使用允许的 HTTPS 或 SSH 地址")
            host = parts.hostname
            if not host:
                raise OperationError("Git 地址格式不正确")
        if not any_host and host not in self.git_hosts:
            raise OperationError("Git 来源不在白名单内")
        if skill and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", skill):
            raise OperationError("Skill 名称不合法")
        return url, skill or None

    def report(self, endpoint: str, body: dict) -> dict:
        response = self.http.post(
            self.api_url + "/api/infra/" + endpoint,
            json={**body, "server_id": self.relay_id},
            headers={"Authorization": "Bearer " + self.token},
            timeout=30,
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise OperationError(f"上报失败（HTTP {response.status_code}）")
        data = response.json()
        if data.get("code") != 0:
            raise OperationError("上报被拒绝")
        return data

    def active_tasks(self) -> list[dict]:
        """本实例正在执行的任务；由运行时守护进程按其在途调用提供。"""
        return []

    def memory_dir(self, working_dir: str) -> Path:
        workspace = self.workspace(working_dir)
        base = (self.home / ".claude/projects").resolve()
        path = base / str(workspace).replace("/", "-") / "memory"
        if not path.resolve().is_relative_to(base):
            raise OperationError("记忆路径不合法")
        return path

    def assigned_workspaces(self) -> list[str]:
        directories = []
        for page in range(1, 101):
            with self.http.get(
                self.api_url + "/api/infra/memories/workspaces",
                params={"server_id": self.relay_id, "page": page},
                headers={"Authorization": "Bearer " + self.token},
                timeout=30,
                allow_redirects=False,
                stream=True,
            ) as response:
                if response.status_code != 200:
                    raise OperationError("无法读取本实例的记忆目录分配")
                raw = bytearray()
                for part in response.iter_content(65536):
                    raw.extend(part)
                    if len(raw) > MAX_BODY:
                        raise OperationError("目录分配响应过大")
                body = json.loads(raw)
                data = body.get("data", {})
                if body.get("code") != 0 or not isinstance(data.get("working_dirs"), list):
                    raise OperationError("目录分配响应无效")
                batch = data["working_dirs"]
                if len(batch) > 500 or any(not isinstance(item, str) for item in batch):
                    raise OperationError("目录分配响应无效")
                directories.extend(batch)
                if not data.get("has_more"):
                    return list(dict.fromkeys(directories))
        raise OperationError("本实例目录数量超过上限")

    def report_memory(self, working_dir: str | None) -> dict:
        directories = [working_dir] if working_dir else self.assigned_workspaces()
        count, failed = 0, 0
        for directory in directories:
            try:
                with self.lock:
                    memories = self.collect_memory(directory)
                # 网络上报在锁外，避免与 CoreMan 换机事务相互等待。
                self.report("memories/collect", {"memories": memories})
                count += len(memories)
            except Exception:
                failed += 1
        if failed:
            raise OperationError(
                f"已回收 {count} 个文件，{failed} 个目录失败，请检查分配与文件状态"
            )
        return {"success": True, "message": "记忆已回收", "count": count}

    def collect_memory(self, working_dir: str | None) -> list[dict]:
        folders = (
            [self.workspace(working_dir)]
            if working_dir
            else [p for p in self.root.iterdir() if p.is_dir() and not p.is_symlink()]
        )
        result = []
        for folder in folders:
            if not folder.is_dir():
                raise OperationError("记忆工作目录不存在")
            for path in self.memory_dir(str(folder)).glob("*.md"):
                if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_MEMORY:
                    continue
                content = path.read_text(encoding="utf-8")
                if not content.strip():
                    continue
                result.append(
                    {
                        "working_dir": str(folder),
                        "project_dir_name": str(folder).replace("/", "-"),
                        "file_name": path.name,
                        "content": content,
                        "content_hash": hashlib.sha256(content.encode()).hexdigest(),
                        "file_mtime": int(path.stat().st_mtime),
                    }
                )
                if len(json.dumps(result).encode()) > MAX_BODY - 1024:
                    raise OperationError("记忆总量过大，请按工作目录分别回收")
        return result

    def deploy_memory(self, memories: list[dict], protocol_version: int = 1) -> int:
        targets = []
        for row in memories:
            name, content = str(row.get("file_name", "")), str(row.get("content", ""))
            if (
                Path(name).name != name
                or not name.endswith(".md")
                or len(content.encode()) > MAX_MEMORY
            ):
                raise OperationError("记忆文件名或大小不合法")
            directory = self.memory_dir(str(row.get("working_dir", "")))
            target = directory / name
            if target.is_symlink():
                raise OperationError("记忆目标不能是符号链接")
            deleted = row.get("deleted", False)
            mtime = row.get("file_mtime")
            original = target.stat().st_mtime_ns if target.exists() else None
            if protocol_version == 2:
                if (
                    not isinstance(deleted, bool)
                    or isinstance(mtime, bool)
                    or not isinstance(mtime, (int, float))
                    or not math.isfinite(mtime)
                    or not 0 < mtime <= time.time() + 300
                    or row.get("content_hash") != hashlib.sha256(content.encode()).hexdigest()
                ):
                    raise OperationError("记忆快照字段或校验值不合法")
                if target.exists():
                    if target.stat().st_size > MAX_MEMORY:
                        raise OperationError("目标记忆文件过大，不能覆盖")
                    if target.stat().st_mtime > mtime + 0.001 and (
                        deleted or target.read_text(encoding="utf-8") != content
                    ):
                        raise OperationError("目标记忆更新，请先回收核对，不能覆盖")
            targets.append((target, content, deleted, mtime, original))
        for target, content, deleted, mtime, original in targets:
            if protocol_version == 2 and (
                (target.stat().st_mtime_ns if target.exists() else None) != original
            ):
                raise OperationError("部署期间目标记忆已变更，请重新回收")
            if protocol_version == 2 and deleted:
                target.unlink(missing_ok=True)
                continue
            atomic_write(target, content)
            if protocol_version == 2:
                os.utime(target, (mtime, mtime))
        return len(targets)

    def prepare_workspace(self, directory: Path) -> None:
        for relative in ("CLAUDE.md", "AGENTS.md", ".git/info/exclude"):
            if not (directory / relative).resolve().is_relative_to(directory):
                raise OperationError("仓库文件链接指向工作区外")
        if (directory / "CLAUDE.md").is_file() and not (directory / "AGENTS.md").exists():
            atomic_write(directory / "AGENTS.md", (directory / "CLAUDE.md").read_text())
        for name, content in OUTPUT_STYLES.items():
            destination = directory / ".claude/output-styles" / name
            if not destination.resolve().is_relative_to(directory):
                raise OperationError("输出样式目录不在工作区内")
            atomic_write(destination, content)
        from .workspace import remove_legacy_instruction_excludes

        remove_legacy_instruction_excludes(directory)
        exclude = directory / ".git/info/exclude"
        if (directory / ".git").is_dir():
            previous = exclude.read_text() if exclude.exists() else ""
            additions = [
                name for name in ("/.claude/output-styles/",) if name not in previous.splitlines()
            ]
            if additions:
                atomic_write(exclude, previous.rstrip() + "\n" + "\n".join(additions) + "\n")

    def pull(self, data: dict) -> dict:
        url, _ = self.git_source(str(data.get("git_url", "")))
        name = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        path = self.workspace(str(data.get("working_dir") or name))
        path.parent.mkdir(parents=True, exist_ok=True)
        if (path / ".git").exists():
            remote = run_command(["git", "remote", "get-url", "origin"], path).strip()
            if remote != url:
                raise OperationError("工作目录的 origin 与请求来源不一致")
            if run_command(["git", "status", "--porcelain"], path).strip():
                raise OperationError("工作目录存在未提交改动，请先提交或另行处理")
            run_command(
                ["git", "-c", "protocol.file.allow=never", "pull", "--ff-only", "origin"],
                path,
                timeout=900,
            )
        else:
            if path.exists() and any(path.iterdir()):
                raise OperationError("目标目录非空，不能克隆覆盖")
            run_command(
                ["git", "-c", "protocol.file.allow=never", "clone", "--", url, str(path)],
                timeout=900,
            )
        self.prepare_workspace(path)
        return {"success": True, "message": "仓库已更新", "working_dir": str(path)}

    def link_claude_skills(
        self, path: Path, skill: str | None, *, validate_only: bool = False
    ) -> None:
        canonical = path / ".agents/skills"
        aliases = path / ".claude/skills"
        for directory in (path / ".agents", canonical, path / ".claude", aliases):
            if not directory.resolve().is_relative_to(path):
                raise OperationError("技能目录链接指向工作区外")
            if directory.exists() and not directory.is_dir():
                raise OperationError("技能目录路径被文件占用")
            if directory.is_symlink() and not directory.exists():
                raise OperationError("技能目录链接已失效")
        sources = (
            [canonical / skill]
            if skill
            else sorted(canonical.iterdir())
            if canonical.is_dir()
            else []
        )
        if not sources and not validate_only:
            raise OperationError("安装后未找到 .agents/skills 技能目录")
        links = []
        for source in sources:
            if not source.resolve().is_relative_to(canonical.resolve()):
                raise OperationError("技能内容链接指向目录外")
            target = aliases / source.name
            if target.is_symlink():
                if target.resolve() != source.resolve():
                    raise OperationError("Claude 技能同名链接指向其他位置，请先处理冲突")
            elif target.exists() and target.resolve() != source.resolve():
                raise OperationError("Claude 技能同名目录或文件已存在，不能覆盖")
            if validate_only:
                continue
            manifest = source / "SKILL.md"
            if not source.is_dir() or not manifest.is_file():
                raise OperationError("安装后未找到技能 SKILL.md")
            if not manifest.resolve().is_relative_to(canonical.resolve()):
                raise OperationError("技能内容链接指向目录外")
            if not target.exists():
                links.append((source, target))
        if not validate_only:
            aliases.mkdir(parents=True, exist_ok=True)
            for source, target in links:
                target.symlink_to(os.path.relpath(source, aliases), target_is_directory=True)

    def install_skill(self, data: dict) -> dict:
        path = self.workspace(str(data.get("project_dir", "")))
        if not path.is_dir():
            raise OperationError("工作目录不存在")
        if data.get("install_type") == "mcp":
            name, config = data.get("skill_name"), data.get("mcp_config")
            if (
                not isinstance(name, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", name)
                or not isinstance(config, dict)
            ):
                raise OperationError("MCP 配置不合法")
            target = path / ".mcp.json"
            if target.is_symlink():
                raise OperationError("MCP 配置不能是符号链接")
            current = json.loads(target.read_text()) if target.exists() else {}
            current.setdefault("mcpServers", {})[name] = config
            atomic_write(target, json.dumps(current, ensure_ascii=False, indent=2))
        else:
            # 技能来源由技能管理员在 CoreMan 目录登记，不再受节点 Git 主机白名单限制；
            # 白名单只约束机器人工作区的拉取、推送与备份。
            url, skill = self.git_source(str(data.get("git_url", "")), any_host=True)
            requested_skill = data.get("skill_name")
            if requested_skill is not None:
                if not isinstance(requested_skill, str) or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", requested_skill
                ):
                    raise OperationError("Skill 名称不合法")
                if skill and skill != requested_skill:
                    raise OperationError("Skill 名称不匹配")
                skill = requested_skill
            self.link_claude_skills(path, skill, validate_only=True)
            command = ["npx", "--yes", "skills", "add", url]
            if skill:
                command += ["--skill", skill]
            access_token = data.get("git_access_token")
            if access_token:
                if (
                    not isinstance(access_token, str)
                    or len(access_token) > 2000
                    or any(c.isspace() or ord(c) < 32 for c in access_token)
                    or not url.startswith("https://")
                ):
                    raise OperationError("Git token 或 HTTPS 仓库地址无效")
                env = {
                    key: value
                    for key, value in os.environ.items()
                    if not key.startswith(("COREMAN_", "GIT_")) and "proxy" not in key.lower()
                }
                env.update(
                    GIT_CONFIG_NOSYSTEM="1",
                    GIT_CONFIG_GLOBAL=os.devnull,
                    GIT_TERMINAL_PROMPT="0",
                    GIT_LFS_SKIP_SMUDGE="1",
                )
                credential = base64.b64encode(f"oauth2:{access_token}".encode()).decode()
                config = {
                    "credential.helper": "",
                    "http.followRedirects": "false",
                    "http.sslVerify": "true",
                    "core.hooksPath": os.devnull,
                    "protocol.file.allow": "never",
                    "protocol.ext.allow": "never",
                    f"http.{url.rstrip('/')}.extraHeader": f"Authorization: Basic {credential}",
                }
                env["GIT_CONFIG_COUNT"] = str(len(config))
                for i, (key, value) in enumerate(config.items()):
                    env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = key, value
                # The installer sees a local checkout, never the repository token.
                with tempfile.TemporaryDirectory(prefix="coreman-skill-") as directory:
                    checkout = str(Path(directory) / "repo")
                    run_command(
                        ["git", "clone", "--depth", "1", "--template=", "--", url, checkout],
                        path,
                        timeout=180,
                        env_override=env,
                    )
                    command[4] = checkout
                    run_command(command + ["-y"], path, timeout=300)
            else:
                run_command(command + ["-y"], path, timeout=300)
            self.link_claude_skills(path, skill)
        return {"success": True, "message": "Skill 已安装"}

    def start_background(self, name: str, function) -> dict:
        with self.background_lock:
            if name in self.background:
                return {"success": True, "message": "同类任务已在运行"}
            self.background.add(name)

        def work():
            try:
                function()
            except Exception as exc:
                event = {
                    "event": "agent_operation_failed",
                    "operation": name,
                    "error": type(exc).__name__,
                }
                if isinstance(exc, OperationError):
                    # 本模块的 OperationError 都是固定提示（最多带退出码或 HTTP 状态码），
                    # 不含命令输出与凭证；其它异常可能带路径或响应内容，只记类型。
                    event["message"] = operation_message(exc)
                print(json.dumps(event, ensure_ascii=False), flush=True)
            finally:
                with self.background_lock:
                    self.background.discard(name)

        threading.Thread(target=work, daemon=True, name=name).start()
        return {"success": True, "message": "任务已启动"}

    def health_check(self) -> None:
        """探测 AI 接口并上报；由运行时守护进程经本机 socket 实现。"""
        raise OperationError("当前环境不支持健康检查")

    def claude_probe_dir(self) -> Path:
        return self.home / ".cache/claude_rate_limits"

    def rate_limit_probe_enabled(self) -> bool:
        # Codex 额度经 app-server 随时可查；Claude 需要安装时显式启用 statusLine 探针。
        return self.provider == "codex" or (self.claude_probe_dir() / "probe.sh").is_file()

    def probe_rate_limits(self) -> None:
        if self.provider == "codex":
            limits = self.codex_limits()
        else:
            cache = self.claude_probe_dir()
            target = cache / "rate_limits.json"
            script = cache / "probe.sh"
            if not script.is_file():
                raise OperationError(CLAUDE_PROBE_DISABLED)
            before = target.stat().st_mtime_ns if target.exists() else 0
            try:
                run_command(["/bin/bash", str(script)], timeout=100)
            except OperationError:
                pass  # 即使 CLI 退出超时，已刷新过的 statusLine 数据仍可使用。
            if not target.exists() or target.stat().st_mtime_ns <= before:
                raise OperationError("额度探测没有刷新数据")
            limits = json.loads(target.read_text()).get("rate_limits", {})
        if not isinstance(limits, dict):
            raise OperationError("额度数据格式不正确")
        self.report("relay/rate-limits", {"rate_limits": limits})

    def codex_limits(self) -> dict:
        proc = subprocess.Popen(
            ["codex", "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        result = None
        try:
            messages = [
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {"name": "coreman-agent", "version": "1"},
                        "capabilities": {},
                    },
                },
                {"method": "initialized", "params": {}},
                {"id": 3, "method": "account/rateLimits/read", "params": {}},
            ]
            for message in messages:
                proc.stdin.write((json.dumps(message) + "\n").encode())
            proc.stdin.flush()
            deadline, buf = time.monotonic() + 15, b""
            while time.monotonic() < deadline and result is None:
                ready, _, _ = select.select(
                    [proc.stdout], [], [], max(0, deadline - time.monotonic())
                )
                if not ready:
                    break
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_BODY:
                    raise OperationError("额度响应过大")
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        message = json.loads(line)
                    except ValueError:
                        continue
                    if message.get("id") == 3:
                        result = message.get("result")
                        break
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=3)
            proc.stdin.close()
            proc.stdout.close()
        if not isinstance(result, dict):
            raise OperationError("Codex 未返回额度数据")
        buckets = result.get("rateLimitsByLimitId") or {}
        data = buckets.get("codex") or result.get("rateLimits") or {}
        out = {}
        for key in ("primary", "secondary"):
            window = data.get(key) or {}
            duration = window.get("windowDurationMins")
            name = {300: "five_hour", 10080: "seven_day"}.get(duration)
            if name and window.get("usedPercent") is not None:
                out[name] = {
                    "used_percentage": window["usedPercent"],
                    "resets_at": window.get("resetsAt"),
                }
        if not out:
            raise OperationError("Codex 额度窗口缺失")
        return out

    def install_claude_probe(self) -> None:
        """在原 statusLine 前串一层额度采集，原状态栏照常显示。

        守护进程每次启动都会调用，必须幂等：已接管时沿用记录的原命令，不会把采集脚本包进自己，
        也不改用户之后调整的 padding 等字段。settings 损坏或 statusLine 无法识别时停止，不改动设置。
        """
        cache = self.claude_probe_dir()
        config = self.home / ".claude/settings.json"
        current = json.loads(config.read_text()) if config.exists() else {}
        if not isinstance(current, dict):
            raise ValueError("Claude settings.json 不是 JSON 对象")
        capture = cache / "capture.sh"
        original_file = cache / "statusline-original.json"
        backup = config.with_name("settings.before-coreman-probe.json")
        status = current.get("statusLine")
        if status is not None and status_line_command(status) is None:
            raise OperationError("无法识别现有 statusLine，未修改 Claude 设置")
        wrapped = status is not None and status_line_command(status) == str(capture)
        if not wrapped:
            original = status
        elif original_file.is_file():
            data = json.loads(original_file.read_text())
            original = data.get("statusLine") if isinstance(data, dict) else None
        else:
            # 旧版探针直接替换了 statusLine，原命令只留在首次启用前的备份里。
            original = saved_status_line(backup)
        command = status_line_command(original)
        if command is None or command == str(capture):
            original, command = None, None
        atomic_write(
            original_file, json.dumps({"statusLine": original}, ensure_ascii=False, indent=2)
        )
        atomic_write(capture, claude_capture_script(cache / "rate_limits.json", command))
        os.chmod(capture, 0o700)
        if config.exists() and not backup.exists():
            atomic_write(backup, config.read_text())
        if not wrapped:
            # 只把命令换成采集脚本，padding 等其它字段原样保留。
            current["statusLine"] = {**(status or {}), "type": "command", "command": str(capture)}
            atomic_write(config, json.dumps(current, ensure_ascii=False, indent=2))
        # CLI 交互模式触发 statusLine；不授予工具自动执行权限。
        pipeline = '(printf "hi\\n"; sleep 10; printf "/exit\\n") | claude'
        if platform.system() == "Darwin":
            command = "script -q /dev/null /bin/bash -c " + shlex.quote(pipeline)
        else:
            command = "script -qec " + shlex.quote(pipeline) + " /dev/null"
        atomic_write(cache / "probe.sh", "#!/bin/bash\n" + command + " >/dev/null 2>&1\n")

    def initialize_repo(self, data: dict) -> dict:
        url, _ = self.git_source(str(data.get("git_url", "")))
        remote_path = (
            url.split(":", 1)[1] if url.startswith("git@") else urlsplit(url).path.lstrip("/")
        )
        repo = remote_path.removesuffix(".git")
        try:
            run_command(["git", "ls-remote", "--", url], timeout=30)
        except OperationError:
            if (
                url.split(":", 1)[0].removeprefix("git@")
                if url.startswith("git@")
                else urlsplit(url).hostname
            ) == "github.com":
                run_command(["gh", "repo", "create", repo, "--private"], timeout=60)
            else:
                group, _, name = repo.rpartition("/")
                if not group or not name:
                    raise OperationError("初始化需要 Git 命名空间") from None
                run_command(
                    [
                        "glab",
                        "repo",
                        "create",
                        name,
                        "--group",
                        group,
                        "--private",
                        "--defaultBranch",
                        "main",
                    ],
                    timeout=60,
                )
        result = self.pull(data)
        directory = self.workspace(result["working_dir"])
        content = str(data.get("content", ""))
        if len(content.encode()) > MAX_MEMORY:
            raise OperationError("初始化内容过大")
        if content:
            if (directory / "CLAUDE.md").exists() or (directory / "AGENTS.md").exists():
                raise OperationError("CLAUDE.md 已存在，不能初始化覆盖")
            atomic_write(directory / "CLAUDE.md", content)
            atomic_write(directory / "AGENTS.md", content)
            run_command(["git", "add", "-f", "--", "CLAUDE.md", "AGENTS.md"], directory)
            run_command(["git", "commit", "-m", "初始化机器人工作说明"], directory)
            run_command(["git", "push", "-u", "origin", "HEAD"], directory)
        return {"success": True, "message": "仓库已初始化"}

    def create_pr(self, data: dict) -> dict:
        url, _ = self.git_source(str(data.get("git_url", "")))
        name = url.rsplit("/", 1)[-1].removesuffix(".git")
        directory = self.workspace(str(data.get("working_dir") or name))
        if run_command(["git", "remote", "get-url", "origin"], directory).strip() != url:
            raise OperationError("工作目录与 Git 来源不一致")
        title = str(data.get("title") or "更新机器人工作区")
        if len(title) > 200:
            raise OperationError("标题过长")
        run_command(["git", "fetch", "origin"], directory)
        base = (
            run_command(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], directory)
            .strip()
            .removeprefix("refs/remotes/origin/")
        )
        branch = run_command(["git", "branch", "--show-current"], directory).strip()
        if branch in {"main", "master", base, ""}:
            branch = "coreman/update-" + str(time.time_ns())
            run_command(["git", "switch", "-c", branch], directory)
        files = data.get("files")
        if files:
            if not isinstance(files, list):
                raise OperationError("files 必须是文件列表")
            for file in files:
                target = (directory / str(file)).resolve()
                if not target.is_relative_to(directory) or any(
                    p in {".git", ".env"} for p in target.relative_to(directory).parts
                ):
                    raise OperationError("提交文件不在允许范围内")
            run_command(["git", "add", "--", *map(str, files)], directory)
        else:
            run_command(["git", "add", "-u"], directory)
        staged = run_command(["git", "diff", "--cached", "--name-only"], directory).splitlines()
        if any(Path(p).name == ".env" or Path(p).suffix in {".pem", ".key"} for p in staged):
            raise OperationError("暂存区含敏感配置，不创建 PR")
        if staged:
            run_command(["git", "commit", "-m", title], directory)
        if (
            run_command(["git", "rev-list", "--count", f"origin/{base}..HEAD"], directory).strip()
            == "0"
        ):
            raise OperationError("没有可提交的变更")
        run_command(["git", "push", "-u", "origin", branch], directory)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as body_file:
            body_file.write(str(data.get("body", "")))
            body_file.flush()
            if (
                url.split(":", 1)[0].removeprefix("git@")
                if url.startswith("git@")
                else urlsplit(url).hostname
            ) == "github.com":
                output = run_command(
                    [
                        "gh",
                        "pr",
                        "create",
                        "--base",
                        base,
                        "--head",
                        branch,
                        "--title",
                        title,
                        "--body-file",
                        body_file.name,
                    ],
                    directory,
                )
            else:
                output = run_command(
                    [
                        "glab",
                        "mr",
                        "create",
                        "--target-branch",
                        base,
                        "--source-branch",
                        branch,
                        "--title",
                        title,
                        "--description",
                        str(data.get("body", "")),
                        "--yes",
                    ],
                    directory,
                )
        urls = re.findall(r"https://[^\s]+", output)
        return {"success": True, "message": "PR 已创建", "url": urls[-1] if urls else None}

    def start_schedules(self) -> None:
        def loop():
            # 从负无穷起算：monotonic 是开机时长，刚开机的主机也要在启动后立即跑第一轮。
            last_quota, last_memory, last_health = -math.inf, -math.inf, ""
            while not self.stop.is_set():
                now = time.monotonic()
                local = time.localtime()
                slot = time.strftime("%Y-%m-%d-%H", local)
                if now - last_quota >= 1800:
                    # 未启用 Claude 探针时跳过，避免每 30 分钟记一条必然失败的日志。
                    if self.rate_limit_probe_enabled():
                        self.start_background("probe-rate-limits", self.probe_rate_limits)
                    last_quota = now
                if os.environ.get("COREMAN_MEMORY_SYNC", "1") == "1" and now - last_memory >= 3600:
                    self.start_background(
                        "collect-memory", lambda: self.dispatch({"type": "collect-memory"})
                    )
                    last_memory = now
                if 9 <= local.tm_hour <= 23 and slot != last_health:
                    self.start_background("health-check", self.health_check)
                    last_health = slot
                self.stop.wait(10)

        threading.Thread(target=loop, daemon=True, name="coreman-agent-schedules").start()

    def dispatch(self, data: dict) -> dict:
        cancelled = COMMAND_CANCEL.get()
        if cancelled is not None and cancelled.is_set():
            raise OperationError("操作已取消")
        kind = data.get("type")
        if kind == "ping":
            return {
                "success": True,
                "message": "pong",
                "memory_protocol": 2,
                "memory_read": True,
                "workspace_protocol": 1,
            }
        if kind == "status":
            tasks = self.active_tasks()
            return {
                "success": True,
                "hostname": socket.gethostname(),
                "user": self.home.name,
                "active_tasks": tasks,
                "processes": tasks,
                "active": bool(tasks),
                "count": len(tasks),
            }
        if kind == "probe-rate-limits":
            if not self.rate_limit_probe_enabled():
                return {"success": False, "message": CLAUDE_PROBE_DISABLED}
            return self.start_background(kind, self.probe_rate_limits)
        if kind == "health-check":
            return self.start_background(kind, self.health_check)
        if kind == "collect-memory":
            return self.report_memory(data.get("working_dir"))
        with self.lock:
            if cancelled is not None and cancelled.is_set():
                raise OperationError("操作已取消")
            if isinstance(kind, str) and kind.startswith("workspace-"):
                from .workspace import Workspace

                return Workspace(self, data).dispatch()
            if kind == "init":
                return self.initialize_repo(data)
            if kind == "pr":
                return self.create_pr(data)
            if kind == "pull":
                return self.pull(data)
            if kind == "install-skill":
                return self.install_skill(data)
            if kind in {"read-memory", "deploy-memory"} and data.get("bot_id") is not None:
                from .workspace import Workspace

                if kind == "read-memory":
                    Workspace(self, data)
                else:
                    for row in data.get("memories", []):
                        Workspace(self, {**data, "working_dir": row.get("working_dir")})
            if kind == "read-memory":
                if not isinstance(data.get("working_dir"), str) or not data["working_dir"]:
                    raise OperationError("快照必须指定工作目录")
                return {"success": True, "memories": self.collect_memory(data["working_dir"])}
            if kind == "deploy-memory":
                return {
                    "success": True,
                    "protocol_version": 2,
                    "count": self.deploy_memory(
                        data.get("memories", []), data.get("protocol_version", 1)
                    ),
                }
        raise OperationError("不支持的操作")
