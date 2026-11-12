"""助理沙箱的参数与自检：参数逐项可查；真起沙箱的用例只在装了 bubblewrap 的 Linux 上跑。"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from runtime_daemon import sandbox

HAS_BWRAP = platform.system() == "Linux" and shutil.which("bwrap") is not None
RELEASE = Path(__file__).resolve().parents[2]


def layout(tmp_path: Path) -> sandbox.TurnLayout:
    turns = tmp_path / "turns"
    sockets = Path(tempfile.mkdtemp(dir="/tmp"))
    built = sandbox.TurnLayout.new(turns, sockets)
    built.create()
    return built


def test_layout_keeps_sockets_short_and_dirs_private(tmp_path):
    built = layout(tmp_path)
    try:
        assert len(str(built.driver_socket)) < 100
        for name in sandbox.INSIDE:
            assert built.host(name).is_dir()
            assert built.host(name).stat().st_mode & 0o077 == 0
        assert built.secrets.is_dir()
    finally:
        built.remove()
    assert not built.root.exists() and not built.run.exists()


def test_system_mounts_bind_dirs_link_symlinks_and_skip_missing(tmp_path):
    real = tmp_path / "usr"
    real.mkdir()
    link = tmp_path / "bin"
    link.symlink_to("usr")
    missing = tmp_path / "lib32"
    args = sandbox.system_mounts([str(real), str(link), str(missing)])
    assert args == ["--ro-bind", str(real), str(real), "--symlink", "usr", str(link)]


def test_package_root_takes_the_whole_npm_package(tmp_path):
    package = tmp_path / "lib" / "node_modules" / "@vendor" / "cli"
    (package / "bin").mkdir(parents=True)
    (package / "package.json").write_text("{}")
    entry = package / "bin" / "cli.js"
    entry.write_text("#!/usr/bin/env node\n")
    assert sandbox.package_root(entry) == package
    single = tmp_path / "opt" / "tool"
    single.parent.mkdir()
    single.write_bytes(b"\x7fELF")
    assert sandbox.package_root(single) == single.parent


def test_cli_mounts_add_node_for_script_entries(tmp_path):
    entry = tmp_path / "pkg" / "cli.js"
    entry.parent.mkdir()
    entry.write_text("#!/usr/bin/env node\n")
    node = tmp_path / "node-bin" / "node"
    node.parent.mkdir()
    node.write_bytes(b"\x7fELF")
    paths = {"claude": str(entry), "node": str(node)}
    assert sandbox.cli_mounts("claude", paths.get) == [entry.parent, node.parent]
    assert sandbox.cli_mounts("codex", paths.get) == []


def test_link_cli_points_into_the_turn_bin(tmp_path):
    built = layout(tmp_path)
    try:
        target = tmp_path / "claude-real"
        target.write_bytes(b"\x7fELF")
        assert sandbox.link_cli(built, "claude", {"claude": str(target)}.get)
        assert (built.host("bin") / "claude").resolve() == target.resolve()
        assert not sandbox.link_cli(built, "codex", {}.get)
    finally:
        built.remove()


def test_env_starts_empty_and_only_adds_paths_proxy_and_credentials():
    base = {
        "HOME": "/home/runner",
        "PATH": "/x",
        "ANTHROPIC_BASE_URL": "https://evil.example.com",
        "COREMAN_NODE_TOKEN": "t",
        "LANG": "en_US.UTF-8",
        "NODE_EXTRA_CA_CERTS": "/etc/ssl/private-ca.pem",
    }
    env = sandbox.sandbox_env("claude", base, {"ANTHROPIC_API_KEY": "k"})
    assert env["HOME"] == "/turn/home" and env["TMPDIR"] == "/turn/tmp"
    assert env["CLAUDE_CONFIG_DIR"] == "/turn/config"
    assert env["HTTPS_PROXY"] == env["https_proxy"] == sandbox.PROXY_URL
    assert env["NO_PROXY"] == ""
    assert env["LANG"] == "en_US.UTF-8"
    assert env["NODE_EXTRA_CA_CERTS"] == "/etc/ssl/private-ca.pem"
    assert env["ANTHROPIC_API_KEY"] == "k"
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert "ANTHROPIC_BASE_URL" not in env and "COREMAN_NODE_TOKEN" not in env
    codex = sandbox.sandbox_env("codex", {}, {})
    assert codex["CODEX_HOME"] == "/turn/config" and "CLAUDE_CONFIG_DIR" not in codex


def test_bwrap_argv_isolates_and_mounts_in_order(tmp_path):
    built = layout(tmp_path)
    try:
        work = tmp_path / "work"
        work.mkdir()
        cred = built.secrets / "credential"
        cred.write_text("{}")
        launch = sandbox.Launch(
            bwrap="/usr/bin/bwrap",
            layout=built,
            workdir=work,
            provider="claude",
            env={"ANTHROPIC_API_KEY": "sk-secret-value", "HOME": "/turn/home"},
            ro_paths=[Path("/opt/release"), Path("/opt/release")],
            credential=cred,
            system=["--ro-bind", "/usr", "/usr"],
        )
        args = sandbox.bwrap_argv(launch, ["driver", "--x"])
        assert args[:4] == [
            "/usr/bin/bwrap",
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
        ]
        assert "--share-net" not in args
        joined = " ".join(args)
        # 环境变量不进参数：/proc/<pid>/cmdline 本机其他用户都读得到。
        assert "--setenv" not in args and "sk-secret-value" not in joined
        assert "--ro-bind /opt/release /opt/release" in joined
        assert joined.count("/opt/release /opt/release") == 1
        assert f"--bind {work} /work" in joined
        assert f"--bind {built.run} /turn/run" in joined
        # 凭据在配置目录挂好之后才盖上去，而且只读。
        config_at = args.index(str(built.host("config")))
        cred_at = args.index(str(cred))
        assert cred_at > config_at
        assert args[cred_at - 1 : cred_at + 2] == [
            "--ro-bind",
            str(cred),
            "/turn/config/.credentials.json",
        ]
        assert args[-4:] == ["/work", "--", "driver", "--x"]
        # 本机的家目录、数据目录一律不出现在参数里。
        assert str(Path.home()) not in joined.replace(str(tmp_path), "")
    finally:
        built.remove()


def test_bridge_and_driver_commands_point_inside_the_turn():
    driver = sandbox.driver_command(Path("/opt/release/runtime_daemon/bin/runtime-claude"))
    assert driver[driver.index("--socket") + 1] == "/turn/run/d.sock"
    assert driver[driver.index("--sessions-dir") + 1] == "/turn/driver"
    assert driver[driver.index("--proxy") + 1] == sandbox.PROXY_URL
    bridge = sandbox.bridge_command("/py", Path("/opt/bridge.py"), driver)
    assert bridge[:3] == ["/py", "-I", "/opt/bridge.py"]
    assert bridge[bridge.index("--upstream") + 1] == "/turn/run/e.sock"
    assert bridge[bridge.index("--") + 1 :] == driver


def inputs(tmp_path: Path) -> sandbox.SelfTestInputs:
    sockets = Path(tempfile.mkdtemp(dir="/tmp"))
    (tmp_path / "projects").mkdir(exist_ok=True)
    return sandbox.SelfTestInputs(
        turns_dir=tmp_path / "turns",
        socket_dir=sockets,
        release=RELEASE,
        hidden=[tmp_path / "projects"],
        clis={"claude": [Path("/opt/claude")], "codex": []},
        system=[],
    )


def test_self_test_reports_unsupported_nodes_without_starting_anything(tmp_path, monkeypatch):
    def runner(*_args, **_kwargs):
        raise AssertionError("must not run")

    result = sandbox.self_test(inputs(tmp_path), system_name="Darwin", runner=runner)
    assert result.providers == {"claude": False, "codex": False}
    assert result.code == "unsupported_platform"
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: None)
    missing = sandbox.self_test(inputs(tmp_path), system_name="Linux", runner=runner)
    assert missing.code == "bwrap_missing" and not any(missing.providers.values())


def fake_runner(probe: dict[str, bool], *, start_rc: int = 0, version_rc: int = 0):
    calls: list[list[str]] = []

    def runner(argv, **kwargs):
        calls.append(argv)
        assert kwargs["env"]["HOME"] == "/turn/home" and "PATH" in kwargs["env"]
        command = argv[argv.index("--") + 1 :]
        if command == ["true"]:
            return subprocess.CompletedProcess(argv, start_rc, "", "")
        if "--version" in command:
            return subprocess.CompletedProcess(argv, version_rc, "1.0.0\n", "")
        return subprocess.CompletedProcess(argv, 0, json.dumps(probe) + "\n", "")

    return runner, calls


ALL_GOOD = {"hidden": True, "readonly": True, "offline": True, "bridge": True, "umask": True}


def test_self_test_marks_providers_whose_cli_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "link_cli", lambda *_a, **_k: True)
    runner, calls = fake_runner(ALL_GOOD)
    result = sandbox.self_test(
        inputs(tmp_path), system_name="Linux", bwrap="/usr/bin/bwrap", runner=runner
    )
    assert result.providers == {"claude": True, "codex": False}
    assert result.code == ""
    probe = next(c for c in calls if "-c" in c)
    assert str(tmp_path / "projects") in probe[-1]
    assert all("--unshare-all" in c for c in calls)
    assert not (tmp_path / "turns").exists() or not any((tmp_path / "turns").iterdir())


@pytest.mark.parametrize("failed", ["hidden", "readonly", "offline", "bridge", "umask"])
def test_self_test_fails_closed_on_any_check(tmp_path, monkeypatch, failed):
    monkeypatch.setattr(sandbox, "link_cli", lambda *_a, **_k: True)
    runner, _ = fake_runner({**ALL_GOOD, failed: False})
    result = sandbox.self_test(
        inputs(tmp_path), system_name="Linux", bwrap="/usr/bin/bwrap", runner=runner
    )
    assert result.providers == {"claude": False, "codex": False}
    assert result.code == f"{failed}_check_failed"


def test_self_test_fails_when_the_sandbox_cannot_start(tmp_path):
    runner, _ = fake_runner(ALL_GOOD, start_rc=1)
    result = sandbox.self_test(
        inputs(tmp_path), system_name="Linux", bwrap="/usr/bin/bwrap", runner=runner
    )
    assert result.code == "sandbox_start_failed" and not any(result.providers.values())


@pytest.mark.skipif(not HAS_BWRAP, reason="需要 Linux 与 bubblewrap")
def test_real_sandbox_self_test(tmp_path):
    """真起沙箱：工作区之外看不见、系统目录只读、没有出口、转发口接得通。"""
    hidden = tmp_path / "projects"
    hidden.mkdir()
    result = sandbox.self_test(
        sandbox.SelfTestInputs(
            turns_dir=tmp_path / "turns",
            socket_dir=Path(tempfile.mkdtemp(dir="/tmp")),
            release=RELEASE,
            hidden=[hidden],
            clis={"claude": [Path(sys.executable).resolve().parent], "codex": []},
        )
    )
    # 测试机上不一定装了 claude：只要求沙箱本身的四项检查通过。
    assert result.code in {"", "cli_unavailable"}


def test_private_dir_means_owned_by_me_and_closed_to_everyone_else(tmp_path):
    closed = tmp_path / "closed"
    closed.mkdir(mode=0o700)
    closed.chmod(0o700)
    opened = tmp_path / "opened"
    opened.mkdir()
    opened.chmod(0o750)
    link = tmp_path / "link"
    link.symlink_to(closed)
    assert sandbox.private_dir(closed) and sandbox.private_dir(link)
    assert not sandbox.private_dir(opened)
    assert not sandbox.private_dir(tmp_path / "missing")
    assert not sandbox.private_dir(closed, uid=closed.stat().st_uid + 1)


def test_self_test_fails_when_a_private_dir_is_open(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "link_cli", lambda *_a, **_k: True)
    runner, calls = fake_runner(ALL_GOOD)
    shared = tmp_path / "data"
    shared.mkdir()
    shared.chmod(0o755)
    base = inputs(tmp_path)
    result = sandbox.self_test(
        sandbox.SelfTestInputs(**{**base.__dict__, "private": [shared]}),
        system_name="Linux",
        bwrap="/usr/bin/bwrap",
        runner=runner,
    )
    assert result.code == "private_dir_check_failed" and not any(result.providers.values())
    assert calls == []


def test_probe_offline_needs_an_empty_namespace_and_enetunreach():
    src = sandbox._PROBE
    assert "if_nameindex" in src and "ENETUNREACH" in src


def test_probe_readonly_checks_mount_flags_not_a_write():
    src = sandbox._PROBE
    assert "ST_RDONLY" in src and ".coreman-probe" not in src


def run_probe(spec: dict, ro_ok_port: int = 1) -> dict:
    done = subprocess.run(
        [sys.executable, "-I", "-c", sandbox._PROBE, str(ro_ok_port), json.dumps(spec)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_probe_readonly_fails_without_any_checked_path_and_on_writable_ones(tmp_path):
    assert run_probe({"hidden": [], "ro": [str(tmp_path / "nope")]})["readonly"] is False
    assert run_probe({"hidden": [], "ro": [str(tmp_path)]})["readonly"] is False


def test_probe_offline_is_not_proven_by_a_host_network_namespace():
    # 测试进程在主机网络里（有 lo 以外的接口，或连得出去/超时）：不能判为隔离。
    import socket

    if all(name == "lo" for _, name in socket.if_nameindex()):
        pytest.skip("本机没有 lo 以外的接口")
    assert run_probe({"hidden": [], "ro": []})["offline"] is False


def test_bwrap_refuses_ro_roots_that_contain_turn_or_work_dirs(tmp_path):
    built = layout(tmp_path)
    try:
        work = tmp_path / "work"
        work.mkdir()
        for bad in (built.root.parent, built.run.parent, work, tmp_path):
            launch = sandbox.Launch(
                bwrap="/usr/bin/bwrap",
                layout=built,
                workdir=work,
                provider="claude",
                env={},
                ro_paths=[bad],
                system=[],
            )
            with pytest.raises(ValueError, match="unsafe_mount"):
                sandbox.bwrap_argv(launch, ["true"])
        system = sandbox.Launch(
            bwrap="/usr/bin/bwrap",
            layout=built,
            workdir=work,
            provider="claude",
            env={},
            system=["--ro-bind", str(tmp_path), str(tmp_path)],
        )
        with pytest.raises(ValueError, match="unsafe_mount"):
            sandbox.bwrap_argv(system, ["true"])
    finally:
        built.remove()


def test_self_test_turns_unsafe_mount_into_a_code(tmp_path, monkeypatch):
    runner, calls = fake_runner(ALL_GOOD)
    base = inputs(tmp_path)
    result = sandbox.self_test(
        sandbox.SelfTestInputs(**{**base.__dict__, "extra_ro": [tmp_path]}),
        system_name="Linux",
        bwrap="/usr/bin/bwrap",
        runner=runner,
    )
    assert result.code == "unsafe_mount" and not any(result.providers.values())
    assert calls == []


def test_self_test_requires_hidden_paths_to_exist_on_the_host(tmp_path):
    runner, calls = fake_runner(ALL_GOOD)
    base = inputs(tmp_path)
    result = sandbox.self_test(
        sandbox.SelfTestInputs(**{**base.__dict__, "hidden": [tmp_path / "typo"]}),
        system_name="Linux",
        bwrap="/usr/bin/bwrap",
        runner=runner,
    )
    assert result.code == "hidden_check_failed" and calls == []
    assert not (tmp_path / "turns").exists() or not any((tmp_path / "turns").iterdir())


def test_self_test_returns_layout_failed_instead_of_raising(tmp_path):
    runner, calls = fake_runner(ALL_GOOD)
    base = inputs(tmp_path)
    missing = sandbox.SelfTestInputs(
        **{**base.__dict__, "socket_dir": tmp_path / "no" / "such" / "dir"}
    )
    result = sandbox.self_test(missing, system_name="Linux", bwrap="/usr/bin/bwrap", runner=runner)
    assert result.code == "layout_failed" and calls == []
    assert not (tmp_path / "turns").exists() or not any((tmp_path / "turns").iterdir())
