"""MCP 技能的 OAuth 登录：由本实例的 Codex / Claude Code 完成授权，令牌留在 CLI 自己的凭据存储。

Figma 等服务只接受登记过的 MCP 客户端，CoreMan 不能自己当 OAuth 客户端。两个 CLI 的
`mcp login --no-browser` 都会打印授权链接，并在本机回环端口等回调：这里启动登录、取回链接，
管理台把用户浏览器停在的回调地址交回来后，原样送到那个回环端口，CLI 自己换取并保存令牌。
令牌按服务名区分，安装时每个 AI 员工用自己的服务名，授权互不共用。
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import select
import signal
import struct
import subprocess
import termios
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests

from .agent import OperationError

SESSION_TTL = 600
START_TIMEOUT = 30
COMPLETE_TIMEOUT = 60
MAX_SESSIONS = 20
MAX_OUTPUT = 64 * 1024
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")
# 终端控制序列：OSC（如 Claude 的超链接 ESC ] 8 ;; URL ESC \）与 CSI。
ESCAPES = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[ -/]*[@-~]")
AUTH_URL = re.compile(r"https://[^\s\x1b\x07]+redirect_uri=[^\s\x1b\x07]+")


@dataclass
class Session:
    proc: subprocess.Popen
    master: int
    name: str
    project: Path
    redirect: str
    state: str
    created: float = field(default_factory=time.monotonic)
    output: bytearray = field(default_factory=bytearray)


def toml_string(value: str) -> str:
    return json.dumps(value)


def cli_name(provider: str, name: str) -> str:
    # Codex 驱动以 skill_<名字> 挂载 .mcp.json 里的服务（见 relay-codex/project_mcp.go）。
    return f"skill_{name}" if provider == "codex" else name


def server_config(project: Path, name: str) -> dict:
    target = project / ".mcp.json"
    if target.is_symlink() or not target.is_file():
        raise OperationError("工作目录里没有这个 MCP 服务，请先安装技能")
    try:
        servers = json.loads(target.read_text()).get("mcpServers", {})
    except (OSError, ValueError, AttributeError):
        raise OperationError("工作目录的 .mcp.json 无法解析") from None
    config = servers.get(name) if isinstance(servers, dict) else None
    if not isinstance(config, dict):
        raise OperationError("工作目录里没有这个 MCP 服务，请先安装技能")
    url = urlsplit(str(config.get("url", "")))
    if config.get("type") not in ("http", "sse") or url.scheme != "https" or not url.hostname:
        raise OperationError("只有 HTTPS 远程 MCP 服务需要授权")
    return config


class MCPLogins:
    def __init__(self, provider: str, env: dict[str, str]) -> None:
        self.provider, self.env = provider, env
        self.sessions: dict[str, Session] = {}
        self.lock = threading.Lock()

    def command(self, action: str, name: str, config: dict, *extra: str) -> list[str]:
        if self.provider == "codex":
            server = cli_name("codex", name)
            return [
                "codex",
                "mcp",
                action,
                server,
                *extra,
                "-c",
                f"mcp_servers.{server}.url={toml_string(config['url'])}",
            ]
        return ["claude", "mcp", action, name, *extra]

    def run(self, command: list[str], project: Path, timeout: int = 30) -> int:
        try:
            return subprocess.run(
                command,
                cwd=project,
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                start_new_session=True,
            ).returncode
        except (OSError, subprocess.SubprocessError):
            return -1

    def register_claude(self, project: Path, name: str, config: dict) -> None:
        # claude mcp login 不接受未经交互批准的 .mcp.json 服务；同名、同配置登记到本项目的
        # local 作用域即可登录，对话里 local 优先于 .mcp.json，配置相同，令牌也对得上。
        self.run(["claude", "mcp", "remove", name, "-s", "local"], project)
        if self.run(
            ["claude", "mcp", "add-json", "-s", "local", name, json.dumps(config)], project
        ):
            raise OperationError("无法在 Claude Code 中登记该 MCP 服务")

    def expire(self, key: str, session: Session) -> None:
        with self.lock:
            if self.sessions.get(key) is session:
                self.close(key)

    def reap(self) -> None:
        now = time.monotonic()
        for key, session in list(self.sessions.items()):
            if session.proc.poll() is not None or now - session.created > SESSION_TTL:
                self.close(key)

    def close(self, key: str) -> None:
        session = self.sessions.pop(key, None)
        if session is None:
            return
        if session.proc.poll() is None:
            try:
                os.killpg(session.proc.pid, signal.SIGTERM)
                session.proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(session.proc.pid, signal.SIGKILL)
                except OSError:
                    pass
        try:
            os.close(session.master)
        except OSError:
            pass

    def read(self, session: Session, deadline: float, until: re.Pattern | None) -> str:
        """读 CLI 输出直到出现 until、进程退出或超时；返回去掉控制序列的文本。"""
        while time.monotonic() < deadline:
            text = ESCAPES.sub("", session.output.decode(errors="replace"))
            if until is not None and until.search(text):
                return text
            ready, _, _ = select.select([session.master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(session.master, 4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                if len(session.output) < MAX_OUTPUT:
                    session.output += chunk
            elif session.proc.poll() is not None:
                break
        return ESCAPES.sub("", session.output.decode(errors="replace"))

    def start(self, project: Path, name: str) -> dict:
        if not NAME.fullmatch(name):
            raise OperationError("MCP 服务名不合法")
        config = server_config(project, name)
        if self.provider == "claude":
            self.register_claude(project, name, config)
        with self.lock:
            self.reap()
            if len(self.sessions) >= MAX_SESSIONS:
                raise OperationError("进行中的授权过多，请稍后再试")
        # CLI 要在终端里读回调地址（Claude 不接受非终端输入），用 pty 启动；宽度放大，链接不折行。
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 4000, 0, 0))
        try:
            proc = subprocess.Popen(
                self.command("login", name, config, "--no-browser"),
                cwd=project,
                env={**self.env, "TERM": "dumb", "NO_COLOR": "1"},
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
            )
        except OSError:
            os.close(master)
            raise OperationError("无法启动 CLI 的 MCP 登录") from None
        finally:
            os.close(slave)
        session = Session(proc, master, name, project, redirect="", state="")
        key = secrets.token_urlsafe(24)
        with self.lock:
            self.sessions[key] = session
        # 用户不回来完成时，到期也要结束 CLI，不能只靠下一次调用顺带清理。
        timer = threading.Timer(SESSION_TTL, self.expire, args=(key, session))
        timer.daemon = True
        timer.start()
        text = self.read(session, time.monotonic() + START_TIMEOUT, AUTH_URL)
        match = AUTH_URL.search(text)
        if match is None:
            with self.lock:
                self.close(key)
            raise OperationError("CLI 没有给出授权链接，该服务可能不支持 OAuth")
        authorization_url = match.group(0)
        query = parse_qs(urlsplit(authorization_url).query)
        redirect = urlsplit((query.get("redirect_uri") or [""])[0])
        if (
            redirect.scheme != "http"
            or redirect.hostname not in LOOPBACK
            or not redirect.port
            or not query.get("state")
        ):
            with self.lock:
                self.close(key)
            raise OperationError("CLI 给出的回调地址不是本机地址")
        session.redirect, session.state = redirect.geturl(), query["state"][0]
        return {
            "success": True,
            "session_id": key,
            "authorization_url": authorization_url,
            "expires_in": SESSION_TTL,
        }

    def complete(self, key: str, project: Path, name: str, callback_url: str) -> dict:
        with self.lock:
            self.reap()
            session = self.sessions.get(key)
        if session is None or session.name != name or session.project != project:
            raise OperationError("授权已过期，请重新开始")
        expected, given = urlsplit(session.redirect), urlsplit(callback_url.strip())
        query = parse_qs(given.query)
        if (
            given.scheme != "http"
            or given.hostname not in LOOPBACK
            or given.port != expected.port
            or given.path != expected.path
        ):
            raise OperationError("粘贴的不是本次授权的回调地址，请复制浏览器跳转后的完整地址")
        if (query.get("state") or [""])[0] != session.state:
            raise OperationError("回调地址与本次授权不匹配，请重新开始")
        if query.get("error"):
            with self.lock:
                self.close(key)
            raise OperationError("授权被拒绝或取消")
        if not query.get("code"):
            raise OperationError("回调地址里没有授权码")
        # 只把查询串送回 CLI 的回环端口；主机和端口取自 CLI 自己给的回调地址。
        target = f"http://127.0.0.1:{expected.port}{expected.path}?{given.query}"
        http = requests.Session()
        http.trust_env = False
        try:
            http.get(target, timeout=COMPLETE_TIMEOUT, allow_redirects=False)
        except requests.RequestException:
            pass  # 有的 CLI 收到回调就关闭端口，结果以进程退出码为准
        deadline = time.monotonic() + COMPLETE_TIMEOUT
        self.read(session, deadline, None)
        try:
            code = session.proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            code = None
        with self.lock:
            self.close(key)
        if code != 0:
            raise OperationError("CLI 未能完成授权，请重新开始")
        return {"success": True, "message": "授权完成"}

    def logout(self, project: Path, name: str) -> dict:
        if not NAME.fullmatch(name):
            raise OperationError("MCP 服务名不合法")
        config = server_config(project, name)
        if self.provider == "claude":
            failed = self.run(self.command("logout", name, config), project)
        else:
            # 没有系统钥匙串的 Linux 上，Codex 默认模式删除令牌会失败；按文件存储再删一次。
            failed = self.run(self.command("logout", name, config), project) and self.run(
                self.command("logout", name, config, "-c", 'mcp_oauth_credentials_store="file"'),
                project,
            )
        if failed:
            raise OperationError("CLI 未能撤销授权")
        return {"success": True, "message": "已撤销授权"}
