"""局域网直连换机：源节点的临时下载口与目标节点的拉取。"""

import hashlib
import shutil
import socket
import time

import pytest

from runtime_daemon import direct_transfer
from runtime_daemon.direct_transfer import DirectError, DirectTransfer

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="需要 openssl 生成证书")

TOKEN = "a" * 64


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def source(tmp_path):
    node = DirectTransfer("office", f"127.0.0.1:{free_port()}", tmp_path / "src")
    yield node
    node.close()


def pack_file(tmp_path, size=3 * 1024 * 1024 + 7):
    path = tmp_path / "pack.tar.gz"
    path.write_bytes(bytes(range(256)) * (size // 256) + b"x" * (size % 256))
    return path


def test_config_needs_zone_and_valid_address(tmp_path):
    assert DirectTransfer.from_config({}, tmp_path) is None
    assert DirectTransfer.from_config({"network_zone": "office"}, tmp_path) is None
    bad = {"network_zone": "office", "direct_address": "192.0.2.10:80"}
    assert DirectTransfer.from_config(bad, tmp_path) is None
    good = {"network_zone": "office", "direct_address": "192.0.2.10:47801"}
    node = DirectTransfer.from_config(good, tmp_path)
    assert node is not None and node.port == 47801


def test_target_pulls_pack_and_checks_certificate(source, tmp_path):
    cap = source.capability()
    assert cap is not None and cap["zone"] == "office" and len(cap["fingerprint"]) == 64
    assert (tmp_path / "src" / "direct-key.pem").stat().st_mode & 0o777 == 0o600
    pack = pack_file(tmp_path)
    assert source.serve(TOKEN, pack)
    dest = tmp_path / "dest.tar.gz"
    direct_transfer.fetch(cap["address"], TOKEN, cap["fingerprint"], pack.stat().st_size, dest)
    assert hashlib.sha256(dest.read_bytes()).digest() == hashlib.sha256(pack.read_bytes()).digest()


def test_wrong_fingerprint_never_sends_the_token(source, tmp_path):
    cap = source.capability()
    pack = pack_file(tmp_path, 1024)
    source.serve(TOKEN, pack)
    with pytest.raises(DirectError, match="证书"):
        direct_transfer.fetch(cap["address"], TOKEN, "0" * 64, 1024, tmp_path / "d")


def test_unknown_or_withdrawn_token_is_refused(source, tmp_path):
    cap = source.capability()
    pack = pack_file(tmp_path, 1024)
    source.serve(TOKEN, pack)
    with pytest.raises(DirectError, match="拒绝"):
        direct_transfer.fetch(cap["address"], "b" * 64, cap["fingerprint"], 1024, tmp_path / "d")
    source.withdraw(pack)
    with pytest.raises(DirectError, match="拒绝"):
        direct_transfer.fetch(cap["address"], TOKEN, cap["fingerprint"], 1024, tmp_path / "d")


def test_resume_continues_from_partial_file(source, tmp_path):
    cap = source.capability()
    pack = pack_file(tmp_path, 4096)
    source.serve(TOKEN, pack)
    dest = tmp_path / "dest"
    dest.write_bytes(pack.read_bytes()[:1000])
    direct_transfer.fetch(cap["address"], TOKEN, cap["fingerprint"], 4096, dest)
    assert dest.read_bytes() == pack.read_bytes()


def test_background_fetch_reports_failure_and_removes_partial_file(tmp_path):
    dest = tmp_path / "dest"
    direct_transfer.start_fetch(f"127.0.0.1:{free_port()}", TOKEN, "0" * 64, 10, dest)
    state: dict = {}
    for _ in range(100):
        state = direct_transfer.status(dest)
        if state["error"] or state["done"]:
            break
        time.sleep(0.05)
    assert state["error"] and not state["done"]
    assert not dest.exists()
