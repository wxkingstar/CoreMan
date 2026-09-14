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

from relay_agent.agent import COMMAND_CANCEL, Agent, OperationError, atomic_write
from runtime_daemon import __version__

LOG = logging.getLogger("coreman-runtime")
CHUNK_SIZE = 48 * 1024
PROVIDERS = ("claude", "codex")
CONTROL_LEASE_SECONDS = 40.0
FRAME_RETRY_SECONDS = 25.0


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
        self.log_handles = []
        self.control_deadline = float("inf")
        self.abandoned: set[str] = set()

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
        self.socket_dir = Path("/tmp") / f"coreman-{os.getuid()}-{self.config['node_id'][:8]}"
        if self.socket_dir.is_symlink():
            raise ValueError("Socket 目录不安全")
        self.socket_dir.mkdir(mode=0o700, exist_ok=True)
        if self.socket_dir.stat().st_uid != os.getuid():
            raise ValueError("Socket 目录属于其它用户")
        os.chmod(self.socket_dir, 0o700)
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

    async def api(self, path: str, body: dict, *, retry: bool = False) -> dict:
        deadline = time.monotonic() + (FRAME_RETRY_SECONDS if retry else 15)
        delay = 0.5
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise httpx.ReadTimeout("control/frame deadline exceeded")
            try:
                async with asyncio.timeout(remaining):
                    response = await self.http.post(path, json=body)
                if retry and response.status_code in {429, 502, 503, 504}:
                    try:
                        wait = max(delay, float(response.headers.get("retry-after", "0")))
                    except ValueError:
                        wait = delay
                else:
                    response.raise_for_status()
                    return response.json()["data"]
            except TimeoutError as exc:
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
        result = await self.api(
            "/api/runtime/enroll",
            {
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
                "workspace_root": self.config["workspace_root"],
            },
            retry=True,
        )
        self.config["backends"] = result["backends"]
        self.config.pop("install_token", None)
        self.save()

    def start_driver(self, provider: str) -> None:
        existing = self.drivers.get(provider)
        if existing and existing.poll() is None:
            return
        path = self.socket_path(provider)
        path.unlink(missing_ok=True)
        release = Path(self.config.get("release", str(Path(__file__).resolve().parents[1])))
        binary = release / "runtime_daemon/bin" / ("runtime-" + provider)
        session_dir = self.data_dir / "sessions" / provider
        session_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.data_dir / (provider + ".log")
        if log_path.exists() and log_path.stat().st_size > 20 * 1024 * 1024:
            log_path.replace(log_path.with_suffix(".log.1"))
        log_handle = log_path.open("ab")
        self.log_handles.append(log_handle)
        env = {k: v for k, v in os.environ.items() if not k.startswith("COREMAN_")}
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

    async def discover(self) -> None:
        for provider in PROVIDERS:
            cap = await asyncio.to_thread(cli_status, provider)
            if cap["installed"]:
                try:
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
        identity, provider, seq = command["id"], command["provider"], 0

        async def publish(data: bytes = b"", **kwargs) -> None:
            nonlocal seq
            await self.api(
                f"/api/runtime/calls/{identity}/frames",
                {
                    "seq": seq,
                    "data": base64.b64encode(data).decode(),
                    **kwargs,
                },
                retry=True,
            )
            seq += 1

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
                if data.get("type") in {"status", "check-active-tasks", "ping"}:
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
                raw = json.dumps(result).encode()
                await publish(status_code=200)
                for pos in range(0, len(raw), CHUNK_SIZE):
                    await publish(raw[pos : pos + CHUNK_SIZE])
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
                        await publish(
                            status_code=response.status_code,
                            content_type=response.headers.get("content-type", "application/json"),
                        )
                        async for part in response.aiter_bytes():
                            for pos in range(0, len(part), CHUNK_SIZE):
                                await publish(part[pos : pos + CHUNK_SIZE])
            await publish(done=True)
        except asyncio.CancelledError:
            # Closing the local stream reaches the Go driver's disconnect watcher.
            raise
        except Exception as exc:
            LOG.warning("Request failed: %s", type(exc).__name__)
            with contextlib.suppress(Exception):
                await publish(error="execution_failed", done=True)
        finally:
            self.task_info.pop(identity, None)

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
            proxy=self.config.get("control_proxy") or None,
            headers={
                "X-Runtime-ID": self.config["node_id"],
                "Authorization": "Bearer " + self.config["node_token"],
            },
        ) as client:
            self.http = client
            await self.enroll()
            await self.discover()
            next_discovery, next_heartbeat, last_success = (
                time.monotonic() + 60,
                0,
                time.monotonic(),
            )
            self.control_deadline = time.monotonic() + CONTROL_LEASE_SECONDS
            lease_watch = asyncio.create_task(self.watch_control_lease())
            try:
                while not self.stopping.is_set():
                    self.tasks = {k: t for k, t in self.tasks.items() if not t.done()}
                    try:
                        clock = time.monotonic()
                        if clock >= next_discovery:
                            # Run probes separately so cancellation remains responsive.
                            if not hasattr(self, "discovery_task") or self.discovery_task.done():
                                self.discovery_task = asyncio.create_task(self.discover())
                            next_discovery = clock + 60
                        if clock >= next_heartbeat:
                            with contextlib.suppress(httpx.HTTPError):
                                await self.api(
                                    "/api/runtime/heartbeat",
                                    {
                                        **self.capabilities,
                                        "version": __version__,
                                        "service_status": self.config.get(
                                            "service_status", "foreground"
                                        ),
                                    },
                                )
                            next_heartbeat = clock + 10
                        abandoned = set(self.abandoned)
                        result = await self.api(
                            "/api/runtime/poll",
                            {
                                "running": list(self.tasks),
                                "abandoned": list(abandoned),
                                "slots": max(
                                    0, self.config.get("max_concurrent", 10) - len(self.tasks)
                                ),
                            },
                        )
                        atomic_write(
                            self.data_dir / "state.json",
                            json.dumps(
                                {
                                    "online": True,
                                    "updated_at": time.time(),
                                    "node_id": self.config["node_id"],
                                }
                            ),
                        )
                        last_success = time.monotonic()
                        self.control_deadline = last_success + CONTROL_LEASE_SECONDS
                        self.abandoned.difference_update(abandoned)
                        for identity in result["cancelled"]:
                            if identity in self.tasks:
                                self.tasks[identity].cancel()
                        for command in result["commands"]:
                            self.tasks[command["id"]] = asyncio.create_task(self.execute(command))
                    except (httpx.HTTPError, KeyError, ValueError):
                        # The independent lease watchdog owns cancellation.
                        await asyncio.sleep(2)
                    try:
                        await asyncio.wait_for(self.stopping.wait(), timeout=0.5)
                    except TimeoutError:
                        pass
            finally:
                lease_watch.cancel()
                await asyncio.gather(lease_watch, return_exceptions=True)
                atomic_write(
                    self.data_dir / "state.json",
                    json.dumps({"online": False, "updated_at": time.time()}),
                )
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
                for handle in self.log_handles:
                    handle.close()
                for provider in PROVIDERS:
                    self.socket_path(provider).unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="CoreMan Runtime Daemon")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    daemon = Daemon(args.config)

    async def start():
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, daemon.stopping.set)
        await daemon.run()

    asyncio.run(start())


if __name__ == "__main__":
    main()
