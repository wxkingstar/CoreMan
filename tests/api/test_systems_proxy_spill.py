"""Large and file responses of proxied calls are kept in short-term storage, with a preview."""

from __future__ import annotations

import json
from io import BytesIO
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from coreman.core.db.models import StoredObject, SystemCall, Task
from coreman.core.s3_store import S3ObjectStore
from coreman.core.systems_catalog import spill
from tests.api.test_systems_catalog import bearer, call
from tests.api.test_systems_proxy import proxied_turn
from tests.fakes.business_system import FakeBusinessSystem, install

REPORT = {"system": "stock", "operation_id": "dailyReport", "body": {}}


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeBusinessSystem:
    system = FakeBusinessSystem()
    install(monkeypatch, system)
    return system


@pytest.fixture
def storage(app: Any, tmp_path: Any) -> None:
    app.state.settings = app.state.settings.model_copy(
        update={"object_storage_root": str(tmp_path)}
    )


def add_get(fake: FakeBusinessSystem, op_id: str, path: str, handler: Any) -> None:
    fake.spec["paths"][path] = {  # type: ignore[index]
        "get": {
            "operationId": op_id,
            "tags": ["documents"],
            "summary": op_id,
            "x-permission": "none",
            "responses": {"200": {"description": "OK"}},
        }
    }
    fake.routes[("GET", path)] = handler


async def test_large_result_gives_a_preview_and_a_runtime_only_link(
    client, app, db_session, fake, storage
) -> None:
    _, member, task = await proxied_turn(db_session)
    auth = bearer(app, task, member)
    result = await call(client, auth, "systems_call", REPORT)
    assert result["ok"] is True and result["stored"] is True
    assert "data" not in result and len(json.dumps(result["preview"])) < 5000
    assert result["preview_omitted_items"] > 0
    assert result["content_trust"] == "system_returned_data"
    url = result["download"]["url"]
    assert url.startswith("http://testserver" + spill.OBJECT_PATH)
    assert "$COREMAN_SYSTEMS_MCP_TOKEN" in result["download"]["curl"]

    # The runtime downloads the whole result with this turn's catalog capability.
    path = url.removeprefix("http://testserver")
    full = await client.get(path, headers=auth)
    assert full.status_code == 200, full.text
    assert len(json.loads(full.content)["data"]) == 5000
    assert full.headers["content-disposition"].startswith("attachment;")

    row = await db_session.scalar(select(SystemCall).where(SystemCall.object_id.is_not(None)))
    assert row is not None and row.outcome == "ok"
    assert (await client.get(path)).status_code == 401
    # Another turn of the same member cannot read it.
    other = Task(bot_id=task.bot_id, kind="chat", payload={}, status="running")
    db_session.add(other)
    await db_session.commit()
    assert (await client.get(path, headers=bearer(app, other, member))).status_code == 404
    # Nor once the turn is over.
    task.status = "succeeded"
    await db_session.commit()
    assert (await client.get(path, headers=auth)).status_code == 403


async def test_file_gets_a_signed_link_for_people(client, app, db_session, fake, storage) -> None:
    pdf = b"%PDF-1.4 " + b"x" * 100
    add_get(
        fake,
        "exportReport",
        "/api/stock/export",
        lambda request: httpx.Response(
            200,
            content=pdf,
            headers={
                "content-type": "application/pdf",
                "content-disposition": "attachment; filename*=UTF-8''%E6%97%A5%E6%8A%A5.pdf",
            },
        ),
    )
    _, member, task = await proxied_turn(db_session)
    result = await call(
        client,
        bearer(app, task, member),
        "systems_call",
        {"system": "stock", "operation_id": "exportReport"},
    )
    assert result["file"] is True and result["filename"] == "日报.pdf"
    assert result["bytes"] == len(pdf) and result["expires_in"] == 3600
    link = result["url"].removeprefix("http://testserver")
    assert link.startswith("/api/objects/")
    downloaded = await client.get(link)
    assert downloaded.status_code == 200 and downloaded.content == pdf
    row = await db_session.scalar(select(StoredObject))
    assert row is not None and row.content_type == "application/pdf"


async def test_stored_text_never_contains_the_token(client, app, db_session, fake, storage) -> None:
    seen: list[str | None] = []

    def echo(request: httpx.Request) -> httpx.Response:
        token = fake.token(request)
        seen.append(token)
        return httpx.Response(200, json={"rows": [{"token": token, "n": i} for i in range(400)]})

    add_get(fake, "echoAll", "/api/stock/echo-all", echo)
    _, member, task = await proxied_turn(db_session)
    auth = bearer(app, task, member)
    result = await call(
        client, auth, "systems_call", {"system": "stock", "operation_id": "echoAll"}
    )
    assert result["stored"] is True
    full = await client.get(
        result["download"]["url"].removeprefix("http://testserver"), headers=auth
    )
    assert seen[0] and seen[0].encode() not in full.content
    assert full.content.count(b"[REDACTED]") == 400


async def test_small_results_stay_inline(client, app, db_session, fake, storage) -> None:
    _, member, task = await proxied_turn(db_session)
    result = await call(
        client,
        bearer(app, task, member),
        "systems_call",
        {"system": "stock", "operation_id": "listDocuments", "query": {"status": "confirmed"}},
    )
    assert result["data"]["data"]["items"][0]["id"] == "D1" and "stored" not in result
    assert not await db_session.scalar(select(StoredObject.id))


async def test_storage_failure_falls_back_to_truncation(
    client, app, db_session, fake, storage, monkeypatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(spill, "object_store", broken)
    _, member, task = await proxied_turn(db_session)
    result = await call(client, bearer(app, task, member), "systems_call", REPORT)
    assert result["ok"] is True and result["truncated"] is True and "stored" not in result


async def test_redaction_spans_chunk_boundaries(monkeypatch) -> None:
    monkeypatch.setattr(spill, "CHUNK", 4)
    body = BytesIO(b"aaSECRETbbSECRETSECRETcc")
    out = b"".join([part async for part in spill._chunks(body, b"SECRET")])
    assert out == b"aa[REDACTED]bb[REDACTED][REDACTED]cc"


def test_s3_addressing_is_configurable(api_settings) -> None:
    base = api_settings.model_copy(
        update={
            "s3_bucket": "test-bucket",
            "s3_access_key": SecretStr("synthetic-access-key"),
            "s3_secret_key": SecretStr("synthetic-secret-key"),
        }
    )
    assert base.s3_addressing == "path"
    for style in ("path", "virtual"):
        client = S3ObjectStore(base.model_copy(update={"s3_addressing": style})).client()
        assert client.meta.config.s3["addressing_style"] == style
