import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from coreman.core.db.models import StoredObject
from coreman.core.object_store import LocalObjectStore


async def source():
    yield b"one"
    yield b"two"


async def test_cleanup_expiry_orphans_and_path_boundary(db_session, tmp_path):
    now = datetime.now(UTC)
    store = LocalObjectStore(str(tmp_path), b"x" * 32, "https://example.test")
    active = await store.put(
        db_session, source(), filename="../../unsafe\n.html", content_type="text/html", now=now
    )
    expired = await store.put(
        db_session, source(), filename="old", content_type="text/plain", now=now - timedelta(days=2)
    )
    await db_session.commit()
    assert active.filename == "unsafe.html"
    assert store.path(active.id).read_bytes() == b"onetwo"
    assert not store.valid(active.id, int(now.timestamp()) - 1, "x" * 64, now)
    assert not store.valid(active.id, int(active.expires_at.timestamp()), "é" * 64, now)
    orphan = store.root / uuid.uuid4().hex
    orphan.write_bytes(b"orphan")
    partial = store.root / ".upload-old"
    partial.write_bytes(b"partial")
    outside = tmp_path / "keep"
    outside.write_bytes(b"keep")
    link = store.root / uuid.uuid4().hex
    link.symlink_to(outside)
    for path in (orphan, partial, link):
        os.utime(path, (now.timestamp() - 100000,) * 2, follow_symlinks=False)
    assert await store.cleanup(db_session, now) == 4
    await db_session.commit()
    assert store.path(active.id).exists() and not store.path(expired.id).exists()
    assert outside.read_bytes() == b"keep" and not link.is_symlink()
    assert (await db_session.scalars(select(StoredObject))).all() == [active]
    assert await store.cleanup(db_session, now) == 0


async def test_failed_or_cancelled_upload_leaves_no_partial(db_session, tmp_path, monkeypatch):
    from coreman.core import object_store

    store = LocalObjectStore(str(tmp_path), b"x" * 32, "https://example.test")
    monkeypatch.setattr(object_store, "MAX_SIZE", 4)
    with pytest.raises(ValueError, match="size limit"):
        await store.put(
            db_session, source(), filename="a", content_type="text/plain", now=datetime.now(UTC)
        )
    assert not list(store.root.iterdir())
    ready = asyncio.Event()

    async def blocked():
        yield b"ok"
        ready.set()
        await asyncio.Future()

    task = asyncio.create_task(
        store.put(
            db_session, blocked(), filename="a", content_type="text/plain", now=datetime.now(UTC)
        )
    )
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list(store.root.iterdir())
    assert await db_session.scalar(select(StoredObject)) is None
