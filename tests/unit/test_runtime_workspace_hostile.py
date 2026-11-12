"""助理写得到的工作目录：守护进程在沙箱外读写它时不跟随链接、不在 FIFO 上卡住、不越界。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import threading
import time
from pathlib import Path

import pytest

from runtime_daemon import workspace
from runtime_daemon.agent import Agent, OperationError


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


def call(agent, kind, **kw):
    return agent.dispatch({"type": "workspace-" + kind, "working_dir": "one", "bot_id": "b1", **kw})


def finishes(fn, seconds: float = 5.0):
    """在线程里跑 `fn`：卡住（例如在 FIFO 上等写端）就判失败，而不是把整个测试进程挂住。"""
    out: dict = {}

    def run() -> None:
        try:
            out["value"] = fn()
        except Exception as exc:  # noqa: BLE001 交给断言
            out["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), "blocked"
    if "error" in out:
        raise out["error"]
    return out.get("value")


def test_read_small_refuses_fifos_links_devices_and_large_files(tmp_path):
    regular = tmp_path / "ok.json"
    regular.write_bytes(b'{"bot_id": "b1"}')
    assert workspace.read_small(regular) == b'{"bot_id": "b1"}'
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    assert finishes(lambda: workspace.read_small(fifo)) is None
    link = tmp_path / "link"
    link.symlink_to(regular)
    assert workspace.read_small(link) is None
    assert workspace.read_small(Path("/dev/zero")) is None
    big = tmp_path / "big"
    big.write_bytes(b"x" * (workspace.MARKER_LIMIT + 1))
    assert workspace.read_small(big) is None
    assert workspace.read_small(tmp_path / "missing") is None


def test_a_fifo_marker_is_not_owned_and_does_not_block(agent):
    p = agent.root / "one"
    p.mkdir()
    os.mkfifo(p / workspace.MARKER)
    info = finishes(lambda: call(agent, "info"))
    assert info["owned"] is False and info["marked"] is True
    with pytest.raises(OperationError):
        finishes(lambda: call(agent, "init", content="x"))


@pytest.mark.parametrize("payload", [b"[]", b'"b1"', b"\xff\xfe", b'{"bot_id": "b2"}'])
def test_malformed_or_foreign_markers_are_not_owned(agent, payload):
    p = agent.root / "one"
    p.mkdir()
    (p / workspace.MARKER).write_bytes(payload)
    assert call(agent, "info")["owned"] is False


def test_assistant_only_init_never_reads_an_existing_directory(agent):
    agent.assistant_only = True
    first = call(agent, "init", content="# rules\n")
    assert first["initialized"] and call(agent, "info")["owned"]
    p = agent.root / "one"
    # 初始化做完之后助理在沙箱里改了说明文件：再初始化不读它（读目录会报错），原样返回。
    (p / "CLAUDE.md").unlink()
    (p / "CLAUDE.md").mkdir()
    again = call(agent, "init", content="# rules\n")
    assert again == {"success": True, "initialized": True, "owned": True, "workspace_protocol": 1}
    assert (p / "CLAUDE.md").is_dir()


def test_assistant_only_init_refuses_a_non_empty_unmarked_directory(agent):
    agent.assistant_only = True
    p = agent.root / "one"
    p.mkdir()
    (p / "notes.md").write_text("left behind")
    with pytest.raises(OperationError, match="全新的目录"):
        call(agent, "init", content="x")
    (p / "notes.md").unlink()
    assert call(agent, "init", content="x")["initialized"]


def test_ordinary_init_keeps_taking_over_existing_directories(agent):
    p = agent.root / "one"
    p.mkdir()
    (p / "notes.md").write_text("kept")
    assert call(agent, "init", content="x")["initialized"]
    assert (p / "notes.md").read_text() == "kept"


def test_remove_is_only_for_assistant_only_daemons(agent):
    call(agent, "init", content="x")
    with pytest.raises(OperationError):
        call(agent, "remove")
    assert (agent.root / "one").is_dir()


def wait_gone(path: Path) -> None:
    for _ in range(100):
        if not os.path.lexists(path):
            return
        time.sleep(0.05)
    raise AssertionError(f"{path} still there")


def test_remove_moves_the_directory_into_the_trash_and_deletes_it_without_following_links(
    agent, tmp_path
):
    agent.assistant_only = True
    call(agent, "init", content="x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("not the assistant's")
    p = agent.root / "one"
    (p / "escape").symlink_to(outside)
    (p / "deep" / "er").mkdir(parents=True)
    (p / "deep" / "er" / "file").write_text("x")
    (p / "deep" / "locked").mkdir()
    (p / "deep" / "locked").chmod(0)
    assert call(agent, "remove") == {"success": True, "removed": True, "existed": True}
    assert not os.path.lexists(p)
    trash = agent.root / workspace.TRASH_DIR
    assert trash.is_dir() and (trash.stat().st_mode & 0o777) == 0o700
    for entry in list(trash.iterdir()):
        wait_gone(entry)
    assert (outside / "keep.txt").read_text() == "not the assistant's"


UNVERIFIED = {"success": True, "removed": False, "existed": True, "unverified": True}
UNVERIFIED_MISSING = {"success": True, "removed": False, "existed": False, "unverified": True}


def test_remove_cannot_vouch_for_missing_or_unregistered_directories(agent):
    """没有登记也没有本助理标记：认不准，不能答「已删除」或「不是我的」，平台留着登记再来。"""
    agent.assistant_only = True
    assert call(agent, "remove") == UNVERIFIED_MISSING
    p = agent.root / "one"
    p.mkdir()
    (p / "file").write_text("unmarked")
    assert call(agent, "remove") == UNVERIFIED
    (p / workspace.MARKER).write_text('{"bot_id": "someone-else"}')
    assert call(agent, "remove") == UNVERIFIED
    assert (p / "file").read_text() == "unmarked"


def test_remove_of_an_unmounted_share_creates_nothing(agent):
    """共享存储没挂上：路径不在、登记也不在。答「认不准」，不在挂载点上建任何目录。"""
    agent.assistant_only = True
    assert call(agent, "remove") == UNVERIFIED_MISSING
    assert not os.path.lexists(agent.root / workspace.OWNERS_DIR)
    assert not os.path.lexists(agent.root / workspace.TRASH_DIR)


def test_remove_refuses_a_trash_that_is_a_link(agent, tmp_path):
    agent.assistant_only = True
    call(agent, "init", content="x")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (agent.root / workspace.TRASH_DIR).symlink_to(elsewhere)
    with pytest.raises(OperationError):
        call(agent, "remove")
    assert (agent.root / "one").is_dir() and not any(elsewhere.iterdir())


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (".coreman-health", True),
        (".coreman-health/x", True),
        (".coreman-trash/abc", True),
        (".coreman-owners", True),
        (".coreman-owners/abc", True),
        ("bot-1", False),
        (".coreman-healthy", False),
        ("", False),
    ],
)
def test_reserved_directories(tmp_path, raw, expected):
    assert workspace.reserved(tmp_path, raw) is expected


def test_reserved_follows_links_to_their_real_place(tmp_path):
    (tmp_path / ".coreman-health").mkdir()
    (tmp_path / "innocent").symlink_to(tmp_path / ".coreman-health")
    assert workspace.reserved(tmp_path, "innocent") is True
    assert workspace.reserved(tmp_path, str(tmp_path / ".coreman-health")) is True


def test_remove_waits_while_a_sandboxed_turn_uses_the_directory(agent):
    from types import SimpleNamespace

    from runtime_daemon.assistant_turns import AssistantTurns

    agent.assistant_only = True
    call(agent, "init", content="x")
    turns = AssistantTurns(SimpleNamespace())
    agent.daemon = SimpleNamespace(assistant=turns)
    turns.workdirs["t1"] = (agent.root / "one").resolve()
    assert call(agent, "remove") == {
        "success": True,
        "removed": False,
        "existed": True,
        "busy": True,
    }
    assert (agent.root / "one").is_dir()
    turns.workdirs.clear()
    assert call(agent, "remove")["removed"] is True


# ---- 守护进程自己保管的归属登记（助理改不到） --------------------------------------------


def record_of(agent, name="one") -> Path:
    path = (agent.root / name).resolve()
    digest = hashlib.sha256(os.fsencode(str(path))).hexdigest()
    return agent.root / workspace.OWNERS_DIR / digest


def test_assistant_init_registers_the_fresh_directory_outside_it(agent):
    agent.assistant_only = True
    call(agent, "init", content="x")
    record = record_of(agent)
    info = os.lstat(agent.root / "one")
    assert json.loads(record.read_text()) == {"bot_id": "b1", "ino": info.st_ino}
    assert stat.S_IMODE(record.stat().st_mode) == 0o600
    assert stat.S_IMODE((agent.root / workspace.OWNERS_DIR).stat().st_mode) == 0o700
    assert [p.name for p in (agent.root / workspace.OWNERS_DIR).iterdir()] == [record.name]


def test_an_ordinary_init_keeps_no_registry(agent):
    call(agent, "init", content="x")
    assert not os.path.lexists(agent.root / workspace.OWNERS_DIR)


@pytest.mark.parametrize("tamper", ["delete", "rewrite"])
def test_remove_trusts_the_registry_over_a_tampered_marker(agent, tamper):
    agent.assistant_only = True
    call(agent, "init", content="x")
    marker = agent.root / "one" / workspace.MARKER
    if tamper == "delete":
        marker.unlink()
    else:
        marker.write_text('{"bot_id": "someone-else"}')
    assert call(agent, "remove") == {"success": True, "removed": True, "existed": True}
    assert not os.path.lexists(agent.root / "one")
    assert json.loads(record_of(agent).read_text()) == {"bot_id": "b1", "removed": True}


def test_remove_leaves_a_directory_registered_to_another_bot(agent):
    agent.assistant_only = True
    call(agent, "init", content="x")
    info = os.lstat(agent.root / "one")
    record_of(agent).write_text(json.dumps({"bot_id": "b2", "ino": info.st_ino}))
    # 标记仍写着 b1：有登记时不看标记。
    assert call(agent, "remove") == {"success": True, "removed": False, "existed": True}
    assert (agent.root / "one" / workspace.MARKER).exists()
    assert record_of(agent).exists()


def test_remove_leaves_a_directory_that_was_swapped_for_another(agent, tmp_path):
    agent.assistant_only = True
    call(agent, "init", content="x")
    p = agent.root / "one"
    p.rename(tmp_path / "original")
    p.mkdir()
    (p / workspace.MARKER).write_text('{"bot_id": "b1"}')
    (p / "file").write_text("not the registered directory")
    # 登记着本助理、inode 却对不上（被人换过、复制或还原过）：认不准，不是「不是我的」。
    assert call(agent, "remove") == UNVERIFIED
    assert (p / "file").read_text() == "not the registered directory"


def test_remove_falls_back_to_the_marker_without_a_record(agent):
    agent.assistant_only = True
    call(agent, "init", content="x")
    shutil.rmtree(agent.root / workspace.OWNERS_DIR)
    assert call(agent, "remove") == {"success": True, "removed": True, "existed": True}
    assert not os.path.lexists(agent.root / "one")
    assert json.loads(record_of(agent).read_text()) == {"bot_id": "b1", "removed": True}


def test_remove_of_a_directory_deleted_by_hand_is_final(agent):
    """登记还记着本助理、目录已不在（运维手工删了，或上次改名进回收站后没来得及写墓碑）：删完了。"""
    agent.assistant_only = True
    call(agent, "init", content="x")
    shutil.rmtree(agent.root / "one")
    assert call(agent, "remove") == {"success": True, "removed": True, "existed": False}
    assert json.loads(record_of(agent).read_text()) == {"bot_id": "b1", "removed": True}


def test_remove_of_a_missing_directory_registered_to_another_bot(agent):
    agent.assistant_only = True
    workspace.owners_dir(agent.root)
    record_of(agent).write_text('{"bot_id": "b2", "ino": 1}')
    assert call(agent, "remove") == {"success": True, "removed": False, "existed": False}
    assert json.loads(record_of(agent).read_text()) == {"bot_id": "b2", "ino": 1}


def test_a_repeated_remove_is_answered_from_the_tombstone(agent):
    """回执丢了平台再来：墓碑说明已经删过，答「已删除」；之后这里出现什么都不碰。"""
    agent.assistant_only = True
    call(agent, "init", content="x")
    assert call(agent, "remove")["removed"] is True
    assert call(agent, "remove") == {"success": True, "removed": True, "existed": False}
    p = agent.root / "one"
    p.mkdir()
    (p / "file").write_text("something new")
    assert call(agent, "remove") == {"success": True, "removed": True, "existed": False}
    assert (p / "file").read_text() == "something new"


def test_remove_ignores_the_device_number(agent):
    """网络存储的设备号换台机器、重新挂载都会变：只认 inode。"""
    agent.assistant_only = True
    call(agent, "init", content="x")
    info = os.lstat(agent.root / "one")
    record_of(agent).write_text(
        json.dumps({"bot_id": "b1", "ino": info.st_ino, "dev": info.st_dev + 1})
    )
    assert call(agent, "remove") == {"success": True, "removed": True, "existed": True}
    assert not os.path.lexists(agent.root / "one")


@pytest.mark.parametrize(
    "raw",
    [b"not json", b"[]", b'{"bot_id": "b1"}', b'{"bot_id": "b1", "ino": "1"}', b'{"ino": 1}'],
)
def test_remove_cannot_vouch_for_a_damaged_record(agent, raw):
    agent.assistant_only = True
    call(agent, "init", content="x")
    record_of(agent).write_bytes(raw)
    assert call(agent, "remove") == UNVERIFIED
    assert (agent.root / "one").is_dir()


def test_init_registers_before_writing_anything_into_the_directory(agent, monkeypatch):
    """初始化中途崩了：目录里已有东西，登记也已经在，删除照样认得出。"""
    agent.assistant_only = True

    def crash(self, root):
        assert json.loads(record_of(agent).read_text())["bot_id"] == "b1"
        (root / "half").write_text("x")
        raise OSError("synthetic")

    monkeypatch.setattr(workspace.Workspace, "instructions", crash)
    with pytest.raises(OperationError):
        call(agent, "init", content="x")
    monkeypatch.undo()
    assert (agent.root / "one" / "half").exists()
    assert not (agent.root / "one" / workspace.MARKER).exists()
    assert call(agent, "remove") == {"success": True, "removed": True, "existed": True}


def test_a_tombstone_does_not_block_a_fresh_init(agent):
    agent.assistant_only = True
    call(agent, "init", content="x")
    call(agent, "remove")
    assert call(agent, "init", content="x")["initialized"]
    info = os.lstat(agent.root / "one")
    assert json.loads(record_of(agent).read_text()) == {"bot_id": "b1", "ino": info.st_ino}


def test_old_tombstones_are_swept_and_nothing_else(agent, tmp_path):
    agent.assistant_only = True
    owners = workspace.owners_dir(agent.root)
    old = time.time() - workspace.TOMBSTONE_SECONDS - 60
    entries = {
        "old-tomb": '{"bot_id": "b1", "removed": true}',
        "new-tomb": '{"bot_id": "b2", "removed": true}',
        "old-live": '{"bot_id": "b3", "ino": 5}',
        "old-junk": "not json",
    }
    for name, text in entries.items():
        (owners / name).write_text(text)
        if name.startswith("old"):
            os.utime(owners / name, (old, old))
    outside = tmp_path / "outside-tomb"
    outside.write_text('{"bot_id": "b4", "removed": true}')
    os.utime(outside, (old, old))
    (owners / "linked").symlink_to(outside)
    os.utime(owners / "linked", (old, old), follow_symlinks=False)
    workspace.purge_tombstones(agent.root)
    assert sorted(p.name for p in owners.iterdir()) == [
        "linked",
        "new-tomb",
        "old-junk",
        "old-live",
    ]
    assert outside.exists()


def test_purging_tombstones_without_a_registry_creates_nothing(agent):
    workspace.purge_tombstones(agent.root)
    assert not os.path.lexists(agent.root / workspace.OWNERS_DIR)


def test_remove_treats_a_deeply_nested_record_as_not_owned(agent):
    agent.assistant_only = True
    call(agent, "init", content="x")
    record_of(agent).write_text("[" * 50000)
    assert call(agent, "remove") == UNVERIFIED
    assert (agent.root / "one").is_dir()


def test_remove_refuses_an_owners_directory_that_is_a_link(agent, tmp_path):
    agent.assistant_only = True
    call(agent, "init", content="x")
    owners = agent.root / workspace.OWNERS_DIR
    elsewhere = tmp_path / "elsewhere"
    owners.rename(elsewhere)
    owners.symlink_to(elsewhere)
    with pytest.raises(OperationError, match="工作区操作失败"):
        call(agent, "remove")
    assert (agent.root / "one").is_dir()


def test_remove_refuses_an_owners_directory_that_is_not_ours(agent, monkeypatch):
    agent.assistant_only = True
    call(agent, "init", content="x")
    uid = os.getuid()
    monkeypatch.setattr(workspace.os, "getuid", lambda: uid + 1)
    with pytest.raises(OperationError, match="工作区操作失败"):
        call(agent, "remove")
    assert (agent.root / "one").is_dir()


def test_assistant_init_refuses_an_empty_directory_registered_to_another_bot(agent):
    agent.assistant_only = True
    p = agent.root / "one"
    p.mkdir()
    owners = workspace.owners_dir(agent.root)
    info = os.lstat(p)
    (owners / record_of(agent).name).write_text(json.dumps({"bot_id": "b2", "ino": info.st_ino}))
    with pytest.raises(OperationError, match="其他员工"):
        call(agent, "init", content="x")
    assert list(p.iterdir()) == []


def test_a_deeply_nested_marker_is_simply_not_owned(agent):
    p = agent.root / "one"
    p.mkdir()
    (p / workspace.MARKER).write_text("[" * 50000)
    assert workspace.marker_data(p) == {}
    assert call(agent, "info")["owned"] is False


def test_the_trash_is_tightened_to_0700(agent):
    agent.assistant_only = True
    call(agent, "init", content="x")
    trash = agent.root / workspace.TRASH_DIR
    trash.mkdir(mode=0o755)
    trash.chmod(0o755)
    assert call(agent, "remove")["removed"] is True
    assert stat.S_IMODE(trash.stat().st_mode) == 0o700


def test_in_use_covers_directories_above_and_below_a_running_turn(tmp_path):
    from types import SimpleNamespace

    from runtime_daemon.assistant_turns import AssistantTurns

    turns = AssistantTurns(SimpleNamespace())
    turns.workdirs["t1"] = tmp_path / "a" / "b"
    assert turns.in_use(tmp_path / "a" / "b")
    assert turns.in_use(tmp_path / "a")  # 轮次的目录在它下面
    assert turns.in_use(tmp_path / "a" / "b" / "c")  # 轮次的目录是它的上级
    assert not turns.in_use(tmp_path / "x")
    assert not turns.in_use(tmp_path / "a" / "bb")


# ---- 轮次准入与删除共用一把锁 ------------------------------------------------------------


class RecordingLock:
    def __init__(self) -> None:
        self.inner = threading.Lock()
        self.held = False
        self.entered = 0

    def __enter__(self):
        self.inner.acquire()
        self.held, self.entered = True, self.entered + 1
        return self

    def __exit__(self, *exc):
        self.held = False
        self.inner.release()


def test_remove_checks_and_renames_under_the_admission_lock(agent, monkeypatch):
    from types import SimpleNamespace

    from runtime_daemon.assistant_turns import AssistantTurns

    agent.assistant_only = True
    call(agent, "init", content="x")
    turns = AssistantTurns(SimpleNamespace())
    lock = RecordingLock()
    turns.admission = lock
    agent.daemon = SimpleNamespace(assistant=turns)
    seen: list[bool] = []
    real_rename, real_in_use = os.rename, turns.in_use

    def rename(src, dst):
        seen.append(lock.held)
        return real_rename(src, dst)

    def in_use(path):
        seen.append(lock.held)
        return real_in_use(path)

    monkeypatch.setattr(workspace.os, "rename", rename)
    monkeypatch.setattr(turns, "in_use", in_use)
    assert call(agent, "remove")["removed"] is True
    assert seen == [True, True] and lock.entered == 1
