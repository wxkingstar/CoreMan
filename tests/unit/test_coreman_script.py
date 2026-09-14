import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "coreman"


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
    assert out.index("up -d postgres") < out.index("run --rm migrate") < out.index("up -d api-1")
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
