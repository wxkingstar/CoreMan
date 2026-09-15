import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "coreman"
# 测试进程里遗留的部署变量会改变脚本行为，全部剔除。
SCRUBBED_ENV = ("DRY_RUN", "COREMAN_IMAGE_TAG", "COREMAN_STATE_DIR", "COREMAN_WAIT_TIMEOUT")


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """状态文件默认在仓库根目录；本机若有真实部署的 .coreman-image-tag 会干扰断言。"""
    state = tmp_path / "state"
    state.mkdir()
    for key in SCRUBBED_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("COREMAN_STATE_DIR", str(state))
    return state


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    target = tmp_path / ".env"
    shutil.copy(ROOT / ".env.example", target)
    return target


def run(*args: str, env_file: Path, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DRY_RUN": "1", "COREMAN_ENV_FILE": str(env_file), "LC_ALL": "C"}
    return subprocess.run([str(SCRIPT), *args], cwd=cwd, env=env, capture_output=True, text=True)


def test_help_and_syntax(env_file: Path) -> None:
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
    r = run("help", env_file=env_file)
    assert r.returncode == 0 and "用法" in r.stdout


def test_upgrade_gateway_drains_before_stop(env_file: Path) -> None:
    r = run("upgrade", "gateway", "feishu", env_file=env_file)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert out.index("ready --prefix gateway-feishu-b") < out.index(
        "drain --prefix gateway-feishu-a"
    )
    assert out.index("drain --prefix gateway-feishu-a") < out.index("stop gateway-feishu-a")


def test_up_sequence_and_env_bootstrap(env_file: Path) -> None:
    r = run("up", env_file=env_file)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert (
        out.index("up -d postgres")
        < out.index("run --rm migrate")
        < out.index("up -d --wait --wait-timeout 300 api-1")
    )
    content = env_file.read_text(encoding="utf-8")
    assert "MASTER_KEY=\n" not in content and "SESSION_SECRET=\n" not in content
    assert "POSTGRES_PASSWORD=change-me" not in content
    assert "BOOTSTRAP_ADMIN_PASSWORD=change-me" not in content
    admin_pw = next(
        line.split("=", 1)[1]
        for line in content.splitlines()
        if line.startswith("BOOTSTRAP_ADMIN_PASSWORD=")
    )
    assert len(admin_pw) >= 32 and admin_pw not in r.stdout + r.stderr
    pw = next(
        line.split("=", 1)[1]
        for line in content.splitlines()
        if line.startswith("POSTGRES_PASSWORD=")
    )
    assert f"coreman:{pw}@postgres" in content
    # .env 里有 MASTER_KEY 和库密码，不能停留在 fixture 复制出来的 644
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_up_wait_timeout_is_configurable(env_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COREMAN_WAIT_TIMEOUT", "90")
    r = run("up", env_file=env_file)
    assert r.returncode == 0, r.stderr
    assert "up -d --wait --wait-timeout 90 " in r.stdout
    monkeypatch.setenv("COREMAN_WAIT_TIMEOUT", "soon")
    assert run("up", env_file=env_file).returncode == 2


def test_up_preserves_configured_admin_password(env_file: Path) -> None:
    env_file.write_text(
        env_file.read_text().replace(
            "BOOTSTRAP_ADMIN_PASSWORD=change-me",
            "BOOTSTRAP_ADMIN_PASSWORD=custom-test-password",
        )
    )
    for _ in range(2):
        result = run("up", env_file=env_file)
        assert result.returncode == 0, result.stderr
        assert "BOOTSTRAP_ADMIN_PASSWORD=custom-test-password" in env_file.read_text()
        assert "custom-test-password" not in result.stdout + result.stderr


def test_upgrade_worker_is_blue_green(env_file: Path) -> None:
    original = env_file.read_bytes()
    r = run("upgrade", "worker", "v1.2.3", env_file=env_file)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert out.index("drain --prefix worker-b") < out.index(
        "up -d --force-recreate --no-deps --pull never worker-b"
    )
    assert out.index("up -d --force-recreate --no-deps --pull never worker-b") < out.index(
        "drain --prefix worker-a"
    )
    assert out.index("drain --prefix worker-a") < out.index(
        "up -d --force-recreate --no-deps --pull never worker-a"
    )
    assert env_file.read_bytes() == original


@pytest.mark.parametrize("cmd", ["down", "status", "migrate", "build"])
def test_simple_commands_dry_run(cmd: str, env_file: Path) -> None:
    assert run(cmd, env_file=env_file).returncode == 0


def test_down_passes_timeout(env_file: Path) -> None:
    r = run("down", "-t", "30", env_file=env_file)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().endswith("down -t 30")


def test_root_resolves_through_symlink(tmp_path: Path) -> None:
    link = tmp_path / "bin" / "coreman"
    link.parent.mkdir()
    link.symlink_to(SCRIPT)
    missing_env = tmp_path / "fresh.env"
    env = {**os.environ, "DRY_RUN": "1", "COREMAN_ENV_FILE": str(missing_env), "LC_ALL": "C"}
    r = subprocess.run([str(link), "up"], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert missing_env.exists()
    assert "COREMAN_IMAGE_TAG=" in missing_env.read_text(encoding="utf-8")


def test_dry_upgrade_does_not_read_or_create_env_file(tmp_path: Path) -> None:
    missing_env = tmp_path / "absent.env"
    env = {**os.environ, "DRY_RUN": "1", "COREMAN_ENV_FILE": str(missing_env), "LC_ALL": "C"}
    r = subprocess.run(
        [str(SCRIPT), "upgrade", "worker", "v1.0.0"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0
    assert not missing_env.exists()
    assert "Traceback" not in r.stderr


def test_dry_run_never_writes_state_files(env_file: Path, isolated_state_dir: Path) -> None:
    assert run("upgrade", "all", "v2", env_file=env_file).returncode == 0
    assert run("build", "--tag", "v3", env_file=env_file).returncode == 0
    assert list(isolated_state_dir.iterdir()) == []


def test_up_message_omits_port_80(env_file: Path) -> None:
    r = run("up", env_file=env_file)
    assert r.returncode == 0, r.stderr
    assert "http://localhost/ " in r.stdout
    assert "localhost:80" not in r.stdout


def test_up_message_includes_custom_port(env_file: Path) -> None:
    content = env_file.read_text(encoding="utf-8").replace(
        "CADDY_HTTP_PORT=80", "CADDY_HTTP_PORT=8081"
    )
    env_file.write_text(content, encoding="utf-8")
    r = run("up", env_file=env_file)
    assert r.returncode == 0, r.stderr
    assert "http://localhost:8081/" in r.stdout


def test_missing_env_file_is_silent_on_stderr(tmp_path: Path) -> None:
    missing_env = tmp_path / "absent.env"
    env = {**os.environ, "DRY_RUN": "1", "COREMAN_ENV_FILE": str(missing_env)}
    r = subprocess.run([str(SCRIPT), "help"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert r.returncode == 0
    assert r.stderr == ""


def test_relative_env_file_is_absolutized_against_cwd(tmp_path: Path) -> None:
    """COREMAN_ENV_FILE 给相对路径时必须按 CWD 绝对化：compose 的 --env-file 按 CWD 解析，
    服务里的 env_file: 按 --project-directory 解析，只有绝对路径能让两者是同一个文件。"""
    r = run("up", env_file=Path(".env.custom"), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    generated = tmp_path / ".env.custom"
    assert generated.exists()
    assert str(generated) in r.stdout


def test_contact_sync_runs_cli_in_migrate_container(env_file: Path) -> None:
    r = run("contact-sync", "--app", "x", env_file=env_file)
    assert r.returncode == 0, r.stderr
    assert "run --rm migrate /app/.venv/bin/python -m coreman.cli contact-sync --app x" in r.stdout


def test_up_refuses_unfinished_upgrade_unless_forced(
    env_file: Path, isolated_state_dir: Path
) -> None:
    (isolated_state_dir / ".coreman-image-tag").write_text("v1\n")
    (isolated_state_dir / ".coreman-image-tag.pending").write_text("v2\n")
    r = run("up", env_file=env_file)
    assert r.returncode == 2
    assert "coreman upgrade all v2" in r.stderr and "coreman rollback v1" in r.stderr
    assert "docker compose" not in r.stdout
    forced = run("up", "--force", env_file=env_file)
    assert forced.returncode == 0, forced.stderr
    assert "up -d --wait" in forced.stdout


# ---- 真实执行路径：假 docker / git / curl 可执行文件记录调用并模拟容器状态 ----

FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
line = " ".join(args)
with open(os.environ["FAKE_DOCKER_LOG"], "a", encoding="utf-8") as log:
    log.write(f"{line} [tag={os.environ.get('COREMAN_IMAGE_TAG', '')}]\n")
fail_on = os.environ.get("FAKE_DOCKER_FAIL_ON", "")
if fail_on and fail_on in line:
    sys.exit(1)
state_path = os.environ["FAKE_DOCKER_STATE"]
state = json.load(open(state_path)) if os.path.exists(state_path) else {}


def save():
    json.dump(state, open(state_path, "w"))


def image_for(service):
    tag = os.environ.get("COREMAN_IMAGE_TAG", "dev")
    if service in ("postgres", "caddy"):
        return {"postgres": "postgres:16", "caddy": "caddy:2"}[service]
    if service.startswith("api-") or service in ("migrate", "operations"):
        return f"coreman-api:{tag}"
    return f"coreman-runtime:{tag}"


if args[:2] == ["image", "inspect"]:
    missing = set(os.environ.get("FAKE_DOCKER_MISSING", "").split())
    sys.exit(1 if missing & set(args[2:]) else 0)
if args[:1] == ["update"]:
    state[args[-1][4:]]["restart"] = "no"
    save()
    sys.exit(0)
if args[:1] == ["inspect"]:
    fmt, cids = args[2], args[3:]
    for cid in cids:
        svc = state[cid[4:]]
        if "Labels" in fmt:
            status = "running/healthy" if svc["running"] else "exited"
            print(cid[4:], svc["image"], status, svc["restart"])
        elif "Hostname" in fmt:
            print("host-" + cid[4:])
        elif "Health" in fmt:
            print("healthy" if svc["running"] else "exited")
        else:
            print("sha256:" + cid[4:])
    sys.exit(0)
if args[:1] == ["compose"]:
    rest = args[1:]
    while rest and rest[0].startswith("-"):  # --project-directory/--env-file/-f/--profile 都带值
        rest = rest[2:]
    sub, opts = rest[0], rest[1:]
    if sub == "ps":
        names = [o for o in opts if not o.startswith("-")]
        if "-a" in opts:
            print("\n".join("cid-" + name for name in state))
        for name in names:
            if state.get(name, {}).get("running"):
                print("cid-" + name)
    elif sub == "up":
        skip = False
        for opt in opts:
            if skip or opt in ("--wait-timeout", "--pull"):
                skip = not skip
                continue
            if not opt.startswith("-"):
                state[opt] = {"image": image_for(opt), "running": True, "restart": "unless-stopped"}
        save()
    elif sub == "stop":
        for name in opts:
            state[name]["running"] = False
        save()
    elif sub == "exec" and "postgres" in opts:
        print(os.environ.get("FAKE_PG", "42|300"))
    elif sub == "port":
        print("0.0.0.0:8081")
sys.exit(0)
"""

FAKE_GIT = r"""#!/usr/bin/env python3
import os, sys

args = sys.argv[1:]
if args[:1] == ["-C"]:
    args = args[2:]
if args == ["rev-parse", "HEAD"]:
    print("abc1234def5678abc1234def5678abc1234def56")
elif args == ["rev-parse", "--short", "HEAD"]:
    print("abc1234")
elif args[:2] == ["diff", "--quiet"]:
    sys.exit(1 if os.environ.get("FAKE_GIT_DIRTY") == "1" else 0)
sys.exit(0)
"""

FAKE_CURL = '#!/bin/sh\necho \'{"status":"ok"}\'\n'

CORE_SERVICES = [
    "postgres",
    "caddy",
    "api-1",
    "api-2",
    "scheduler",
    "worker-a",
    "worker-b",
    "gateway-wecom-a",
    "gateway-feishu-a",
]


@dataclass
class FakeStack:
    tmp: Path
    state_dir: Path
    env_file: Path

    @property
    def docker_state(self) -> Path:
        return self.tmp / "docker-state.json"

    @property
    def docker_log(self) -> Path:
        return self.tmp / "docker.log"

    def seed(self, tag: str, services: list[str] = CORE_SERVICES, **tags: str) -> None:
        """记录部署标签并让服务以该标签运行。

        tags 用 service_name=tag 覆盖个别服务的标签（服务名中的连字符写成下划线）。
        """
        state = {}
        for name in services:
            service_tag = tags.get(name.replace("-", "_"), tag)
            image = {"postgres": "postgres:16", "caddy": "caddy:2"}.get(name)
            if image is None:
                kind = "api" if name.startswith("api-") else "runtime"
                image = f"coreman-{kind}:{service_tag}"
            state[name] = {"image": image, "running": True, "restart": "unless-stopped"}
        self.docker_state.write_text(json.dumps(state))
        (self.state_dir / ".coreman-image-tag").write_text(tag + "\n")

    def run(self, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "PATH": f"{self.tmp / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "COREMAN_ENV_FILE": str(self.env_file),
            "FAKE_DOCKER_STATE": str(self.docker_state),
            "FAKE_DOCKER_LOG": str(self.docker_log),
            "LC_ALL": "C",
            **extra,
        }
        return subprocess.run(
            [str(SCRIPT), *args], cwd=ROOT, env=env, capture_output=True, text=True
        )

    def calls(self) -> list[str]:
        if not self.docker_log.exists():
            return []
        return self.docker_log.read_text(encoding="utf-8").splitlines()

    def services(self) -> dict[str, dict[str, object]]:
        state: dict[str, dict[str, object]] = json.loads(self.docker_state.read_text())
        return state

    def tag_file(self, suffix: str = "") -> str | None:
        path = self.state_dir / f".coreman-image-tag{suffix}"
        return path.read_text().strip() if path.exists() else None


@pytest.fixture
def stack(tmp_path: Path, env_file: Path, isolated_state_dir: Path) -> FakeStack:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("git", FAKE_GIT), ("curl", FAKE_CURL)):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)
    return FakeStack(tmp=tmp_path, state_dir=isolated_state_dir, env_file=env_file)


def test_upgrade_preflights_images_then_promotes_tag(stack: FakeStack) -> None:
    stack.seed("v1")
    r = stack.run("upgrade", "scheduler", "v2")
    assert r.returncode == 0, r.stderr
    calls = stack.calls()
    assert calls[0].startswith("image inspect coreman-api:v2")
    assert calls[1].startswith("image inspect coreman-runtime:v2")
    assert stack.tag_file() == "v2"
    assert stack.tag_file(".pending") is None
    assert stack.services()["scheduler"]["image"] == "coreman-runtime:v2"


def test_failed_upgrade_keeps_pending_and_reports_running_tags(stack: FakeStack) -> None:
    stack.seed("v1")
    r = stack.run("upgrade", "all", "v2", FAKE_DOCKER_FAIL_ON="drain --prefix worker-b")
    assert r.returncode != 0
    assert stack.tag_file(".pending") == "v2"
    assert stack.tag_file() == "v1"
    err = r.stderr
    rows = {line.split()[0]: line.split()[1] for line in err.splitlines() if line.startswith("  ")}
    assert rows["api-1"] == "v2" and rows["scheduler"] == "v2"
    assert rows["worker-a"] == "v1" and rows["gateway-wecom-a"] == "v1"
    assert "多个镜像标签" in err
    assert "coreman upgrade all v2" in err and "coreman rollback v1" in err
    # worker-b 已关闭自动重启但仍在运行，需要提示恢复方法
    assert "worker-b" in err and "docker update --restart=unless-stopped" in err


@pytest.mark.parametrize("command", [("upgrade", "all", "v9"), ("rollback", "v9")])
def test_missing_image_stops_before_any_change(stack: FakeStack, command: tuple[str, ...]) -> None:
    stack.seed("v1")
    r = stack.run(*command, FAKE_DOCKER_MISSING="coreman-runtime:v9")
    assert r.returncode == 2
    assert "coreman-runtime:v9" in r.stderr
    assert all(call.startswith("image inspect") for call in stack.calls())
    assert stack.tag_file() == "v1" and stack.tag_file(".pending") is None


def test_status_shows_image_tags_and_pg_connections(stack: FakeStack) -> None:
    stack.seed("v1", api_1="v2")
    r = stack.run("status", FAKE_PG="250|300")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    warning = next(line for line in out.splitlines() if "多个镜像标签" in line)
    assert "v1" in warning and "v2" in warning
    assert "PostgreSQL 连接：250/300（83%）" in out
    assert "max_connections 的 80%" in out
    assert "部署标签：v1" in out


def test_build_tags_with_git_commit(stack: FakeStack) -> None:
    stack.seed("v1")
    r = stack.run("build")
    assert r.returncode == 0, r.stderr
    assert any(c.endswith("build migrate worker-a [tag=abc1234]") for c in stack.calls())
    assert "coreman upgrade all abc1234" in r.stdout
    assert stack.tag_file() == "v1"
    dirty = stack.run("build", FAKE_GIT_DIRTY="1")
    assert dirty.returncode == 0, dirty.stderr
    assert any(c.endswith("[tag=abc1234-dirty]") for c in stack.calls())


def test_build_refuses_to_overwrite_deployed_or_running_tag(stack: FakeStack) -> None:
    stack.seed("abc1234")
    r = stack.run("build")
    assert r.returncode == 2 and "拒绝覆盖" in r.stderr
    assert not any(" build " in c for c in stack.calls())
    forced = stack.run("build", "--force")
    assert forced.returncode == 0, forced.stderr
    assert any(c.endswith("build migrate worker-a [tag=abc1234]") for c in stack.calls())

    stack.seed("v1", worker_a="hotfix")
    running = stack.run("build", "--tag", "hotfix")
    assert running.returncode == 2 and "worker-a" in running.stderr


def test_first_build_records_deploy_tag(stack: FakeStack) -> None:
    r = stack.run("build")
    assert r.returncode == 0, r.stderr
    assert stack.tag_file() == "abc1234"
    assert "coreman up" in r.stdout


# ---- 网关活跃侧持久化 ----


def gateway_state(state_dir: Path) -> str | None:
    path = state_dir / ".coreman-gateway-active"
    return path.read_text() if path.exists() else None


def test_up_starts_recorded_gateway_side(env_file: Path, isolated_state_dir: Path) -> None:
    (isolated_state_dir / ".coreman-gateway-active").write_text("wecom=b\nfeishu=a\n")
    r = run("up", env_file=env_file)
    assert r.returncode == 0, r.stderr
    wait_line = next(line for line in r.stdout.splitlines() if "up -d --wait" in line)
    services = wait_line.split()
    assert "gateway-wecom-b" in services and "gateway-feishu-a" in services
    assert "gateway-wecom-a" not in services and "gateway-feishu-b" not in services


def test_up_without_state_starts_side_a(env_file: Path) -> None:
    r = run("up", env_file=env_file)
    assert r.returncode == 0, r.stderr
    wait_line = next(line for line in r.stdout.splitlines() if "up -d --wait" in line)
    assert wait_line.endswith("worker-b gateway-wecom-a gateway-feishu-a")


def test_upgrade_gateway_records_and_alternates_active_side(stack: FakeStack) -> None:
    stack.seed("v1")
    assert stack.run("upgrade", "gateway", "wecom").returncode == 0
    assert gateway_state(stack.state_dir) == "wecom=b\n"
    services = stack.services()
    assert services["gateway-wecom-b"]["running"] and not services["gateway-wecom-a"]["running"]

    assert stack.run("upgrade", "gateway", "feishu").returncode == 0
    assert gateway_state(stack.state_dir) == "wecom=b\nfeishu=b\n"

    second = stack.run("upgrade", "gateway", "wecom")
    assert second.returncode == 0, second.stderr
    assert "drain --prefix gateway-wecom-b" in "\n".join(stack.calls())
    assert gateway_state(stack.state_dir) == "wecom=a\nfeishu=b\n"


def test_upgrade_all_follows_recorded_sides(stack: FakeStack) -> None:
    services = [s for s in CORE_SERVICES if s != "gateway-wecom-a"] + ["gateway-wecom-b"]
    stack.seed("v1", services=services)
    (stack.state_dir / ".coreman-gateway-active").write_text("wecom=b\nfeishu=a\n")
    r = stack.run("upgrade", "all", "v2")
    assert r.returncode == 0, r.stderr
    calls = "\n".join(stack.calls())
    assert "drain --prefix gateway-wecom-b:host-gateway-wecom-b:" in calls
    assert "drain --prefix gateway-feishu-a:host-gateway-feishu-a:" in calls
    assert gateway_state(stack.state_dir) == "wecom=a\nfeishu=b\n"
    assert stack.services()["gateway-wecom-a"]["image"] == "coreman-runtime:v2"
    assert stack.tag_file() == "v2"


def test_state_contradicting_running_containers_prefers_running_side(stack: FakeStack) -> None:
    stack.seed("v1")  # 只有 a 侧在运行
    (stack.state_dir / ".coreman-gateway-active").write_text("wecom=b\n")
    r = stack.run("upgrade", "gateway", "wecom")
    assert r.returncode == 0, r.stderr
    assert "只有 a 侧在运行" in r.stderr
    assert "drain --prefix gateway-wecom-a:host-gateway-wecom-a:" in "\n".join(stack.calls())
    assert gateway_state(stack.state_dir) == "wecom=b\n"
