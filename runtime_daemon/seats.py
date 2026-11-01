"""席位：同一节点上同一种 AI 登录的多个账号，管理员在管理台切换当前用哪个。

默认席位就是 CLI 原来的位置（`~/.claude` / `~/.codex`，或服务环境里给定的目录）；其它席位各占
一个配置目录，经 `CLAUDE_CONFIG_DIR` / `CODEX_HOME` 交给 CLI。凭据与账号状态各席位分开，会话、
插件、技能、设置等用软链指回默认席位，换席位后员工的会话照常续上。

当前席位写进守护进程自己的环境变量：之后启动的驱动、健康检查、技能安装、MCP 授权都用它。
登录由节点上的 CLI 完成（见 `SeatLogins`），令牌留在该席位目录里，不经过 CoreMan。
"""

from __future__ import annotations

import fcntl
import os
import re
import secrets
import select
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .agent import OperationError

DEFAULT = "default"
SEAT_ID = re.compile(r"(?:default|[a-f0-9]{12})")
ENV_KEY = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
# 指回默认席位的条目：（名字，是否目录）。目录先在默认席位建好，免得 CLI 在新席位里另建一份。
SHARED = {
    "claude": (
        ("projects", True),
        ("plugins", True),
        ("skills", True),
        ("agents", True),
        ("commands", True),
        ("hooks", True),
        ("settings.json", False),
        ("CLAUDE.md", False),
    ),
    "codex": (
        ("sessions", True),
        ("archived_sessions", True),
        ("prompts", True),
        ("skills", True),
        ("config.toml", False),
        ("AGENTS.md", False),
    ),
}
MAX_SEATS = 20
MAX_LABEL = 60


def clean_label(value: object) -> str:
    label = "".join(c for c in str(value or "") if c.isprintable()).strip()[:MAX_LABEL]
    if not label:
        raise OperationError("请填写席位名称")
    return label


class Seats:
    """席位清单在 config.json 的 seats 里：{provider: {"active": id, "items": {id: {"label"}}}}。"""

    def __init__(self, data_dir: Path, config: dict, save, home: Path | None = None) -> None:
        self.data_dir, self.config, self.save = data_dir, config, save
        self.home = home or Path.home()
        # 服务环境原来给的目录就是默认席位，切回默认时还原它。
        self.base = {key: os.environ.get(key) for key in ENV_KEY.values()}
        self.lock = threading.Lock()

    def state(self, provider: str) -> dict:
        seats = self.config.setdefault("seats", {})
        return seats.setdefault(provider, {"active": DEFAULT, "items": {}})

    def default_dir(self, provider: str) -> Path:
        base = self.base[ENV_KEY[provider]]
        if base:
            return Path(base)
        return self.home / (".claude" if provider == "claude" else ".codex")

    def directory(self, provider: str, seat_id: str) -> Path | None:
        """席位的配置目录；默认席位返回 None（CLI 用原来的位置）。"""
        if seat_id == DEFAULT:
            return None
        return self.data_dir / "seats" / provider / seat_id

    def ids(self, provider: str) -> list[str]:
        return [DEFAULT, *self.state(provider)["items"]]

    def label(self, provider: str, seat_id: str) -> str:
        return str(self.state(provider)["items"].get(seat_id, {}).get("label", ""))

    def active(self, provider: str) -> str:
        seat = str(self.state(provider).get("active") or DEFAULT)
        return seat if seat in self.ids(provider) else DEFAULT

    def require(self, provider: str, seat_id: object) -> str:
        if not isinstance(seat_id, str) or not SEAT_ID.fullmatch(seat_id):
            raise OperationError("席位标识无效")
        if seat_id not in self.ids(provider):
            raise OperationError("席位不存在")
        return seat_id

    def env(self, provider: str, seat_id: str) -> dict[str, str]:
        """以该席位运行 CLI 的环境（守护进程环境去掉 COREMAN_ 变量）。"""
        env = {k: v for k, v in os.environ.items() if not k.startswith("COREMAN_")}
        key, directory = ENV_KEY[provider], self.directory(provider, seat_id)
        value = str(directory) if directory is not None else self.base[key]
        if value:
            env[key] = value
        else:
            env.pop(key, None)
        return env

    def apply_env(self, provider: str) -> None:
        """把当前席位写进守护进程环境；之后启动的 CLI 与驱动都继承它。"""
        key = ENV_KEY[provider]
        value = self.env(provider, self.active(provider)).get(key)
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)

    def link_shared(self, provider: str, directory: Path) -> None:
        source = self.default_dir(provider)
        source.mkdir(parents=True, exist_ok=True)
        for name, is_dir in SHARED[provider]:
            target, link = source / name, directory / name
            if is_dir:
                target.mkdir(exist_ok=True)
            elif not target.exists():
                continue
            if link.is_symlink() or link.exists():
                continue
            link.symlink_to(target)

    def create(self, provider: str, label: object) -> str:
        name = clean_label(label)
        with self.lock:
            items = self.state(provider)["items"]
            if len(items) >= MAX_SEATS:
                raise OperationError("席位数量已达上限")
            seat_id = secrets.token_hex(6)
            directory = self.directory(provider, seat_id)
            assert directory is not None
            directory.mkdir(parents=True, mode=0o700)
            self.link_shared(provider, directory)
            items[seat_id] = {"label": name}
            self.save()
        return seat_id

    def activate(self, provider: str, seat_id: str) -> None:
        with self.lock:
            directory = self.directory(provider, seat_id)
            if directory is not None:
                # 默认席位后来才有的设置文件，这时补上软链。
                self.link_shared(provider, directory)
            self.state(provider)["active"] = seat_id
            self.save()
            self.apply_env(provider)

    def remove(self, provider: str, seat_id: str) -> None:
        if seat_id == DEFAULT:
            raise OperationError("默认席位不能移除")
        with self.lock:
            if seat_id == self.active(provider):
                raise OperationError("当前席位不能移除，请先切换到其它席位")
            directory = self.directory(provider, seat_id)
            assert directory is not None
            command = ["claude", "auth", "logout"] if provider == "claude" else ["codex", "logout"]
            try:
                subprocess.run(
                    command,
                    env=self.env(provider, seat_id),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=30,
                    start_new_session=True,
                )
            except (OSError, subprocess.SubprocessError):
                pass  # 删掉目录令牌也就没了；远端会话随令牌过期
            # 软链指向默认席位的共用内容：只删链接本身，不进去删。
            for entry in directory.iterdir() if directory.is_dir() else ():
                if entry.is_symlink():
                    entry.unlink()
            shutil.rmtree(directory, ignore_errors=True)
            self.state(provider)["items"].pop(seat_id, None)
            self.save()


SESSION_TTL = 900  # Codex 的设备码 15 分钟过期
START_TIMEOUT = 30
COMPLETE_TIMEOUT = 60
MAX_SESSIONS = 10
MAX_OUTPUT = 64 * 1024
ESCAPES = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[ -/]*[@-~]")
CLAUDE_URL = re.compile(r"https://\S+/oauth/authorize\?\S+")
CODEX_URL = re.compile(r"https://\S+/device\b\S*")
CODEX_CODE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4,6}\b")
CODE = re.compile(r"[A-Za-z0-9_\-.~#=+/]{8,2000}")


@dataclass
class LoginSession:
    proc: subprocess.Popen
    master: int
    provider: str
    seat_id: str
    created: float = field(default_factory=time.monotonic)
    output: bytearray = field(default_factory=bytearray)


class SeatLogins:
    """在 pty 里跑 CLI 的登录命令，把授权链接交给管理台，再把认证码送回 CLI。

    Claude：浏览器授权后页面给出认证码，贴回来写进 CLI 的输入；Codex：设备码登录，浏览器里输入
    一次性代码即完成，这边只等 CLI 退出。
    """

    def __init__(self) -> None:
        self.sessions: dict[str, LoginSession] = {}
        self.lock = threading.Lock()

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

    def reap(self) -> None:
        now = time.monotonic()
        for key, session in list(self.sessions.items()):
            if session.proc.poll() is not None or now - session.created > SESSION_TTL:
                self.close(key)

    def expire(self, key: str, session: LoginSession) -> None:
        with self.lock:
            if self.sessions.get(key) is session:
                self.close(key)

    def read(self, session: LoginSession, deadline: float, *until: re.Pattern) -> str:
        """读 CLI 输出直到每个 until 都出现、进程退出或超时；返回去掉控制序列的文本。"""
        while time.monotonic() < deadline:
            text = ESCAPES.sub("", session.output.decode(errors="replace"))
            if until and all(p.search(text) for p in until):
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

    def start(self, provider: str, seat_id: str, env: dict[str, str], cwd: Path) -> dict:
        with self.lock:
            self.reap()
            for key, session in list(self.sessions.items()):
                if session.provider == provider and session.seat_id == seat_id:
                    self.close(key)  # 同一席位重新开始登录，旧的作废
            if len(self.sessions) >= MAX_SESSIONS:
                raise OperationError("进行中的登录过多，请稍后再试")
        command = (
            ["claude", "auth", "login"]
            if provider == "claude"
            else ["codex", "login", "--device-auth"]
        )
        # 链接很长，终端放宽，免得折行；BROWSER 设成 true，CLI 不在节点上尝试打开浏览器。
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 4000, 0, 0))
        try:
            proc = subprocess.Popen(
                command,
                cwd=cwd,
                env={**env, "TERM": "dumb", "NO_COLOR": "1", "BROWSER": "true"},
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
            )
        except OSError:
            os.close(master)
            raise OperationError("无法启动 CLI 登录") from None
        finally:
            os.close(slave)
        session = LoginSession(proc, master, provider, seat_id)
        key = secrets.token_urlsafe(24)
        with self.lock:
            self.sessions[key] = session
        timer = threading.Timer(SESSION_TTL, self.expire, args=(key, session))
        timer.daemon = True
        timer.start()
        patterns = (CLAUDE_URL,) if provider == "claude" else (CODEX_URL, CODEX_CODE)
        text = self.read(session, time.monotonic() + START_TIMEOUT, *patterns)
        url = patterns[0].search(text)
        code = CODEX_CODE.search(text) if provider == "codex" else None
        if url is None or (provider == "codex" and code is None):
            with self.lock:
                self.close(key)
            raise OperationError("CLI 没有给出登录链接，请确认 CLI 版本后重试")
        return {
            "success": True,
            "session_id": key,
            "authorization_url": url.group(0),
            "user_code": code.group(0) if code else "",
            "needs_code": provider == "claude",
            "expires_in": SESSION_TTL,
        }

    def complete(self, key: str, provider: str, seat_id: str, code: object) -> dict:
        with self.lock:
            self.reap()
            session = self.sessions.get(key)
        if session is None or session.provider != provider or session.seat_id != seat_id:
            raise OperationError("登录已过期，请重新开始")
        if provider == "claude":
            text = str(code or "").strip()
            if not CODE.fullmatch(text):
                raise OperationError("认证码格式不对，请复制浏览器页面上的完整认证码")
            os.write(session.master, (text + "\r").encode())
            deadline = time.monotonic() + COMPLETE_TIMEOUT
        else:
            # 设备码：浏览器里完成后 CLI 自己退出；还在等就说明没完成，会话留着可以再点。
            deadline = time.monotonic() + 5
        self.read(session, deadline)
        try:
            status = session.proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            status = None
        if status is None and provider == "codex":
            raise OperationError("还没有在浏览器里完成授权，完成后再确认")
        with self.lock:
            self.close(key)
        if status != 0:
            raise OperationError("CLI 未能完成登录，请重新开始")
        return {"success": True, "message": "登录完成"}
