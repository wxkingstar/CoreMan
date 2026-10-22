"""员工记忆放在工作目录的 .coreman-memory/：Claude 原来的记忆迁过来，原位置换成链接兜底。"""

import asyncio
import json
import os
import subprocess

import pytest

from runtime_daemon.agent import Agent, OperationError
from runtime_daemon.daemon import Daemon


@pytest.fixture
def agent(tmp_path):
    root = tmp_path / "workspaces"
    root.mkdir()
    return Agent(
        root=root,
        home=tmp_path / "home",
        api_url="https://example.com",
        token="synthetic-test-token-123456789",
        relay_id="test",
    )


def workspace(agent, name="bot"):
    folder = agent.root / name
    folder.mkdir()
    return folder


def legacy_with(agent, folder, files):
    legacy = agent.legacy_memory_dir(folder)
    legacy.mkdir(parents=True)
    for name, (content, mtime) in files.items():
        (legacy / name).write_text(content)
        os.utime(legacy / name, (mtime, mtime))
    return legacy


def test_legacy_memory_moves_into_workspace_and_leaves_a_link(agent):
    folder = workspace(agent)
    legacy = legacy_with(agent, folder, {"MEMORY.md": ("- [a](a.md)\n", 1000), "a.md": ("A", 2000)})
    target = agent.ensure_memory_layout(str(folder))
    assert target == folder / ".coreman-memory"
    assert (target / "a.md").read_text() == "A"
    assert (target / "a.md").stat().st_mtime == 2000
    assert legacy.is_symlink() and legacy.resolve() == target
    backups = list(legacy.parent.glob("memory.migrated-*"))
    assert len(backups) == 1 and (backups[0] / "a.md").read_text() == "A"
    # 退回旧版时按原位置读写，命中的是同一份。
    (legacy / "b.md").write_text("B")
    assert (target / "b.md").read_text() == "B"


def test_migration_keeps_the_newer_copy_and_is_repeatable(agent):
    folder = workspace(agent)
    target = folder / ".coreman-memory"
    target.mkdir()
    (target / "old.md").write_text("workspace newer")
    os.utime(target / "old.md", (3000, 3000))
    (target / "new.md").write_text("workspace older")
    os.utime(target / "new.md", (1000, 1000))
    legacy = legacy_with(
        agent, folder, {"old.md": ("legacy older", 2000), "new.md": ("legacy newer", 2000)}
    )
    agent.ensure_memory_layout(str(folder))
    assert (target / "old.md").read_text() == "workspace newer"
    assert (target / "new.md").read_text() == "legacy newer"
    agent.ensure_memory_layout(str(folder))
    assert legacy.is_symlink() and len(list(legacy.parent.glob("memory.migrated-*"))) == 1


def test_link_to_somewhere_else_is_left_alone(agent, tmp_path):
    folder = workspace(agent)
    legacy = agent.legacy_memory_dir(folder)
    legacy.parent.mkdir(parents=True)
    legacy.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OperationError, match="链接到别处"):
        agent.ensure_memory_layout(str(folder))
    assert legacy.resolve() == tmp_path


def test_memory_directory_cannot_be_a_link(agent, tmp_path):
    folder = workspace(agent)
    (folder / ".coreman-memory").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OperationError, match="不能是链接"):
        agent.ensure_memory_layout(str(folder))


def test_repository_subdirectory_uses_claude_naming_of_the_repository_root(agent):
    repo = workspace(agent, "repo")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    folder = repo / "bots" / "sales"
    folder.mkdir(parents=True)
    assert agent.legacy_memory_dir(folder) == agent.project_dir_for(repo) / "memory"
    assert agent.legacy_memory_dir(repo) == agent.project_dir_for(repo) / "memory"


def test_repository_excludes_the_memory_directory(agent):
    folder = workspace(agent)
    subprocess.run(["git", "init", "-q", str(folder)], check=True)
    agent.ensure_memory_layout(str(folder))
    assert "/.coreman-memory/" in (folder / ".git/info/exclude").read_text().splitlines()


def test_collect_and_deploy_use_the_workspace_directory(agent):
    folder = workspace(agent)
    legacy_with(agent, folder, {"kept.md": ("from claude", 1000)})
    collected = agent.collect_memory(str(folder))
    assert [row["file_name"] for row in collected] == ["kept.md"]
    agent.deploy_memory([{"working_dir": str(folder), "file_name": "new.md", "content": "N"}])
    assert (folder / ".coreman-memory/new.md").read_text() == "N"


def test_long_paths_skip_only_the_fallback_link(agent):
    folder = workspace(agent, "x" * 220)
    target = agent.ensure_memory_layout(str(folder))
    assert target.is_dir()
    assert not (agent.home / ".claude").exists()


def test_files_view_and_copy_never_include_memory():
    from runtime_daemon.workspace import MEMORY_DIR, forbidden

    assert forbidden(MEMORY_DIR + "/note.md")
    assert forbidden(MEMORY_DIR + "/note.md", transfer=True)


class FakeDaemon:
    attach_memory = Daemon.attach_memory

    def __init__(self, agent):
        self.agents = {"claude": agent, "codex": agent}


def test_claude_requests_point_auto_memory_at_the_main_workspace(agent):
    folder = workspace(agent)
    data = {"working_dir": str(folder), "settings": json.dumps({"model": "keep"})}
    asyncio.run(FakeDaemon(agent).attach_memory("claude", data))
    assert json.loads(data["settings"]) == {
        "model": "keep",
        "autoMemoryDirectory": str(folder / ".coreman-memory"),
    }


def test_codex_requests_get_the_layout_but_no_settings(agent):
    folder = workspace(agent)
    legacy_with(agent, folder, {"a.md": ("A", 1000)})
    data = {"working_dir": str(folder)}
    asyncio.run(FakeDaemon(agent).attach_memory("codex", data))
    assert "settings" not in data
    assert (folder / ".coreman-memory/a.md").read_text() == "A"


def test_broken_layout_does_not_block_the_chat(agent, tmp_path):
    folder = workspace(agent)
    (folder / ".coreman-memory").symlink_to(tmp_path, target_is_directory=True)
    data = {"working_dir": str(folder)}
    asyncio.run(FakeDaemon(agent).attach_memory("claude", data))
    assert "settings" not in data
