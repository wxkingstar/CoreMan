#!/usr/bin/env python3
"""CoreMan relay-agent，Python 3.10+ / requests；以实例用户运行。

必需：COREMAN_API_URL、COREMAN_AGENT_TOKEN、COREMAN_RELAY_ID。
可选：COREMAN_WORKSPACE_ROOT=/data/skills、COREMAN_RELAY_URL=http://127.0.0.1:50009、
COREMAN_MODEL_PROVIDER=claude、COREMAN_MODEL、COREMAN_GIT_HOSTS、COREMAN_AGENT_PORT=52123。
"""

from __future__ import annotations

import argparse
import contextvars
import hashlib
import hmac
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


class OperationError(Exception):
    pass


def run_command(
    command: list[str], cwd: Path | None = None, timeout: int = 300,
    *, env_override: dict[str, str] | None = None,
) -> str:
    """不通过 shell；超时终止整个进程组，不把命令输出（可能有凭证）放进异常。"""
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            # 第三方安装器与探针不需要 Agent 的服务端身份或服务端配置。
            env=env_override if env_override is not None else {
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


class Agent:
    def __init__(
        self,
        *,
        root: Path,
        api_url: str,
        token: str,
        relay_id: str,
        relay_url: str = "http://127.0.0.1:50009",
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
        self.relay_url, self.provider, self.model = relay_url.rstrip("/"), provider, model
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

    def git_source(self, raw: str) -> tuple[str, str | None]:
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
        if host not in self.git_hosts:
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
        tasks = []
        proc_root = Path("/proc")
        if not proc_root.is_dir():
            return tasks
        for entry in proc_root.iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                if entry.stat().st_uid != os.getuid():
                    continue
                cmd = (entry / "comm").read_text().strip()
                if not any(name in cmd.lower() for name in ("claude", "codex", "node")):
                    continue
                env = {}
                for field in (entry / "environ").read_bytes().split(b"\0"):
                    key, _, value = field.partition(b"=")
                    env[key.decode(errors="replace")] = value.decode(errors="replace")
                bot = env.get("COREMAN_BOT_KEY") or env.get("BOT_KEY")
                if not bot:
                    continue
                tasks.append(
                    {
                        "pid": int(entry.name),
                        "bot_key": bot,
                        "user": env.get("COREMAN_USER_LOGIN")
                        or env.get("BOT_USER_LOGIN", ""),
                        "chat_id": env.get("COREMAN_CHAT_ID") or env.get("AGENT_CHAT_ID", ""),
                        "chat_type": env.get("COREMAN_CHAT_TYPE") or env.get("AGENT_CHAT_TYPE", ""),
                    }
                )
            except (OSError, ValueError):
                continue
        return tasks

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
        exclude = directory / ".git/info/exclude"
        if (directory / ".git").is_dir():
            previous = exclude.read_text() if exclude.exists() else ""
            additions = [
                name
                for name in ("/.claude/output-styles/", "/AGENTS.md")
                if name not in previous.splitlines()
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
            url, skill = self.git_source(str(data.get("git_url", "")))
            requested_skill = data.get("skill_name")
            if requested_skill is not None:
                if not isinstance(requested_skill, str) or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", requested_skill
                ):
                    raise OperationError("Skill 名称不合法")
                if skill and skill != requested_skill:
                    raise OperationError("Skill 名称不匹配")
                skill = requested_skill
            command = ["npx", "--yes", "skills", "add", url]
            if skill:
                command += ["--skill", skill]
            run_command(command + ["-y"], path, timeout=300)
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
                print(
                    json.dumps(
                        {
                            "event": "agent_operation_failed",
                            "operation": name,
                            "error": type(exc).__name__,
                        }
                    ),
                    flush=True,
                )
            finally:
                with self.background_lock:
                    self.background.discard(name)

        threading.Thread(target=work, daemon=True, name=name).start()
        return {"success": True, "message": "任务已启动"}

    def health_check(self) -> None:
        start = time.monotonic()
        status = "healthy" if self.model else "unknown"
        try:
            result = self.http.get(self.relay_url + "/health", timeout=10, allow_redirects=False)
            if result.status_code != 200 or result.json().get("status") != "healthy":
                status = "down"
            elif self.model:
                result = self.http.post(
                    self.relay_url + "/v1/chat/completions",
                    json={
                        "model": self.model,
                        "stream": False,
                        "messages": [{"role": "user", "content": "hi"}],
                        "session_id": "",
                    },
                    timeout=90 if self.provider == "codex" else 60,
                    allow_redirects=False,
                )
                if result.status_code in (401, 403):
                    status = "auth_fail"
                elif result.status_code != 200:
                    status = "down"
        except requests.Timeout:
            status = "timeout"
        except (requests.RequestException, ValueError):
            status = "down"
        self.report(
            "relay/health", {"status": status, "latency_ms": int((time.monotonic() - start) * 1000)}
        )

    def probe_rate_limits(self) -> None:
        if self.provider == "codex":
            limits = self.codex_limits()
        else:
            cache = self.home / ".cache/claude_rate_limits"
            target = cache / "rate_limits.json"
            script = cache / "probe.sh"
            if not script.is_file():
                raise OperationError("尚未安装 Claude 额度探测，请运行 --install-claude-probe")
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
        # 仅在显式安装参数下修改 statusLine；保留其它全局设置，损坏 JSON 则停止。
        cache = self.home / ".cache/claude_rate_limits"
        config = self.home / ".claude/settings.json"
        current = json.loads(config.read_text()) if config.exists() else {}
        capture = cache / "capture.sh"
        atomic_write(
            capture,
            "#!/bin/sh\numask 077\ncat > "
            + shlex.quote(str(cache / "rate_limits.json"))
            + "\nprintf ok\n",
        )
        os.chmod(capture, 0o700)
        backup = config.with_name("settings.before-coreman-probe.json")
        if config.exists() and not backup.exists():
            atomic_write(backup, config.read_text())
        current["statusLine"] = {"type": "command", "command": str(capture)}
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

    def mail_probe(self, data: dict) -> dict:
        import email
        import imaplib
        from email.header import decode_header, make_header

        providers = {
            "gmail": "imap.gmail.com",
            "qq": "imap.qq.com",
            "wecom": "imap.exmail.qq.com",
            "exmail": "imap.exmail.qq.com",
        }
        host = providers.get(str(data.get("provider", "gmail")))
        if not host or not data.get("username") or not data.get("password"):
            raise OperationError("邮箱服务或凭证未配置")
        minutes = max(1, min(int(data.get("since_minutes", 20)), 1440))
        limit = max(1, min(int(data.get("max_scan", 50)), 100))
        cutoff, started = time.time() - minutes * 60, time.monotonic()
        matched, scanned = [], 0
        with imaplib.IMAP4_SSL(host, timeout=20) as conn:
            conn.login(str(data["username"]), str(data["password"]))
            if conn.select("INBOX", readonly=True)[0] != "OK":
                raise OperationError("无法只读打开收件箱")
            status, found = conn.uid(
                "SEARCH", None, "SINCE", time.strftime("%d-%b-%Y", time.gmtime(cutoff - 86400))
            )
            if status != "OK":
                raise OperationError("邮件查询失败")
            for uid in (found[0] or b"").split()[-limit:]:
                if time.monotonic() - started > 110:
                    raise OperationError("邮箱查询超时")
                status, response = conn.uid("FETCH", uid, "(INTERNALDATE BODY.PEEK[]<0.131072>)")
                if status != "OK" or not response or not isinstance(response[0], tuple):
                    continue
                date = imaplib.Internaldate2tuple(response[0][0])
                if date is None or time.mktime(date) < cutoff:
                    continue
                scanned += 1
                message = email.message_from_bytes(response[0][1])
                subject = str(make_header(decode_header(message.get("Subject", ""))))
                sender = str(make_header(decode_header(message.get("From", ""))))
                texts = []
                for part in message.walk():
                    if (
                        part.get_content_type() in {"text/plain", "text/html"}
                        and part.get_content_disposition() != "attachment"
                    ):
                        raw = part.get_payload(decode=True) or b""
                        texts.append(
                            raw.decode(part.get_content_charset() or "utf-8", errors="replace")
                        )
                text = "\n".join(texts)
                keywords = [str(k).casefold() for k in data.get("keywords", [])]
                if keywords and not any(
                    k in (subject + sender + text).casefold() for k in keywords
                ):
                    continue
                row = {
                    "uid": uid.decode(),
                    "subject": subject,
                    "from": sender,
                    "date": time.strftime("%Y-%m-%d %H:%M:%S", date),
                    "bulk": bool(message.get("List-Unsubscribe") or message.get("List-Id")),
                    "snippet": text[: max(0, min(int(data.get("body_chars", 400)), 4000))],
                }
                if data.get("include_links"):
                    row["links"] = list(dict.fromkeys(re.findall(r"https?://[^\s<>\"']+", text)))
                matched.append(row)
        return {"success": True, "scanned": scanned, "matched": matched, "window_minutes": minutes}

    def start_schedules(self) -> None:
        def loop():
            last_quota, last_memory, last_health = 0.0, 0.0, ""
            while not self.stop.is_set():
                now = time.monotonic()
                local = time.localtime()
                slot = time.strftime("%Y-%m-%d-%H", local)
                if now - last_quota >= 1800:
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
            return {"success": True, "message": "pong", "memory_protocol": 2, "memory_read": True}
        if kind in {"status", "check-active-tasks"}:
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
            return self.start_background(kind, self.probe_rate_limits)
        if kind == "mail-probe":
            return self.mail_probe(data)
        if kind == "health-check":
            return self.start_background(kind, self.health_check)
        if kind == "collect-memory":
            return self.report_memory(data.get("working_dir"))
        with self.lock:
            if cancelled is not None and cancelled.is_set():
                raise OperationError("操作已取消")
            if kind == "init":
                return self.initialize_repo(data)
            if kind == "pr":
                return self.create_pr(data)
            if kind == "pull":
                return self.pull(data)
            if kind == "install-skill":
                return self.install_skill(data)
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


class Handler(BaseHTTPRequestHandler):
    timeout = 30
    agent: Agent

    def log_message(self, format, *args) -> None:
        pass

    def do_POST(self) -> None:
        auth = self.headers.get("Authorization", "")
        if not hmac.compare_digest(auth.encode(), ("Bearer " + self.agent.token).encode()):
            self.respond(401, {"success": False, "message": "认证失败"})
            return
        if self.path != "/":
            self.respond(404, {"success": False, "message": "路径不存在"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                self.respond(413, {"success": False, "message": "请求体大小不合法"})
                return
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("object required")
            self.respond(200, self.agent.dispatch(data))
        except OperationError as exc:
            self.respond(409, {"success": False, "message": str(exc)})
        except (ValueError, TypeError):
            self.respond(400, {"success": False, "message": "请求格式不正确"})
        except Exception as exc:
            print(
                json.dumps({"event": "agent_request_failed", "error": type(exc).__name__}),
                flush=True,
            )
            self.respond(500, {"success": False, "message": "Agent 操作失败"})

    def respond(self, status: int, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True


def main() -> None:
    parser = argparse.ArgumentParser(description="CoreMan relay-agent")
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("COREMAN_AGENT_PORT", "52123"))
    )
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--install-claude-probe", action="store_true")
    parser.add_argument("--no-schedules", action="store_true")
    args = parser.parse_args()
    agent = Agent(
        root=Path(os.environ.get("COREMAN_WORKSPACE_ROOT", "/data/skills")),
        api_url=os.environ["COREMAN_API_URL"],
        token=os.environ["COREMAN_AGENT_TOKEN"],
        relay_id=os.environ["COREMAN_RELAY_ID"],
        relay_url=os.environ.get("COREMAN_RELAY_URL", "http://127.0.0.1:50009"),
        provider=os.environ.get("COREMAN_MODEL_PROVIDER", "claude"),
        model=os.environ.get("COREMAN_MODEL", ""),
        git_hosts=tuple(
            os.environ.get("COREMAN_GIT_HOSTS", "github.com").split(",")
        ),
    )
    if args.install_claude_probe:
        agent.install_claude_probe()
    if not args.no_schedules:
        agent.start_schedules()
    Handler.agent = agent
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        agent.stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
