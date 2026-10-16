"""开发模式：会话 worktree 的创建与复用、定期拉取主干与清理闲置 worktree（真实 git 仓库）。"""

import base64
import json
import os
import subprocess
import time

import httpx
import pytest

from runtime_daemon import worktree
from runtime_daemon.agent import Agent
from runtime_daemon.daemon import Daemon


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def identity(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{key}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{key}_EMAIL", "test@example.com")


@pytest.fixture
def remote(tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    (seed / "README.md").write_text("one\n")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "init")
    bare = tmp_path / "remote.git"
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    return seed, bare


@pytest.fixture
def repo(tmp_path, remote):
    path = tmp_path / "root" / "bot"
    path.parent.mkdir()
    git(tmp_path, "clone", "-q", str(remote[1]), str(path))
    return path


def push_upstream(remote, text):
    seed, bare = remote
    (seed / "README.md").write_text(text)
    git(seed, "commit", "-q", "-am", "update")
    git(seed, "push", "-q", str(bare), "main")


def test_plain_directory_and_empty_session_stay_in_place(tmp_path, repo):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert worktree.ensure(plain, "s1") == plain
    assert worktree.ensure(repo, "") == repo


def test_session_gets_own_worktree_from_latest_default_branch(repo, remote):
    push_upstream(remote, "two\n")
    tree = worktree.ensure(repo, "s1")
    assert tree == repo / ".coreman-worktrees" / "s1"
    assert git(tree, "branch", "--show-current") == "coreman/session-s1"
    assert (tree / "README.md").read_text() == "two\n"
    # 主检出不动，worktree 目录不出现在主检出的 git status 里。
    assert (repo / "README.md").read_text() == "one\n"
    assert git(repo, "status", "--porcelain") == ""
    (tree / "new.txt").write_text("x")
    assert worktree.ensure(repo, "s1") == tree
    assert (tree / "new.txt").exists()
    assert worktree.ensure(repo, "s2") != tree


def test_untracked_skills_and_mcp_are_linked_and_excluded(repo):
    (repo / ".agents/skills/query").mkdir(parents=True)
    (repo / ".agents/skills/query/SKILL.md").write_text("skill")
    (repo / ".mcp.json").write_text("{}")
    tree = worktree.ensure(repo, "s1")
    assert (tree / ".agents/skills/query/SKILL.md").read_text() == "skill"
    assert (tree / ".mcp.json").is_symlink()
    assert git(tree, "status", "--porcelain") == ""


def test_removed_worktree_is_rebuilt_from_its_branch(repo):
    tree = worktree.ensure(repo, "s1")
    (tree / "work.txt").write_text("kept")
    git(tree, "add", ".")
    git(tree, "commit", "-q", "-m", "work")
    git(repo, "worktree", "remove", str(tree))
    assert (worktree.ensure(repo, "s1") / "work.txt").read_text() == "kept"


def test_refresh_fast_forwards_clean_main_checkout(repo, remote):
    worktree.ensure(repo, "s1")
    push_upstream(remote, "two\n")
    worktree.refresh(repo)
    assert (repo / "README.md").read_text() == "two\n"
    push_upstream(remote, "three\n")
    (repo / "README.md").write_text("local edit\n")
    worktree.refresh(repo)
    assert (repo / "README.md").read_text() == "local edit\n"


def test_refresh_skips_repositories_without_dev_mode(repo, remote):
    push_upstream(remote, "two\n")
    worktree.refresh(repo)
    assert (repo / "README.md").read_text() == "one\n"


def test_refresh_prunes_only_idle_worktrees_without_unsaved_work(repo):
    idle = worktree.ensure(repo, "idle")
    dirty = worktree.ensure(repo, "dirty")
    (dirty / "draft.txt").write_text("x")
    unpushed = worktree.ensure(repo, "unpushed")
    (unpushed / "work.txt").write_text("x")
    git(unpushed, "add", ".")
    git(unpushed, "commit", "-q", "-m", "work")
    fresh = worktree.ensure(repo, "fresh")
    old = time.time() - worktree.IDLE_SECONDS - 60
    for tree in (idle, dirty, unpushed):
        os.utime(tree, (old, old))
    worktree.refresh(repo)
    assert not idle.exists()
    assert "coreman/session-idle" not in git(repo, "branch")
    assert dirty.exists() and unpushed.exists() and fresh.exists()
    assert "coreman/session-unpushed" in git(repo, "branch")


async def test_daemon_rewrites_working_dir_only_for_dev_mode(tmp_path, repo, monkeypatch):
    import runtime_daemon.daemon as module

    config = tmp_path / "config.json"
    root = repo.parent
    config.write_text(json.dumps({"workspace_root": str(root), "api_url": "http://localhost"}))
    node = Daemon(config)
    node.agents["claude"] = Agent(
        root=root, api_url="http://localhost", token="x" * 32, relay_id="test", home=tmp_path
    )
    forwarded = []

    def handler(request):
        forwarded.append(json.loads(request.content))
        return httpx.Response(200, json={})

    monkeypatch.setattr(
        module.httpx, "AsyncHTTPTransport", lambda **_: httpx.MockTransport(handler)
    )

    async def send_frames(identity, frames):
        pass

    node.send_frames = send_frames
    node.socket_dir = tmp_path
    for flag in (True, False):
        body = {"working_dir": str(repo), "session_id": "s1", "worktree": flag}
        await node.execute(
            {
                "id": "t",
                "provider": "claude",
                "method": "POST",
                "path": "/v1/chat/completions",
                "body": base64.b64encode(json.dumps(body).encode()).decode(),
            }
        )
    assert forwarded[0]["working_dir"] == str(repo / ".coreman-worktrees" / "s1")
    assert forwarded[1]["working_dir"] == str(repo)
    assert "worktree" not in forwarded[0] and "worktree" not in forwarded[1]


def test_pruned_worktree_returns_to_the_branch_it_took_over(repo):
    first = worktree.ensure(repo, "s1")
    (first / "work.txt").write_text("day one")
    git(first, "add", ".")
    git(first, "commit", "-q", "-m", "work")
    git(first, "push", "-q", "origin", "coreman/session-s1")
    git(repo, "worktree", "remove", str(first))
    # 新会话接着做之前会话的分支。
    second = worktree.ensure(repo, "s2")
    git(second, "switch", "-q", "coreman/session-s1")
    old = time.time() - worktree.IDLE_SECONDS - 60
    os.utime(second, (old, old))
    worktree.refresh(repo)
    assert not second.exists()
    assert worktree.ensure(repo, "s2") == second
    assert git(second, "branch", "--show-current") == "coreman/session-s1"
    assert (second / "work.txt").read_text() == "day one"
    assert not (repo / ".coreman-worktrees" / "s2.branch").exists()
