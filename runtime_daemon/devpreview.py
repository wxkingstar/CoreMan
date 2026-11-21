"""开发环境预览：开发模式的 worktree 在本节点用 docker compose 跑起来，经 frp 隧道给出开发域名。

节点 config.json 的 `dev_preview` 段打开这项能力（没有这一段就不支持）。守护进程负责：
- 下载并核对固定版本的 frpc，常驻运行，断了重启；隧道经 wss 连到网关后面的 frps；
- 每 30 分钟回收：worktree 没了就删容器和卷，闲置超时就停容器，每天清一次旧镜像和构建缓存。

AI 员工在 worktree 根目录用 `coreman-dev up|down|status|logs|list`（守护进程写进 cli-bin）。
状态都在 `<数据目录>/dev-preview/` 下：state.json（预览 → 端口）、
proxies/*.toml（每个预览一段 frpc 配置）。
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import io
import json
import logging
import os
import platform
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

LOG = logging.getLogger("coreman-runtime")

FRP_VERSION = "0.71.1"
FRP_SHA256 = {
    "darwin_amd64": "8acb59b6fb998cc8d209d1373d90bf70fa5ec3efeb4dfbc7bda6ce3abd78cb45",
    "darwin_arm64": "d15d0324b99e14619c9aa46e37f6db588c06a09be4bc6f7948890b6b61077842",
    "linux_amd64": "1d9875210972a5d1a62b2dc4c48df45ab0af05d3ca1b5e494a014428259df803",
    "linux_arm64": "8edf590b0c396fa42f9c147121a76af83cfc2002b08f5aef0ea1008c43482019",
}
FRP_URL = "https://github.com/fatedier/frp/releases/download/v{v}/frp_{v}_{p}.tar.gz"
COMPOSE_FILE = "dev/compose.yaml"
RECLAIM_SECONDS = 1800
PRUNE_SECONDS = 24 * 3600
NAME = re.compile(r"[^a-z0-9]+")


class PreviewError(RuntimeError):
    """给 AI 员工看的失败原因：说清哪一步、怎么办。"""


@dataclass
class Settings:
    data_dir: Path
    node_id: str
    domain: str
    tunnel_server: str
    token_file: Path
    env_file: Path
    tunnel_port: int = 443
    port_range: tuple[int, int] = (42000, 42999)
    max_previews: int = 8
    idle_hours: float = 24
    admin_port: int = 17400
    visitors: list[dict] = field(default_factory=list)
    visitor_key_file: Path | None = None
    frpc_path: Path | None = None
    docker: str = "docker"

    @classmethod
    def from_config(cls, config: dict, data_dir: Path) -> Settings | None:
        raw = config.get("dev_preview")
        if not raw:
            return None
        if not isinstance(raw, dict):
            raise ValueError("dev_preview 必须是对象")

        def path(key: str) -> Path | None:
            value = raw.get(key)
            return Path(os.path.expanduser(str(value))) if value else None

        for key in ("domain", "tunnel_server", "token_file", "env_file"):
            if not raw.get(key):
                raise ValueError(f"dev_preview 缺少 {key}")
        low, high = raw.get("port_range", [42000, 42999])
        visitors = raw.get("visitors") or []
        for item in visitors:
            if not (isinstance(item, dict) and item.get("server_name") and item.get("bind_port")):
                raise ValueError("dev_preview.visitors 每项要有 server_name 和 bind_port")
        if visitors and not raw.get("visitor_key_file"):
            raise ValueError("配了 visitors 就要配 visitor_key_file")
        return cls(
            data_dir=data_dir / "dev-preview",
            node_id=str(config.get("node_id") or ""),
            domain=str(raw["domain"]).strip("."),
            tunnel_server=str(raw["tunnel_server"]),
            tunnel_port=int(raw.get("tunnel_port", 443)),
            token_file=path("token_file"),  # type: ignore[arg-type]
            env_file=path("env_file"),  # type: ignore[arg-type]
            port_range=(int(low), int(high)),
            max_previews=max(1, int(raw.get("max_previews", 8))),
            idle_hours=float(raw.get("idle_hours", 24)),
            admin_port=int(raw.get("admin_port", 17400)),
            visitors=visitors,
            visitor_key_file=path("visitor_key_file"),
            frpc_path=path("frpc_path"),
            # 节点配置里常写 ~/...：命令不经 shell，这里自己展开。
            docker=os.path.expanduser(str(raw.get("docker") or "docker")),
        )

    @property
    def proxies_dir(self) -> Path:
        return self.data_dir / "proxies"

    @property
    def frpc_config(self) -> Path:
        return self.data_dir / "frpc.toml"

    def project_env_file(self, repo: str) -> Path:
        """仓库独有的变量（如它自己的密钥）放在公共环境文件旁的 projects/<仓库名>.env。"""
        return self.env_file.parent / "projects" / f"{repo}.env"


def toml_str(value: object) -> str:
    # JSON 字符串的转义是 TOML 基本字符串的子集。
    return json.dumps(str(value), ensure_ascii=False)


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name("." + path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    os.replace(temp, path)


# ---------------------------------------------------------------- 状态


@contextlib.contextmanager
def locked_state(settings: Settings):
    """读改写 state.json，全程持文件锁：多个会话可能同时 up。"""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with (settings.data_dir / "state.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = settings.data_dir / "state.json"
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            state = {}
        state.setdefault("previews", {})
        yield state
        write_private(path, json.dumps(state, indent=2, ensure_ascii=False))


def preview_id(settings: Settings, tree: Path, repo_name: str) -> str:
    slug = NAME.sub("-", repo_name.lower()).strip("-")[:30].strip("-") or "app"
    digest = hashlib.sha1(f"{settings.node_id}:{tree}".encode()).hexdigest()[:6]
    return f"{slug}-{digest}"


def repo_name(tree: Path) -> str:
    try:
        url = run(["git", "-C", str(tree), "remote", "get-url", "origin"]).strip()
    except PreviewError:
        url = ""
    name = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1] if url else ""
    name = name.removesuffix(".git")
    if not name:
        # worktree 的目录名是会话 id，用主检出的目录名。
        common = run(
            ["git", "-C", str(tree), "rev-parse", "--path-format=absolute", "--git-common-dir"]
        )
        name = Path(common.strip()).parent.name
    return name


def preview_env_file(settings: Settings, repo: str, pid: str) -> Path:
    """有仓库专属变量时，把公共文件和它拼成一份给这个预览用（同名以仓库专属的为准）。"""
    extra = settings.project_env_file(repo)
    merged = settings.data_dir / "env" / f"{pid}.env"
    if not extra.is_file():
        merged.unlink(missing_ok=True)
        return settings.env_file
    base = settings.env_file.read_text()
    write_private(merged, base.rstrip("\n") + "\n" + extra.read_text())
    return merged


def port_free(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def allocate_port(settings: Settings, state: dict, key: str) -> int:
    entry = state["previews"].get(key)
    if entry:
        return int(entry["port"])
    used = {int(e["port"]) for e in state["previews"].values()}
    low, high = settings.port_range
    for port in range(low, high + 1):
        if port not in used and port_free(port):
            return port
    raise PreviewError("本节点开发环境端口已用完，先用 coreman-dev list 看看有哪些可以 down")


# ---------------------------------------------------------------- 命令


def run(
    cmd: list[str], *, cwd: Path | None = None, env: dict | None = None, timeout: int = 120
) -> str:
    try:
        done = subprocess.run(
            cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as exc:
        raise PreviewError(f"找不到命令 {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PreviewError(f"{shlex.join(cmd[:4])} 超过 {timeout} 秒没有结束") from exc
    if done.returncode != 0:
        tail = "\n".join((done.stdout + done.stderr).strip().splitlines()[-40:])
        raise PreviewError(f"{shlex.join(cmd[:4])} 失败（退出码 {done.returncode}）：\n{tail}")
    return done.stdout


def compose(settings: Settings, project: str, *args: str, tree: Path | None = None, **kw) -> str:
    cmd = [settings.docker, "compose", "-p", project]
    if tree is not None:
        cmd += ["--project-directory", str(tree), "-f", str(tree / COMPOSE_FILE)]
    return run(cmd + list(args), **kw)


def write_proxy(settings: Settings, pid: str, port: int) -> None:
    write_private(
        settings.proxies_dir / f"{pid}.toml",
        "[[proxies]]\n"
        f"name = {toml_str(pid)}\n"
        'type = "http"\n'
        f"subdomain = {toml_str(pid)}\n"
        'localIP = "127.0.0.1"\n'
        f"localPort = {port}\n",
    )


def reload_tunnel(settings: Settings) -> None:
    binary = frpc_binary(settings)
    if not binary.is_file() or not settings.frpc_config.is_file():
        raise PreviewError("隧道客户端还没准备好（守护进程启动后会自动下载），稍后再试")
    run([str(binary), "reload", "-c", str(settings.frpc_config)], timeout=30)


def url_of(settings: Settings, pid: str) -> str:
    return f"https://{pid}.{settings.domain}"


def probe(url: str, timeout: float) -> tuple[bool, str]:
    """2xx–4xx 都算服务起来了（登录页、401 也是正常页面）；frps 自己的 404 说明隧道还没接上。

    403 例外：多半是开发服务器没放行开发域名（如 Vite allowedHosts），直接报出来。
    """
    import httpx

    deadline = time.monotonic() + timeout
    last = ""
    while True:
        try:
            response = httpx.get(url, timeout=10, follow_redirects=False)
            text = response.text[:2000]
            if response.status_code == 403:
                return False, "HTTP 403（多半是开发服务器没放行这个主机名，或入口没转发 Host 头）"
            if response.status_code < 500 and "fatedier/frp" not in text:
                return True, str(response.status_code)
            last = f"HTTP {response.status_code}" + (
                "（隧道未接上）" if "fatedier/frp" in text else ""
            )
        except httpx.HTTPError as exc:
            last = type(exc).__name__
        if time.monotonic() >= deadline:
            return False, last
        time.sleep(3)


def stop_preview(settings: Settings, state: dict, key: str, *, remove: bool = False) -> None:
    entry = state["previews"].get(key)
    if not entry:
        return
    pid = entry["id"]
    args = ["down", "--remove-orphans"] + (["-v", "--rmi", "local"] if remove else [])
    with contextlib.suppress(PreviewError):
        compose(settings, pid, *args, timeout=300)
    (settings.proxies_dir / f"{pid}.toml").unlink(missing_ok=True)
    if remove:
        (settings.data_dir / "env" / f"{pid}.env").unlink(missing_ok=True)
        del state["previews"][key]
    else:
        entry["running"] = False


def current_tree() -> Path:
    top = run(["git", "rev-parse", "--show-toplevel"]).strip()
    if not top:
        raise PreviewError("请在 Git 仓库（会话 worktree）的根目录里运行")
    return Path(top)


def cmd_up(settings: Settings, path: str) -> int:
    tree = current_tree()
    if not (tree / COMPOSE_FILE).is_file():
        raise PreviewError(
            f"仓库里没有 {COMPOSE_FILE}：按开发环境约定生成（只含本仓库的服务和一个入口，"
            "入口监听 127.0.0.1:${DEV_PORT}，环境变量从 ${DEV_ENV_FILE} 读）"
        )
    if not settings.env_file.is_file():
        raise PreviewError(f"节点缺少开发环境变量文件 {settings.env_file}，请联系管理员")
    key = str(tree)
    repo = repo_name(tree)
    with locked_state(settings) as state:
        pid = preview_id(settings, tree, repo)
        port = allocate_port(settings, state, key)
        running = [
            (e.get("last_up", 0), k)
            for k, e in state["previews"].items()
            if e.get("running") and k != key
        ]
        for _, old in sorted(running)[: max(0, len(running) - settings.max_previews + 1)]:
            print(f"本节点开发环境已满，先停掉最久没用的 {state['previews'][old]['id']}")
            stop_preview(settings, state, old)
        state["previews"][key] = {
            "id": pid,
            "port": port,
            "tree": key,
            "running": False,
            "last_up": time.time(),
        }
    url = url_of(settings, pid)
    env = dict(os.environ)
    env.update(
        DEV_PORT=str(port),
        DEV_ENV_FILE=str(preview_env_file(settings, repo, pid)),
        DEV_URL=url,
        DEV_PREVIEW_ID=pid,
    )
    print(f"启动 {pid}（端口 {port}）……", flush=True)
    try:
        compose(
            settings,
            pid,
            "up",
            "-d",
            "--build",
            # 只改了挂载进容器的配置文件时 compose 认为没变化、不会重建，旧配置会一直生效。
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "600",
            tree=tree,
            env=env,
            timeout=1800,
        )
    except PreviewError as exc:
        logs = ""
        with contextlib.suppress(PreviewError):
            logs = compose(settings, pid, "logs", "--tail", "40", tree=tree, env=env)
        raise PreviewError(f"{exc}\n\n最近的容器日志：\n{logs[-6000:]}") from exc
    ok, detail = probe(f"http://127.0.0.1:{port}{path}", 60)
    if not ok:
        raise PreviewError(f"容器起来了，但本机入口 127.0.0.1:{port}{path} 没有响应（{detail}）")
    write_proxy(settings, pid, port)
    reload_tunnel(settings)
    ok, detail = probe(url + path, 90)
    with locked_state(settings) as state:
        if key in state["previews"]:
            state["previews"][key]["running"] = True
    if not ok:
        raise PreviewError(f"本机入口正常，但经开发域名访问失败（{detail}）：{url}{path}")
    print(f"开发环境：{url}{path if path != '/' else ''}")
    return 0


def cmd_down(settings: Settings) -> int:
    tree = current_tree()
    with locked_state(settings) as state:
        if str(tree) not in state["previews"]:
            print("这个 worktree 没有开发环境")
            return 0
        stop_preview(settings, state, str(tree))
    with contextlib.suppress(PreviewError):
        reload_tunnel(settings)
    print("已停止")
    return 0


def cmd_status(settings: Settings) -> int:
    tree = current_tree()
    with locked_state(settings) as state:
        entry = state["previews"].get(str(tree))
    if not entry:
        print("这个 worktree 没有开发环境，用 coreman-dev up 启动")
        return 0
    print(
        f"{url_of(settings, entry['id'])}  端口 {entry['port']}  "
        f"{'运行中' if entry.get('running') else '已停止'}  "
        f"最近启动 {time.strftime('%Y-%m-%d %H:%M', time.localtime(entry['last_up']))}"
    )
    print(compose(settings, entry["id"], "ps"))
    return 0


def cmd_logs(settings: Settings, service: str | None, tail: int) -> int:
    tree = current_tree()
    with locked_state(settings) as state:
        entry = state["previews"].get(str(tree))
    if not entry:
        raise PreviewError("这个 worktree 没有开发环境")
    args = ["logs", "--tail", str(tail)] + ([service] if service else [])
    print(compose(settings, entry["id"], *args))
    return 0


def cmd_list(settings: Settings) -> int:
    with locked_state(settings) as state:
        previews = list(state["previews"].values())
    if not previews:
        print("本节点没有开发环境")
    for entry in sorted(previews, key=lambda e: e["id"]):
        print(
            f"{url_of(settings, entry['id'])}  {'运行中' if entry.get('running') else '已停止'}  "
            f"{entry['tree']}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="coreman-dev", description="开发环境预览")
    parser.add_argument("--config", required=True, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("up", help="在当前 worktree 构建并启动，输出开发地址")
    up.add_argument("--path", default="/", help="自检访问的路径，默认 /")
    sub.add_parser("down", help="停止当前 worktree 的开发环境")
    sub.add_parser("status", help="当前 worktree 的地址与容器状态")
    logs = sub.add_parser("logs", help="容器日志")
    logs.add_argument("service", nargs="?")
    logs.add_argument("--tail", type=int, default=200)
    sub.add_parser("list", help="本节点所有开发环境")
    args = parser.parse_args(argv)
    config_path = Path(args.config)
    try:
        settings = Settings.from_config(json.loads(config_path.read_text()), config_path.parent)
    except (OSError, ValueError) as exc:
        print(f"开发环境配置有误：{exc}", file=sys.stderr)
        return 2
    if settings is None:
        print("本节点没有开启开发环境预览，直接推分支走测试环境", file=sys.stderr)
        return 2
    try:
        if args.command == "up":
            return cmd_up(settings, args.path if args.path.startswith("/") else "/" + args.path)
        if args.command == "down":
            return cmd_down(settings)
        if args.command == "status":
            return cmd_status(settings)
        if args.command == "logs":
            return cmd_logs(settings, args.service, args.tail)
        return cmd_list(settings)
    except PreviewError as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1


def command_script(config_path: Path, release: Path) -> str:
    loader = (
        'import sys; sys.argv[0] = "coreman-dev"; sys.path.insert(0, sys.argv.pop(1)); '
        "from runtime_daemon.devpreview import main; raise SystemExit(main())"
    )
    command = [str(release / ".venv/bin/python"), "-I", "-c", loader, str(release)]
    command += ["--config", str(config_path)]
    return (
        "#!/bin/sh\n# CoreMan 开发环境预览命令，由守护进程生成，请勿手工修改。\n"
        "exec " + " ".join(shlex.quote(part) for part in command) + ' "$@"\n'
    )


# ---------------------------------------------------------------- 守护进程侧


def frp_platform() -> str:
    system = {"darwin": "darwin", "linux": "linux"}.get(sys.platform, sys.platform)
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(
        machine, machine
    )
    return f"{system}_{arch}"


def frpc_binary(settings: Settings) -> Path:
    return settings.frpc_path or settings.data_dir / "bin" / f"frpc-{FRP_VERSION}"


def install_frpc(settings: Settings) -> Path:
    binary = frpc_binary(settings)
    if binary.is_file():
        return binary
    target = frp_platform()
    expected = FRP_SHA256.get(target)
    if not expected:
        raise PreviewError(f"frpc 没有 {target} 版本")
    import httpx

    response = httpx.get(
        FRP_URL.format(v=FRP_VERSION, p=target), follow_redirects=True, timeout=120
    )
    response.raise_for_status()
    if hashlib.sha256(response.content).hexdigest() != expected:
        raise PreviewError("frpc 下载内容校验不符")
    member = f"frp_{FRP_VERSION}_{target}/frpc"
    with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as archive:
        data = archive.extractfile(member)
        if data is None:
            raise PreviewError("frpc 压缩包里没有 frpc")
        content = data.read()
    binary.parent.mkdir(parents=True, exist_ok=True)
    temp = binary.with_name(binary.name + ".tmp")
    temp.write_bytes(content)
    temp.chmod(0o700)
    os.replace(temp, binary)
    return binary


def frpc_config_text(settings: Settings, admin_password: str) -> str:
    token = settings.token_file.read_text().strip()
    lines = [
        f"serverAddr = {toml_str(settings.tunnel_server)}",
        f"serverPort = {settings.tunnel_port}",
        "loginFailExit = false",
        'auth.method = "token"',
        f"auth.token = {toml_str(token)}",
        'transport.protocol = "wss"',
        # 多路复用 + 保活：经网关的连接空闲满 60 秒会被断开，frps 复用断掉的工作连接
        # 就失败（页面 404）。所有工作连接走同一条连接里的流，每 20 秒保活一次。
        # frps 要同样打开 tcpMux，两边不一致登录不上。
        "transport.tcpMux = true",
        "transport.tcpMuxKeepaliveInterval = 20",
        "transport.poolCount = 0",
        f"log.to = {toml_str(settings.data_dir / 'frpc.log')}",
        "log.maxDays = 3",
        'webServer.addr = "127.0.0.1"',
        f"webServer.port = {settings.admin_port}",
        'webServer.user = "coreman"',
        f"webServer.password = {toml_str(admin_password)}",
        f"includes = [{toml_str(settings.proxies_dir / '*.toml')}]",
    ]
    if settings.visitors:
        key = settings.visitor_key_file.read_text().strip()  # type: ignore[union-attr]
        for item in settings.visitors:
            name = str(item["server_name"])
            lines += [
                "",
                "[[visitors]]",
                f"name = {toml_str('v-' + name)}",
                'type = "stcp"',
                f"serverName = {toml_str(name)}",
                f"secretKey = {toml_str(key)}",
                f"bindAddr = {toml_str(item.get('bind_addr', '127.0.0.1'))}",
                f"bindPort = {int(item['bind_port'])}",
            ]
    return "\n".join(lines) + "\n"


class DevPreview:
    """守护进程里的一份：常驻 frpc 并定期回收。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.process: subprocess.Popen | None = None
        self.stopping = threading.Event()
        self.thread: threading.Thread | None = None
        self.error = ""

    @classmethod
    def from_config(cls, config: dict, data_dir: Path) -> DevPreview | None:
        settings = Settings.from_config(config, data_dir)
        return cls(settings) if settings else None

    def capability(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def write_command(self, bin_dir: Path, config_path: Path, release: Path) -> None:
        path = bin_dir / "coreman-dev"
        script = command_script(config_path, release)
        with contextlib.suppress(OSError):
            if path.read_text() == script:
                return
        write_private(path, script)
        path.chmod(0o700)

    def start(self) -> None:
        self.thread = threading.Thread(target=self.loop, daemon=True, name="dev-preview")
        self.thread.start()

    def stop(self) -> None:
        self.stopping.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.process.wait(10)

    def ensure_tunnel(self) -> None:
        if self.process and self.process.poll() is None:
            return
        settings = self.settings
        binary = install_frpc(settings)
        settings.proxies_dir.mkdir(parents=True, exist_ok=True)
        stale = settings.data_dir / "frpc.pid"
        with contextlib.suppress(OSError, ValueError, PreviewError):
            # 守护进程上次异常退出留下的 frpc：先停掉，免得两份抢同一批子域名。
            old = int(stale.read_text())
            if "frpc" in run(["ps", "-o", "command=", "-p", str(old)]):
                os.kill(old, 15)
                time.sleep(1)
        write_private(settings.frpc_config, frpc_config_text(settings, secrets.token_urlsafe(24)))
        self.process = subprocess.Popen(
            [str(binary), "-c", str(settings.frpc_config)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        write_private(stale, str(self.process.pid))
        LOG.info("dev preview tunnel started")

    def loop(self) -> None:
        last_reclaim = 0.0
        backoff = 5.0
        while not self.stopping.is_set():
            try:
                self.ensure_tunnel()
                self.error = ""
                backoff = 5.0
            except Exception as exc:  # noqa: BLE001 隧道起不来只影响开发环境，不能拖垮守护进程
                self.error = str(exc)[:300]
                LOG.warning("dev preview tunnel failed: %s", self.error)
                backoff = min(backoff * 2, 600)
            if time.monotonic() - last_reclaim >= RECLAIM_SECONDS:
                last_reclaim = time.monotonic()
                try:
                    reclaim(self.settings)
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("dev preview reclaim failed: %s", str(exc)[:300])
            self.stopping.wait(backoff if self.error else 10)


def reclaim(settings: Settings, now: float | None = None) -> None:
    """worktree 没了：删容器、卷和本地构建的镜像；闲置超时：停容器；每天清一次旧镜像与构建缓存。"""
    now = time.time() if now is None else now
    changed = False
    with locked_state(settings) as state:
        for key in list(state["previews"]):
            tree = Path(key)
            if not (tree / ".git").exists():
                stop_preview(settings, state, key, remove=True)
                changed = True
            elif state["previews"][key].get("running"):
                idle = now - max(tree.stat().st_mtime, state["previews"][key].get("last_up", 0))
                if idle > settings.idle_hours * 3600:
                    stop_preview(settings, state, key)
                    changed = True
        due = now - float(state.get("last_prune", 0)) >= PRUNE_SECONDS
        if due:
            state["last_prune"] = now
    if changed:
        with contextlib.suppress(PreviewError):
            reload_tunnel(settings)
    if due and shutil.which(settings.docker):
        for args in (["image", "prune", "-f"], ["builder", "prune", "-f"]):
            with contextlib.suppress(PreviewError):
                run([settings.docker, *args, "--filter", "until=72h"], timeout=600)
