"""带文件的写操作走完整条路：真的文件中转区与个人工具，只有飞书、Tower 和临时桶是替身。"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from coreman.core.assistants.approvals import attachments, execute, service, tower
from coreman.core.db.models import AssistantApproval
from coreman.core.feishu_personal.files import FileRelay
from tests.api.test_assistant_approval_gate import (  # noqa: F401
    FEISHU,
    ok,
    owner_turn,
    tower_ready,
)
from tests.api.test_feishu_personal import URL, headers, rpc, value
from tests.integration.approval_helpers import FakeBucket, install_tower

UPLOAD = f"{FEISHU}/drive/v1/files/upload_all"
CONTENT = b"%PDF-1.4 plan"


@pytest.fixture
def bucket(app: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> FakeBucket:
    """配好临时桶的部署；桶本身在内存里。"""
    app.state.settings = app.state.settings.model_copy(
        update={
            "object_storage_root": str(tmp_path),
            "assistant_tmp_s3_endpoint": "https://s3.example.com",
            "assistant_tmp_s3_bucket": "tmp-bucket",
            "assistant_tmp_s3_access_key": SecretStr("access"),
            "assistant_tmp_s3_secret_key": SecretStr("secret"),
        }
    )
    fake = FakeBucket()
    monkeypatch.setattr(attachments, "client", fake)
    return fake


async def upload(client: httpx.AsyncClient, auth: dict[str, str]) -> str:
    """助理取上传链接、把本地文件传到中转区，拿到 upload_id。"""
    prepared = value(
        await client.post(
            URL, headers=auth, json=rpc("feishu_prepare_upload", {"filename": "方案.pdf"})
        )
    )
    path = prepared["upload_url"].removeprefix("http://testserver")
    return str((await client.put(path, content=CONTENT)).json()["upload_id"])


@pytest.mark.usefixtures("tower_ready")
async def test_file_reaches_feishu_only_after_approval_and_unchanged(
    client: httpx.AsyncClient,
    app: Any,
    db_session: Any,
    bucket: FakeBucket,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_tower(monkeypatch)
    _, user, task = await owner_turn(db_session, app, assistant=True)
    auth = await headers(app, task, user)
    # 取上传链接不改动飞书里的任何东西：不过审批，也不调飞书（没有 mock 任何飞书接口）。
    upload_id = await upload(client, auth)
    assert fake.submitted == [] and bucket.objects == {}
    arguments = {"upload_id": upload_id, "folder_token": "fld_1", "approval": {"topic": "方案"}}
    with respx.mock as mock:
        saved = mock.post(UPLOAD).mock(return_value=ok({"file_token": "box_1"}))
        out = value(
            await client.post(URL, headers=auth, json=rpc("feishu_save_to_drive", arguments))
        )
    assert out["status"] == "pending_approval" and not saved.called
    (key,) = bucket.objects
    assert bucket.objects[key] == CONTENT
    assert f"https://tmp.example.com/{key}" in fake.submitted[0]["content"]
    assert '- "方案.pdf"（application/pdf，13 B）' in fake.submitted[0]["content"]
    row = await db_session.get(AssistantApproval, out["approval_id"])
    await service.decide(
        db_session,
        approval_id=row.approval_id,
        external_id=row.external_id,
        status="approved",
        reason=None,
        decided_at=None,
    )
    await db_session.commit()
    # 批准之后：从临时桶取回、重新落进中转区，交给原来的工具函数以所有者身份上传。
    relay = FileRelay(app.state.settings, app.state.cipher)
    with respx.mock as mock:
        saved = mock.post(UPLOAD).mock(return_value=ok({"file_token": "box_1"}))
        status = await execute.run(
            app.state.session_factory,
            app.state.cipher,
            row.id,
            task,
            tower.config(app.state.settings),
            relay,
        )
    assert status == "executed" and saved.call_count == 1
    request = saved.calls[0].request
    assert request.headers["authorization"] == "Bearer user-secret"
    assert CONTENT in request.content and b'name="parent_node"\r\n\r\nfld_1' in request.content
    assert len(fake.redeemed) == 1


@pytest.mark.usefixtures("tower_ready")
async def test_without_a_temporary_bucket_file_writes_are_unavailable(
    client: httpx.AsyncClient,
    app: Any,
    db_session: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_tower(monkeypatch)
    monkeypatch.setattr(app.state.settings, "object_storage_root", str(tmp_path))
    _, user, task = await owner_turn(db_session, app, assistant=True)
    auth = await headers(app, task, user)
    upload_id = await upload(client, auth)
    with respx.mock as mock:
        saved = mock.post(UPLOAD).mock(return_value=ok({"file_token": "box_1"}))
        out = value(
            await client.post(
                URL,
                headers=auth,
                json=rpc("feishu_save_to_drive", {"upload_id": upload_id, "folder_token": "f"}),
            )
        )
    assert out["error"] == "attachments_not_supported"
    assert not saved.called and fake.submitted == []
