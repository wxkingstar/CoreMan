"""开发环境预览：节点配置、预览标识、端口、frpc 配置、回收与 coreman-dev 的出错提示。"""

import json
import os
import subprocess
import time
from types import SimpleNamespace as NS

import pytest

from runtime_daemon import devpreview
from runtime_daemon.devpreview import PreviewError, Settings


def make_settings(tmp_path, **extra) -> Settings:
    (tmp_path / "token").write_text("tok-123\n")
    (tmp_path / "env").write_text("DEV_MYSQL_HOST=db\n")
    raw = {
        "domain": "dev.example.com",
        "tunnel_server": "tunnel.dev.example.com",
        "token_file": str(tmp_path / "token"),
        "env_file": str(tmp_path / "env"),
        "port_range": [42000, 42003],
        **extra,
    }
    settings = Settings.from_config({"node_id": "node-1", "dev_preview": raw}, tmp_path)
    assert settings is not None
    return settings


def test_no_section_means_not_supported(tmp_path):
    assert Settings.from_config({}, tmp_path) is None


@pytest.mark.parametrize(
    "raw",
    [
        {"domain": "d", "tunnel_server": "t", "token_file": "x"},
        {
            "domain": "d",
            "tunnel_server": "t",
            "token_file": "x",
            "env_file": "e",
            "visitors": [{"server_name": "db"}],
        },
        {
            "domain": "d",
            "tunnel_server": "t",
            "token_file": "x",
            "env_file": "e",
            "visitors": [{"server_name": "db", "bind_port": 13306}],
        },
    ],
)
def test_invalid_config_is_rejected(tmp_path, raw):
    with pytest.raises(ValueError):
        Settings.from_config({"dev_preview": raw}, tmp_path)


def test_preview_id_is_stable_per_worktree_and_dns_safe(tmp_path):
    settings = make_settings(tmp_path)
    a = devpreview.preview_id(settings, tmp_path / "w1", "My_Repo.Name")
    assert a == devpreview.preview_id(settings, tmp_path / "w1", "My_Repo.Name")
    assert a.startswith("my-repo-name-") and len(a.rsplit("-", 1)[1]) == 6
    assert a != devpreview.preview_id(settings, tmp_path / "w2", "My_Repo.Name")
    long = devpreview.preview_id(settings, tmp_path / "w1", "x" * 80)
    assert len(long) <= 63


def test_ports_are_reused_and_skip_taken_ones(tmp_path):
    settings = make_settings(tmp_path)
    state = {"previews": {"/a": {"id": "a", "port": 42000}}}
    assert devpreview.allocate_port(settings, state, "/a") == 42000
    assert devpreview.allocate_port(settings, state, "/b") == 42001
    state["previews"].update(
        {f"/{i}": {"id": str(i), "port": p} for i, p in enumerate([42001, 42002, 42003])}
    )
    with pytest.raises(PreviewError, match="端口已用完"):
        devpreview.allocate_port(settings, state, "/new")


def test_frpc_config_carries_tunnel_admin_includes_and_visitors(tmp_path):
    (tmp_path / "key").write_text("stcp-key\n")
    settings = make_settings(
        tmp_path,
        visitor_key_file=str(tmp_path / "key"),
        visitors=[{"server_name": "test-mysql", "bind_port": 13306}],
    )
    text = devpreview.frpc_config_text(settings, "admin-pw")
    assert 'serverAddr = "tunnel.dev.example.com"' in text
    assert 'auth.token = "tok-123"' in text
    assert 'transport.protocol = "wss"' in text and "transport.tcpMux = false" in text
    assert "transport.poolCount = 0" in text
    assert 'webServer.addr = "127.0.0.1"' in text and 'webServer.password = "admin-pw"' in text
    assert f'includes = ["{settings.proxies_dir}/*.toml"]' in text
    assert 'serverName = "test-mysql"' in text and "bindPort = 13306" in text
    assert 'secretKey = "stcp-key"' in text


def test_proxy_file_is_private_and_routes_subdomain(tmp_path):
    settings = make_settings(tmp_path)
    devpreview.write_proxy(settings, "app-abc123", 42001)
    path = settings.proxies_dir / "app-abc123.toml"
    assert 'subdomain = "app-abc123"' in path.read_text()
    assert "localPort = 42001" in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600


def test_reclaim_removes_gone_worktrees_and_stops_idle_ones(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, idle_hours=1)
    gone, idle, busy = tmp_path / "gone", tmp_path / "idle", tmp_path / "busy"
    for tree in (idle, busy):
        (tree / ".git").mkdir(parents=True)
    old = time.time() - 7200
    os.utime(idle, (old, old))
    calls = []
    monkeypatch.setattr(
        devpreview, "compose", lambda s, pid, *args, **kw: calls.append((pid, args))
    )
    monkeypatch.setattr(devpreview, "reload_tunnel", lambda s: calls.append(("reload",)))
    monkeypatch.setattr(devpreview.shutil, "which", lambda name: None)
    with devpreview.locked_state(settings) as state:
        state["previews"] = {
            str(gone): {"id": "g", "port": 42000, "running": True, "last_up": old},
            str(idle): {"id": "i", "port": 42001, "running": True, "last_up": old},
            str(busy): {"id": "b", "port": 42002, "running": True, "last_up": time.time()},
        }
    devpreview.reclaim(settings)
    with devpreview.locked_state(settings) as state:
        previews = state["previews"]
    assert str(gone) not in previews
    assert previews[str(idle)]["running"] is False
    assert previews[str(busy)]["running"] is True
    assert ("g", ("down", "--remove-orphans", "-v", "--rmi", "local")) in calls
    assert ("i", ("down", "--remove-orphans")) in calls
    assert ("reload",) in calls


def test_up_explains_missing_compose_file(tmp_path, monkeypatch, capsys):
    tree = tmp_path / "repo"
    tree.mkdir()
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    config = tmp_path / "config.json"
    (tmp_path / "token").write_text("t")
    (tmp_path / "env").write_text("")
    config.write_text(
        json.dumps(
            {
                "node_id": "n",
                "dev_preview": {
                    "domain": "dev.example.com",
                    "tunnel_server": "t.example.com",
                    "token_file": str(tmp_path / "token"),
                    "env_file": str(tmp_path / "env"),
                },
            }
        )
    )
    monkeypatch.chdir(tree)
    assert devpreview.main(["--config", str(config), "up"]) == 1
    assert "dev/compose.yaml" in capsys.readouterr().err


def test_main_says_when_node_has_no_preview(tmp_path, capsys):
    config = tmp_path / "config.json"
    config.write_text("{}")
    assert devpreview.main(["--config", str(config), "list"]) == 2
    assert "没有开启开发环境预览" in capsys.readouterr().err


def test_probe_treats_frps_404_as_not_connected(monkeypatch):
    import httpx

    pages = iter(
        [
            NS(status_code=404, text="<p>powered by <a href='https://github.com/fatedier/frp'>"),
            NS(status_code=302, text=""),
        ]
    )
    monkeypatch.setattr(httpx, "get", lambda *a, **k: next(pages))
    monkeypatch.setattr(devpreview.time, "sleep", lambda s: None)
    assert devpreview.probe("https://x.dev.example.com/", 30) == (True, "302")


def test_probe_reports_403_at_once(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **k: NS(status_code=403, text="Blocked request"))
    ok, detail = devpreview.probe("http://127.0.0.1:42000/", 30)
    assert not ok and "403" in detail


def test_command_script_runs_release_python(tmp_path):
    script = devpreview.command_script(tmp_path / "config.json", tmp_path / "release")
    assert script.startswith("#!/bin/sh")
    assert "runtime_daemon.devpreview import main" in script
    assert str(tmp_path / "release/.venv/bin/python") in script


def test_platform_keeps_the_capability_from_heartbeats():
    from coreman.api.routers.runtime_protocol import Capability

    stored = Capability.model_validate({"dev_preview_v1": True}).model_dump()
    assert stored["dev_preview_v1"] is True
    assert Capability.model_validate({}).dev_preview_v1 is False


def test_docker_path_expands_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = make_settings(tmp_path, docker="~/.local/bin/docker")
    assert settings.docker == str(tmp_path / ".local/bin/docker")
    assert make_settings(tmp_path).docker == "docker"


def test_project_env_file_is_merged_after_the_shared_one(tmp_path):
    settings = make_settings(tmp_path)
    assert devpreview.preview_env_file(settings, "shop", "shop-abc123") == settings.env_file

    extra = settings.project_env_file("shop")
    extra.parent.mkdir()
    extra.write_text("SHOP_KEY=k1\n")
    merged = devpreview.preview_env_file(settings, "shop", "shop-abc123")
    assert merged.read_text() == "DEV_MYSQL_HOST=db\nSHOP_KEY=k1\n"
    assert merged.stat().st_mode & 0o777 == 0o600
    assert devpreview.preview_env_file(settings, "other", "other-abc123") == settings.env_file

    extra.unlink()
    assert devpreview.preview_env_file(settings, "shop", "shop-abc123") == settings.env_file
    assert not merged.exists()


def test_tunnel_starts_when_pid_file_points_at_a_gone_process(tmp_path, monkeypatch):
    # systemd 重启时连 frpc 一起杀掉，留下的 pid 已不存在：ps 退出码 1，不能因此起不来。
    settings = make_settings(tmp_path)
    settings.data_dir.mkdir(parents=True)
    (settings.data_dir / "frpc.pid").write_text("999999")
    monkeypatch.setattr(devpreview, "install_frpc", lambda s: tmp_path / "frpc")

    def fake_run(cmd, **kw):
        raise PreviewError("ps 失败（退出码 1）")

    started = []
    monkeypatch.setattr(devpreview, "run", fake_run)
    monkeypatch.setattr(
        devpreview.subprocess, "Popen", lambda cmd, **kw: started.append(cmd) or NS(pid=4242)
    )
    devpreview.DevPreview(settings).ensure_tunnel()
    assert started and (settings.data_dir / "frpc.pid").read_text() == "4242"
