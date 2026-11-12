"""节点上的转录包：只带续聊需要的文件，还原时逐个成员把关。"""

from __future__ import annotations

import io
import os
import tarfile

import pytest

from runtime_daemon import transcripts


def touch(path, content=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def names(data: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        return sorted(tar.getnames())


def test_claude_bundle_holds_only_projects(tmp_path):
    touch(tmp_path / "config/projects/-work/s1.jsonl", b'{"type":"user"}\n')
    touch(tmp_path / "config/history.jsonl")
    touch(tmp_path / "config/.credentials.json")
    touch(tmp_path / "driver/s1/codex_thread.txt")
    touch(tmp_path / "home/.cache/x")
    data = transcripts.pack(tmp_path, "claude", "s1")
    assert data is not None
    assert names(data) == ["config/projects/-work/s1.jsonl"]


def test_codex_bundle_adds_the_thread_binding(tmp_path):
    touch(tmp_path / "config/sessions/2026/11/12/rollout-a-t1.jsonl")
    touch(tmp_path / "driver/s1/codex_thread.txt", b"t1")
    touch(tmp_path / "driver/s1.jsonl")
    data = transcripts.pack(tmp_path, "codex", "s1")
    assert data is not None
    assert names(data) == [
        "config/sessions/2026/11/12/rollout-a-t1.jsonl",
        "driver/s1/codex_thread.txt",
    ]


def test_nothing_to_pack(tmp_path):
    assert transcripts.pack(tmp_path, "claude", "s1") is None


def test_symlinks_are_not_followed(tmp_path):
    secret = tmp_path / "outside.txt"
    touch(secret, b"secret")
    (tmp_path / "config/projects").mkdir(parents=True)
    (tmp_path / "config/projects/link.jsonl").symlink_to(secret)
    assert transcripts.pack(tmp_path, "claude", "s1") is None


def test_too_large(tmp_path, monkeypatch):
    monkeypatch.setattr(transcripts, "MAX_BYTES", 64)
    touch(tmp_path / "config/projects/-work/s1.jsonl", os.urandom(4096))
    with pytest.raises(transcripts.TooLarge):
        transcripts.pack(tmp_path, "claude", "s1")


def test_round_trip(tmp_path):
    source, target = tmp_path / "a", tmp_path / "b"
    touch(source / "config/sessions/r.jsonl", b"rollout")
    touch(source / "driver/s1/codex_thread.txt", b"t1")
    data = transcripts.pack(source, "codex", "s1")
    assert data is not None
    target.mkdir()
    assert transcripts.unpack(target, "codex", "s1", data) == 2
    assert (target / "config/sessions/r.jsonl").read_bytes() == b"rollout"
    assert (target / "driver/s1/codex_thread.txt").read_bytes() == b"t1"
    assert (target / "config/sessions/r.jsonl").stat().st_mode & 0o077 == 0


def bundle(*members: tuple[str, bytes | None, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, link, content in members:
            info = tarfile.TarInfo(name)
            if link is not None:
                info.type = tarfile.SYMTYPE
                info.linkname = link.decode()
                tar.addfile(info)
            else:
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


@pytest.mark.parametrize(
    "member",
    [
        ("/etc/passwd", None, b"x"),
        ("config/projects/../../escape", None, b"x"),
        ("config/history.jsonl", None, b"x"),
        ("driver/other/codex_thread.txt", None, b"x"),
        ("config/projects/link", b"/etc/passwd", b""),
    ],
)
def test_unpack_rejects_anything_out_of_place(tmp_path, member):
    with pytest.raises(transcripts.BadBundle):
        transcripts.unpack(tmp_path, "codex", "s1", bundle(member))
    assert not (tmp_path / "escape").exists()


def test_unpack_rejects_garbage_and_bombs(tmp_path, monkeypatch):
    with pytest.raises(transcripts.BadBundle):
        transcripts.unpack(tmp_path, "claude", "s1", b"not a tarball")
    monkeypatch.setattr(transcripts, "MAX_EXPANDED", 10)
    with pytest.raises(transcripts.BadBundle):
        transcripts.unpack(
            tmp_path, "claude", "s1", bundle(("config/projects/a.jsonl", None, b"x" * 64))
        )


CODES = {"path", "type", "duplicate", "too_many", "too_large", "io"}


def test_pack_never_follows_a_symlinked_tree_root(tmp_path):
    host = tmp_path / "host"
    touch(host / "secret.jsonl", b"secret")
    root = tmp_path / "turn"
    (root / "config").mkdir(parents=True)
    (root / "config/projects").symlink_to(host)
    assert transcripts.pack(root, "claude", "s1") is None


def test_pack_never_follows_a_symlinked_config(tmp_path):
    host = tmp_path / "host"
    touch(host / "projects/-work/s1.jsonl", b"secret")
    touch(host / "sessions/r.jsonl", b"secret")
    root = tmp_path / "turn"
    root.mkdir()
    (root / "config").symlink_to(host)
    assert transcripts.pack(root, "claude", "s1") is None
    assert transcripts.pack(root, "codex", "s1") is None


def test_pack_never_follows_a_symlinked_subdirectory(tmp_path):
    host = tmp_path / "host"
    touch(host / "secret.jsonl", b"secret")
    root = tmp_path / "turn"
    touch(root / "config/projects/ok/s1.jsonl", b"fine")
    (root / "config/projects/evil").symlink_to(host)
    data = transcripts.pack(root, "claude", "s1")
    assert data is not None
    assert names(data) == ["config/projects/ok/s1.jsonl"]


def test_pack_never_follows_a_symlinked_driver_dir(tmp_path):
    host = tmp_path / "host"
    touch(host / "codex_thread.txt", b"secret")
    root = tmp_path / "turn"
    (root / "driver").mkdir(parents=True)
    (root / "driver/s1").symlink_to(host)
    assert transcripts.pack(root, "codex", "s1") is None
    (root / "driver/s1").unlink()
    (root / "driver").rmdir()
    (root / "driver").symlink_to(tmp_path)
    touch(tmp_path / "s1/codex_thread.txt", b"secret")
    assert transcripts.pack(root, "codex", "s1") is None


def test_pack_skips_a_symlinked_thread_file(tmp_path):
    secret = tmp_path / "secret"
    touch(secret, b"secret")
    root = tmp_path / "turn"
    touch(root / "config/sessions/r.jsonl", b"rollout")
    (root / "driver/s1").mkdir(parents=True)
    (root / "driver/s1/codex_thread.txt").symlink_to(secret)
    data = transcripts.pack(root, "codex", "s1")
    assert data is not None
    assert names(data) == ["config/sessions/r.jsonl"]


def test_pack_refuses_early_on_total_size(tmp_path, monkeypatch):
    monkeypatch.setattr(transcripts, "MAX_EXPANDED", 1024)
    path = tmp_path / "config/projects/s1.jsonl"
    path.parent.mkdir(parents=True)
    with path.open("wb") as handle:
        handle.truncate(10**9)
    with pytest.raises(transcripts.TooLarge) as info:
        transcripts.pack(tmp_path, "claude", "s1")
    assert str(info.value) == "too_large"


def test_pack_stops_when_the_output_passes_the_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(transcripts, "MAX_BYTES", 2000)
    for i in range(5):
        touch(tmp_path / f"config/projects/f{i}.jsonl", os.urandom(1500))
    with pytest.raises(transcripts.TooLarge):
        transcripts.pack(tmp_path, "claude", "s1")


def test_pack_ignores_growth_after_the_size_check(tmp_path, monkeypatch):
    path = tmp_path / "config/projects/s1.jsonl"
    touch(path, b"a" * 10)
    real_open = os.open
    opened = []

    def growing(name, flags, *args, **kwargs):
        if name == "s1.jsonl" and not flags & os.O_DIRECTORY:
            opened.append(1)
        if len(opened) == 2 and name == "s1.jsonl" and not flags & os.O_DIRECTORY:
            opened.append(1)
            with path.open("ab") as handle:
                handle.write(b"b" * 100)
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(transcripts.os, "open", growing)
    data = transcripts.pack(tmp_path, "claude", "s1")
    assert data is not None
    monkeypatch.undo()
    target = tmp_path / "t"
    target.mkdir()
    transcripts.unpack(target, "claude", "s1", data)
    assert len((target / "config/projects/s1.jsonl").read_bytes()) == 10


def test_unpack_directories_are_private(tmp_path):
    old = os.umask(0o022)
    try:
        data = bundle(("config/projects/a/b/s.jsonl", None, b"x"))
        transcripts.unpack(tmp_path, "claude", "s1", data)
    finally:
        os.umask(old)
    for rel in ("config", "config/projects", "config/projects/a", "config/projects/a/b"):
        assert (tmp_path / rel).stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "config/projects/a/b/s.jsonl").stat().st_mode & 0o777 == 0o600


def test_unpack_refuses_a_symlinked_component(tmp_path):
    host = tmp_path / "host"
    host.mkdir()
    root = tmp_path / "turn"
    root.mkdir()
    (root / "config").symlink_to(host)
    with pytest.raises(transcripts.BadBundle):
        transcripts.unpack(root, "claude", "s1", bundle(("config/projects/a.jsonl", None, b"x")))
    assert list(host.iterdir()) == []


def test_unpack_rejects_a_duplicate_member(tmp_path):
    data = bundle(
        ("config/projects/a.jsonl", None, b"one"), ("config/projects/a.jsonl", None, b"two")
    )
    with pytest.raises(transcripts.BadBundle) as info:
        transcripts.unpack(tmp_path, "claude", "s1", data)
    assert str(info.value) == "duplicate"
    assert not (tmp_path / "config").exists()


def test_unpack_does_not_follow_a_planted_symlink(tmp_path):
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep")
    root = tmp_path / "turn"
    (root / "config/projects").mkdir(parents=True)
    (root / "config/projects/a.jsonl").symlink_to(victim)
    with pytest.raises(transcripts.BadBundle):
        transcripts.unpack(root, "claude", "s1", bundle(("config/projects/a.jsonl", None, b"x")))
    assert victim.read_bytes() == b"keep"
    assert (root / "config/projects/a.jsonl").is_symlink()


def test_unpack_caps_the_member_count(tmp_path, monkeypatch):
    monkeypatch.setattr(transcripts, "MAX_MEMBERS", 3)
    data = bundle(*[(f"config/projects/f{i}", None, b"x") for i in range(4)])
    with pytest.raises(transcripts.BadBundle) as info:
        transcripts.unpack(tmp_path, "claude", "s1", data)
    assert str(info.value) == "too_many"
    assert not (tmp_path / "config").exists()


def test_unpack_counts_directories_as_members(tmp_path, monkeypatch):
    monkeypatch.setattr(transcripts, "MAX_MEMBERS", 2)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for i in range(3):
            info = tarfile.TarInfo(f"config/projects/d{i}")
            info.type = tarfile.DIRTYPE
            tar.addfile(info)
    with pytest.raises(transcripts.BadBundle):
        transcripts.unpack(tmp_path, "claude", "s1", buffer.getvalue())


def test_unpack_rejects_an_oversized_archive_up_front(tmp_path, monkeypatch):
    monkeypatch.setattr(transcripts, "MAX_BYTES", 8)
    with pytest.raises(transcripts.BadBundle) as info:
        transcripts.unpack(tmp_path, "claude", "s1", b"x" * 9)
    assert str(info.value) == "too_large"


def test_unpack_rejects_a_file_named_like_a_tree_root(tmp_path):
    with pytest.raises(transcripts.BadBundle):
        transcripts.unpack(tmp_path, "claude", "s1", bundle(("config/projects", None, b"x")))


def test_errors_carry_only_fixed_codes(tmp_path):
    for member in (
        ("/etc/passwd/secret-name", None, b"x"),
        ("config/projects/link", b"/etc/passwd", b""),
    ):
        with pytest.raises(transcripts.BadBundle) as info:
            transcripts.unpack(tmp_path, "claude", "s1", bundle(member))
        assert str(info.value) in CODES
    with pytest.raises(transcripts.BadBundle) as info:
        transcripts.unpack(tmp_path, "claude", "s1", b"not a tarball")
    assert str(info.value) in CODES


def test_unpack_leaves_nothing_behind_on_failure(tmp_path):
    keep = tmp_path / "config/projects/old.jsonl"
    touch(keep, b"old")
    data = bundle(
        ("config/projects/a/one.jsonl", None, b"1"),
        ("config/projects/a/two.jsonl", None, b"2"),
        ("config/history.jsonl", None, b"3"),
    )
    with pytest.raises(transcripts.BadBundle):
        transcripts.unpack(tmp_path, "claude", "s1", data)
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == [
        "config",
        "config/projects",
        "config/projects/old.jsonl",
    ]
    assert keep.read_bytes() == b"old"
