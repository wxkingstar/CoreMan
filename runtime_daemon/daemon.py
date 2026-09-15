"""One user environment, two AI providers, no inbound network ports."""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import fcntl
import getpass
import hashlib
import hmac
import json
import logging
import os
import platform
import secrets
import shutil
import signal
import socket
import subprocess
import time
import uuid
from pathlib import Path

import httpx

from runtime_daemon import PROTOCOL_VERSION, __version__
from runtime_daemon.agent import COMMAND_CANCEL, Agent, OperationError, atomic_write
from runtime_daemon.lifecycle import (
    FatalConfigError,
    choose_socket_dir,
    ensure_private_dir,
    fatal_exit_status,
    record_fatal,
)
from runtime_daemon.logs import (
    LogRotator,
    appended_stderr_file,
    configure_logging,
    rotate_copytruncate,
)
from runtime_daemon.tls import build_trust_bundle, client_context

LOG = logging.getLogger("coreman-runtime")
CHUNK_SIZE = 48 * 1024
PROVIDERS = ("claude", "codex")
CONTROL_LEASE_SECONDS = 40.0
FRAME_RETRY_SECONDS = 25.0
POLL_WAIT_SECONDS = 20.0
LEGACY_POLL_INTERVAL = 0.5
HEARTBEAT_SECONDS = 10.0
STATE_WRITE_SECONDS = 10.0
MAX_BATCH_FRAMES = 32
# Disabled node or revoked token: back off instead of retrying every few seconds.
REJECTED_STATUSES = {401, 403}
REJECTED_BACKOFF_MAX = 60.0
# Discovery rounds in a row a live driver's socket may refuse connections before a restart.
SOCKET_FAILURE_LIMIT = 2
# Discovery rounds a broken driver is kept for its in-flight streams before a forced restart.
HEAL_DEFER_ROUNDS = 30
# Enrollment rejections worth retrying; any other 4xx will not change with the same token.
RETRYABLE_ENROLL_STATUSES = {408, 429}


def environment() -> str:
    container = os.environ.get("container", "")
    try:
        container = Path("/run/systemd/container").read_text().strip() or container
    except OSError:
        pass
    if container == "systemd-nspawn":
        return "nspawn"
    if platform.system() == "Linux":
        try:
            root, init_root = Path("/").stat(), Path("/proc/1/root").stat()
            if (root.st_dev, root.st_ino) != (init_root.st_dev, init_root.st_ino):
                return "chroot"
        except OSError:
            pass
    return "host"


def enrollment_rejection(response: httpx.Response) -> str:
    try:
        detail = str(response.json().get("message") or "")[:200]
    except (ValueError, AttributeError):
        detail = ""
    return (
        f"安装注册被 CoreMan 拒绝（HTTP {response.status_code}：{detail or '无详细信息'}）。"
        "重启服务无法解决：请在「运行时管理」重新生成安装链接，先执行 "
        "python -m runtime_daemon.install_service --uninstall --purge 清理本次安装，"
        "再运行新的安装命令。"
    )


def cli_status(provider: str) -> dict:
    path = shutil.which(provider)
    result = {
        "installed": bool(path),
        "version": "",
        "login": "unknown",
        "health": "unknown",
        "models": [],
        "detail": "",
    }
    if not path:
        result["detail"] = "未安装 CLI"
        return result
    try:
        version = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10)
        result["version"] = (version.stdout or version.stderr).strip()[:100]
        command = (
            [path, "auth", "status", "--json"]
            if provider == "claude"
            else [path, "login", "status"]
        )
        proc = subprocess.run(command, capture_output=True, text=True, timeout=15)
        if provider == "claude":
            data = json.loads(proc.stdout)
            result["login"] = "ready" if data.get("loggedIn") else "required"
        else:
            output = (proc.stdout + proc.stderr).lower()
            if proc.returncode == 0 and "logged in" in output:
                result["login"] = "ready"
            elif "not logged in" in output:
                result["login"] = "required"
    except (OSError, ValueError, subprocess.TimeoutExpired):
        result["detail"] = "CLI 状态检测失败，请查看本机登录状态"
    return result


class RuntimeAgent(Agent):
    def __init__(self, daemon, provider: str, relay_id: str) -> None:
        token = hmac.new(
            daemon.config["node_token"].encode(), provider.encode(), hashlib.sha256
        ).hexdigest()
        super().__init__(
            root=Path(daemon.config["workspace_root"]),
            api_url=daemon.config["api_url"],
            token=token,
            relay_id=relay_id,
            provider=provider,
            home=Path(daemon.config.get("home", str(Path.home()))),
            git_hosts=tuple(daemon.config.get("git_hosts", ["github.com"])),
        )
        self.daemon = daemon
        self.lock = daemon.operations_lock
        self.http.trust_env = False
        if daemon.trust_file:
            # Same chain as the control-plane client; proxy variables stay ignored.
            self.http.verify = str(daemon.trust_file)
        if daemon.config.get("control_proxy"):
            self.http.proxies.update(
                {"http": daemon.config["control_proxy"], "https": daemon.config["control_proxy"]}
            )

    def active_tasks(self) -> list[dict]:
        return [
            dict(v) for v in list(self.daemon.task_info.values()) if v["provider"] == self.provider
        ]

    def health_check(self) -> None:
        start = time.monotonic()
        status = "unknown"
        try:
            with httpx.Client(
                transport=httpx.HTTPTransport(uds=str(self.daemon.socket_path(self.provider))),
                base_url="http://runtime",
                timeout=90,
            ) as client:
                health = client.get("/health")
                if health.status_code != 200:
                    status = "down"
                elif self.model:
                    # Probe stays under the selected project root and cannot execute tools.
                    directory = self.root / ".coreman-health"
                    directory.mkdir(exist_ok=True)
                    response = client.post(
                        "/v1/chat/completions",
                        json={
                            "model": self.model,
                            "stream": False,
                            "session_id": "",
                            "working_dir": str(directory),
                            "max_turns": 1,
                            "permission_mode": "plan" if self.provider == "claude" else "read-only",
                            "messages": [
                                {"role": "user", "content": "Reply OK. Do not use any tools."}
                            ],
                        },
                    )
                    body = response.json()
                    text = json.dumps(body).lower()
                    if response.status_code in {401, 403} or any(
                        x in text
                        for x in (
                            "not logged in",
                            "authentication_error",
                            "oauth token",
                            "failed to authenticate",
                            "oauth session expired",
                        )
                    ):
                        status = "auth_fail"
                    elif (
                        response.status_code != 200
                        or body.get("error")
                        or body.get("x_relay_error")
                    ):
                        status = "down"
                    else:
                        status = "healthy"
        except httpx.TimeoutException:
            status = "timeout"
        except (httpx.HTTPError, OSError, ValueError):
            status = "down"
        self.report(
            "relay/health", {"status": status, "latency_ms": int((time.monotonic() - start) * 1000)}
        )


class FrameBatcher:
    """Coalesces one call's response bytes: flush at 16 KiB or 50 ms after the first byte.

    Frames keep consecutive sequence numbers. Frames whose send failed are kept and
    resent with the next flush; the API skips sequence numbers it already stored.
    """

    MAX_DELAY = 0.05
    MAX_BYTES = 16 * 1024

    def __init__(self, send) -> None:
        self._send = send
        self._buffer = bytearray()
        self._meta: dict = {}
        self._unsent: list[dict] = []
        self._seq = 0
        self._lock = asyncio.Lock()
        self._timer: asyncio.Task | None = None
        self._error: BaseException | None = None

    async def add(self, data: bytes = b"", **meta) -> None:
        if self._error is not None:
            error, self._error = self._error, None
            raise error
        self._meta.update(meta)
        self._buffer.extend(data)
        if len(self._buffer) >= self.MAX_BYTES:
            await self.flush()
        elif self._timer is None:
            self._timer = asyncio.create_task(self._flush_later())

    async def finish(self, **final) -> None:
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
        self._error = None
        await self.flush(done=True, **final)

    def discard(self) -> None:
        if self._timer is not None:
            self._timer.cancel()

    async def flush(self, **final) -> None:
        async with self._lock:
            data, meta = bytes(self._buffer), self._meta
            self._buffer.clear()
            self._meta = {}
            if data or meta or final:
                pieces = [data[pos : pos + CHUNK_SIZE] for pos in range(0, len(data), CHUNK_SIZE)]
                pieces = pieces or [b""]
                for index, piece in enumerate(pieces):
                    frame = {"seq": self._seq, "data": base64.b64encode(piece).decode()}
                    if index == 0:
                        frame.update(meta)
                    if index == len(pieces) - 1:
                        frame.update(final)
                    self._unsent.append(frame)
                    self._seq += 1
            if self._unsent:
                await self._send(list(self._unsent))
                self._unsent.clear()

    async def _flush_later(self) -> None:
        try:
            await asyncio.sleep(self.MAX_DELAY)
            self._timer = None
            await self.flush()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # surfaced by the next add()
            self._error = exc


class Daemon:
    def __init__(self, config_path: Path) -> None:
        import threading

        self.config_path = config_path
        self.config = json.loads(config_path.read_text())
        self.data_dir = config_path.parent
        self.drivers: dict[str, subprocess.Popen] = {}
        self.agents: dict[str, RuntimeAgent] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.task_info: dict[str, dict] = {}
        self.capabilities = {p: {"installed": False} for p in PROVIDERS}
        self.operations_lock = threading.Lock()
        self.stopping = asyncio.Event()
        self.http = None
        self.lock_file = None
        self.control_deadline = float("inf")
        self.abandoned: set[str] = set()
        self.cancel_requested: set[str] = set()
        self.slots_freed = asyncio.Event()
        self.server_protocol = 1
        self.state_online: bool | None = None
        self.state_written = 0.0
        self.trust_file: Path | None = None
        self.tls_context = None
        self.log_rotator = LogRotator()
        self.socket_failures: dict[str, int] = {}
        self.heal_deferrals: dict[str, int] = {}

    def socket_path(self, provider: str) -> Path:
        # Keep under Unix's 104-byte sockaddr limit, including long macOS home paths.
        return self.socket_dir / (provider + ".sock")

    def save(self) -> None:
        atomic_write(self.config_path, json.dumps(self.config, indent=2))

    def prepare(self) -> None:
        self.lock_file = (self.data_dir / "daemon.lock").open("a")
        fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        root = Path(self.config["workspace_root"])
        if not root.is_absolute() or root == Path("/") or ".." in root.parts:
            raise ValueError("项目主目录必须是绝对路径")
        root.mkdir(parents=True, exist_ok=True)
        if root.resolve() != root:
            raise ValueError("项目主目录不能通过符号链接指向其它目录")
        import tempfile

        with tempfile.TemporaryFile(dir=root):
            pass
        self.config.setdefault("node_id", str(uuid.uuid4()))
        self.config.setdefault("node_token", secrets.token_urlsafe(32))
        self.save()  # durable before enrollment: response loss is safely retryable
        # XDG_RUNTIME_DIR or the data directory; /tmp cleaners age out idle sockets.
        self.socket_dir = ensure_private_dir(
            choose_socket_dir(self.data_dir, self.config["node_id"])
        )
        self.trust_file = build_trust_bundle(self.data_dir, self.config.get("ca_file"))
        self.tls_context = client_context(self.trust_file)
        self.log_rotator.start()
        configured_path = self.config.get("path") or os.environ.get("PATH", "")
        bin_dir = self.data_dir / "cli-bin"
        bin_dir.mkdir(exist_ok=True)
        for provider in PROVIDERS:
            custom = self.config.get(provider + "_path")
            if custom:
                executable = Path(custom)
                if not executable.is_file() or not os.access(executable, os.X_OK):
                    raise ValueError(f"{provider} CLI 路径不可执行")
                link = bin_dir / provider
                if link.is_symlink():
                    link.unlink()
                link.symlink_to(executable)
        os.environ["PATH"] = str(bin_dir) + os.pathsep + configured_path
        proxy = self.config.get("proxy")
        if proxy:
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                os.environ[key] = proxy

    async def api(
        self, path: str, body: dict, *, retry: bool = False, budget: float | None = None
    ) -> dict:
        deadline = time.monotonic() + (FRAME_RETRY_SECONDS if retry else (budget or 15))
        options = {"timeout": budget} if budget else {}
        delay = 0.5
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise httpx.ReadTimeout("control/frame deadline exceeded")
            try:
                # wait_for rather than asyncio.timeout: the daemon supports Python 3.10.
                response = await asyncio.wait_for(
                    self.http.post(path, json=body, **options), timeout=remaining
                )
                if retry and response.status_code in {429, 502, 503, 504}:
                    try:
                        wait = max(delay, float(response.headers.get("retry-after", "0")))
                    except ValueError:
                        wait = delay
                else:
                    response.raise_for_status()
                    return response.json()["data"]
            except (TimeoutError, asyncio.TimeoutError) as exc:  # noqa: UP041 distinct on 3.10
                raise httpx.ReadTimeout("control/frame deadline exceeded") from exc
            except httpx.TransportError:
                if not retry:
                    raise
                wait = delay
            await asyncio.sleep(min(wait, max(0, deadline - time.monotonic())))
            delay = min(delay * 2, 4.0)

    async def watch_control_lease(self) -> None:
        while not self.stopping.is_set():
            if time.monotonic() >= self.control_deadline:
                self.abandoned.update(self.tasks)
                await self.cancel_all()
            await asyncio.sleep(0.25)

    async def enroll(self) -> None:
        if "backends" in self.config:
            return
        machine = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine())
        payload = {
            "install_token": self.config["install_token"],
            "node_id": self.config["node_id"],
            "node_token": self.config["node_token"],
            "hostname": socket.gethostname(),
            "username": getpass.getuser(),
            "platform": platform.system().lower(),
            "architecture": machine,
            "environment": (
                environment()
                if self.config.get("environment", "auto") == "auto"
                else self.config["environment"]
            ),
            "version": __version__,
            "protocol": PROTOCOL_VERSION,
            "workspace_root": self.config["workspace_root"],
        }
        try:
            result = await self.api("/api/runtime/enroll", payload, retry=True)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if 400 <= status < 500 and status not in RETRYABLE_ENROLL_STATUSES:
                # Expired, revoked or reused links: stop instead of crash-looping forever.
                raise FatalConfigError(enrollment_rejection(exc.response)) from exc
            raise
        self.config["backends"] = result["backends"]
        self.config.pop("install_token", None)
        self.save()

    def start_driver(self, provider: str) -> None:
        existing = self.drivers.get(provider)
        if existing and existing.poll() is None:
            return
        # Cleaners may remove the whole directory, not only the socket file.
        ensure_private_dir(self.socket_dir)
        path = self.socket_path(provider)
        path.unlink(missing_ok=True)
        release = Path(self.config.get("release", str(Path(__file__).resolve().parents[1])))
        binary = release / "runtime_daemon/bin" / ("runtime-" + provider)
        session_dir = self.data_dir / "sessions" / provider
        session_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.data_dir / (provider + ".log")
        rotate_copytruncate(log_path)
        env = {k: v for k, v in os.environ.items() if not k.startswith("COREMAN_")}
        # The driver appends to this file for its whole life. A pipe would kill it with
        # SIGPIPE before it reaps its CLI process groups if the daemon died first, so the
        # rotator copy-truncates the O_APPEND file on a timer instead.
        with log_path.open("ab") as log_handle:
            os.chmod(log_path, 0o600)
            self.drivers[provider] = subprocess.Popen(
                [
                    str(binary),
                    "--parent-pid",
                    str(os.getpid()),
                    "--socket",
                    str(path),
                    "--sessions-dir",
                    str(session_dir),
                    "--log-file",
                    "-",
                ],
                env=env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.log_rotator.watch(log_path)
        self.socket_failures[provider] = 0
        self.heal_deferrals[provider] = 0

    def driver_fault(self, provider: str) -> str:
        """Why a live driver can no longer be reached; empty when fine or not running."""
        process = self.drivers.get(provider)
        if process is None or process.poll() is not None:
            return ""
        if not self.socket_path(provider).is_socket():
            return "socket missing"
        if self.socket_failures.get(provider, 0) >= SOCKET_FAILURE_LIMIT:
            return "socket unreachable"
        return ""

    def stop_driver(self, provider: str, timeout: float = 15) -> None:
        process = self.drivers.get(provider)
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    async def heal_driver(self, provider: str) -> None:
        """Terminate a live driver whose socket is gone so ``start_driver`` recreates it."""
        reason = self.driver_fault(provider)
        if not reason:
            return
        busy = any(info["provider"] == provider for info in list(self.task_info.values()))
        deferred = self.heal_deferrals.get(provider, 0)
        if busy and deferred < HEAL_DEFER_ROUNDS:
            # Connected streams outlive an unlinked socket; let them finish first.
            self.heal_deferrals[provider] = deferred + 1
            LOG.warning("%s driver %s; restart deferred for running requests", provider, reason)
            return
        LOG.warning("Restarting %s driver: %s", provider, reason)
        await asyncio.to_thread(self.stop_driver, provider)

    async def discover(self) -> None:
        for provider in PROVIDERS:
            cap = await asyncio.to_thread(cli_status, provider)
            if cap["installed"]:
                try:
                    await self.heal_driver(provider)
                    self.start_driver(provider)
                    async with httpx.AsyncClient(
                        transport=httpx.AsyncHTTPTransport(uds=str(self.socket_path(provider))),
                        base_url="http://runtime",
                        timeout=10,
                    ) as client:
                        for attempt in range(30):
                            try:
                                response = await client.get("/v1/models")
                                break
                            except httpx.ConnectError:
                                if attempt == 29:
                                    raise
                                await asyncio.sleep(0.1)
                        response.raise_for_status()
                        cap["models"] = [r["id"] for r in response.json()["data"]]
                    self.socket_failures[provider] = 0
                except httpx.ConnectError:
                    self.socket_failures[provider] = self.socket_failures.get(provider, 0) + 1
                    cap["detail"] = "AI API 启动中或不可用"
                except (httpx.HTTPError, OSError, ValueError):
                    cap["detail"] = "AI API 启动中或不可用"
                if provider not in self.agents:
                    agent = RuntimeAgent(
                        self, provider, self.config["backends"][provider]["relay_id"]
                    )
                    self.agents[provider] = agent
                    if provider == "claude" and self.config.get("install_claude_probe"):
                        try:
                            await asyncio.to_thread(agent.install_claude_probe)
                        except (OSError, ValueError, OperationError):
                            cap["detail"] = "额度探针安装失败，原配置未覆盖"
                    agent.model = (cap["models"] or [""])[0]
                    if self.config.get("schedules", True):
                        agent.start_schedules()
                self.agents[provider].model = (cap["models"] or [""])[0]
            self.capabilities[provider] = cap

    def validate_command(self, command: dict) -> dict:
        provider = command["provider"]
        if provider not in PROVIDERS or provider not in self.agents:
            raise ValueError("AI 未安装")
        method, path = command["method"], command["path"]
        import re

        session_events = method == "GET" and bool(
            re.fullmatch(r"/session/[A-Za-z0-9_-]{1,200}/events", path)
        )
        if not session_events and (method, path) not in {
            ("GET", "/health"),
            ("GET", "/v1/models"),
            ("GET", "/v1/stats"),
            ("GET", "/sessions"),
            ("POST", "/v1/chat/completions"),
            ("POST", "/"),
        }:
            raise ValueError("请求路径不支持")
        raw = base64.b64decode(command["body"], validate=True)
        if len(raw) > 64 * 1024 * 1024:
            raise ValueError("请求过大")
        data = json.loads(raw) if raw else {}
        if path == "/v1/chat/completions":
            directory = self.agents[provider].workspace(data.get("working_dir", ""))
            directory.mkdir(parents=True, exist_ok=True)
            data["working_dir"] = str(directory)
            # API inputs are rooted; CLI tools retain their original permission semantics.
            for extra in data.get("add_dirs") or []:
                self.agents[provider].workspace(extra)
            if data.get("system_prompt_file"):
                self.agents[provider].workspace(data["system_prompt_file"])
            sid = data.get("session_id", "")
            if sid:
                import re

                if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", sid):
                    raise ValueError("会话标识无效")
            env = data.get("env_vars") or {}
            blocked = {
                "HOME",
                "PATH",
                "COREMAN_AGENT_TOKEN",
                "COREMAN_NODE_TOKEN",
                "COREMAN_INSTALL_TOKEN",
            }
            if any(k in blocked for k in env):
                raise ValueError("请求不能覆盖运行时身份或用户环境")
        return data

    async def execute(self, command: dict) -> None:
        identity, provider = command["id"], command["provider"]
        frames = FrameBatcher(lambda batch: self.send_frames(identity, batch))

        try:
            data = self.validate_command(command)
            env = data.get("env_vars") or {}
            self.task_info[identity] = {
                "request_id": identity,
                "provider": provider,
                "bot_key": env.get("COREMAN_BOT_KEY") or env.get("BOT_KEY", ""),
                "user": env.get("COREMAN_USER_LOGIN") or env.get("BOT_USER_LOGIN", ""),
                "chat_id": env.get("COREMAN_CHAT_ID", ""),
                "chat_type": env.get("COREMAN_CHAT_TYPE", ""),
                "started_at": time.time(),
                "working_dir": data.get("working_dir", ""),
            }
            if command["path"] == "/":
                if data.get("type") in {"status", "ping"}:
                    self.task_info.pop(identity, None)
                import threading

                cancelled = threading.Event()
                token = COMMAND_CANCEL.set(cancelled)
                operation = asyncio.create_task(
                    asyncio.to_thread(self.agents[provider].dispatch, data)
                )
                try:
                    result = await asyncio.shield(operation)
                except asyncio.CancelledError:
                    cancelled.set()
                    # Cancel Git process groups before releasing the operation lock.
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(asyncio.shield(operation), timeout=5)
                    raise
                finally:
                    COMMAND_CANCEL.reset(token)
                await frames.add(json.dumps(result).encode(), status_code=200)
            else:
                async with httpx.AsyncClient(
                    transport=httpx.AsyncHTTPTransport(uds=str(self.socket_path(provider))),
                    base_url="http://runtime",
                    timeout=httpx.Timeout(120, connect=10),
                ) as local:
                    async with local.stream(
                        command["method"],
                        command["path"],
                        json=data if command["method"] == "POST" else None,
                    ) as response:
                        await frames.add(
                            status_code=response.status_code,
                            content_type=response.headers.get("content-type", "application/json"),
                        )
                        async for part in response.aiter_bytes():
                            await frames.add(part)
            await frames.finish()
        except asyncio.CancelledError:
            # Closing the local stream reaches the Go driver's disconnect watcher.
            frames.discard()
            raise
        except Exception as exc:
            LOG.warning("Request failed: %s", type(exc).__name__)
            with contextlib.suppress(Exception):
                await frames.finish(error="execution_failed")
        finally:
            self.task_info.pop(identity, None)
            self.slots_freed.set()

    @property
    def max_concurrent(self) -> int:
        return int(self.config.get("max_concurrent", 10))

    async def pause(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(self.stopping.wait(), timeout=seconds)

    def write_state(self, online: bool, *, force: bool = False) -> None:
        """Local status for the service installer: on change, otherwise at most every 10 s.

        The first successful contact after start always writes (the state changes), which
        the installer compares against the service start time.
        """
        clock = time.monotonic()
        if (
            not force
            and online == self.state_online
            and clock - self.state_written < STATE_WRITE_SECONDS
        ):
            return
        atomic_write(
            self.data_dir / "state.json",
            json.dumps(
                {"online": online, "updated_at": time.time(), "node_id": self.config["node_id"]}
            ),
        )
        self.state_online, self.state_written = online, clock

    def heartbeat_body(self) -> dict:
        return {
            **self.capabilities,
            "version": __version__,
            "protocol": PROTOCOL_VERSION,
            "service_status": self.config.get("service_status", "foreground"),
            # Shown in the console so an overloaded node is visible before calls time out.
            "max_concurrent": self.max_concurrent,
            "active_calls": len([task for task in self.tasks.values() if not task.done()]),
        }

    async def heartbeat_loop(self) -> None:
        while not self.stopping.is_set():
            delay = HEARTBEAT_SECONDS
            try:
                await self.api("/api/runtime/heartbeat", self.heartbeat_body())
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in REJECTED_STATUSES:
                    delay = REJECTED_BACKOFF_MAX
            except (httpx.HTTPError, KeyError, ValueError):
                pass
            else:
                # A long poll can outlast the installer's freshness check; heartbeats keep it fresh.
                self.write_state(True)
            await self.pause(delay)

    async def send_frames(self, identity: str, frames: list[dict]) -> None:
        path = f"/api/runtime/calls/{identity}/frames"
        if self.server_protocol >= 2:
            try:
                for start in range(0, len(frames), MAX_BATCH_FRAMES):
                    batch = frames[start : start + MAX_BATCH_FRAMES]
                    await self.api(path, {"frames": batch}, retry=True)
                return
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 422:
                    raise
                # An older API replica during a rolling upgrade: resend frame by frame.
                self.server_protocol = 1
        for frame in frames:
            await self.api(path, frame, retry=True)

    async def poll_once(self) -> dict | None:
        """One claim round; None when a wait with no free slots was interrupted."""
        self.tasks = {k: t for k, t in self.tasks.items() if not t.done()}
        self.cancel_requested.intersection_update(self.tasks)
        abandoned = set(self.abandoned)
        slots = max(0, self.max_concurrent - len(self.tasks))
        body = {
            "running": [k for k in self.tasks if k not in self.cancel_requested],
            "abandoned": list(abandoned),
            "slots": slots,
            "wait": POLL_WAIT_SECONDS,
        }
        self.slots_freed.clear()
        request = asyncio.ensure_future(
            self.api("/api/runtime/poll", body, budget=POLL_WAIT_SECONDS + 15)
        )
        watchers = [asyncio.ensure_future(self.stopping.wait())]
        if slots == 0:
            watchers.append(asyncio.ensure_future(self.slots_freed.wait()))
        try:
            await asyncio.wait({request, *watchers}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for watcher in watchers:
                watcher.cancel()
        if not request.done() and slots == 0:
            # Nothing can be claimed without free slots, so dropping the wait loses nothing.
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            return None
        # With free slots the round is always finished, so claimed commands are never lost.
        result = await request
        self.server_protocol = int(result.get("protocol") or 1)
        self.control_deadline = time.monotonic() + CONTROL_LEASE_SECONDS
        self.write_state(True)
        self.abandoned.difference_update(abandoned)
        for identity in result["cancelled"]:
            if identity in self.tasks:
                self.cancel_requested.add(identity)
                self.tasks[identity].cancel()
        if self.stopping.is_set():
            # Shutting down: hand claimed commands straight back instead of starting them.
            self.abandoned.update(command["id"] for command in result["commands"])
            if self.abandoned:
                with contextlib.suppress(httpx.HTTPError, KeyError, ValueError):
                    await self.api(
                        "/api/runtime/poll", {"abandoned": list(self.abandoned), "slots": 0}
                    )
            return result
        for command in result["commands"]:
            self.tasks[command["id"]] = asyncio.create_task(self.execute(command))
        return result

    async def cancel_all(self) -> None:
        current, self.tasks = self.tasks, {}
        for task in current.values():
            task.cancel()
        await asyncio.gather(*current.values(), return_exceptions=True)

    async def run(self) -> None:
        self.prepare()
        async with httpx.AsyncClient(
            base_url=self.config["api_url"],
            timeout=15,
            follow_redirects=False,
            trust_env=False,
            verify=self.tls_context or True,
            proxy=self.config.get("control_proxy") or None,
            headers={
                "X-Runtime-ID": self.config["node_id"],
                "Authorization": "Bearer " + self.config["node_token"],
            },
        ) as client:
            self.http = client
            await self.enroll()
            await self.discover()
            next_discovery = time.monotonic() + 60
            self.control_deadline = time.monotonic() + CONTROL_LEASE_SECONDS
            lease_watch = asyncio.create_task(self.watch_control_lease())
            beats = asyncio.create_task(self.heartbeat_loop())
            rejected_backoff = 0.0
            try:
                while not self.stopping.is_set():
                    if time.monotonic() >= next_discovery:
                        # Run probes separately so cancellation remains responsive.
                        if not hasattr(self, "discovery_task") or self.discovery_task.done():
                            self.discovery_task = asyncio.create_task(self.discover())
                        next_discovery = time.monotonic() + 60
                    try:
                        result = await self.poll_once()
                    except httpx.HTTPStatusError as exc:
                        # The independent lease watchdog owns cancellation.
                        if exc.response.status_code in REJECTED_STATUSES:
                            rejected_backoff = min(
                                max(rejected_backoff * 2, 2.0), REJECTED_BACKOFF_MAX
                            )
                            await self.pause(rejected_backoff)
                        else:
                            await self.pause(2)
                        continue
                    except (httpx.HTTPError, KeyError, ValueError):
                        await self.pause(2)
                        continue
                    rejected_backoff = 0.0
                    if result is not None and int(result.get("protocol") or 1) < 2:
                        # An API without long polling answers at once: keep the old cadence.
                        await self.pause(LEGACY_POLL_INTERVAL)
            finally:
                for watcher in (beats, lease_watch):
                    watcher.cancel()
                await asyncio.gather(beats, lease_watch, return_exceptions=True)
                self.write_state(False, force=True)
                await self.cancel_all()
                if hasattr(self, "discovery_task"):
                    self.discovery_task.cancel()
                    await asyncio.gather(self.discovery_task, return_exceptions=True)
                for agent in self.agents.values():
                    agent.stop.set()
                for proc in self.drivers.values():
                    if proc.poll() is None:
                        proc.terminate()
                        try:
                            await asyncio.to_thread(proc.wait, timeout=15)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            await asyncio.to_thread(proc.wait)
                self.log_rotator.stop()
                for provider in PROVIDERS:
                    self.socket_path(provider).unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="CoreMan Runtime Daemon")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    data_dir = args.config.parent
    configure_logging(data_dir)
    daemon = Daemon(args.config)
    service_log = appended_stderr_file(data_dir / "service.log")
    if service_log:
        # launchd and the chroot supervisor append stderr here for the service's lifetime.
        rotate_copytruncate(service_log)
        daemon.log_rotator.watch(service_log)

    async def start():
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, daemon.stopping.set)
        await daemon.run()

    try:
        asyncio.run(start())
    except FatalConfigError as exc:
        LOG.error("%s", exc)
        record_fatal(data_dir, str(exc))
        raise SystemExit(fatal_exit_status(daemon.config.get("service_status", ""))) from None
    except Exception:
        LOG.exception("Runtime stopped unexpectedly")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
