"""Install, upgrade and uninstall without touching the developer's real services.

Service managers are replaced by recorders; ``install.sh`` runs for real against a fake
HTTPS server with a stub ``install_service`` and a symlinked ``$HOME``.
"""

import base64
import hashlib
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from runtime_daemon import install_service, lifecycle, supervisor
from runtime_daemon.lifecycle import EXIT_CONFIG, FatalConfigError, InstallError
from tests.unit.runtime_daemon_support import https_server, make_bundle, make_pki, release_files

ROOT = Path(__file__).resolve().parents[2]


class Recorder:
    """Stands in for ``subprocess.run``; commands containing a word in ``fail`` fail."""

    def __init__(self, fail=()):
        self.calls: list[list[str]] = []
        self.fail = set(fail)

    def __call__(self, command, check=False, **kwargs):
        self.calls.append([str(x) for x in command])
        if check and self.fail.intersection(map(str, command)):
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def ran(self, *words):
        return any(all(word in call for word in words) for call in self.calls)


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    return path


@pytest.fixture
def systemd(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(install_service.subprocess, "run", recorder)
    monkeypatch.setattr(install_service, "detect_manager", lambda: "systemd")
    monkeypatch.setattr(install_service, "systemd_status", lambda: "systemd-user")
    return recorder


def staged_config(tmp_path, **extra):
    path = tmp_path / "stage-config.json"
    path.write_text(json.dumps({"release": str(tmp_path / "release-a"), "path": "/bin", **extra}))
    return path


# ---------------------------------------------------------------------------
# Service definitions
# ---------------------------------------------------------------------------


def test_service_definitions_do_not_restart_configuration_errors(tmp_path):
    cfg = {"release": "/srv/rt/release-1", "path": "/usr/bin"}
    unit = install_service.systemd_unit(cfg, tmp_path / "config.json")
    assert f"RestartPreventExitStatus={EXIT_CONFIG}" in unit
    assert "Restart=on-failure" in unit
    plist = install_service.launchd_definition(cfg, tmp_path / "config.json")
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["StandardErrorPath"].endswith("service.log")
    assert plistlib.loads(plistlib.dumps(plist))["ProgramArguments"][0].startswith(cfg["release"])
    assert lifecycle.fatal_exit_status("launchd") == 0
    assert lifecycle.fatal_exit_status("systemd-user") == EXIT_CONFIG


def test_supervisor_stops_when_the_daemon_reports_a_configuration_error(tmp_path, monkeypatch):
    release = tmp_path / "release"
    python = release / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text(f"#!/bin/sh\nexit {EXIT_CONFIG}\n")
    python.chmod(0o700)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"release": str(release)}))
    monkeypatch.setattr(supervisor.signal, "signal", lambda *args: None)
    assert supervisor.main(["--config", str(config)]) == EXIT_CONFIG
    assert not (tmp_path / "supervisor.pid").exists()


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


def test_registration_failure_leaves_no_identity_or_unit(tmp_path, home, systemd):
    systemd.fail = {"enable"}
    config = tmp_path / "config.json"
    with pytest.raises(InstallError, match="服务注册失败"):
        install_service.install(config, staged_config(tmp_path))
    assert not config.exists()
    assert not install_service.systemd_unit_path().exists()
    assert systemd.ran("disable", "--now")


def test_rejected_enrollment_during_install_is_rolled_back(tmp_path, home, systemd, monkeypatch):
    def rejected(*args, **kwargs):
        raise FatalConfigError("安装注册被 CoreMan 拒绝（HTTP 410：安装链接已过期）")

    monkeypatch.setattr(install_service, "wait_online", rejected)
    config = tmp_path / "config.json"
    with pytest.raises(InstallError, match="已过期"):
        install_service.install(config, staged_config(tmp_path))
    assert not config.exists() and not install_service.systemd_unit_path().exists()


def test_unconfirmed_install_is_kept_for_a_restart(tmp_path, home, systemd, monkeypatch):
    def slow(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(install_service, "wait_online", slow)
    config = tmp_path / "config.json"
    code = install_service.install(config, staged_config(tmp_path))
    assert code == install_service.EXIT_NOT_CONFIRMED
    assert json.loads(config.read_text())["service_status"] == "systemd-user"
    assert config.stat().st_mode & 0o777 == 0o600
    assert install_service.systemd_unit_path().exists()
    assert systemd.ran("restart", "coreman-runtime")


def test_install_never_overwrites_an_identity(tmp_path, home, systemd):
    config = tmp_path / "config.json"
    config.write_text('{"node_id": "existing"}')
    with pytest.raises(InstallError, match="已有 Runtime"):
        install_service.install(config, staged_config(tmp_path))
    assert json.loads(config.read_text()) == {"node_id": "existing"}
    assert not systemd.calls


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


@pytest.fixture
def upgradable(tmp_path, monkeypatch):
    data = tmp_path / "coreman-runtime"
    old = data / "release-old"
    older = data / "release-older"
    for path in (old, older):
        path.mkdir(parents=True)
    config = data / "config.json"
    config.write_text(
        json.dumps({"release": str(old), "service_status": "systemd-user", "node_id": "n"})
    )
    bundle = tmp_path / "coreman-runtime-linux-amd64.tar.gz"
    bundle.write_bytes(make_bundle(release_files()))
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()

    def fake_venv(release):
        python = release / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)

    events: list[tuple] = []
    monkeypatch.setattr(lifecycle, "create_venv", fake_venv)
    monkeypatch.setattr(
        install_service, "stop_service", lambda kind, path, *a: events.append(("stop", kind))
    )
    monkeypatch.setattr(
        install_service, "start_service", lambda kind, path: events.append(("start", kind))
    )
    monkeypatch.setattr(
        install_service,
        "write_service",
        lambda kind, cfg, path: events.append(("write", Path(cfg["release"]).name)),
    )
    return {
        "config": config,
        "bundle": bundle,
        "digest": digest,
        "old": old,
        "older": older,
        "events": events,
        "data": data,
    }


def test_upgrade_switches_release_and_keeps_one_previous(upgradable, monkeypatch):
    monkeypatch.setattr(install_service, "wait_online", lambda *args, **kwargs: None)
    install_service.upgrade(upgradable["config"], upgradable["bundle"], upgradable["digest"])
    release = Path(json.loads(upgradable["config"].read_text())["release"])
    assert release.name.startswith("release-" + upgradable["digest"][:16])
    assert (release / "runtime_daemon/bin/runtime-claude").exists()
    assert upgradable["old"].exists() and not upgradable["older"].exists()
    assert [e[0] for e in upgradable["events"]] == ["stop", "write", "start"]
    assert not list(upgradable["data"].glob("stage-*"))


def test_failed_upgrade_rolls_back_to_the_previous_release(upgradable, monkeypatch):
    outcomes = iter([TimeoutError("not online"), None])

    def wait(*args, **kwargs):
        outcome = next(outcomes)
        if outcome:
            raise outcome

    monkeypatch.setattr(install_service, "wait_online", wait)
    with pytest.raises(InstallError, match="已回滚到 release-old"):
        install_service.upgrade(upgradable["config"], upgradable["bundle"], upgradable["digest"])
    assert json.loads(upgradable["config"].read_text())["release"] == str(upgradable["old"])
    assert not list(upgradable["data"].glob("release-" + upgradable["digest"][:16] + "*"))
    writes = [e[1] for e in upgradable["events"] if e[0] == "write"]
    assert writes[0].startswith("release-" + upgradable["digest"][:16])
    assert writes[-1] == "release-old"


def test_upgrade_checks_the_bundle_before_touching_the_service(upgradable):
    with pytest.raises(InstallError, match="校验失败"):
        install_service.upgrade(upgradable["config"], upgradable["bundle"], "0" * 64)
    assert not upgradable["events"]
    assert sorted(p.name for p in upgradable["data"].glob("release-*")) == [
        "release-old",
        "release-older",
    ]


def test_upgrade_aborts_when_the_new_drivers_cannot_run(upgradable, monkeypatch):
    files = release_files()
    files["runtime_daemon/bin/runtime-codex"] = ("#!/bin/sh\nexit 1\n", 0o755)
    upgradable["bundle"].write_bytes(make_bundle(files))
    digest = hashlib.sha256(upgradable["bundle"].read_bytes()).hexdigest()
    with pytest.raises(InstallError, match="自检失败"):
        install_service.upgrade(upgradable["config"], upgradable["bundle"], digest)
    assert not upgradable["events"]
    assert not list(upgradable["data"].glob("release-" + digest[:16] + "*"))


def test_cli_rejects_flag_combinations(capsys):
    with pytest.raises(SystemExit):
        install_service.main(["--purge"])
    with pytest.raises(SystemExit):
        install_service.main(["--uninstall", "--sha256", "0" * 64])
    assert "--purge" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------


def test_uninstall_keeps_identity_unless_purged(tmp_path, home, systemd):
    data = home / ".local/share/coreman-runtime"
    current, stale = data / "release-current", data / "release-stale"
    for path in (current, stale, data / "cli-bin", data / "run", data / "sessions/claude"):
        path.mkdir(parents=True)
    config = data / "config.json"
    config.write_text(
        json.dumps(
            {"node_id": "0123456789", "release": str(current), "service_status": "systemd-user"}
        )
    )
    unit = install_service.systemd_unit_path()
    unit.parent.mkdir(parents=True)
    unit.write_text("[Unit]\n")

    assert install_service.main(["--uninstall", "--config", str(config)]) == 0
    assert systemd.ran("stop", "coreman-runtime") and systemd.ran("disable", "--now")
    assert not unit.exists()
    assert not (data / "cli-bin").exists() and not (data / "run").exists()
    assert current.exists() and not stale.exists() and (data / "sessions/claude").exists()
    assert json.loads(config.read_text())["service_status"] == "uninstalled"

    assert install_service.main(["--uninstall", "--purge", "--config", str(config)]) == 0
    assert not data.exists()


def test_purge_refuses_directories_that_are_not_an_install(tmp_path, home, systemd):
    stranger = tmp_path / "documents"
    stranger.mkdir()
    (stranger / "notes.txt").write_text("keep")
    code = install_service.main(["--uninstall", "--purge", "--config", str(stranger / "x.json")])
    assert code == 1 and (stranger / "notes.txt").exists()


# ---------------------------------------------------------------------------
# install.sh
# ---------------------------------------------------------------------------

STUB_INSTALL_SERVICE = """
import argparse, os, shutil, sys
parser = argparse.ArgumentParser()
parser.add_argument("--config")
parser.add_argument("--staged-config")
args = parser.parse_args()
if os.environ.get("FAKE_INSTALL_MODE") == "ok":
    shutil.copy(args.staged_config, args.config)
    sys.exit(0)
print("launchctl bootstrap failed: 5", file=sys.stderr)
sys.exit(1)
"""


def test_install_script_retries_cleanly_with_private_ca_and_symlinked_home(tmp_path):
    pki = make_pki(tmp_path / "pki")
    bundle = make_bundle(release_files(STUB_INSTALL_SERVICE))
    digest = hashlib.sha256(bundle).hexdigest()
    real_home = tmp_path / "data-home"
    real_home.mkdir()
    (tmp_path / "home").symlink_to(real_home)
    workspace = (tmp_path / "projects").resolve()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "python3").symlink_to(sys.executable)
    install_dir = real_home.resolve() / ".local/share/coreman-runtime"

    def respond(path):
        if path.endswith("/bundle"):
            return 200, {"X-SHA256": digest, "Content-Type": "application/gzip"}, bundle
        return 404, {}, b""

    def run(mode, **cfg):
        script = (ROOT / "runtime_daemon/install.sh").read_text()
        payload = {
            "api_url": f"https://127.0.0.1:{port}",
            "install_token": "link.token",
            "workspace_root": str(workspace),
            **cfg,
        }
        path = tmp_path / "install.sh"
        path.write_text(
            script.replace(
                "__COREMAN_CONFIG__", base64.b64encode(json.dumps(payload).encode()).decode()
            )
        )
        env = {
            "HOME": str(tmp_path / "home"),
            "PATH": f"{bindir}:/usr/bin:/bin",
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
            "FAKE_INSTALL_MODE": mode,
        }
        return subprocess.run(
            ["sh", str(path)], env=env, capture_output=True, text=True, timeout=300
        )

    with https_server(pki, respond) as port:
        untrusted = run("ok")
        assert untrusted.returncode != 0 and "私有 CA" in untrusted.stderr
        assert not (install_dir / "config.json").exists()

        failed = run("fail", ca_pem=pki.ca_pem)
        assert failed.returncode != 0, failed.stderr
        assert "服务注册失败，已清理本次安装" in failed.stderr
        assert not (install_dir / "config.json").exists()
        assert not (install_dir / "ca.pem").exists()
        assert not list(install_dir.glob("release-*")) and not list(install_dir.glob("stage-*"))

        (install_dir / "release-interrupted").mkdir()
        installed = run("ok", ca_pem=pki.ca_pem)
        assert installed.returncode == 0, installed.stderr

    config = json.loads((install_dir / "config.json").read_text())
    assert "ca_pem" not in config
    assert config["ca_file"] == str(install_dir / "ca.pem")
    assert (install_dir / "ca.pem").read_text() == pki.ca_pem
    assert (install_dir / "ca.pem").stat().st_mode & 0o777 == 0o600
    release = Path(config["release"])
    assert release.parent == install_dir and (release / ".venv/bin/python").exists()
    assert [p.name for p in install_dir.glob("release-*")] == [release.name]
    assert not list(install_dir.glob("stage-*"))

    again = run("ok", ca_pem=pki.ca_pem)
    assert again.returncode != 0 and "--uninstall --purge" in again.stderr
