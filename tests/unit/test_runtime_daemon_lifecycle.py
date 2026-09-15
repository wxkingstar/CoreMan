"""Runtime deployment helpers: trust chain, socket placement, bounded logs, bundles."""

import hashlib
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
import pytest
import requests

from runtime_daemon import lifecycle, logs, tls
from runtime_daemon.lifecycle import FatalConfigError, InstallError
from tests.unit.runtime_daemon_support import https_server, make_bundle, make_pki

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    return make_pki(tmp_path_factory.mktemp("pki"))


# ---------------------------------------------------------------------------
# Trust chain
# ---------------------------------------------------------------------------


def test_private_ca_is_trusted_by_both_http_clients(pki, tmp_path):
    bundle = tls.build_trust_bundle(tmp_path, str(pki.ca))
    assert bundle == tmp_path / "trust.pem"
    assert bundle.stat().st_mode & 0o777 == 0o600
    content = bundle.read_bytes()
    assert pki.ca.read_bytes().strip() in content
    assert tls.certifi_bundle().read_bytes().strip()[:200] in content

    with https_server(pki, lambda path: (200, {}, b"ok")) as port:
        url = f"https://127.0.0.1:{port}/"
        with httpx.Client(verify=tls.client_context(bundle), trust_env=False) as client:
            assert client.get(url).text == "ok"
        session = requests.Session()
        session.trust_env = False
        session.verify = str(bundle)
        assert session.get(url, timeout=10).text == "ok"
        # Without the private CA the same server stays untrusted.
        with (
            httpx.Client(verify=tls.client_context(None), trust_env=False) as client,
            pytest.raises(httpx.ConnectError),
        ):
            client.get(url)


def test_relative_ca_file_resolves_inside_data_dir(pki, tmp_path):
    (tmp_path / "ca.pem").write_bytes(pki.ca.read_bytes())
    bundle = tls.build_trust_bundle(tmp_path, "ca.pem")
    assert pki.ca.read_bytes().strip() in bundle.read_bytes()


@pytest.mark.parametrize("content", [None, "not a certificate", "-----BEGIN CERTIFICATE-----\nx"])
def test_unusable_private_ca_is_a_fatal_config_error(tmp_path, content):
    path = tmp_path / "ca.pem"
    if content is not None:
        path.write_text(content)
    with pytest.raises(tls.TrustError) as caught:
        tls.build_trust_bundle(tmp_path, str(path))
    assert isinstance(caught.value, FatalConfigError)
    assert str(path) in str(caught.value)


def test_system_bundle_ignores_ssl_cert_file(monkeypatch, tmp_path):
    fake = tmp_path / "env.pem"
    fake.write_text("x")
    monkeypatch.setenv("SSL_CERT_FILE", str(fake))
    assert tls.system_bundle() != fake


# ---------------------------------------------------------------------------
# Socket directory
# ---------------------------------------------------------------------------


NODE = "0123456789abcdef"


def test_linux_prefers_private_xdg_runtime_dir(tmp_path):
    # Short like /run/user/<uid>; pytest's own temp paths exceed the sockaddr limit on macOS.
    runtime = Path(tempfile.mkdtemp(prefix="xdg-", dir="/tmp"))
    try:
        chosen = lifecycle.choose_socket_dir(
            tmp_path / "data", NODE, system="Linux", env={"XDG_RUNTIME_DIR": str(runtime)}
        )
        assert chosen == runtime / "coreman-01234567"
    finally:
        runtime.rmdir()


def test_linux_skips_shared_or_missing_runtime_dir(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o755)
    data = Path("/srv/coreman")
    for env in ({"XDG_RUNTIME_DIR": str(shared)}, {"XDG_RUNTIME_DIR": "relative"}, {}):
        assert lifecycle.choose_socket_dir(data, NODE, system="Linux", env=env) == data / "run"


def test_tmp_is_only_the_long_path_fallback():
    short = Path("/Users/someone/.local/share/coreman-runtime")
    assert lifecycle.choose_socket_dir(short, NODE, system="Darwin", env={}) == short / "run"
    long = Path("/Users/" + "x" * 90 + "/.local/share/coreman-runtime")
    chosen = lifecycle.choose_socket_dir(long, NODE, system="Darwin", env={}, uid=501)
    assert chosen == Path("/tmp/coreman-501-01234567")
    assert lifecycle.socket_fits(chosen)


def test_private_dir_rejects_symlinks_and_tightens_mode(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (tmp_path / "link").symlink_to(target)
    with pytest.raises(ValueError, match="不安全"):
        lifecycle.ensure_private_dir(tmp_path / "link")
    loose = tmp_path / "run"
    loose.mkdir(mode=0o755)
    lifecycle.ensure_private_dir(loose)
    assert loose.stat().st_mode & 0o777 == 0o700


def test_remove_socket_dirs_only_touches_owned_directories(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "claude.sock").write_text("")
    removed = lifecycle.remove_socket_dirs(tmp_path, NODE)
    assert run in removed and not run.exists()


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------


def test_copytruncate_keeps_an_appending_writer_consistent(tmp_path):
    path = tmp_path / "claude.log"
    with path.open("ab", buffering=0) as writer:
        writer.write(b"a" * 2000)
        assert logs.rotate_copytruncate(path, max_bytes=1000, backups=2)
        writer.write(b"b" * 10)
        assert path.read_bytes() == b"b" * 10
        assert (tmp_path / "claude.log.1").read_bytes() == b"a" * 2000
        assert (tmp_path / "claude.log.1").stat().st_mode & 0o777 == 0o600
        for letter in (b"c", b"d"):
            writer.write(letter * 2000)
            assert logs.rotate_copytruncate(path, max_bytes=1000, backups=2)
    assert not logs.rotate_copytruncate(path, max_bytes=1000, backups=2)
    assert (tmp_path / "claude.log.1").read_bytes() == b"d" * 2000
    assert (tmp_path / "claude.log.2").read_bytes().startswith(b"b" * 10 + b"c")
    assert not (tmp_path / "claude.log.3").exists()


def test_rotator_thread_rotates_watched_files(tmp_path):
    path = tmp_path / "codex.log"
    path.write_bytes(b"x" * 50)
    rotator = logs.LogRotator(interval=0.01)
    rotator.watch(path, max_bytes=10, backups=1)
    rotator.start()
    try:
        for _ in range(200):
            if path.stat().st_size == 0:
                break
            rotator.stopped.wait(0.01)
    finally:
        rotator.stop()
    assert path.stat().st_size == 0 and (tmp_path / "codex.log.1").stat().st_size == 50


@pytest.mark.parametrize("mode,detected", [("ab", True), ("wb", False)])
def test_service_log_is_capped_only_when_stderr_appends_to_it(tmp_path, mode, detected):
    service_log = tmp_path / "service.log"
    code = (
        "import sys; from pathlib import Path; "
        "from runtime_daemon.logs import appended_stderr_file; "
        "print(appended_stderr_file(Path(sys.argv[1])))"
    )
    with service_log.open(mode) as stderr:
        result = subprocess.run(
            [sys.executable, "-c", code, str(service_log)],
            cwd=ROOT,
            stderr=stderr,
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        )
    assert (result.stdout.strip() == str(service_log)) is detected


def test_configure_logging_rotates_and_quiets_http_clients(tmp_path):
    root = logging.getLogger()
    previous = (root.level, {name: logging.getLogger(name).level for name in logs.QUIET_LOGGERS})
    handlers = logs.configure_logging(tmp_path, console_level=logging.CRITICAL)
    try:
        main = handlers[0]
        assert isinstance(main, logging.handlers.RotatingFileHandler)
        assert (main.maxBytes, main.backupCount) == (10 * 1024 * 1024, 5)
        assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
        logging.getLogger("httpx").info('HTTP Request: POST /api/runtime/poll "HTTP/1.1 200 OK"')
        logging.getLogger("coreman-runtime").info("driver started")
        main.flush()
        content = (tmp_path / "runtime.log").read_text()
        assert "driver started" in content and "/api/runtime/poll" not in content
        assert (tmp_path / "runtime.log").stat().st_mode & 0o777 == 0o600
    finally:
        for handler in handlers:
            root.removeHandler(handler)
            handler.close()
        root.setLevel(previous[0])
        for name, level in previous[1].items():
            logging.getLogger(name).setLevel(level)


# ---------------------------------------------------------------------------
# State, locks and bundles
# ---------------------------------------------------------------------------


def test_wait_online_ignores_stale_state_and_surfaces_fatal_errors(tmp_path):
    ticks = iter(range(100))
    clock = lambda: float(next(ticks))  # noqa: E731
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"online": True, "updated_at": 1}))
    with pytest.raises(TimeoutError):
        lifecycle.wait_online(tmp_path, since=50, timeout=3, sleep=lambda _: None, clock=clock)
    lifecycle.record_fatal(tmp_path, "安装链接已过期")
    with pytest.raises(FatalConfigError, match="已过期"):
        lifecycle.wait_online(tmp_path, since=0, timeout=3, sleep=lambda _: None)
    state.write_text(json.dumps({"online": True, "updated_at": 10**12}))
    lifecycle.wait_online(tmp_path, since=0, timeout=3, sleep=lambda _: None)


def test_install_lock_is_exclusive(tmp_path):
    with lifecycle.install_lock(tmp_path):
        assert lifecycle.lock_held(tmp_path / "install.lock")
        with pytest.raises(InstallError, match="正在进行"), lifecycle.install_lock(tmp_path):
            pass
    assert not lifecycle.lock_held(tmp_path / "install.lock")


def test_bundle_checksum_comes_from_argument_or_sidecar(tmp_path):
    bundle = tmp_path / "coreman-runtime-linux-amd64.tar.gz"
    bundle.write_bytes(b"bundle")
    digest = hashlib.sha256(b"bundle").hexdigest()
    with pytest.raises(InstallError, match="sha256"):
        lifecycle.read_expected_sha256(bundle)
    (tmp_path / (bundle.name + ".sha256")).write_text(digest.upper() + "\n")
    assert lifecycle.read_expected_sha256(bundle) == digest
    assert lifecycle.load_bundle(bundle, digest) == b"bundle"
    with pytest.raises(InstallError, match="校验失败"):
        lifecycle.load_bundle(bundle, "0" * 64)
    with pytest.raises(InstallError, match="格式"):
        lifecycle.read_expected_sha256(bundle, "abc")


def test_extract_rejects_escaping_and_special_members(tmp_path):
    import io
    import tarfile

    escaping = make_bundle({"../evil.txt": ("x", 0o644)})
    with pytest.raises(InstallError, match="无效文件"):
        lifecycle.extract_bundle(escaping, tmp_path / "a")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        link = tarfile.TarInfo("runtime_daemon/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)
    with pytest.raises(InstallError, match="无效文件"):
        lifecycle.extract_bundle(buffer.getvalue(), tmp_path / "b")


def test_extract_accepts_a_target_below_a_symlinked_home(tmp_path):
    real = tmp_path / "data-home"
    real.mkdir()
    (tmp_path / "home").symlink_to(real)
    target = tmp_path / "home" / "release"
    target.mkdir()
    lifecycle.extract_bundle(make_bundle({"runtime_daemon/__init__.py": ("", 0o644)}), target)
    assert (real / "release/runtime_daemon/__init__.py").exists()


def test_unix_socket_limit_matches_the_platform(tmp_path):
    directory = Path("/tmp") / f"cm-limit-{os.getpid()}"
    directory.mkdir(exist_ok=True)
    try:
        path = directory / ("x" * (lifecycle.SOCKET_PATH_LIMIT - len(str(directory)) - 1))
        assert len(os.fsencode(str(path))) == lifecycle.SOCKET_PATH_LIMIT
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(path))
    finally:
        for child in directory.iterdir():
            child.unlink()
        directory.rmdir()
