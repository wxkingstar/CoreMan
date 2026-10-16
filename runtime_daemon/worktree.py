"""开发模式：工作目录是 Git 仓库时，每个会话在自己的 worktree 里改，改完开 PR 合进主干。

worktree 放在工作目录下的 `.coreman-worktrees/<会话 id>`，分支 `coreman/session-<会话 id>`。
路径由会话 id 定死：Claude 的续聊记录按目录存，同一会话换了目录就续不上。
`.coreman-` 前缀的目录文件管理看不到、换机复制也不带（worktree 元数据里是绝对路径）。

定期刷新：拉取远端、主检出干净且停在默认分支时快进；闲置超过 IDLE_SECONDS、没有未提交
也没有未推送提交的 worktree 删掉（分支已合进主干的连分支一起删），再用到时按分支重建。
AI 员工可能把 worktree 切到之前会话的分支上接着做：删的时候在旁边留 `<会话 id>.branch`，
重建时回到那个分支。
"""

from __future__ import annotations

import fcntl
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

from .agent import OperationError, run_command
from .workspace import git_exclude_file, replace_file

DIRECTORY = ".coreman-worktrees"
BRANCH_PREFIX = "coreman/session-"
# 开发常常一做几天：闲置一周才删。删掉会丢被忽略的文件（装好的依赖、构建产物），分支和续聊记录都在。
IDLE_SECONDS = 7 * 24 * 3600
# 没有提交进仓库的技能与项目 MCP 配置只在主检出里：链接进 worktree，并列入本地排除免得误提交。
SHARED = (".mcp.json", ".agents/skills", ".claude/skills")
SESSION = re.compile(r"[A-Za-z0-9_-]{1,200}")


def is_repo(directory: Path) -> bool:
    gitdir = directory / ".git"
    return gitdir.is_dir() and not gitdir.is_symlink()


def git(directory: Path, *args: str, timeout: int = 120) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("COREMAN_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return run_command(["git", *args], directory, timeout, env_override=env).strip()


def succeeds(directory: Path, *args: str) -> bool:
    try:
        git(directory, *args)
    except OperationError:
        return False
    return True


@contextmanager
def locked(repo: Path):
    """同一仓库的 worktree 增删、拉取串行：多个会话同时开始时 git 会抢同一把锁而失败。"""
    with open(repo / ".git" / "coreman-worktree.lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def exclude(repo: Path, entries: list[str]) -> None:
    current = git_exclude_file(repo)
    previous = current.read_text() if current.is_file() else ""
    lines = previous.splitlines()
    missing = [e for e in entries if e not in lines]
    if missing:
        current.parent.mkdir(parents=True, exist_ok=True)
        text = previous + ("\n" if previous and not previous.endswith("\n") else "")
        replace_file(current, (text + "".join(e + "\n" for e in missing)).encode())


def default_branch(repo: Path) -> str | None:
    """远端默认分支名；没有远端或取不到时返回 None。"""
    try:
        ref = git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
        return ref.removeprefix("refs/remotes/origin/")
    except OperationError:
        pass
    for name in ("main", "master"):
        if succeeds(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{name}"):
            return name
    return None


def fetch(repo: Path) -> str | None:
    """拉取远端并返回默认分支；离线或没有远端时用本地已有的远端分支。"""
    if succeeds(repo, "remote", "get-url", "origin"):
        try:
            git(repo, "fetch", "--prune", "origin", timeout=300)
        except OperationError:
            pass
    return default_branch(repo)


def link_shared(repo: Path, tree: Path) -> None:
    linked = []
    for relative in SHARED:
        source, target = repo / relative, tree / relative
        if not (source.exists() or source.is_symlink()):
            continue
        if target.exists() or target.is_symlink():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(os.path.relpath(source, target.parent))
        linked.append("/" + relative)
    if linked:
        exclude(repo, linked)


def ensure(repo: Path, session_id: str) -> Path:
    """返回这个会话的 worktree；不是 Git 仓库（或会话 id 为空）时原样返回工作目录。"""
    if not session_id or not is_repo(repo):
        return repo
    if not SESSION.fullmatch(session_id):
        raise OperationError("会话标识无效")
    tree = repo / DIRECTORY / session_id
    if not (tree / ".git").is_file():
        with locked(repo):
            if not (tree / ".git").is_file():
                create(repo, tree, BRANCH_PREFIX + session_id)
    # 每轮都补：worktree 建好之后才在主检出装的技能也要看得到。
    link_shared(repo, tree)
    # 记下最近一次使用，定期清理按它判断闲置。
    os.utime(tree)
    return tree


def pointer(tree: Path) -> Path:
    return tree.with_name(tree.name + ".branch")


def create(repo: Path, tree: Path, branch: str) -> None:
    exclude(repo, ["/" + DIRECTORY + "/"])
    git(repo, "worktree", "prune")
    if tree.exists() and any(tree.iterdir()):
        raise OperationError("会话 worktree 目录已存在且不是 worktree")
    if pointer(tree).is_file():
        resumed = pointer(tree).read_text().strip()
        if resumed.startswith(BRANCH_PREFIX) and succeeds(
            repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{resumed}"
        ):
            branch = resumed
    if succeeds(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"):
        git(repo, "worktree", "add", "--", str(tree), branch)
    else:
        base = fetch(repo)
        start = f"origin/{base}" if base else "HEAD"
        git(repo, "worktree", "add", "--no-track", "-b", branch, "--", str(tree), start)
    pointer(tree).unlink(missing_ok=True)


def refresh(repo: Path, now: float | None = None) -> None:
    """定期任务：只处理用过开发模式（有 worktree 目录）的仓库。"""
    if not is_repo(repo) or not (repo / DIRECTORY).is_dir():
        return
    now = time.time() if now is None else now
    with locked(repo):
        base = fetch(repo)
        if base and git(repo, "branch", "--show-current") == base:
            if not git(repo, "status", "--porcelain"):
                git(repo, "merge", "--ff-only", f"origin/{base}")
        for tree in sorted((repo / DIRECTORY).iterdir()):
            if (tree / ".git").is_file() and now - tree.stat().st_mtime > IDLE_SECONDS:
                prune(repo, tree, base)
        git(repo, "worktree", "prune")


def prune(repo: Path, tree: Path, base: str | None) -> None:
    branch = git(tree, "branch", "--show-current")
    if git(tree, "status", "--porcelain"):
        return
    if branch and git(repo, "rev-list", "--count", branch, "--not", "--remotes") != "0":
        return
    merged = bool(
        branch.startswith(BRANCH_PREFIX)
        and base
        and succeeds(repo, "merge-base", "--is-ancestor", branch, f"origin/{base}")
    )
    git(repo, "worktree", "remove", "--", str(tree))
    if merged:
        git(repo, "branch", "-D", branch)
    elif branch.startswith(BRANCH_PREFIX) and branch != BRANCH_PREFIX + tree.name:
        # 接手的是之前会话的分支：重建时回到它，而不是本会话自己那条空分支。
        pointer(tree).write_text(branch + "\n")
