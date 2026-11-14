"""Upload and import operations (multipart/form-data) through the platform proxy."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import select

from coreman.core.db.models import OutboxItem, Task
from coreman.core.systems_catalog import proxy, uploads
from tests.api.test_systems_catalog import bearer, call
from tests.api.test_systems_proxy import proxied_turn
from tests.api.test_systems_write_confirm import (
    batch,
    outcomes,
    press,
    release,
    run_click,
    turn,
)
from tests.fakes.business_system import FakeBusinessSystem, install

FORM = {
    "type": "object",
    "required": ["file", "mode"],
    "properties": {
        "file": {"type": "string", "format": "binary"},
        "mode": {"type": "string", "enum": ["check", "apply"]},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
}
IMAGES = {
    "type": "object",
    "required": ["files"],
    "properties": {"files": {"type": "array", "items": {"type": "string", "format": "binary"}}},
}


def form_op(op_id: str, schema: dict[str, Any], risk: str) -> dict[str, Any]:
    return {
        "operationId": op_id,
        "tags": ["documents"],
        "summary": op_id,
        "x-permission": "none",
        "x-agent": {"risk": risk},
        "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": schema}}},
        "responses": {"200": {"description": "OK"}},
    }


def parts(request: httpx.Request) -> dict[str, Any]:
    """The multipart form the business system received: field -> text or (filename, bytes)."""
    import email.parser
    import email.policy

    head = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(head + request.content)
    out: dict[str, Any] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        payload = part.get_payload(decode=True)
        value = (filename, payload, part.get_content_type()) if filename else payload.decode()
        out.setdefault(name, []).append(value)
    return {k: v[0] if len(v) == 1 else v for k, v in out.items()}


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeBusinessSystem:
    system = FakeBusinessSystem()
    received: list[dict[str, Any]] = []

    def accept(request: httpx.Request) -> httpx.Response:
        assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
        received.append(parts(request))
        return httpx.Response(200, json={"code": 0, "data": {"rows": 2}})

    paths = system.spec["paths"]
    paths["/api/stock/imports/preview"] = {"post": form_op("previewImport", FORM, "read")}  # type: ignore[index]
    paths["/api/stock/images"] = {"post": form_op("uploadImages", IMAGES, "write")}  # type: ignore[index]
    system.routes[("POST", "/api/stock/imports/preview")] = accept
    system.routes[("POST", "/api/stock/images")] = accept
    system.received = received  # type: ignore[attr-defined]
    install(monkeypatch, system)
    return system


@pytest.fixture
def storage(app: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    app.state.settings = app.state.settings.model_copy(
        update={"object_storage_root": str(tmp_path)}
    )
    # Card clicks run in the worker, which reads the deployment settings.
    monkeypatch.setattr(proxy, "get_settings", lambda: app.state.settings)


async def upload(
    client: Any,
    auth: dict[str, str],
    data: bytes,
    filename: str = "stock.csv",
    mime: str = "text/csv",
) -> httpx.Response:
    return await client.put(
        uploads.UPLOAD_PATH,
        params={"filename": filename},
        content=data,
        headers={**auth, "content-type": mime},
    )


CSV = b"sku,quantity\nSKU-1,3\nSKU-2,5\n"


async def test_import_preview_sends_the_uploaded_file(
    client, app, db_session, fake, storage
) -> None:
    _, member, task = await proxied_turn(db_session)
    auth = bearer(app, task, member)

    described = await call(
        client, auth, "systems_describe", {"system": "stock", "operation_id": "previewImport"}
    )
    assert described["call"]["upload"]["file_fields"] == ["file"]
    assert uploads.UPLOAD_PATH in described["call"]["upload"]["curl"]
    assert "$COREMAN_SYSTEMS_MCP_TOKEN" in described["call"]["upload"]["curl"]

    sent = await upload(client, auth, CSV)
    assert sent.status_code == 200, sent.text
    stored = sent.json()
    assert stored["filename"] == "stock.csv" and stored["bytes"] == len(CSV)
    assert stored["content_type"] == "text/csv" and stored["expires_in"] == 3600

    body = {"file": {"upload_id": stored["upload_id"]}, "mode": "check", "tags": ["a", "b"]}
    result = await call(
        client,
        auth,
        "systems_call",
        {"system": "stock", "operation_id": "previewImport", "body": body},
    )
    assert result["ok"] is True and result["data"] == {"code": 0, "data": {"rows": 2}}
    (form,) = fake.received  # type: ignore[attr-defined]
    assert form["file"] == ("stock.csv", CSV, "text/csv")
    assert form["mode"] == "check" and form["tags"] == '["a", "b"]'


async def test_form_fields_are_checked(client, app, db_session, fake, storage) -> None:
    _, member, task = await proxied_turn(db_session)
    auth = bearer(app, task, member)

    async def preview(body: Any) -> dict[str, Any]:
        args = {"system": "stock", "operation_id": "previewImport", "body": body}
        return await call(client, auth, "systems_call", args)

    missing = await preview({"mode": "check"})
    assert missing["error"] == "invalid_arguments"
    assert any("body.file: required" in item for item in missing["invalid"])
    inline = await preview({"file": "sku,quantity", "mode": "check"})
    assert any("file field takes" in item for item in inline["invalid"])
    bad_mode = await preview({"file": {"upload_id": "x"}, "mode": "wipe"})
    assert bad_mode["error"] == "invalid_arguments"
    unknown = await preview({"file": {"upload_id": "not-an-upload"}, "mode": "check"})
    assert unknown["error"] == "upload_not_found" and unknown["field"] == "file"
    assert fake.received == []  # type: ignore[attr-defined]


async def test_an_upload_belongs_to_its_turn(client, app, db_session, fake, storage) -> None:
    _, member, task = await proxied_turn(db_session)
    stored = (await upload(client, bearer(app, task, member), CSV)).json()
    other = Task(bot_id=task.bot_id, kind="chat", payload={}, status="running")
    db_session.add(other)
    await db_session.commit()
    result = await call(
        client,
        bearer(app, other, member),
        "systems_call",
        {
            "system": "stock",
            "operation_id": "previewImport",
            "body": {"file": {"upload_id": stored["upload_id"]}, "mode": "check"},
        },
    )
    assert result["error"] == "upload_not_found"
    assert fake.received == []  # type: ignore[attr-defined]


async def test_upload_endpoint_checks(client, app, db_session, fake, storage, monkeypatch) -> None:
    _, member, task = await proxied_turn(db_session)
    auth = bearer(app, task, member)
    assert (await upload(client, {}, CSV)).status_code == 401
    assert (await upload(client, auth, b"")).status_code == 400
    missing_name = await client.put(uploads.UPLOAD_PATH, content=CSV, headers=auth)
    assert missing_name.status_code == 400
    monkeypatch.setattr(uploads, "UPLOAD_LIMIT", 10)
    assert (await upload(client, auth, CSV)).status_code == 413
    task.status = "succeeded"
    await db_session.commit()
    monkeypatch.setattr(uploads, "UPLOAD_LIMIT", 1024)
    assert (await upload(client, auth, CSV)).status_code == 403


async def test_file_write_is_confirmed_on_the_card_then_sent(
    client, app, db_session, db_engine, fake, storage
) -> None:
    bot, user, task = await turn(db_session, app)
    auth = bearer(app, task, user)
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 2000
    # What plain `curl --data-binary` declares: the type comes from the file name instead.
    curl_default = "application/x-www-form-urlencoded"
    first = (await upload(client, auth, png, "banner.png", curl_default)).json()
    second = (await upload(client, auth, png[:100], "logo.png", curl_default)).json()
    assert first["content_type"] == "image/png"
    staged = await call(
        client,
        auth,
        "systems_call",
        {
            "system": "stock",
            "operation_id": "uploadImages",
            "body": {
                "files": [{"upload_id": first["upload_id"]}, {"upload_id": second["upload_id"]}]
            },
        },
    )
    assert staged["status"] == "pending_confirmation"
    assert fake.received == []  # type: ignore[attr-defined]

    assert await release(app, task)
    card = (await db_session.scalars(select(OutboxItem).where(OutboxItem.kind == "send"))).one()
    text = card.payload["card"]["body"]["elements"][0]["text"]["content"]
    assert '文件 files = "banner.png"（2.0 KB，sha256 ' in text
    assert '文件 files = "logo.png"（100 B' in text
    assert first["upload_id"] not in text and "请求体" not in text

    state = await batch(db_session)
    result = await run_click(db_session, db_engine, await press(db_session, bot, task, state.id))
    assert result == "systems_write_done"
    (form,) = fake.received  # type: ignore[attr-defined]
    assert form["files"] == [
        ("banner.png", png, "image/png"),
        ("logo.png", png[:100], "image/png"),
    ]
    assert await outcomes(db_session) == [
        ("uploadImages", "proposed"),
        ("uploadImages", "confirmed_ok"),
    ]
