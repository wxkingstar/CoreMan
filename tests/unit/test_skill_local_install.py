"""技能本地安装：令牌、目录定位、打包，以及安装脚本在 sh 下的实际效果。"""

import io
import os
import shutil
import stat
import subprocess
import tarfile
import uuid
from pathlib import Path

import pytest

from coreman.core.crypto import Cipher
from coreman.core.knowledge import local_install
from coreman.core.timeutils import utcnow

CIPHER = Cipher(b"\x05" * 32)


def skill(path: Path, name: str, body: str = "") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(f"---\nname: {name}\ndescription: test\n---\n{body}\n")
    return path


def test_token_binds_user_and_skill_and_expires():
    user, target, now = uuid.uuid4(), uuid.uuid4(), utcnow()
    token = local_install.seal_token(CIPHER, user, target, now)
    assert local_install.open_token(CIPHER, token, now) == (user, target)
    with pytest.raises(ValueError, match="expired"):
        local_install.open_token(CIPHER, token, now + local_install.TOKEN_TTL)
    with pytest.raises(ValueError, match="invalid"):
        local_install.open_token(Cipher(b"\x06" * 32), token, now)
    with pytest.raises(ValueError, match="invalid"):
        local_install.open_token(CIPHER, token[:-4] + "AAAA", now)
    command = local_install.install_command("https://coreman.example.com/", token)
    assert command == f"curl -fsSL https://coreman.example.com/api/skill-install/{token} | sh"


def test_find_skill_dir_matches_frontmatter_name_and_prefers_shallowest(tmp_path):
    skill(tmp_path / "plugins/report/skills/report", "report")
    skill(tmp_path / "plugins/report/skills/report/examples/copy", "report")
    skill(tmp_path / "plugins/other/skills/report-v2", "Report-V2")
    skill(tmp_path / ".git/skills/hidden", "hidden")
    (tmp_path / "plugins/broken").mkdir()
    (tmp_path / "plugins/broken/SKILL.md").write_text("no frontmatter")
    assert local_install.find_skill_dir(tmp_path, "report") == (
        tmp_path / "plugins/report/skills/report"
    )
    assert local_install.find_skill_dir(tmp_path, "report-v2") == (
        tmp_path / "plugins/other/skills/report-v2"
    )
    for missing in ("hidden", "broken"):
        with pytest.raises(LookupError):
            local_install.find_skill_dir(tmp_path, missing)


def test_pack_keeps_files_and_modes_without_git_metadata(tmp_path):
    root = skill(tmp_path / "report", "report")
    (root / "scripts").mkdir()
    (root / "scripts/run.sh").write_text("#!/bin/sh\necho ok\n")
    (root / "scripts/run.sh").chmod(0o755)
    (root / ".git").mkdir()
    (root / ".git/config").write_text("[core]")
    os.symlink("scripts/run.sh", root / "run")
    with tarfile.open(fileobj=io.BytesIO(local_install.pack(root))) as archive:
        members = {m.name: m for m in archive.getmembers()}
    assert set(members) == {"SKILL.md", "scripts", "scripts/run.sh", "run"}
    assert members["scripts/run.sh"].mode & stat.S_IXUSR
    assert members["run"].issym() and members["run"].linkname == "scripts/run.sh"
    assert {m.uid for m in members.values()} == {0}


def test_pack_refuses_oversized_directory(tmp_path, monkeypatch):
    root = skill(tmp_path / "big", "big", "x" * 100)
    monkeypatch.setattr(local_install, "ARCHIVE_LIMIT", 50)
    with pytest.raises(OverflowError):
        local_install.pack(root)


def test_repository_strips_runtime_skill_suffix():
    assert (
        local_install.repository("https://git.example.com/group/skills.git@report")
        == "https://git.example.com/group/skills.git"
    )
    assert local_install.repository("git@git.example.com:g/s.git") == "git@git.example.com:g/s.git"


def test_export_lines_quote_values_and_list_user_fields():
    text = local_install.export_lines(
        "report",
        [
            ("oss", "OSS", {"OSS_KEY": "a'b c", "SHARED": "1"}),
            ("db_erp", "db_erp", {"SHARED": "1", "DB_PASSWORD": "p$w\nx"}),
        ],
        {"API_KEY": {"label": "个人\n密钥", "required": True}, "OSS_KEY": {"label": "x"}},
    )
    assert text.splitlines()[1:] == [
        "# oss（OSS）",
        "export OSS_KEY='a'\"'\"'b c'",
        "export SHARED=1",
        "# db_erp",
        "export DB_PASSWORD='p$w",
        "x'",
        "# 以下由使用者自行填写",
        "# export API_KEY=''  # 个人 密钥（必填）",
    ]
    env = subprocess.run(
        ["sh", "-c", text + 'printf %s "$OSS_KEY|$DB_PASSWORD"'],
        capture_output=True,
        text=True,
        check=True,
    )
    assert env.stdout == "a'b c|p$w\nx"


@pytest.fixture
def fake_curl(tmp_path):
    """sh 里的 curl 换成假的：把准备好的压缩包拷到 -o 指定的位置，并打印 200。"""
    bin_dir, archive = tmp_path / "bin", tmp_path / "skill.tar.gz"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        'while [ $# -gt 0 ]; do case "$1" in -o) out=$2; shift;; esac; shift; done\n'
        f'cp "{archive}" "$out"\nprintf 200\n'
    )
    curl.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    return archive, env


def run_script(cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    script = local_install.install_script("report", "https://coreman.example.com/a")
    return subprocess.run(["sh"], input=script, cwd=cwd, env=env, capture_output=True, text=True)


def test_install_script_installs_links_and_updates(tmp_path, fake_curl):
    archive, env = fake_curl
    project = tmp_path / "project"
    project.mkdir()
    archive.write_bytes(local_install.pack(skill(tmp_path / "v1", "report", "v1")))
    first = run_script(project, env)
    assert first.returncode == 0, first.stderr
    assert "已安装技能 report" in first.stdout
    link = project / ".claude/skills/report"
    assert os.readlink(link) == "../../.agents/skills/report"
    assert "v1" in (link / "SKILL.md").read_text()

    archive.write_bytes(local_install.pack(skill(tmp_path / "v2", "report", "v2")))
    second = run_script(project, env)
    assert second.returncode == 0, second.stderr
    assert "已更新技能 report" in second.stdout
    assert "v2" in (project / ".agents/skills/report/SKILL.md").read_text()
    # 临时解压目录不留在技能目录里。
    assert sorted(p.name for p in (project / ".agents/skills").iterdir()) == ["report"]


def test_install_script_refuses_to_overwrite_foreign_claude_entry(tmp_path, fake_curl):
    archive, env = fake_curl
    archive.write_bytes(local_install.pack(skill(tmp_path / "v1", "report")))
    project = tmp_path / "project"
    (project / ".claude/skills/report").mkdir(parents=True)
    result = run_script(project, env)
    assert result.returncode != 0 and "不是软链" in result.stderr
    assert not (project / ".agents/skills/report").exists()
    shutil.rmtree(project / ".claude/skills/report")
    os.symlink("/elsewhere", project / ".claude/skills/report")
    result = run_script(project, env)
    assert result.returncode != 0 and "指向其他位置" in result.stderr


def test_install_script_reports_server_error(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text(
        "#!/bin/sh\n"
        'while [ $# -gt 0 ]; do case "$1" in -o) out=$2; shift;; esac; shift; done\n'
        'printf \'{"code":410,"message":"expired"}\' > "$out"\nprintf 410\n'
    )
    (bin_dir / "curl").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    result = run_script(tmp_path, env)
    assert result.returncode != 0
    assert "HTTP 410" in result.stderr and "expired" in result.stderr
    assert not (tmp_path / ".agents/skills/report").exists()
