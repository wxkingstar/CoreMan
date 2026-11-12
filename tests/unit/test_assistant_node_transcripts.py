"""节点上的转录包：只带续聊需要的文件，还原时逐个成员把关。"""

from __future__ import annotations

import io
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
    import os

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
