"""局域网直连换机：同一网络区的两台节点之间，换机压缩包由目标节点直接从源节点拉取。

平台仍负责调度与核对（清单、大小、哈希都经认证的控制通道下发），只是压缩包的字节不再逐块经平台
和数据库中转。源节点在 `direct_address` 上开一个只在换机期间有内容的 HTTPS 下载口：证书是本机
自签的，指纹随 ping 报给平台，平台再交给目标节点核对；每个压缩包配一个平台签发的一次性令牌，
压缩包删掉时令牌一起作废。

`config.json` 里配了 `network_zone` 与 `direct_address` 才启用；两台节点的网络区相同，平台才走
直连，拉不通时退回经平台中转。
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import http.server
import shutil
import ssl
import subprocess
import threading
import time
from pathlib import Path

CERT_NAME = "direct-cert.pem"
KEY_NAME = "direct-key.pem"
# 令牌只在一次换机里用：压缩包删掉时作废，忘了删的到期作废。
TOKEN_TTL = 3600
READ_SIZE = 1024 * 1024
FETCH_TIMEOUT = 30
FETCH_ATTEMPTS = 3

_statuses: dict[str, dict] = {}
_statuses_lock = threading.Lock()


class DirectError(Exception):
    """直连失败；文本是给人看的固定提示，平台会原样带进换机失败原因。"""


def parse_address(raw: object) -> tuple[str, int] | None:
    """`主机:端口`：主机可以是局域网 IP 或主机名，端口在 1024–65535。"""
    text = str(raw or "").strip()
    host, sep, port = text.rpartition(":")
    host = host.strip("[]")
    if not sep or not host or not port.isdigit() or not 1024 <= int(port) <= 65535:
        return None
    return host, int(port)


def fingerprint(cert: Path) -> str:
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()


class DirectTransfer:
    """一个守护进程一份：本机的网络区、对外地址、自签证书与下载口。"""

    def __init__(self, zone: str, address: str, data_dir: Path) -> None:
        parsed = parse_address(address)
        if not zone or parsed is None:
            raise ValueError("network_zone / direct_address 无效")
        self.zone, self.address, self.port = zone, address, parsed[1]
        self.cert, self.key = data_dir / CERT_NAME, data_dir / KEY_NAME
        self.packs: dict[str, tuple[Path, float]] = {}
        self.lock = threading.Lock()
        self.server: http.server.ThreadingHTTPServer | None = None

    @classmethod
    def from_config(cls, config: dict, data_dir: Path) -> DirectTransfer | None:
        zone = str(config.get("network_zone") or "").strip()
        address = str(config.get("direct_address") or "").strip()
        if not zone or not address:
            return None
        try:
            return cls(zone, address, data_dir)
        except ValueError:
            return None

    def ensure_cert(self) -> None:
        if self.cert.is_file() and self.key.is_file():
            return
        self.cert.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(self.key),
                "-out",
                str(self.cert),
                "-days",
                "3650",
                "-subj",
                "/CN=coreman-runtime",
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        self.key.chmod(0o600)

    def capability(self) -> dict | None:
        """ping 里报给平台的直连信息；证书生成不了就不报，平台照旧经中转。"""
        try:
            self.ensure_cert()
            return {
                "zone": self.zone,
                "address": self.address,
                "fingerprint": fingerprint(self.cert),
            }
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    def serve(self, token: str, path: Path) -> bool:
        """登记一个可供下载的压缩包；下载口起不来（端口被占等）返回 False，平台退回中转。"""
        try:
            self.start()
        except (OSError, ValueError, ssl.SSLError, subprocess.SubprocessError):
            return False
        with self.lock:
            now = time.monotonic()
            self.packs = {k: v for k, v in self.packs.items() if v[1] > now}
            self.packs[token] = (path, now + TOKEN_TTL)
        return True

    def withdraw(self, path: Path) -> None:
        with self.lock:
            self.packs = {k: v for k, v in self.packs.items() if v[0] != path}

    def lookup(self, token: str) -> Path | None:
        with self.lock:
            now = time.monotonic()
            for known, (path, expires) in self.packs.items():
                if hmac.compare_digest(known, token) and expires > now:
                    return path
        return None

    def start(self) -> None:
        with self.lock:
            if self.server is not None:
                return
            self.ensure_cert()
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(self.cert, self.key)
            owner = self

            class Handler(http.server.BaseHTTPRequestHandler):
                def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                    """不往服务日志里写访问记录。"""

                def do_GET(self) -> None:  # noqa: N802
                    auth = self.headers.get("Authorization", "")
                    path = (
                        owner.lookup(auth.removeprefix("Bearer "))
                        if self.path == "/pack" and auth.startswith("Bearer ")
                        else None
                    )
                    if path is None or not path.is_file():
                        self.send_error(404)
                        return
                    size = path.stat().st_size
                    start = 0
                    ranged = self.headers.get("Range", "")
                    if ranged.startswith("bytes=") and ranged.endswith("-"):
                        value = ranged[6:-1]
                        start = int(value) if value.isdigit() else -1
                    if not 0 <= start <= size:
                        self.send_error(416)
                        return
                    self.send_response(206 if start else 200)
                    self.send_header("Content-Length", str(size - start))
                    self.end_headers()
                    with path.open("rb") as f:
                        f.seek(start)
                        shutil.copyfileobj(f, self.wfile, READ_SIZE)

            server = http.server.ThreadingHTTPServer(("", self.port), Handler)
            server.daemon_threads = True
            server.socket = context.wrap_socket(server.socket, server_side=True)
            threading.Thread(
                target=server.serve_forever, daemon=True, name="coreman-direct-transfer"
            ).start()
            self.server = server

    def close(self) -> None:
        with self.lock:
            server, self.server = self.server, None
        if server is not None:
            server.shutdown()
            server.server_close()


def status(dest: Path) -> dict:
    with _statuses_lock:
        return dict(_statuses.get(str(dest)) or {"received": 0, "done": False, "error": None})


def _set(dest: Path, **values: object) -> None:
    with _statuses_lock:
        _statuses.setdefault(str(dest), {"received": 0, "done": False, "error": None}).update(
            values
        )


def start_fetch(address: str, token: str, expected_fp: str, size: int, dest: Path) -> None:
    """目标节点：后台从源节点拉压缩包到 `dest`，进度用 status() 查。失败时删掉不完整的文件，
    平台可以接着按块中转写同一个位置。"""
    with _statuses_lock:
        _statuses[str(dest)] = {"received": 0, "done": False, "error": None}
    dest.unlink(missing_ok=True)
    threading.Thread(
        target=_fetch,
        args=(address, token, expected_fp, size, dest),
        daemon=True,
        name="coreman-direct-fetch",
    ).start()


def _fetch(address: str, token: str, expected_fp: str, size: int, dest: Path) -> None:
    try:
        fetch(address, token, expected_fp, size, dest)
    except (OSError, DirectError, http.client.HTTPException, ssl.SSLError) as exc:
        dest.unlink(missing_ok=True)
        message = str(exc) if isinstance(exc, DirectError) else "连不上源节点的局域网下载口"
        _set(dest, error=message, received=0)
    else:
        _set(dest, done=True, received=size)


def fetch(address: str, token: str, expected_fp: str, size: int, dest: Path) -> None:
    parsed = parse_address(address)
    if parsed is None:
        raise DirectError("源节点的局域网地址无效")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # 自签证书不走 CA 校验，改为核对平台下发的指纹；核对通过之前不发令牌。
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    last: Exception | None = None
    for _ in range(FETCH_ATTEMPTS):
        received = dest.stat().st_size if dest.exists() else 0
        conn = http.client.HTTPSConnection(
            parsed[0], parsed[1], context=context, timeout=FETCH_TIMEOUT
        )
        try:
            conn.connect()
            cert = conn.sock.getpeercert(binary_form=True) if conn.sock else None
            if not cert or not hmac.compare_digest(hashlib.sha256(cert).hexdigest(), expected_fp):
                raise DirectError("源节点的证书与平台登记的不符，已停止直连")
            conn.request(
                "GET",
                "/pack",
                headers={"Authorization": f"Bearer {token}", "Range": f"bytes={received}-"},
            )
            response = conn.getresponse()
            if response.status not in (200, 206):
                raise DirectError("源节点拒绝了下载请求（令牌无效或压缩包已删除）")
            if response.status == 200:
                received = 0
            with dest.open("r+b" if received else "wb") as out:
                out.seek(received)
                out.truncate()
                while True:
                    chunk = response.read(READ_SIZE)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > size:
                        raise DirectError("收到的压缩包比登记的大，已停止")
                    out.write(chunk)
                    _set(dest, received=received)
            if received == size:
                return
        except DirectError:
            raise
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            last = exc
        finally:
            conn.close()
    if last is not None:
        raise last
    raise DirectError("压缩包没有收全")
