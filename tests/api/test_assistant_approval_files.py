"""带文件的写操作走完整条路：真的文件中转区与个人工具，只有飞书、Tower 和临时桶是替身。"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import select

from coreman.core.assistants.approvals import attachments, batch
from coreman.core.db.models import Bot, InteractionState, OutboxItem, Task
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
async def test_file_reaches_feishu_only_after_the_owner_confirms_and_unchanged(
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
    assert out["status"] == "pending_confirmation" and not saved.called
    (key,) = bucket.objects
    assert bucket.objects[key] == CONTENT and fake.submitted == []
    # 本轮结束：确认卡列出文件名、类型与大小，附件另有平台写的查看链接。
    monkeypatch.setattr(batch, "_store", lambda: attachments.store(app.state.settings))
    async with app.state.session_factory() as session:
        assert await batch.release(session, bot_id=task.bot_id, task_id=task.id, send=True)
        await session.commit()
    card = await db_session.scalar(select(OutboxItem).where(OutboxItem.kind == "send"))
    elements = card.payload["card"]["body"]["elements"]
    assert '"方案.pdf"（application/pdf，13 B）' in elements[0]["text"]["content"]
    assert f"(https://tmp.example.com/{key}?expires=3600)" in elements[1]["content"]
    # 所有者确认之后：从临时桶取回、核对、重新落进中转区，交给原来的工具函数以所有者身份上传。
    state = await batch_state(db_session)
    relay = FileRelay(app.state.settings, app.state.cipher)
    async with app.state.session_factory() as session:
        bot = await session.get(Bot, task.bot_id)
        origin = await session.get(Task, task.id)
        with respx.mock as mock:
            saved = mock.post(UPLOAD).mock(return_value=ok({"file_token": "box_1"}))
            ran = await batch.run_confirmed(
                app.state.session_factory,
                session,
                app.state.cipher,
                bot,
                origin,
                state.state["items"],
                relay,
            )
        await session.commit()
    assert isinstance(ran, list) and ran[0] is not None and ran[0][1] is None
    assert saved.call_count == 1
    request = saved.calls[0].request
    assert request.headers["authorization"] == "Bearer user-secret"
    assert CONTENT in request.content and b'name="parent_node"\r\n\r\nfld_1' in request.content
    # 桶里的内容被换掉：核对不过，整批不执行。
    bucket.objects[key] = b"tampered"
    async with app.state.session_factory() as session:
        bot = await session.get(Bot, task.bot_id)
        origin = await session.get(Task, task.id)
        with respx.mock as mock:
            again = mock.post(UPLOAD).mock(return_value=ok({"file_token": "box_1"}))
            refused = await batch.run_confirmed(
                app.state.session_factory,
                session,
                app.state.cipher,
                bot,
                origin,
                state.state["items"],
                relay,
            )
    assert refused == (1, "attachment_changed") and not again.called


async def batch_state(session: Any) -> Any:
    found = await session.scalar(
        select(InteractionState)
        .where(InteractionState.kind == batch.KIND)
        .execution_options(populate_existing=True)
    )
    assert found is not None
    return found


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
