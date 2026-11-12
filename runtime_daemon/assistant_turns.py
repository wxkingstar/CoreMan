"""个人助理的轮次：每一轮在沙箱里另起一个驱动，结束后上传转录、清空本轮目录。

平台在请求体里带 `sandbox: true` 与 `transcript: {"transfer": <32 位十六进制>, "restore": <布尔>}`。
这里的一切都与普通员工的轮次分开：不用常驻驱动、不占驱动锁、不改守护进程的环境；本模块的
任何失败只结束这一轮（或只影响下一次清扫），不会传到普通轮次。

一轮的顺序：
1. 本节点自检没通过、并发已满、拿不到模型凭证、转录取不下来：先告诉平台转录没动（`unchanged`，
   上一份照旧可续），再回 503 与原因代码，什么都不跑；
2. 建本轮目录，写 meta.json（清扫时靠它认出这是哪一轮），续聊时把转录还原进来；
3. 起本轮的放行代理，在 bubblewrap 里起驱动（sandbox.py），转发请求与流；
4. 流里最后的 `data: [DONE]` 先扣住：沙箱停掉、转录上传完，再放出去。平台见到 [DONE] 才收尾，
   所以下一轮开始时上一轮的转录一定已经落库（或已确定传不上去）；
5. 上传成功就删掉本轮目录；没成功的留给下一次清扫——清扫再传一次，然后无论如何都删除。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from runtime_daemon import egress, sandbox, transcripts, turn_credentials

LOG = logging.getLogger("coreman-runtime")
DEFAULT_LIMIT = 8
SELF_TEST_SECONDS = 1800.0
SWEEP_SECONDS = 600.0
STOP_SECONDS = 10.0
SOCKET_WAIT_SECONDS = 10.0
TRANSFER_TIMEOUT = 180.0
DONE = b"data: [DONE]"
TRANSFER_ID = re.compile(r"[0-9a-f]{32}")
# 守护进程启动时定下的 AI 代理（daemon.apply_proxy 写进了环境）：模型接口经它出去。
PROXY_ENV_KEYS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
# 拒绝时回给平台的说明（只给运维看；助理的所有者看到的是平台的固定文案）。
MESSAGES = {
    "sandbox_unavailable": "本节点不能在沙箱里运行个人助理",
    "sandbox_busy": "本节点的个人助理轮次已达并发上限",
    "sandbox_start_failed": "沙箱启动失败",
    "unsafe_mount": "只读挂载会把本轮目录、socket 目录或工作目录带进沙箱",
    "credential_missing": "节点上没有这种 AI 的登录",
    "credential_unreadable": "节点上的登录文件读不懂",
    "credential_expiring": "节点上的登录令牌即将到期，等普通轮次刷新后再试",
    "transcript_unavailable": "取不到这个会话的转录",
}


def ai_proxy() -> str | None:
    return next((os.environ[k] for k in PROXY_ENV_KEYS if os.environ.get(k, "").strip()), None)


def _is_socket(path: Path) -> bool:
    """本轮 socket 目录在沙箱里可写：只认真正的 socket，不跟随里面的软链。

    驱动的 socket 在连上之前都先这样核对；连接发生在 CLI 启动之前（驱动收到请求才起 CLI），
    那时沙箱里只有发布目录里的驱动与转发口在跑，没有能换掉它的代码。
    """
    try:
        return stat.S_ISSOCK(os.lstat(path).st_mode)
    except OSError:
        return False


class Refused(Exception):
    """这一轮不跑；`code` 回给平台。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class TurnPlan:
    provider: str
    workdir: Path
    body: dict[str, Any]
    transfer: str
    restore: bool
    session_id: str


class DoneGate:
    """转发流里的字节，扣住 `data: [DONE]` 及其之后的部分，等 `release()` 再交出去。"""

    def __init__(self) -> None:
        self.pending = b""
        self.held = False

    def feed(self, part: bytes) -> bytes:
        if self.held:
            self.pending += part
            return b""
        self.pending += part
        index = self.pending.find(DONE)
        if index >= 0:
            out, self.pending, self.held = self.pending[:index], self.pending[index:], True
            return out
        # 标记可能跨两段到达：末尾留一小截，等下一段再判断。
        keep = len(DONE) - 1
        out, self.pending = self.pending[:-keep], self.pending[-keep:]
        return out

    def release(self) -> bytes:
        out, self.pending = self.pending, b""
        return out


def plan_turn(provider: str, data: dict[str, Any]) -> TurnPlan:
    """检查请求并改写成沙箱里的样子：工作目录换成 /work，去掉只给守护进程看的字段。

    Raises:
        ValueError: 请求不合规（按普通的请求失败处理）
    """
    if data.get("sandbox") is not True:
        raise ValueError("sandbox 只能是 true")
    transcript = data.get("transcript")
    if (
        not isinstance(transcript, dict)
        or not TRANSFER_ID.fullmatch(str(transcript.get("transfer", "")))
        or not isinstance(transcript.get("restore"), bool)
    ):
        raise ValueError("transcript 无效")
    if data.get("worktree") or data.get("add_dirs"):
        raise ValueError("个人助理的轮次不支持 worktree 与 add_dirs")
    workdir = Path(data["working_dir"])
    body = {k: v for k, v in data.items() if k not in ("sandbox", "transcript", "worktree")}
    body["working_dir"] = sandbox.INSIDE_WORK
    if body.get("system_prompt_file"):
        path = Path(body["system_prompt_file"]).resolve()
        if not path.is_relative_to(workdir):
            raise ValueError("system_prompt_file 必须在工作目录里")
        body["system_prompt_file"] = f"{sandbox.INSIDE_WORK}/{path.relative_to(workdir).as_posix()}"
    if provider == "claude":
        # 查外部资料走平台的工具：CLI 自带的联网搜索与抓取一律关掉。
        settings = json.loads(body["settings"]) if body.get("settings") else {}
        if not isinstance(settings, dict):
            raise ValueError("settings 必须是 JSON 对象")
        permissions = settings.setdefault("permissions", {})
        if not isinstance(permissions, dict):
            raise ValueError("settings.permissions 必须是对象")
        deny = [*(permissions.get("deny") or []), "WebSearch", "WebFetch"]
        permissions["deny"] = list(dict.fromkeys(deny))
        body["settings"] = json.dumps(settings)
    return TurnPlan(
        provider,
        workdir,
        body,
        str(transcript["transfer"]),
        bool(transcript["restore"]),
        str(data.get("session_id") or ""),
    )


class AssistantTurns:
    """守护进程上的个人助理轮次：能力自检、并发、沙箱里的一轮、转录清扫。"""

    def __init__(self, daemon: Any) -> None:
        self.daemon = daemon
        self.ready: dict[str, bool] = dict.fromkeys(sandbox.PROVIDERS, False)
        self.code = "not_tested"
        self.active: set[str] = set()
        self.loop_task: asyncio.Task | None = None
        self.background: set[asyncio.Task] = set()

    # ---- 配置 ------------------------------------------------------------------

    @property
    def turns_dir(self) -> Path:
        return self.daemon.data_dir / "assistant-turns"

    @property
    def limit(self) -> int:
        return int(self.daemon.config.get("assistant_max_concurrent", DEFAULT_LIMIT))

    @property
    def release(self) -> Path:
        return Path(self.daemon.config.get("release", str(Path(__file__).resolve().parents[1])))

    def extra_ro(self) -> list[Path]:
        paths = self.daemon.config.get("assistant_sandbox_ro_paths") or []
        return [Path(p) for p in paths if isinstance(p, str) and Path(p).is_absolute()]

    def capability(self, provider: str) -> bool:
        return self.ready.get(provider, False)

    def private_paths(self) -> list[Path]:
        """自检时必须只有本用户能进的目录：家目录、本安装的数据目录、员工工作目录的上级。"""
        return [
            Path(self.daemon.config.get("home", str(Path.home()))),
            self.daemon.data_dir,
            Path(self.daemon.config["workspace_root"]),
        ]

    def hidden_paths(self) -> list[Path]:
        """自检时必须在沙箱里看不到的路径：员工工作目录、本安装的配置、各席位的登录目录。

        自检要求每一项在主机上存在；登录目录只列出存在的（没装或没登录的 AI 没有这个目录）。
        """
        home = Path(self.daemon.config.get("home", str(Path.home())))
        logins = [home / ".claude", home / ".codex"]
        for provider, key in sandbox.CONFIG_ENV.items():
            value = self.daemon.seats.env(provider, self.daemon.seats.active(provider)).get(key)
            if value:
                logins.append(Path(value))
        return [
            Path(self.daemon.config["workspace_root"]),
            self.daemon.data_dir / "config.json",
            *(p for p in logins if p.exists()),
        ]

    # ---- 后台：自检与清扫 -----------------------------------------------------

    def start(self) -> None:
        self.loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        tasks = [t for t in (self.loop_task, *self.background) if t is not None]
        if self.background:
            # 被取消的轮次还在传转录：给它们一点时间，传不完就留给下次启动的清扫。
            await asyncio.wait(self.background, timeout=30)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _loop(self) -> None:
        await self._guard("sweep", self.sweep)
        next_test = 0.0
        while True:
            if time.monotonic() >= next_test:
                await self._guard("self_test", self.refresh_capability)
                next_test = time.monotonic() + SELF_TEST_SECONDS
            await asyncio.sleep(SWEEP_SECONDS)
            await self._guard("sweep", self.sweep)

    @staticmethod
    async def _guard(name: str, step: Any) -> None:
        try:
            await step()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 助理侧的后台工作不能拖垮守护进程
            LOG.warning("Assistant sandbox %s failed: %s", name, type(exc).__name__)

    async def refresh_capability(self) -> None:
        inputs = sandbox.SelfTestInputs(
            turns_dir=self.turns_dir,
            socket_dir=self.daemon.socket_dir,
            release=self.release,
            hidden=self.hidden_paths(),
            clis={p: sandbox.cli_mounts(p) for p in sandbox.PROVIDERS},
            private=self.private_paths(),
            extra_ro=self.extra_ro(),
        )
        result = await asyncio.to_thread(sandbox.self_test, inputs)
        if result.providers != self.ready or result.code != self.code:
            LOG.warning(
                "Assistant sandbox: %s (%s)",
                ", ".join(f"{p}={'ready' if ok else 'no'}" for p, ok in result.providers.items()),
                result.code or "ok",
            )
        self.ready, self.code = result.providers, result.code

    async def sweep(self) -> None:
        """上一轮异常退出、或传不上去留下的本轮目录：再传一次转录，然后无论如何都删除。"""
        for entry, meta in await asyncio.to_thread(self._leftovers):
            if meta is not None:
                await self._settle(entry, meta["provider"], meta["transfer"], meta["session_id"])
            await asyncio.to_thread(shutil.rmtree, entry, True)

    def _leftovers(self) -> list[tuple[Path, dict[str, str] | None]]:
        """不在跑的本轮目录及其 meta（读不懂的为 None）；顺手删掉无主的 socket 目录。"""
        found: list[tuple[Path, dict[str, str] | None]] = []
        if self.turns_dir.is_dir():
            for entry in sorted(self.turns_dir.iterdir()):
                if entry.name in self.active:
                    continue
                try:
                    raw = json.loads((entry / "meta.json").read_text())
                    meta = {k: str(raw.get(k, "")) for k in ("provider", "transfer", "session_id")}
                except (OSError, ValueError, AttributeError):
                    meta = None
                if meta is not None and (
                    meta["provider"] not in sandbox.PROVIDERS
                    or not TRANSFER_ID.fullmatch(meta["transfer"])
                ):
                    meta = None
                found.append((entry, meta))
        socket_dir = getattr(self.daemon, "socket_dir", None)
        active_runs = {f"a{turn[:10]}" for turn in self.active}
        if socket_dir is not None and socket_dir.is_dir():
            for entry in socket_dir.glob("a*"):
                if entry.name not in active_runs and entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry, ignore_errors=True)
        return found

    # ---- 与平台交换转录 --------------------------------------------------------

    async def download(self, transfer: str) -> bytes | None:
        """取续聊要用的转录；平台说没有（404）返回 None，其它失败拒绝这一轮。"""
        try:
            response = await self.daemon.http.get(
                f"/api/runtime/assistant-transcripts/{transfer}", timeout=TRANSFER_TIMEOUT
            )
        except httpx.HTTPError:
            raise Refused("transcript_unavailable") from None
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise Refused("transcript_unavailable")
        return response.content

    async def upload(self, transfer: str, data: bytes | None, status: str) -> bool:
        """上传本轮转录；平台收下（200）或已不需要（410）都算办完。"""
        try:
            response = await self.daemon.http.put(
                f"/api/runtime/assistant-transcripts/{transfer}",
                content=data or b"",
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-Transcript-Status": status,
                },
                timeout=TRANSFER_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            LOG.warning("Assistant transcript upload failed: %s", type(exc).__name__)
            return False
        if response.status_code in (200, 410):
            return True
        LOG.warning("Assistant transcript upload failed: HTTP %d", response.status_code)
        return False

    async def _settle(self, root: Path, provider: str, transfer: str, session_id: str) -> bool:
        """打包并上传一轮的转录，返回平台是否已办完。"""
        try:
            data = await asyncio.to_thread(transcripts.pack, root, provider, session_id)
            status = "ok" if data else "empty"
        except transcripts.TooLarge:
            data, status = None, "too_large"
        except OSError as exc:
            LOG.warning("Assistant transcript pack failed: %s", type(exc).__name__)
            return False
        return await self.upload(transfer, data, status)

    # ---- 一轮 ------------------------------------------------------------------

    async def refuse(self, frames: Any, code: str, transfer: str | None = None) -> None:
        """不跑这一轮。先告诉平台转录没动（续聊照旧），再回 503：平台见到 503 才会结束这一轮。"""
        LOG.warning("Assistant turn refused: %s", code)
        if transfer is not None:
            await self.upload(transfer, None, "unchanged")
        body = {"error": {"code": code, "message": MESSAGES.get(code, code)}}
        await frames.add(
            json.dumps(body, ensure_ascii=False).encode(),
            status_code=503,
            content_type="application/json",
        )
        await frames.finish()

    def _admission(self, provider: str) -> str | None:
        if provider not in sandbox.PROVIDERS or not self.capability(provider):
            return "sandbox_unavailable"
        if len(self.active) >= self.limit:
            return "sandbox_busy"
        return None

    async def run(self, identity: str, provider: str, data: dict[str, Any], frames: Any) -> None:
        """跑一轮；回给平台的帧（含收尾帧）都在这里发。"""
        plan = plan_turn(provider, data)
        refused = self._admission(provider)
        if refused is not None:
            await self.refuse(frames, refused, plan.transfer)
            return
        layout = sandbox.TurnLayout.new(self.turns_dir, self.daemon.socket_dir)
        self.active.add(layout.turn_id)
        self.daemon.task_info[identity] = {
            "request_id": identity,
            "provider": provider,
            "bot_key": "",
            "user": "",
            "chat_id": "",
            "chat_type": "",
            "started_at": time.time(),
            "working_dir": "",
        }
        process: subprocess.Popen | None = None
        proxy: egress.Egress | None = None
        settled = handed_off = False
        try:
            meta = {
                "transfer": plan.transfer,
                "provider": provider,
                "session_id": plan.session_id,
                "started_at": time.time(),
            }
            try:
                layout.create()
                layout.meta.write_text(json.dumps(meta))
                layout.meta.chmod(0o600)
            except OSError as exc:
                # 异常信息里带路径：日志只记类型名。
                LOG.warning("Assistant turn layout failed: %s", type(exc).__name__)
                raise Refused("sandbox_start_failed") from None
            seat_env = self.daemon.seats.env(provider, self.daemon.seats.active(provider))
            home = Path(self.daemon.config.get("home", str(Path.home())))
            try:
                credential = turn_credentials.for_provider(provider, seat_env, home)
            except turn_credentials.Unavailable as exc:
                raise Refused(exc.code) from None
            try:
                credential_file = turn_credentials.write(layout.secrets, credential)
            except OSError as exc:
                LOG.warning("Assistant turn credential copy failed: %s", type(exc).__name__)
                raise Refused("sandbox_start_failed") from None
            if plan.restore:
                bundle = await self.download(plan.transfer)
                if bundle:
                    try:
                        await asyncio.to_thread(
                            transcripts.unpack, layout.root, provider, plan.session_id, bundle
                        )
                    except transcripts.BadBundle:
                        LOG.warning("Assistant transcript bundle rejected")
            if not sandbox.link_cli(layout, provider):
                raise Refused("sandbox_unavailable")
            policy = egress.turn_policy(
                provider=provider,
                api_url=str(self.daemon.config["api_url"]),
                env_vars=plan.body.get("env_vars") or {},
                model_hosts=(self.daemon.config.get("assistant_egress_hosts") or {}).get(provider),
                control_proxy=self.daemon.config.get("control_proxy"),
                ai_proxy=ai_proxy(),
            )
            proxy = egress.Egress(policy)
            await proxy.start(layout.egress_socket)
            process = self._launch(layout, plan, credential, credential_file)
            await self._wait_ready(layout, process)
            gate = DoneGate()
            await self._stream(layout, plan, frames, gate)
            await self._stop(process)
            settled = await self._settle(layout.root, provider, plan.transfer, plan.session_id)
            tail = gate.release()
            if tail:
                await frames.add(tail)
            await frames.finish()
        except Refused as exc:
            settled = True
            await self.refuse(frames, exc.code, plan.transfer)
        except asyncio.CancelledError:
            # 平台取消或守护进程退出：转录放到后台去传，传完再删本轮目录。
            handed_off = True
            task = asyncio.create_task(self._settle_later(layout, plan, process, proxy))
            self.background.add(task)
            task.add_done_callback(self.background.discard)
            raise
        except Exception:
            # 驱动或流出错：照样把已有的转录传上去，下一轮还能续上。
            await self._stop(process)
            if layout.meta.exists():
                with contextlib.suppress(Exception):
                    settled = await self._settle(
                        layout.root, provider, plan.transfer, plan.session_id
                    )
            raise
        finally:
            self.daemon.task_info.pop(identity, None)
            if not handed_off:
                await self._cleanup(layout, process, proxy, settled)

    async def _settle_later(
        self,
        layout: sandbox.TurnLayout,
        plan: TurnPlan,
        process: subprocess.Popen | None,
        proxy: egress.Egress | None,
    ) -> None:
        settled = False
        try:
            await self._stop(process)
            if layout.meta.exists():
                settled = await self._settle(
                    layout.root, plan.provider, plan.transfer, plan.session_id
                )
        finally:
            await self._cleanup(layout, process, proxy, settled)

    async def _cleanup(
        self,
        layout: sandbox.TurnLayout,
        process: subprocess.Popen | None,
        proxy: egress.Egress | None,
        settled: bool,
    ) -> None:
        try:
            await self._stop(process)
            if proxy is not None:
                await proxy.close()
            shutil.rmtree(layout.secrets, ignore_errors=True)
            shutil.rmtree(layout.run, ignore_errors=True)
            if settled or not layout.meta.exists():
                shutil.rmtree(layout.root, ignore_errors=True)
        finally:
            self.active.discard(layout.turn_id)

    def _launch(
        self,
        layout: sandbox.TurnLayout,
        plan: TurnPlan,
        credential: turn_credentials.Credential,
        credential_file: Path | None,
    ) -> subprocess.Popen:
        bwrap = shutil.which("bwrap")
        if not bwrap:
            raise Refused("sandbox_unavailable")
        binary = self.release / "runtime_daemon" / "bin" / ("runtime-" + plan.provider)
        bridge = self.release / "runtime_daemon" / "sandbox_bridge.py"
        launch = sandbox.Launch(
            bwrap=bwrap,
            layout=layout,
            workdir=plan.workdir,
            provider=plan.provider,
            env=sandbox.sandbox_env(plan.provider, os.environ, credential.env),
            ro_paths=[
                self.release,
                Path(sys.base_prefix),
                *sandbox.cli_mounts(plan.provider),
                *self.extra_ro(),
            ],
            credential=credential_file,
        )
        command = sandbox.bridge_command(sys.executable, bridge, sandbox.driver_command(binary))
        try:
            argv = sandbox.bwrap_argv(launch, command)
        except ValueError as exc:
            # 只读挂载会把本轮目录、socket 目录或工作目录带进沙箱：不起这一轮。
            code = "unsafe_mount" if str(exc) == "unsafe_mount" else "sandbox_start_failed"
            raise Refused(code) from None
        log = (layout.root / "driver.log").open("ab")
        try:
            return subprocess.Popen(
                argv,
                env=dict(launch.env),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError:
            raise Refused("sandbox_start_failed") from None
        finally:
            log.close()

    async def _wait_ready(self, layout: sandbox.TurnLayout, process: subprocess.Popen) -> None:
        deadline = time.monotonic() + SOCKET_WAIT_SECONDS
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise Refused("sandbox_start_failed")
            if _is_socket(layout.driver_socket):
                try:
                    async with httpx.AsyncClient(
                        transport=httpx.AsyncHTTPTransport(uds=str(layout.driver_socket)),
                        base_url="http://runtime",
                        timeout=5,
                    ) as client:
                        if (await client.get("/v1/models")).status_code == 200:
                            return
                except httpx.HTTPError:
                    pass
            await asyncio.sleep(0.1)
        raise Refused("sandbox_start_failed")

    async def _stream(
        self, layout: sandbox.TurnLayout, plan: TurnPlan, frames: Any, gate: DoneGate
    ) -> None:
        if not _is_socket(layout.driver_socket):
            raise Refused("sandbox_start_failed")
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(layout.driver_socket)),
            base_url="http://runtime",
            timeout=httpx.Timeout(120, connect=10),
        ) as local:
            async with local.stream("POST", "/v1/chat/completions", json=plan.body) as response:
                await frames.add(
                    status_code=response.status_code,
                    content_type=response.headers.get("content-type", "application/json"),
                )
                if plan.body.get("stream") and response.status_code == 200:
                    seat = self.daemon.seats.active(plan.provider)
                    chunk = {"x_runtime": {"seat_id": seat}, "choices": []}
                    await frames.add(("data: " + json.dumps(chunk) + "\n\n").encode())
                async for part in response.aiter_bytes():
                    out = gate.feed(part)
                    if out:
                        await frames.add(out)

    @staticmethod
    async def _stop(process: subprocess.Popen | None) -> None:
        """停掉沙箱：bwrap 是会话首进程，整组发信号；它一死，沙箱里的进程随之结束。"""
        if process is None or process.poll() is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(asyncio.to_thread(process.wait), STOP_SECONDS)
        except (asyncio.TimeoutError, TimeoutError):  # noqa: UP041 distinct on 3.10
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await asyncio.to_thread(process.wait)
