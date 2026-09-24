"""Install, upgrade and uninstall without touching the developer's real services.

Service managers are replaced by recorders; ``install.sh`` runs for real against a fake
HTTPS server with a stub ``install_service`` and a symlinked ``$HOME``.
"""

import base64
import fcntl
import hashlib
import json
import os
import plistlib
import shutil
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


def test_systemd_working_directory_is_not_quoted(tmp_path):
    # systemd 不解析 WorkingDirectory= 的引号，带引号时报 "path is not absolute" 并拒绝整个 unit。
    unit = install_service.systemd_unit({"release": "/srv/rt/50%/release-1"}, tmp_path / "c.json")
    assert "WorkingDirectory=/srv/rt/50%%/release-1\n" in unit


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
# Management command
# ---------------------------------------------------------------------------

ECHO_INSTALL_SERVICE = """
import json, os, sys
def main():
    print(json.dumps({"argv": sys.argv, "cwd": os.getcwd(), "module": __file__}))
    return 0
"""


def fake_release(path: Path, install_service_source: str) -> Path:
    (path / "runtime_daemon").mkdir(parents=True)
    (path / "runtime_daemon/__init__.py").write_text("")
    (path / "runtime_daemon/install_service.py").write_text(install_service_source)
    python = path / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    return path


def test_manage_command_ignores_the_callers_directory(tmp_path):
    data = tmp_path / "coreman runtime"
    release = fake_release(data / "release-a", ECHO_INSTALL_SERVICE)
    config = data / "config.json"
    command = lifecycle.write_manage_command(config, release)
    assert command == data / "bin/coreman-runtime"
    assert command.stat().st_mode & 0o777 == 0o700
    # A checkout in the current directory must not shadow the release's module.
    fake_release(tmp_path / "checkout", "raise SystemExit('shadowed')")
    env = {**os.environ, "PYTHONPATH": str(tmp_path / "checkout")}

    def run(*args):
        result = subprocess.run(
            [str(command), *args],
            cwd=tmp_path / "checkout",
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    upgrade = run("--upgrade", "bundle.tar.gz")
    assert upgrade["module"] == str(release / "runtime_daemon/install_service.py")
    assert upgrade["argv"] == [
        "coreman-runtime",
        "--config",
        str(config),
        "--upgrade",
        "bundle.tar.gz",
    ]
    assert upgrade["cwd"] == str(tmp_path / "checkout")
    assert run()["argv"][-1] == "--help"

    before = command.stat().st_mtime_ns
    lifecycle.write_manage_command(config, release)
    assert command.stat().st_mtime_ns == before
    lifecycle.write_manage_command(config, data / "release-b")
    assert "release-b" in command.read_text() and "release-a" not in command.read_text()


def test_manage_command_help_names_itself(tmp_path, capsys):
    with pytest.raises(SystemExit):
        install_service.main(["--help"])
    output = capsys.readouterr().out
    assert "--register" in output and "--retire" not in output and "--replace" in output


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
    assert not lifecycle.manage_command_path(config).exists()
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
    assert str(tmp_path / "release-a") in lifecycle.manage_command_path(config).read_text()


def test_install_never_overwrites_an_identity(tmp_path, home, systemd):
    config = tmp_path / "config.json"
    config.write_text('{"node_id": "existing"}')
    with pytest.raises(InstallError, match="本机已安装"):
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
    assert str(release) in lifecycle.manage_command_path(upgradable["config"]).read_text()
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
    command = lifecycle.manage_command_path(upgradable["config"]).read_text()
    assert str(upgradable["old"]) in command and upgradable["digest"][:16] not in command


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


def test_uninstall_keeps_identity_unless_purged(tmp_path, home, systemd, capsys):
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
    command = lifecycle.manage_command_path(config)
    assert str(current) in command.read_text()
    assert f"{command} --register" in capsys.readouterr().out

    assert install_service.main(["--uninstall", "--purge", "--config", str(config)]) == 0
    assert not data.exists()


def test_register_restores_the_service_from_the_kept_identity(tmp_path, home, systemd, monkeypatch):
    monkeypatch.setattr(install_service, "wait_online", lambda *args, **kwargs: None)
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"release": str(tmp_path / "release-a"), "service_status": "uninstalled"})
    )
    assert install_service.main(["--register", "--config", str(config)]) == 0
    assert json.loads(config.read_text())["service_status"] == "systemd-user"
    assert systemd.ran("enable", "coreman-runtime") and systemd.ran("restart", "coreman-runtime")


def retired_install(home):
    data = home / ".local/share/coreman-runtime"
    for path in (
        data / "release-old/.venv",
        data / "sessions/claude",
        data / "stage-new",
        data / "bin",
    ):
        path.mkdir(parents=True)
    (data / "install.lock").write_text("")
    (data / "runtime.log").write_text("old log")
    config = data / "config.json"
    config.write_text(json.dumps({"node_id": "0123456789", "service_status": "systemd-user"}))
    unit = install_service.systemd_unit_path()
    unit.parent.mkdir(parents=True)
    unit.write_text("[Unit]\n")
    return data, config, unit


def test_retire_stops_the_runtime_and_moves_everything_aside(tmp_path, home, systemd):
    data, config, unit = retired_install(home)
    backup = tmp_path / "coreman-runtime.bak"
    assert install_service.main(["--config", str(config), "--retire", str(backup)]) == 0
    assert systemd.ran("disable", "--now") and not unit.exists()
    assert sorted(p.name for p in data.iterdir()) == ["install.lock", "stage-new"]
    assert json.loads((backup / "config.json").read_text())["node_id"] == "0123456789"
    assert (backup / "runtime.log").read_text() == "old log"
    assert (backup / "release-old/.venv").is_dir() and (backup / "sessions/claude").is_dir()
    assert backup.stat().st_mode & 0o777 == 0o700


def test_retire_moves_nothing_while_the_daemon_still_runs(tmp_path, home, systemd, monkeypatch):
    data, config, _ = retired_install(home)
    # The lock stays held on purpose: give up at once instead of waiting STOP_TIMEOUT_SECONDS.
    wait = install_service.wait_for_unlock
    monkeypatch.setattr(install_service, "wait_for_unlock", lambda path, timeout: wait(path, 0))
    with (data / "daemon.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        code = install_service.main(["--config", str(config), "--retire", str(tmp_path / "bak")])
    assert code == 1
    assert config.exists() and (data / "runtime.log").exists() and not (tmp_path / "bak").exists()


def test_retire_leaves_the_service_alone_when_no_backup_can_be_made(
    tmp_path, home, systemd, monkeypatch, capsys
):
    # Like a data directory bind-mounted under a read-only ~/.local/share.
    data, config, unit = retired_install(home)
    code = install_service.main(["--config", str(config), "--retire", str(tmp_path / "ro/bak")])
    assert code == 1 and "未改动现有 Runtime" in capsys.readouterr().err
    assert not systemd.calls and unit.exists() and config.exists()

    real_stat = Path.stat

    def other_device(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if path.name != "bak":
            return result
        return os.stat_result((*result[:2], result.st_dev + 1, *result[3:]))

    monkeypatch.setattr(Path, "stat", other_device)
    code = install_service.main(["--config", str(config), "--retire", str(tmp_path / "bak")])
    assert code == 1 and "不在同一文件系统" in capsys.readouterr().err
    assert not systemd.calls and unit.exists() and not (tmp_path / "bak").exists()


def test_purge_refuses_directories_that_are_not_an_install(tmp_path, home, systemd):
    stranger = tmp_path / "documents"
    stranger.mkdir()
    (stranger / "notes.txt").write_text("keep")
    code = install_service.main(["--uninstall", "--purge", "--config", str(stranger / "x.json")])
    assert code == 1 and (stranger / "notes.txt").exists()


# ---------------------------------------------------------------------------
# install.sh
# ---------------------------------------------------------------------------

# Registration is faked; --retire runs the real code, which finds no service in the fake $HOME.
STUB_INSTALL_SERVICE = """
import argparse, json, os, pathlib, shutil, sys
if "--retire" in sys.argv:
    from runtime_daemon import real_install_service
    sys.exit(real_install_service.main())
from runtime_daemon.lifecycle import write_manage_command
parser = argparse.ArgumentParser()
parser.add_argument("--config")
parser.add_argument("--staged-config")
parser.add_argument("--register", action="store_true")
args = parser.parse_args()
if args.register:
    print("registered")
    sys.exit(0)
if os.environ.get("FAKE_INSTALL_MODE") == "ok":
    shutil.copy(args.staged_config, args.config)
    release = json.loads(pathlib.Path(args.staged_config).read_text())["release"]
    write_manage_command(pathlib.Path(args.config), pathlib.Path(release))
    sys.exit(0)
print("launchctl bootstrap failed: 5", file=sys.stderr)
sys.exit(1)
"""


def install_script_bundle() -> bytes:
    files = release_files(STUB_INSTALL_SERVICE)
    for name in ("lifecycle.py", "install_service.py"):
        source = (ROOT / "runtime_daemon" / name).read_text()
        files["runtime_daemon/" + name.replace("install_service", "real_install_service")] = (
            source,
            0o644,
        )
    return make_bundle(files)


def test_install_script_retries_cleanly_with_private_ca_and_symlinked_home(tmp_path):
    pki = make_pki(tmp_path / "pki")
    bundle = install_script_bundle()
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

    def run(mode, *args, **cfg):
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
            ["sh", str(path), *args], env=env, capture_output=True, text=True, timeout=300
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
    assert again.returncode != 0 and "未做任何改动" in again.stderr
    assert "同一平台" in again.stderr and "| sh -s -- --replace" in again.stderr
    assert "--cacert coreman-ca.pem" in again.stderr
    assert f"{install_dir}/bin/coreman-runtime --upgrade" in again.stderr

    def printed(result, marker):
        return next(line.strip() for line in result.stderr.splitlines() if marker in line)

    # Every printed command must work from any directory, not only from inside the release.
    upgrade = printed(again, "--upgrade")
    assert (
        subprocess.run(
            ["sh", "-c", upgrade.split(" --upgrade")[0] + " --help"],
            cwd=tmp_path,
            capture_output=True,
        ).returncode
        == 0
    )

    (install_dir / "config.json").write_text(
        json.dumps({**config, "service_status": "uninstalled"})
    )
    uninstalled = run("ok", ca_pem=pki.ca_pem)
    assert f"{install_dir}/bin/coreman-runtime --register" in uninstalled.stderr

    # Installs from before bin/coreman-runtime get a command that changes into the release.
    shutil.rmtree(install_dir / "bin")
    legacy = run("ok", ca_pem=pki.ca_pem)
    assert "恢复原节点" in legacy.stderr
    restore = printed(legacy, "runtime_daemon.install_service")
    assert restore.startswith("cd ") and restore.endswith("-m runtime_daemon.install_service")
    assert (
        subprocess.run(
            ["sh", "-c", restore + " --help"], cwd=tmp_path, capture_output=True
        ).returncode
        == 0
    )

    shutil.rmtree(release)
    broken = run("ok", ca_pem=pki.ca_pem)
    assert (
        broken.returncode != 0 and "--replace" in broken.stderr and "--upgrade" not in broken.stderr
    )


def test_install_script_replace_backs_up_the_existing_runtime(tmp_path):
    pki = make_pki(tmp_path / "pki")
    bundle = install_script_bundle()
    digest = hashlib.sha256(bundle).hexdigest()
    home = tmp_path / "home"
    home.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "python3").symlink_to(sys.executable)
    install_dir = home.resolve() / ".local/share/coreman-runtime"

    def respond(path):
        if "expired" in path:
            return 410, {}, b""
        if path.endswith("/bundle"):
            return 200, {"X-SHA256": digest, "Content-Type": "application/gzip"}, bundle
        return 404, {}, b""

    def run(mode, *args, token="link.token"):
        payload = {
            "api_url": f"https://127.0.0.1:{port}",
            "install_token": token,
            "workspace_root": str(tmp_path / "projects"),
            "ca_pem": pki.ca_pem,
        }
        path = tmp_path / "install.sh"
        path.write_text(
            (ROOT / "runtime_daemon/install.sh")
            .read_text()
            .replace("__COREMAN_CONFIG__", base64.b64encode(json.dumps(payload).encode()).decode())
        )
        env = {
            "HOME": str(home),
            "PATH": f"{bindir}:/usr/bin:/bin",
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
            "FAKE_INSTALL_MODE": mode,
        }
        return subprocess.run(
            ["sh", str(path), *args], env=env, capture_output=True, text=True, timeout=300
        )

    def backups():
        return sorted(install_dir.parent.glob("coreman-runtime.bak-*"))

    # The previous runtime: another platform's identity plus sessions and logs.
    install_dir.mkdir(parents=True)
    old_config = {"api_url": "http://localhost:8081", "node_id": "old-node-id"}
    (install_dir / "config.json").write_text(json.dumps(old_config))
    (install_dir / "sessions").mkdir()
    (install_dir / "runtime.log").write_text("old log")

    with https_server(pki, respond) as port:
        assert run("ok", "--force").returncode != 0

        expired = run("ok", "--replace", token="expired.token")
        assert expired.returncode != 0 and "安装包下载失败" in expired.stderr
        assert json.loads((install_dir / "config.json").read_text()) == old_config
        assert not backups()

        failed = run("fail", "--replace")
        assert failed.returncode != 0 and "服务注册失败" in failed.stderr
        assert "要恢复原 Runtime" in failed.stderr
        assert not (install_dir / "config.json").exists()
        [backup] = backups()
        assert json.loads((backup / "config.json").read_text()) == old_config
        assert (backup / "runtime.log").read_text() == "old log"

        restore = next(line.strip() for line in failed.stderr.splitlines() if "&& mv " in line)
        assert subprocess.run(["sh", "-c", restore], capture_output=True).returncode == 0
        assert json.loads((install_dir / "config.json").read_text()) == old_config
        assert not backups()

        replaced = run("ok", "--replace")
        assert replaced.returncode == 0, replaced.stderr
        assert "已替换原 Runtime" in replaced.stdout and "http://localhost:8081" in replaced.stdout

    config = json.loads((install_dir / "config.json").read_text())
    assert config["api_url"].startswith("https://127.0.0.1:") and "node_id" not in config
    assert (install_dir / "ca.pem").read_text() == pki.ca_pem
    assert (install_dir / "bin/coreman-runtime").is_file()
    [backup] = backups()
    assert json.loads((backup / "config.json").read_text()) == old_config
    assert (backup / "sessions").is_dir() and not (install_dir / "runtime.log").exists()
    assert (backup / "install.lock").exists() is False
