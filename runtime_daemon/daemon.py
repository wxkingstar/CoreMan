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
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import threading
import time
import unicodedata
import uuid
from pathlib import Path

import httpx

from runtime_daemon import PROTOCOL_VERSION, __version__, worktree
from runtime_daemon.agent import (
    COMMAND_CANCEL,
    SEAT_OPERATIONS,
    Agent,
    OperationError,
    atomic_write,
    operation_message,
)
from runtime_daemon.assistant_turns import AssistantTurns
from runtime_daemon.lifecycle import (
    FatalConfigError,
    choose_socket_dir,
    ensure_private_dir,
    fatal_exit_status,
    record_fatal,
    write_manage_command,
)
from runtime_daemon.logs import (
    LogRotator,
    appended_stderr_file,
    configure_logging,
    rotate_copytruncate,
)
from runtime_daemon.sandbox import CAPABILITY as SANDBOX_CAPABILITY
from runtime_daemon.sandbox import remove_tree
from runtime_daemon.seats import SeatLogins, Seats, opencode_auth_file
from runtime_daemon.tls import build_trust_bundle, client_context
from runtime_daemon.workspace import MARKER, TRASH_DIR, TRASH_ENTRY_NAME, reserved

LOG = logging.getLogger("coreman-runtime")
CHUNK_SIZE = 48 * 1024
PROVIDERS = ("claude", "codex", "opencode")
# Only the driver's /health may grant these; anything but a literal true is false.
# The retired feishu_personal_restricted_v1 is never forwarded, so servers that
# still expect the restricted mode send no personal credentials to this node.
DRIVER_CAPABILITIES = {
    "claude": (
        "feishu_personal_tools_v1",
        "feishu_group_docs_v1",
        "wecom_personal_tools_v1",
        "owner_session_view_v1",
        "systems_catalog_v1",
        "resume_fallback_v1",
        "system_mcp_v1",
    ),
    "codex": (
        "feishu_personal_tools_v1",
        "feishu_group_docs_v1",
        "wecom_personal_tools_v1",
        "owner_session_view_v1",
        "systems_catalog_v1",
        "resume_fallback_v1",
        "system_mcp_v1",
    ),
    "opencode": (
        "feishu_personal_tools_v1",
        "feishu_group_docs_v1",
        "wecom_personal_tools_v1",
        "owner_session_view_v1",
        "systems_catalog_v1",
        "resume_fallback_v1",
        "system_mcp_v1",
    ),
}
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
# A freshly started driver needs a moment to bind its socket: about 3 seconds of retries.
DRIVER_CONNECT_ATTEMPTS = 30
DRIVER_CONNECT_RETRY_SECONDS = 0.1
# Discovery rounds a broken driver is kept for its in-flight streams before a forced restart.
HEAL_DEFER_ROUNDS = 30
# Enrollment rejections worth retrying; any other 4xx will not change with the same token.
RETRYABLE_ENROLL_STATUSES = {408, 429}

# 助理专用实例（config.json 里 assistant_only 为 true）只接这些节点命令：平台对助理所在节点会发的
# 探活、状态、额度、席位，开通与换运行时用到的目录检查与初始化，以及停用或换走之后删除助理的工作
# 目录。其余命令（读写或执行工作目录里的内容、导出内容、遍历所有工作目录）一律以固定代码拒绝。
# 经驱动的读请求只留探活与模型列表。
ASSISTANT_ONLY_OPERATIONS = frozenset(
    {
        "ping",
        "status",
        "health-check",
        "probe-rate-limits",
        "workspace-info",
        "workspace-probe",
        "workspace-init",
        "workspace-remove",
        *SEAT_OPERATIONS,
    }
)
ASSISTANT_ONLY_READS = frozenset({("GET", "/health"), ("GET", "/v1/models")})

# 出站代理变量：大小写两套都写，curl 等程序只认小写的那两个。HTTPS 在前，读取时优先它。
PROXY_ENV_KEYS: tuple[str, ...] = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")


def redact_proxy_url(url: str) -> str:
    """The proxy address without its credentials: only the host and port leave the node."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        scheme, rest = "", url
    netloc, slash, path = rest.partition("/")
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
    return scheme + sep + netloc + slash + path


# 运行时控制类环境变量黑名单，与 coreman/core/bots/env_policy.py 逐项一致（守护进程是独立包，
# 不能 import coreman；各条目的理由见那边的注释）。tests/unit/test_env_policy.py 校验两份相同。
# 请求 env 会原样并入免审批运行的 CLI 进程，命中任一条就拒绝整个请求。
BLOCKED_ENV_PREFIXES: tuple[str, ...] = (
    "ANTHROPIC_",
    "OPENAI_",
    "CLAUDE_",
    "CODEX_",
    "OPENCODE_",
    "LD_",
    "DYLD_",
    "PYTHON",
    "NPM_CONFIG_",
    "CORECLR_",
    "OPENSSL_",
    "BASH_FUNC_",
    "GIT_",
    "SSH_",
    "SSL_",
    "XDG_",
)
BLOCKED_ENV_KEYS: frozenset[str] = frozenset(
    {
        "HOME",
        "PATH",
        "SHELL",
        "TMPDIR",
        "CLAUDECODE",
        "NODE_OPTIONS",
        "NODE_PATH",
        "BUN_OPTIONS",
        "PERL5OPT",
        "PERL5LIB",
        "PERLLIB",
        "PERL5DB",
        "RUBYOPT",
        "RUBYLIB",
        "JAVA_TOOL_OPTIONS",
        "JDK_JAVA_OPTIONS",
        "_JAVA_OPTIONS",
        "DOTNET_STARTUP_HOOKS",
        "GOFLAGS",
        "GCONV_PATH",
        "BASH_ENV",
        "ENV",
        "SHELLOPTS",
        "BASHOPTS",
        "PS4",
        "PROMPT_COMMAND",
        "ZDOTDIR",
        "EDITOR",
        "VISUAL",
        "PAGER",
        "MANPAGER",
        "BROWSER",
        "LESSOPEN",
        "LESSCLOSE",
        "SUDO_ASKPASS",
        "RIPGREP_CONFIG_PATH",
        "NODE_EXTRA_CA_CERTS",
        "NODE_TLS_REJECT_UNAUTHORIZED",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "FTP_PROXY",
        "COREMAN_AGENT_TOKEN",
        "COREMAN_NODE_TOKEN",
        "COREMAN_INSTALL_TOKEN",
    }
)
ALLOWED_ENV_KEYS: frozenset[str] = frozenset(
    {
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        # 厂商 API key 与模型名只换凭据，不改变请求端点；端点类变量仍被前缀拦下。
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
    }
)


def blocked_env_keys(keys) -> list[str]:
    """Sorted blocked key names; compared upper-case because curl also honours http_proxy."""
    found = set()
    for key in keys:
        name = str(key).upper()
        if name in ALLOWED_ENV_KEYS:
            continue
        if name in BLOCKED_ENV_KEYS or name.startswith(BLOCKED_ENV_PREFIXES):
            found.add(str(key))
    return sorted(found)


MACHINE_ID_FILES = ("/etc/machine-id", "/var/lib/dbus/machine-id")


def machine_fingerprint(files: tuple[str, ...] = MACHINE_ID_FILES) -> str:
    """本机的稳定指纹：机器编号的带前缀哈希，原值不出本机。读不到时为空串。

    平台用它认出同一台机器上的几个节点（个人助理要求助理节点与普通员工的节点用不同的系统用户）。
    """
    for name in files:
        try:
            value = Path(name).read_text().strip()
        except (OSError, ValueError):
            continue
        if value:
            return hashlib.sha256(b"coreman-machine-v1:" + value.encode()).hexdigest()
    return ""


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
        "重启服务无法解决：请在「运行时管理」重新生成安装链接，"
        "把安装命令末尾的 | sh 换成 | sh -s -- --replace 后重新安装。"
    )


async def optional_capabilities(client: httpx.AsyncClient) -> dict:
    """驱动 /health 声明的能力；请求失败、状态码或内容不对都当什么也没声明。"""
    try:
        response = await client.get("/health")
        declared = response.json().get("capabilities", {}) if response.status_code == 200 else {}
    except (httpx.HTTPError, ValueError, AttributeError):
        return {}
    return declared if isinstance(declared, dict) else {}


def opencode_auth(env: dict | None = None) -> dict:
    """OpenCode 的凭据（auth.json，按服务商分键）；读不到时为空。"""
    try:
        data = json.loads(opencode_auth_file(dict(os.environ if env is None else env)).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def login_account(provider: str, status: dict, env: dict | None = None) -> str:
    """CLI 当前登录的账号邮箱；读不到时为空，不影响登录状态。

    OpenCode 没有账号，显示已登录的服务商。"""
    env = os.environ if env is None else env
    if provider == "opencode":
        return ", ".join(sorted(str(k) for k in opencode_auth(env)))[:255]
    try:
        if provider == "claude":
            if status.get("email"):
                return str(status["email"])[:255]
            config_dir = env.get("CLAUDE_CONFIG_DIR")
            state = (
                Path(config_dir) / ".claude.json" if config_dir else Path.home() / ".claude.json"
            )
            data = json.loads(state.read_text())
            return str((data.get("oauthAccount") or {}).get("emailAddress") or "")[:255]
        codex_home = Path(env.get("CODEX_HOME") or Path.home() / ".codex")
        data = json.loads((codex_home / "auth.json").read_text())
        payload = ((data.get("tokens") or {}).get("id_token") or "").split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return str(claims.get("email") or "")[:255]
    except (OSError, ValueError, IndexError, AttributeError):
        return ""


AUTH_FAILURE_MARKERS = (
    "not logged in",
    "authentication_error",
    "oauth token",
    "failed to authenticate",
    "oauth session expired",
    "session has expired",
    "session_expired",
    # 凭据文件还在、auth status 仍报已登录，但账号已不可用（令牌吊销、组织或账号停用、账号被暂停）。
    "/login",
    "invalid api key",
    "token revoked",
    "has been disabled",
    "account is on hold",
    "claude.ai/restricted",
    "api error: 401",
    "api error: 403",
)


def probe_error(status_code: int, body: object) -> str:
    """健康检查请求的失败原因；成功返回空串。只看错误本身，不拿模型的正常回复去匹配。"""
    if not isinstance(body, dict):
        return f"HTTP {status_code}"
    error = body.get("error")
    if error:
        message = error.get("message") if isinstance(error, dict) else None
        return str(message or error)
    if body.get("x_relay_error"):
        try:
            return str(body["choices"][0]["message"]["content"]) or "驱动返回错误"
        except (KeyError, IndexError, TypeError):
            return "驱动返回错误"
    return "" if status_code == 200 else f"HTTP {status_code}"


def cli_status(provider: str, env: dict | None = None) -> dict:
    """CLI 的安装与登录状态；env 给定时按该环境（席位）查登录。"""
    path = shutil.which(provider)
    result = {
        "installed": bool(path),
        "version": "",
        "account": "",
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
        if provider == "opencode":
            # 登录即 auth.json 里有凭据（OpenCode Go 等的 API Key）；没有登录状态命令可问。
            result["login"] = "ready" if opencode_auth(env) else "required"
            if result["login"] == "ready":
                result["account"] = login_account(provider, {}, env)
            return result
        command = (
            [path, "auth", "status", "--json"]
            if provider == "claude"
            else [path, "login", "status"]
        )
        proc = subprocess.run(command, capture_output=True, text=True, timeout=15, env=env)
        data = {}
        if provider == "claude":
            data = json.loads(proc.stdout)
            result["login"] = "ready" if data.get("loggedIn") else "required"
            plan = data.get("subscriptionType")
            if isinstance(plan, str) and re.fullmatch(r"[a-zA-Z0-9_.-]{1,40}", plan):
                result["plan_type"] = plan
            verified = data.get("accountVerified")
            if isinstance(verified, bool):
                result["account_verified"] = verified
        else:
            output = (proc.stdout + proc.stderr).lower()
            if proc.returncode == 0 and "logged in" in output:
                result["login"] = "ready"
            elif "not logged in" in output:
                result["login"] = "required"
        if result["login"] == "ready":
            result["account"] = login_account(provider, data, env)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        result["detail"] = "CLI 状态检测失败，请查看本机登录状态"
    return result


def assistant_only_refusal(
    method: str, path: str, data: dict, root: Path | None = None
) -> str | None:
    """助理专用实例对一条请求的裁决：不接就返回固定代码。

    聊天请求必须带 `sandbox: true`（只在沙箱里跑）；节点命令只接名单上的；经驱动的读请求只留
    探活与模型列表。给了工作目录根时，工作目录落在保留目录（健康探测目录、回收站）里的一律拒绝：
    沙箱里的助理能写自己的工作目录，探测在沙箱外跑。
    """
    if path == "/v1/chat/completions":
        refused = None if data.get("sandbox") is True else "assistant_only"
    elif path == "/":
        refused = None if data.get("type") in ASSISTANT_ONLY_OPERATIONS else "assistant_only"
    else:
        return None if (method, path) in ASSISTANT_ONLY_READS else "assistant_only"
    if refused is None and root is not None and reserved(root, data.get("working_dir")):
        return "reserved_directory"
    return refused


class AssistantOnlyRefused(Exception):
    """助理专用实例不接这条请求；`code` 回给平台。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


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
            git_hosts=tuple(daemon.git_hosts),
            sessions_dir=daemon.data_dir / "sessions" / provider,
        )
        self.daemon = daemon
        # 助理专用实例不跑遍历所有工作目录的定时任务（回收记忆、刷新 worktree）。
        self.workspace_jobs = not daemon.assistant_only
        # 工作目录的删除（停用助理）只在助理专用实例上做，见 workspace.Workspace.remove。
        self.assistant_only = daemon.assistant_only
        # 服务端在心跳里给的检查模型（模型目录的默认模型）。驱动发现的列表只是默认值，
        # 第一个未必是账号能用的（如 ChatGPT 账号用不了 gpt-5.5）。
        self.probe_model = ""
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

    def seat_operation(self, kind: str, data: dict) -> dict:
        """席位的增删、登录与切换（见 seats）。不占实例锁：等管理员授权期间其它操作照常。"""
        seats, provider = self.daemon.seats, self.provider
        if kind == "seat-create":
            seat_id = seats.create(provider, data.get("label"))
            result = {"success": True, "seat_id": seat_id}
        elif kind == "seat-login-start":
            seat_id = seats.require(provider, data.get("seat_id"))
            result = self.daemon.seat_logins.start(
                provider, seat_id, seats.env(provider, seat_id), self.home
            )
        elif kind == "seat-login-complete":
            seat_id = seats.require(provider, data.get("seat_id"))
            result = self.daemon.seat_logins.complete(
                str(data.get("session_id", "")), provider, seat_id, data.get("code")
            )
        elif kind == "seat-activate":
            seat_id = seats.require(provider, data.get("seat_id"))
            if seat_id == seats.active(provider):
                return {
                    "success": True,
                    "message": "已是当前席位",
                    "seats": self.daemon.seat_states(provider),
                }
            if cli_status(provider, seats.env(provider, seat_id))["login"] != "ready":
                raise OperationError("该席位未登录，请先登录")
            with self.daemon.driver_lock:
                seats.activate(provider, seat_id)
                for key in ("plan_type", "account_verified"):
                    self.daemon.capabilities[provider].pop(key, None)
                self.daemon.rotate_driver(provider)
            with self.background_lock:
                self.mcp_logins = None  # MCP 授权跟着 CLI 的当前席位走
            result = {"success": True, "message": "已切换"}
        else:
            seats.remove(provider, seats.require(provider, data.get("seat_id")))
            result = {"success": True, "message": "已移除"}
        # 结果带上最新的席位状态，管理台不必等下一次心跳。
        self.daemon.capabilities[provider]["seats"] = self.daemon.seat_states(provider)
        return {**result, "seats": self.daemon.capabilities[provider]["seats"]}

    def claude_limits(self, socket_path: Path | None = None) -> dict | None:
        path = socket_path or self.daemon.socket_path(self.provider)
        with httpx.Client(
            transport=httpx.HTTPTransport(uds=str(path)),
            base_url="http://runtime",
            timeout=10,
        ) as client:
            try:
                response = client.get("/v1/stats")
                response.raise_for_status()
                data = response.json().get("rate_limits")
            except (httpx.HTTPError, ValueError, AttributeError) as exc:
                raise OperationError("读取驱动额度失败") from exc
        return data if isinstance(data, dict) else None

    def health_check(self) -> None:
        start = time.monotonic()
        status, detail = "unknown", ""
        kind, verified, actual_model = "driver", False, None
        driver_status = None
        model = self.probe_model or self.model
        with self.daemon.driver_lock:
            generation = self.daemon.driver_gen[self.provider]
            seat_id = self.daemon.driver_seats.get(
                (self.provider, generation), self.daemon.seats.active(self.provider)
            )
            socket_path = self.daemon.socket_path(self.provider, generation)
        try:
            with httpx.Client(
                transport=httpx.HTTPTransport(uds=str(socket_path)),
                base_url="http://runtime",
                timeout=90,
            ) as client:
                health = client.get("/health")
                if health.status_code == 200:
                    driver_status, status = "healthy", "healthy"
                if health.status_code != 200:
                    status, detail = "down", f"驱动 /health 返回 HTTP {health.status_code}"
                elif model:
                    kind = "model"
                    # Probe stays under the selected project root and cannot execute tools.
                    directory = self.root / ".coreman-health"
                    directory.mkdir(exist_ok=True)
                    probe = {
                        "stream": False,
                        "session_id": "",
                        "working_dir": str(directory),
                        "max_turns": 1,
                        "permission_mode": "plan" if self.provider == "claude" else "read-only",
                        "messages": [
                            {"role": "user", "content": "Reply OK. Do not use any tools."}
                        ],
                    }
                    # Claude 用驱动的默认模型：模型列表第一个未必是账号能直接用的
                    # （如需要额外付费的模型），检查失败就拿不到额度。
                    if self.provider != "claude":
                        probe["model"] = model
                    response = client.post("/v1/chat/completions", json=probe)
                    body = response.json()
                    error = probe_error(response.status_code, body)
                    if not error:
                        try:
                            content = body["choices"][0]["message"]["content"]
                            # Tolerate presentation punctuation/case, but never
                            # a sentence, extra words, or Unicode lookalikes.
                            normalized = (
                                content.strip()
                                if isinstance(content, str) and len(content) <= 128
                                else ""
                            )
                            while normalized and unicodedata.category(normalized[0]).startswith(
                                "P"
                            ):
                                normalized = normalized[1:].lstrip()
                            while normalized and unicodedata.category(normalized[-1]).startswith(
                                "P"
                            ):
                                normalized = normalized[:-1].rstrip()
                            verified = (
                                normalized.isascii() and normalized.strip().casefold() == "ok"
                            )
                        except (KeyError, IndexError, TypeError):
                            verified = False
                        status, detail = (
                            ("healthy", "")
                            if verified
                            else (
                                "unknown",
                                "模型回复未通过探活校验",
                            )
                        )
                        candidate = body.get("x_actual_model") if isinstance(body, dict) else None
                        if isinstance(candidate, str) and re.fullmatch(
                            r"[A-Za-z0-9._:/-]{1,150}", candidate
                        ):
                            actual_model = candidate
                    elif response.status_code in {401, 403} or any(
                        x in error.lower() for x in AUTH_FAILURE_MARKERS
                    ):
                        status, detail = "auth_fail", error
                    else:
                        status, detail = "down", error
        except httpx.TimeoutException:
            status, detail = "timeout", "检查请求超时"
        except (httpx.HTTPError, OSError, ValueError) as exc:
            status, detail = "down", f"{type(exc).__name__}: {exc}"
        if self.daemon.driver_gen[self.provider] != generation:
            return
        self.report(
            "relay/health",
            {
                "status": status,
                "detail": detail[:255] or None,
                "latency_ms": int((time.monotonic() - start) * 1000),
                "check_kind": kind,
                "requested_model": (model or None) if self.provider != "claude" else None,
                "model": actual_model,
                "reply_verified": verified,
                "seat_id": seat_id,
                "driver_status": driver_status,
            },
        )
        if self.daemon.driver_gen[self.provider] == generation:
            self.last_model_check = time.time()

    def probe_rate_limits(self) -> None:
        with self.daemon.driver_lock:
            generation = self.daemon.driver_gen[self.provider]
            seat_id = self.daemon.driver_seats.get(
                (self.provider, generation), self.daemon.seats.active(self.provider)
            )
            socket_path = self.daemon.socket_path(self.provider, generation)

        def read_limits() -> dict | None:
            if self.provider == "codex":
                return self.codex_limits(env=self.daemon.seats.env(self.provider, seat_id))
            return self.claude_limits(socket_path)

        cap = self.daemon.capabilities.get(self.provider) or {}
        context = {
            "seat_id": seat_id,
            **{
                key: cap[key]
                for key in ("plan_type", "account_verified")
                if cap.get(key) is not None
            },
        }
        self._probe_rate_limits(
            context,
            read_limits,
            generation=generation,
            is_current=lambda: self.daemon.driver_gen[self.provider] == generation,
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
        self.config_path = config_path
        self.config = json.loads(config_path.read_text())
        self.data_dir = config_path.parent
        self.drivers: dict[str, subprocess.Popen] = {}
        # 换席位时起新驱动接新对话；旧驱动等它手上的对话结束再停（见 rotate_driver）。
        self.driver_gen = {p: 0 for p in PROVIDERS}
        self.driver_seats: dict[tuple[str, int], str] = {}
        self.retired: list[tuple[str, int, subprocess.Popen]] = []
        self.driver_lock = threading.RLock()
        self.seats = Seats(
            self.data_dir, self.config, self.save, Path(self.config.get("home", str(Path.home())))
        )
        self.seat_logins = SeatLogins()
        self.agents: dict[str, RuntimeAgent] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.task_info: dict[str, dict] = {}
        self.capabilities = {p: {"installed": False} for p in PROVIDERS}
        self.operations_lock = threading.Lock()
        self.stopping = asyncio.Event()
        self.http = None
        self.lock_file = None
        self.control_deadline = float("inf")
        # 轮询连不上平台的起始时刻与最近一次原因（只记类型或状态码），控制租约过期时写进日志。
        self.poll_failing_since: float | None = None
        self.poll_error = ""
        self.lease_lost = False
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
        # 生效中的出站代理（prepare 时确定，重启才会变），供心跳上报与管理台展示。
        self.proxy: dict[str, str | bool] = {"source": "none", "url": "", "pending": False}
        # 个人助理的轮次（沙箱、转录、自检）：与上面这些普通轮次的状态完全分开。
        self.assistant = AssistantTurns(self)
        # 助理专用实例：只声明沙箱能力的就是它，它也只跑沙箱里的轮次与名单上的节点命令。
        self.assistant_only = self.config.get("assistant_only") is True
        try:
            self.machine = machine_fingerprint()
        except Exception:  # noqa: BLE001 助理侧的信息读不到不能拦住守护进程启动
            self.machine = ""

    def socket_path(self, provider: str, gen: int | None = None) -> Path:
        # Keep under Unix's 104-byte sockaddr limit, including long macOS home paths.
        gen = self.driver_gen[provider] if gen is None else gen
        return self.socket_dir / (provider + (f"-{gen}" if gen else "") + ".sock")

    def save(self) -> None:
        atomic_write(self.config_path, json.dumps(self.config, indent=2), durable=True)

    def apply_proxy(self) -> None:
        """Decide the outbound proxy once, before any driver inherits the environment.

        The configured proxy wins and overwrites the four variables; without one the
        service environment's own proxy is kept and only reported. Everything the CLIs
        do inherits this environment, so the choice cannot change without a restart.
        """
        configured = str(self.config.get("proxy") or "").strip()
        if configured:
            for key in PROXY_ENV_KEYS:
                os.environ[key] = configured
            shown = redact_proxy_url(configured)
            self.proxy = {"source": "coreman", "url": shown, "pending": False}
            return
        values = (os.environ.get(key, "").strip() for key in PROXY_ENV_KEYS)
        inherited = next((value for value in values if value), "")
        self.proxy = {
            "source": "environment" if inherited else "none",
            "url": redact_proxy_url(inherited),
            "pending": False,
        }

    def proxy_state(self) -> dict[str, str | bool]:
        """The applied proxy, flagged as pending when config.json has been edited since."""
        configured = str(self.config.get("proxy") or "").strip()
        with contextlib.suppress(OSError, ValueError):
            on_disk = str(json.loads(self.config_path.read_text()).get("proxy") or "").strip()
            return {**self.proxy, "pending": on_disk != configured}
        return dict(self.proxy)

    def apply_root_change(self, change: dict | None) -> None:
        """Durable acknowledgement: retries/restarts never reapply a completed request.

        Called on the event loop without awaits, so poll cannot start work between
        the idle check and replacing the agents' root. No existing files are moved.
        """
        if not change or change.get("status") != "pending":
            return
        if self.config.get("root_change_result", {}).get("id") == change.get("id"):
            return
        if any(not task.done() for task in self.tasks.values()):
            return
        if not self.operations_lock.acquire(blocking=False):
            return
        previous = self.config.copy()
        try:
            result = {"id": change["id"], "path": change["path"], "status": "applied"}
            try:
                root = Path(change["path"])
                if not root.is_absolute() or root == Path("/") or ".." in root.parts:
                    raise ValueError("invalid root")
                if root.resolve() != root:
                    raise ValueError("symlink root")
                root.mkdir(parents=True, exist_ok=True)
                import tempfile

                with tempfile.TemporaryFile(dir=root):
                    pass
            except (OSError, ValueError, RuntimeError):
                result["status"] = "failed"
            else:
                self.config["workspace_root"] = str(root)
            self.config["root_change_result"] = result
            try:
                self.save()
            except OSError:
                self.config = previous
                return  # no acknowledgement until the configuration is durable
            if result["status"] == "applied":
                for agent in self.agents.values():
                    agent.root = root
        finally:
            self.operations_lock.release()

    def prepare(self) -> None:
        self.lock_file = (self.data_dir / "daemon.lock").open("a")
        fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # 在任何平台调用之前：令牌被吊销、每次启动都在登记处退出的节点也要清掉助理的残留。
        self.start_leftover_cleanup()
        if self.config.get("release"):
            # Releases switched by an installer that predates the command still get one.
            try:
                write_manage_command(self.config_path, Path(self.config["release"]))
            except OSError as exc:
                LOG.warning("无法写入本机管理命令：%s", exc)
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
        self.apply_proxy()
        for provider in PROVIDERS:
            self.seats.apply_env(provider)

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
                if not self.lease_lost:
                    self.lease_lost = True
                    LOG.warning(
                        "Control lease lost: no successful poll for %.0fs (last error: %s); "
                        "cancelling %d running request(s)",
                        time.monotonic() - self.control_deadline + CONTROL_LEASE_SECONDS,
                        self.poll_error or "poll pending",
                        len(self.tasks),
                    )
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
        with self.driver_lock:
            self._start_driver(provider)

    def _start_driver(self, provider: str) -> None:
        self.driver_seats[(provider, self.driver_gen[provider])] = self.seats.active(provider)
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

    def rotate_driver(self, provider: str) -> None:
        """换席位：新驱动（继承新席位的环境）在新 socket 上接新请求，在途请求留在旧驱动上跑完。"""
        with self.driver_lock:
            process = self.drivers.pop(provider, None)
            if process is not None and process.poll() is None:
                self.retired.append((provider, self.driver_gen[provider], process))
            self.driver_gen[provider] += 1
            if provider in self.agents:
                self.agents[provider].last_model_check = 0
            self._start_driver(provider)
        self.socket_failures[provider] = 0
        self.reap_retired()

    def reap_retired(self) -> None:
        """停掉已经没有在途请求的旧驱动。"""
        with self.driver_lock:
            busy = {
                (info["provider"], info.get("driver_gen")) for info in list(self.task_info.values())
            }
            keep = []
            for provider, gen, process in self.retired:
                if (provider, gen) in busy and process.poll() is None:
                    keep.append((provider, gen, process))
                    continue
                if process.poll() is None:
                    process.terminate()
                    threading.Thread(
                        target=self.finish_process, args=(process,), daemon=True
                    ).start()
                self.socket_path(provider, gen).unlink(missing_ok=True)
            self.retired = keep

    @staticmethod
    def finish_process(process: subprocess.Popen, timeout: float = 15) -> None:
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def seat_states(self, provider: str) -> list[dict]:
        """各席位的账号与登录状态；当前席位直接用发现时查到的结果。"""
        cap, active = self.capabilities[provider], self.seats.active(provider)
        states = []
        for seat_id in self.seats.ids(provider):
            if seat_id == active:
                status = {"account": cap.get("account", ""), "login": cap.get("login", "unknown")}
            else:
                status = cli_status(provider, self.seats.env(provider, seat_id))
            states.append(
                {
                    "id": seat_id,
                    "label": self.seats.label(provider, seat_id),
                    "account": status.get("account", ""),
                    "login": status.get("login", "unknown"),
                    "active": seat_id == active,
                }
            )
        return states

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
            metadata_seat = self.seats.active(provider)
            cap = await asyncio.to_thread(cli_status, provider)
            # 守护进程自己的能力，不看驱动：记忆在工作目录的 .coreman-memory/，两种后端共用。
            cap["memory_layout_v1"] = True
            # 守护进程自检的结果：只有声明了它的节点才会收到个人助理的轮次。
            try:
                cap[SANDBOX_CAPABILITY] = self.assistant.capability(provider) is True
            except Exception:  # noqa: BLE001 自检结果出错按不支持处理，不影响普通轮次
                cap[SANDBOX_CAPABILITY] = False
            if cap["installed"]:
                try:
                    await self.heal_driver(provider)
                    self.start_driver(provider)
                    async with httpx.AsyncClient(
                        transport=httpx.AsyncHTTPTransport(uds=str(self.socket_path(provider))),
                        base_url="http://runtime",
                        timeout=10,
                    ) as client:
                        for attempt in range(DRIVER_CONNECT_ATTEMPTS):
                            try:
                                response = await client.get("/v1/models")
                                break
                            except httpx.ConnectError:
                                if attempt == DRIVER_CONNECT_ATTEMPTS - 1:
                                    raise
                                await asyncio.sleep(DRIVER_CONNECT_RETRY_SECONDS)
                        response.raise_for_status()
                        cap["models"] = [r["id"] for r in response.json()["data"]]
                        if provider == "claude":
                            health = await client.get("/health")
                            health.raise_for_status()
                            declared = health.json().get("capabilities", {})
                        else:
                            # Codex 以前不读 /health：读不到只是不声明能力，不影响可用性。
                            declared = await optional_capabilities(client)
                        for flag in DRIVER_CAPABILITIES[provider]:
                            cap[flag] = isinstance(declared, dict) and declared.get(flag) is True
                    self.socket_failures[provider] = 0
                except httpx.ConnectError:
                    self.socket_failures[provider] = self.socket_failures.get(provider, 0) + 1
                    cap["detail"] = "AI API 启动中或不可用"
                except (httpx.HTTPError, OSError, ValueError):
                    cap["detail"] = "AI API 启动中或不可用"
                # 注册之后才支持的后端（如 OpenCode）要等服务端在心跳里补建 relay。
                if provider not in self.agents and provider in self.config["backends"]:
                    agent = RuntimeAgent(
                        self, provider, self.config["backends"][provider]["relay_id"]
                    )
                    self.agents[provider] = agent
                    agent.model = (cap["models"] or [""])[0]
                    if self.config.get("schedules", True):
                        agent.start_schedules()
                if provider in self.agents:
                    self.agents[provider].model = (cap["models"] or [""])[0]
            if self.seats.active(provider) != metadata_seat:
                for key in ("plan_type", "account_verified"):
                    cap.pop(key, None)
            self.capabilities[provider] = cap
            if cap["installed"]:
                cap["seat_switch_v1"] = True
                cap["seats"] = await asyncio.to_thread(self.seat_states, provider)

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
        if self.assistant_only:
            refused = assistant_only_refusal(
                method, path, data, Path(self.config["workspace_root"])
            )
            if refused is not None:
                raise AssistantOnlyRefused(refused)
        if path == "/v1/chat/completions":
            directory = self.agents[provider].workspace(data.get("working_dir", ""))
            directory.mkdir(parents=True, exist_ok=True)
            data["working_dir"] = str(directory)
            if not isinstance(data.get("worktree", False), bool):
                raise ValueError("worktree 必须是布尔值")
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
            if not isinstance(env, dict):
                raise ValueError("env_vars 必须是对象")
            blocked = blocked_env_keys(env)
            if blocked:
                # Only key names: values may carry the bot's secrets.
                raise ValueError(
                    "请求包含运行时控制类环境变量（模型端点、代码加载、凭据或运行时身份），"
                    f"已拒绝执行：{', '.join(blocked)}；请在机器人或技能环境配置中删除"
                )
        return data

    async def attach_memory(self, provider: str, data: dict) -> None:
        """聊天前确保员工的记忆布局（首次会把 Claude 原来的记忆迁过来），Claude 再用
        autoMemoryDirectory 指向它。在换成开发模式 worktree 之前做，各 worktree 共用主目录这份。
        布局异常时不拦聊天，Claude 退回原来的记忆位置。"""
        agent = self.agents[provider]
        try:
            directory = await asyncio.to_thread(agent.ensure_memory_layout, data["working_dir"])
        except (OperationError, OSError) as exc:
            LOG.warning("memory layout unavailable: %s", operation_message(exc))
            return
        if provider != "claude":
            return
        settings = json.loads(data["settings"]) if data.get("settings") else {}
        if not isinstance(settings, dict):
            raise ValueError("settings 必须是 JSON 对象")
        data["settings"] = json.dumps({**settings, "autoMemoryDirectory": str(directory)})

    async def execute(self, command: dict) -> None:
        identity, provider = command["id"], command["provider"]
        frames = FrameBatcher(lambda batch: self.send_frames(identity, batch))
        operation_name = ""

        try:
            data = self.validate_command(command)
            if command["path"] == "/v1/chat/completions" and "sandbox" in data:
                # 个人助理的一轮：在沙箱里另起驱动（见 assistant_turns），帧与收尾都在里面发。
                await self.assistant.run(identity, provider, data, frames)
                return
            if command["path"] == "/v1/chat/completions":
                await self.attach_memory(provider, data)
            if command["path"] == "/v1/chat/completions" and data.pop("worktree", False):
                # 开发模式：工作目录是 Git 仓库时换成本会话的 worktree（建时要拉远端，放线程里）。
                data["working_dir"] = str(
                    await asyncio.to_thread(
                        worktree.ensure, Path(data["working_dir"]), data.get("session_id", "")
                    )
                )
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
            gen = self.driver_gen[provider]
            if command["path"] != "/":
                # 经驱动的请求记下驱动代次：换席位后旧驱动等这些请求结束才停。
                self.task_info[identity]["driver_gen"] = gen
            if command["path"] == "/":
                operation_name = str(data.get("type"))[:40]
                if data.get("type") in {"status", "ping"}:
                    self.task_info.pop(identity, None)
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
                except OperationError as exc:
                    # Fixed prompts only (at most an exit code, HTTP status or count), never
                    # command output or credentials: safe to log and to hand back. Older
                    # platforms read any unsuccessful result as a failed operation.
                    message = operation_message(exc)
                    LOG.warning("Operation %s failed: %s", operation_name, message)
                    result = {"success": False, "code": "operation_failed", "message": message}
                finally:
                    COMMAND_CANCEL.reset(token)
                await frames.add(json.dumps(result).encode(), status_code=200)
            else:
                async with httpx.AsyncClient(
                    transport=httpx.AsyncHTTPTransport(uds=str(self.socket_path(provider, gen))),
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
                        if (
                            command["path"] == "/v1/chat/completions"
                            and data.get("stream")
                            and response.status_code == 200
                        ):
                            seat = self.driver_seats.get(
                                (provider, gen), self.seats.active(provider)
                            )
                            await frames.add(
                                (
                                    "data: "
                                    + json.dumps({"x_runtime": {"seat_id": seat}, "choices": []})
                                    + "\n\n"
                                ).encode()
                            )
                        async for part in response.aiter_bytes():
                            await frames.add(part)
            await frames.finish()
        except asyncio.CancelledError:
            # Closing the local stream reaches the Go driver's disconnect watcher.
            frames.discard()
            raise
        except AssistantOnlyRefused as exc:
            LOG.warning("Request refused on an assistant-only node: %s", command["path"][:40])
            body = {"error": {"code": exc.code, "message": "本节点只运行沙箱里的个人助理轮次"}}
            with contextlib.suppress(Exception):
                await frames.add(
                    json.dumps(body, ensure_ascii=False).encode(),
                    status_code=403,
                    content_type="application/json",
                )
                await frames.finish()
        except Exception as exc:
            # Other exception types may carry paths or response text: log the type only.
            reason = type(exc).__name__
            if isinstance(exc, OperationError):
                reason += ": " + operation_message(exc)
            if operation_name:
                reason = f"{operation_name}: {reason}"
            LOG.warning("Request failed: %s", reason)
            with contextlib.suppress(Exception):
                await frames.finish(error="execution_failed")
        finally:
            self.task_info.pop(identity, None)
            self.slots_freed.set()
            if self.retired:
                self.reap_retired()

    @property
    def max_concurrent(self) -> int:
        return int(self.config.get("max_concurrent", 10))

    @property
    def git_hosts(self) -> list[str]:
        """Hosts the Agent may use for Git; changed in config.json, applied on restart."""
        return list(self.config.get("git_hosts", ["github.com"]))

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
            # Read-only in the console, so a rejected Git source can be explained there.
            "git_hosts": self.git_hosts,
            # Which proxy the CLIs inherit, so the console can warn when there is none.
            "proxy": self.proxy_state(),
            "root_edit_supported": True,
            "root_change_result": self.config.get("root_change_result"),
            # 同一台机器上的节点靠这两项区分系统用户（个人助理的助理池准入用）。
            "machine": self.machine,
            "uid": os.getuid(),
        }

    def add_backends(self, backends: object) -> None:
        """服务端为节点补建的 relay（注册后才装的后端）记进 config；下一轮发现时启用。"""
        if not isinstance(backends, dict):
            return
        added = {
            provider: {"relay_id": str(item["relay_id"])}
            for provider, item in backends.items()
            if provider in PROVIDERS
            and provider not in self.config["backends"]
            and isinstance(item, dict)
            and item.get("relay_id")
        }
        if added:
            self.config["backends"].update(added)
            self.save()

    def apply_probe_models(self, models: object) -> None:
        """服务端给的健康检查模型（模型目录的默认模型）；旧服务端不下发时沿用驱动发现的第一个。"""
        if not isinstance(models, dict):
            return
        for provider, agent in self.agents.items():
            model = models.get(provider)
            agent.probe_model = model if isinstance(model, str) else ""

    def apply_model_activity(self, activity: object) -> None:
        if not isinstance(activity, dict):
            return
        from datetime import datetime

        for provider, value in activity.items():
            agent = self.agents.get(provider)
            if (
                agent is None
                or not isinstance(value, dict)
                or value.get("seat_id") != self.seats.active(provider)
            ):
                continue
            try:
                checked = datetime.fromisoformat(
                    str(value.get("observed_at")).replace("Z", "+00:00")
                ).timestamp()
            except (ValueError, TypeError):
                continue
            if checked <= time.time() + 30:
                agent.last_model_check = max(getattr(agent, "last_model_check", 0), checked)

    async def heartbeat_loop(self) -> None:
        while not self.stopping.is_set():
            delay = HEARTBEAT_SECONDS
            try:
                result = await self.api("/api/runtime/heartbeat", self.heartbeat_body())
                self.apply_root_change(result.get("root_change"))
                self.add_backends(result.get("backends"))
                self.apply_probe_models(result.get("probe_models"))
                self.apply_model_activity(result.get("model_activity"))
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
        last_success = self.control_deadline - CONTROL_LEASE_SECONDS
        self.control_deadline = time.monotonic() + CONTROL_LEASE_SECONDS
        if self.poll_failing_since is not None or self.lease_lost:
            LOG.warning(
                "Control poll recovered: %.0fs since the previous successful poll (last error: %s)",
                time.monotonic() - last_success,
                self.poll_error or "poll pending",
            )
        self.poll_failing_since, self.poll_error, self.lease_lost = None, "", False
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

    def note_poll_failure(self, reason: str) -> None:
        """记下轮询失败的原因：只在连续失败的第一次写日志，恢复时再写一条。"""
        if self.poll_failing_since is None:
            self.poll_failing_since = time.monotonic()
            LOG.warning("Control poll failed: %s", reason)
        self.poll_error = reason

    def start_leftover_cleanup(self) -> None:
        """普通实例每次启动清一次助理的残留（prepare 拿到数据目录锁后立即调用），在后台线程里做。"""
        if not self.assistant_only:
            # 从助理专用实例改回普通实例的节点：本轮目录里可能还有没传上去的明文转录，回收站里
            # 可能还有没删完的助理工作目录。
            threading.Thread(target=self.clear_assistant_leftovers, daemon=True).start()

    def start_assistant(self) -> None:
        if not self.assistant_only:
            return
        try:
            self.assistant.start()
        except Exception as exc:  # noqa: BLE001 助理侧起不来，普通轮次照常
            LOG.warning("Assistant sandbox start failed: %s", type(exc).__name__)

    def clear_assistant_leftovers(self) -> None:
        """普通实例启动时删掉助理的本轮目录，再清掉回收站里助理专用实例留下的条目（都不跟随链接）。
        在线程里跑，失败只记一笔。"""
        try:
            turns = self.assistant.turns_dir
            if turns.is_symlink():
                turns.unlink()
            elif turns.is_dir():
                LOG.warning("Removing assistant turn leftovers")
                remove_tree(turns)
        except Exception as exc:  # noqa: BLE001 清不掉不能影响普通轮次
            LOG.warning("Assistant leftovers cleanup failed: %s", type(exc).__name__)
        try:
            self.clear_removed_workspaces()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Assistant trash cleanup failed: %s", type(exc).__name__)

    def clear_removed_workspaces(self) -> None:
        """普通实例只删回收站里 Workspace.remove 写进去的条目（名字是 32 位小写十六进制）。

        普通实例上 `.coreman-trash` 不是保留名，可能就是某个员工的工作目录：不改它的权限、不建它、
        带所有权标记就整个不碰、其中名字对不上的东西不碰。它是链接（或不是目录）时也不碰——
        那可能是用户自己的链接，不能当守护进程的东西去删。
        """
        root = Path(str(self.config.get("workspace_root") or ""))
        if not root.is_absolute():
            return
        trash = root / TRASH_DIR
        try:
            if not stat.S_ISDIR(os.lstat(trash).st_mode):
                return
        except FileNotFoundError:
            return
        if os.path.lexists(trash / MARKER):
            return
        with os.scandir(trash) as entries:
            found = [e for e in entries if TRASH_ENTRY_NAME.fullmatch(e.name)]
        for entry in found:
            try:
                if entry.is_dir(follow_symlinks=False):
                    remove_tree(Path(entry.path))
                else:
                    os.unlink(entry.path)
            except OSError as exc:
                LOG.warning("Assistant trash entry failed: %s", type(exc).__name__)

    async def stop_assistant(self) -> None:
        try:
            await self.assistant.stop()
        except Exception as exc:  # noqa: BLE001 不能挡住后面停驱动的收尾
            LOG.warning("Assistant sandbox stop failed: %s", type(exc).__name__)

    async def cancel_all(self) -> None:
        current, self.tasks = self.tasks, {}
        for task in current.values():
            task.cancel()
        await asyncio.gather(*current.values(), return_exceptions=True)

    async def stop_work(self) -> None:
        """关停时停下所有工作：取消在跑的请求，然后助理侧的收尾与普通驱动的终止同时进行——
        助理侧等多久（或失败）都不推迟、不跳过普通驱动的终止。"""
        await self.cancel_all()
        assistant = asyncio.ensure_future(self.stop_assistant())
        try:
            if hasattr(self, "discovery_task"):
                self.discovery_task.cancel()
                await asyncio.gather(self.discovery_task, return_exceptions=True)
            for agent in self.agents.values():
                agent.stop.set()
            for proc in [*self.drivers.values(), *(p for _, _, p in self.retired)]:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        await asyncio.to_thread(proc.wait, timeout=15)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        await asyncio.to_thread(proc.wait)
        finally:
            await assistant

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
            self.start_assistant()
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
                        self.note_poll_failure(f"HTTP {exc.response.status_code}")
                        # The independent lease watchdog owns cancellation.
                        if exc.response.status_code in REJECTED_STATUSES:
                            rejected_backoff = min(
                                max(rejected_backoff * 2, 2.0), REJECTED_BACKOFF_MAX
                            )
                            await self.pause(rejected_backoff)
                        else:
                            await self.pause(2)
                        continue
                    except (httpx.HTTPError, KeyError, ValueError) as exc:
                        self.note_poll_failure(type(exc).__name__)
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
                await self.stop_work()
                self.log_rotator.stop()
                for provider in PROVIDERS:
                    self.socket_path(provider).unlink(missing_ok=True)
                for provider, gen, _ in self.retired:
                    self.socket_path(provider, gen).unlink(missing_ok=True)


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
