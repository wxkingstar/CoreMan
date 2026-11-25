"""业务系统代员工发起任务时附带图片：签名上传、按文件头认格式、去重、归属、限时下载链接。"""

from __future__ import annotations

import hashlib
import time
import uuid
from datetime import timedelta

from sqlalchemy import select

from coreman.core.api_tasks import uploads
from coreman.core.auth.signatures import sign_request
from coreman.core.db.models import ApiTask, ApiTaskUpload, InboundEvent, OutboxItem, Task
from coreman.core.timeutils import utcnow
from tests.api.test_api_task_clients_api import _setup

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x01" * 64


def _signed_form(
    path: str, fields: dict[str, str], secret: str = "client-secret"
) -> dict[str, str]:
    ts = str(int(time.time()))
    return {
        "X-App-Key": "intake",
        "X-Timestamp": ts,
        "X-Signature": sign_request("POST", path, fields, ts, "intake", secret),
    }


async def _upload(client, data: bytes, *, user: str = "alice", sha: str | None = None):  # type: ignore[no-untyped-def]
    path = "/api/infra/tasks/uploads"
    fields = {"user": user, "sha256": sha or hashlib.sha256(data).hexdigest()}
    auth, client.auth = client.auth, None
    try:
        return await client.post(
            path,
            data=fields,
            files={"file": ("shot.png", data, "image/png")},
            headers=_signed_form(path, fields),
        )
    finally:
        client.auth = auth


async def test_upload_dedupes_and_checks_format_size_and_binding(client, db_session):
    await _setup(client, db_session)
    first = await _upload(client, PNG)
    assert first.status_code == 201, first.text
    data = first.json()["data"]
    assert data["content_type"] == "image/png" and data["bytes"] == len(PNG)
    again = await _upload(client, PNG)
    assert again.json()["data"]["upload_id"] == data["upload_id"]
    assert (await _upload(client, b"GIF89a" + b"\x00" * 20)).status_code == 415
    # sha256 是签过名的字段：文件换了就对不上，按签名无效处理。
    assert (await _upload(client, JPEG, sha=hashlib.sha256(PNG).hexdigest())).status_code == 401
    assert (await _upload(client, PNG, user="nobody")).status_code == 403


async def test_upload_size_limit(client, db_session, monkeypatch):
    await _setup(client, db_session)
    monkeypatch.setattr(uploads, "MAX_BYTES", 32)
    assert (await _upload(client, PNG)).status_code == 413


async def test_task_references_own_uploads_and_keeps_them(client, db_session):
    await _setup(client, db_session)
    png = (await _upload(client, PNG)).json()["data"]["upload_id"]
    jpg = (await _upload(client, JPEG)).json()["data"]["upload_id"]
    other = (await _upload(client, PNG + b"x", user="bob")).json()["data"]["upload_id"]
    body = {"user": "alice", "bot_key": "sales_bot", "prompt": "识别截图", "images": [png, jpg]}
    created = await client.post("/api/infra/tasks/run", json=body)
    assert created.status_code == 202, created.text
    row = await db_session.get(ApiTask, uuid.UUID(created.json()["data"]["id"]))
    assert [str(i) for i in row.image_ids] == [png, jpg]
    task = await db_session.get(Task, row.task_id)
    event = await db_session.get(InboundEvent, task.inbound_event_id)
    images = [p for p in event.payload["parts"] if p["type"] == "image"]
    assert [p["ref"]["file_key"] for p in images] == [png, jpg]
    kept = await db_session.get(ApiTaskUpload, uuid.UUID(png))
    assert kept.expires_at > utcnow() + timedelta(days=29)
    trigger = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.dedupe_key == f"api-task:{row.id}:trigger")
    )
    assert "附 2 张图片" in trigger.payload["markdown"]
    # 别的员工传的图引用不到。
    denied = await client.post("/api/infra/tasks/run", json={**body, "images": [other]})
    assert denied.status_code == 404


async def test_raw_link_needs_a_valid_token(client, db_session, app):
    await _setup(client, db_session)
    png = uuid.UUID((await _upload(client, PNG)).json()["data"]["upload_id"])
    url = uploads.link(app.state.cipher, "http://test", png, utcnow())
    path = url.removeprefix("http://test")
    client.auth = None
    got = await client.get(path)
    assert got.status_code == 200 and got.content == PNG
    assert got.headers["content-type"] == "image/png"
    other = uuid.uuid4()
    assert (await client.get(path.replace(str(png), str(other)))).status_code == 404
    expired = uploads.link(app.state.cipher, "http://test", png, utcnow() - timedelta(days=2))
    assert (await client.get(expired.removeprefix("http://test"))).status_code == 404


async def test_sweep_drops_expired_uploads(client, db_session):
    await _setup(client, db_session)
    png = uuid.UUID((await _upload(client, PNG)).json()["data"]["upload_id"])
    assert await uploads.sweep(db_session, utcnow()) == 0
    assert await uploads.sweep(db_session, utcnow() + timedelta(days=2)) == 1
    await db_session.commit()
    assert await db_session.get(ApiTaskUpload, png) is None
