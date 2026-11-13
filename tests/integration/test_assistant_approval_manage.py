"""改稿、撤回、列表：只动 pending 的单；改稿沿用外部编号；以及这些工具在 MCP 上的样子。"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import tools as assistant_tools
from coreman.core.assistants.approvals import execute
from coreman.core.assistants.kinds import APPROVAL_EXECUTE_KIND
from coreman.core.crypto import Cipher
from coreman.core.db.models import AssistantApproval
from coreman.core.db.session import make_session_factory
from coreman.core.feishu_personal.policy import Scope
from tests.integration.approval_helpers import (
    CFG,
    assistant_turn,
    decided,
    install,
    install_files,
    pending,
    queued,
    reply,
)
from tests.integration.assistant_helpers import SECRET, raw
from tests.integration.worker_helpers import MASTER

pytestmark = pytest.mark.usefixtures("assistant_master")
REVISED = {**reply(), "text": "周四给你，辛苦再等一下", "uuid": "u9"}


async def call(session: AsyncSession, scope: Scope, name: str, arguments: dict[str, Any]) -> Any:
    ctx = assistant_tools.Context(session, None, scope, None, CFG)  # type: ignore[arg-type]
    out = await assistant_tools.dispatch(ctx, name, arguments, groups=assistant_tools.GROUPS)
    await session.commit()
    return out


async def reload(session: AsyncSession, row: AssistantApproval) -> AssistantApproval:
    found = await session.get(AssistantApproval, row.id, populate_existing=True)
    assert found is not None
    return found


def test_own_tools_are_listed_for_assistants() -> None:
    listed = assistant_tools.definitions([], groups=assistant_tools.GROUPS)
    assert [item["name"] for item in listed] == [
        "notes_index",
        "notes_read",
        "notes_search",
        "notes_write",
        "notes_delete",
        "followup_add",
        "followup_update",
        "followup_snooze",
        "followup_close",
        "followup_list",
        "assistant_settings_get",
        "assistant_settings_update",
        "approval_revise",
        "approval_withdraw",
        "approval_list",
        "assistant_mark_processing",
        "assistant_spawn",
        "assistant_job_message",
        "assistant_job_list",
        "assistant_job_cancel",
    ]
    for item in listed:
        assert item["inputSchema"]["type"] == "object" and item["description"]


async def test_revise_replaces_the_pending_approval_in_place(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    feishu, fake = install(monkeypatch)
    _, _, scope = await assistant_turn(db_session)
    row = await pending(db_session, scope)
    before = (row.args_hash, row.request_hash, row.external_id, row.approval_id)
    row.notified_at = row.created_at
    assert row.revised_at is None
    await db_session.commit()
    out = await call(
        db_session, scope, "approval_revise", {"approval_id": str(row.id), "arguments": REVISED}
    )
    assert out == {"status": "pending_approval", "approval_id": str(row.id), "revised": True}
    row = await reload(db_session, row)
    # 记下改稿的时刻：平台判断「会话在这张单之后动没动过」从这一刻算起。
    assert row.revised_at is not None and row.revised_at >= row.created_at
    # 同一张单、同一个外部编号；内容、去重键、请求哈希都换成新的；提醒重新来过。
    assert row.status == "pending" and row.external_id == before[2]
    assert row.args_enc is not None
    assert row.args_enc["calls"][0]["arguments"]["text"] == "周四给你，辛苦再等一下"
    assert (row.args_hash, row.request_hash) != before[:2] and row.notified_at is None
    assert [item["external_id"] for item in fake.submitted] == [before[2], before[2]]
    assert "周四给你，辛苦再等一下" in fake.submitted[1]["content"]
    # 批准之后执行的是改过的那一份。
    await decided(db_session, row)
    task = (await queued(db_session, APPROVAL_EXECUTE_KIND))[0]
    status = await execute.run(make_session_factory(db_engine), Cipher(MASTER), row.id, task, CFG)
    assert status == "executed" and "周四给你" in feishu.writes[0][2]["content"]


async def test_revise_is_refused_when_it_cannot_apply(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, fake = install(monkeypatch)
    _, _, scope = await assistant_turn(db_session)
    _, _, stranger = await assistant_turn(db_session, "lisi")
    row = await pending(db_session, scope)
    other = await pending(db_session, scope, {**reply(), "text": "另一件事"})
    row_id = row.id
    revise = {"approval_id": str(row_id), "arguments": REVISED}
    # 改成与另一张在途的单一样的内容。
    same = {"approval_id": str(row.id), "arguments": {**reply(), "text": "另一件事"}}
    assert await call(db_session, scope, "approval_revise", same) == {
        "error": "duplicate",
        "approval_id": str(other.id),
    }
    # 新参数过不了检查：原单不动。
    bad = {"approval_id": str(row.id), "arguments": {"message_id": "om_2"}}
    assert (await call(db_session, scope, "approval_revise", bad))["error"] == (
        "invalid_tool_or_arguments"
    )
    # 审批系统不可用：原单不动。
    fake.down = True
    assert (await call(db_session, scope, "approval_revise", revise))["error"] == (
        "approval_service_unavailable"
    )
    fake.down = False
    # 提交失败时回滚到改稿之前：重新取出来的还是原来那份。
    row = await db_session.get(AssistantApproval, row_id, populate_existing=True)
    assert row is not None and row.status == "pending" and row.args_enc is not None
    assert SECRET in row.args_enc["calls"][0]["arguments"]["text"]
    # 不是 pending 的单、不存在的编号、别人的单。
    await decided(db_session, row, "rejected")
    assert await call(db_session, scope, "approval_revise", revise) == {
        "error": "not_pending",
        "status": "rejected",
    }
    for missing in ("not-a-uuid", "00000000-0000-4000-8000-000000000000"):
        gone = await call(
            db_session, scope, "approval_revise", {"approval_id": missing, "arguments": REVISED}
        )
        assert gone == {"error": "approval_not_found"}
    theirs = await call(db_session, stranger, "approval_withdraw", {"approval_id": str(other.id)})
    assert theirs == {"error": "approval_not_found"}


async def test_withdraw_voids_the_approval_even_if_it_is_approved_later(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, fake = install(monkeypatch)
    _, _, scope = await assistant_turn(db_session)
    row = await pending(db_session, scope)
    out = await call(
        db_session,
        scope,
        "approval_withdraw",
        {"approval_id": str(row.id), "reason": f"本人已经自己回了 {SECRET}"},
    )
    assert out == {"status": "withdrawn", "approval_id": str(row.id)}
    row = await reload(db_session, row)
    assert row.status == "withdrawn" and row.decided_at is not None
    # 发给审批系统的只有固定代码；助理写的原因加密留在本地。
    assert fake.withdrawn == [row.external_id]
    stored = await raw(
        db_session, "SELECT reason_enc FROM assistant_approvals WHERE id = :id", id=row.id
    )
    assert SECRET not in str(stored)
    # 撤回之后迟到的批准：不执行。
    await decided(db_session, row)
    assert (await reload(db_session, row)).status == "withdrawn"
    assert await queued(db_session, APPROVAL_EXECUTE_KIND) == []
    assert await call(db_session, scope, "approval_withdraw", {"approval_id": str(row.id)}) == {
        "error": "not_pending",
        "status": "withdrawn",
    }


async def test_withdraw_holds_locally_when_tower_is_down(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, fake = install(monkeypatch)
    _, _, scope = await assistant_turn(db_session)
    row = await pending(db_session, scope)
    fake.down = True
    out = await call(db_session, scope, "approval_withdraw", {"approval_id": str(row.id)})
    assert out["status"] == "withdrawn" and "warning" in out
    assert (await reload(db_session, row)).status == "withdrawn"


async def test_list_shows_only_this_owners_approvals(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    _, _, scope = await assistant_turn(db_session)
    _, _, stranger = await assistant_turn(db_session, "lisi")
    first = await pending(db_session, scope)
    second = await pending(db_session, scope, {**reply(topic="周会"), "text": "周会改到周三"})
    await decided(db_session, first, "rejected", reason="先别回")
    await pending(db_session, stranger)
    everything = await call(db_session, scope, "approval_list", {})
    assert [item["approval_id"] for item in everything["items"]] == [str(second.id), str(first.id)]
    assert everything["items"][0]["summary"] == "私聊 → 李四"
    rejected = await call(db_session, scope, "approval_list", {"status": "rejected"})
    assert [(i["approval_id"], i["reason"]) for i in rejected["items"]] == [
        (str(first.id), "先别回")
    ]
    by_topic = await call(db_session, scope, "approval_list", {"topic": "周会"})
    assert [item["approval_id"] for item in by_topic["items"]] == [str(second.id)]
    bad = await call(db_session, scope, "approval_list", {"status": "done"})
    assert bad["error"] == "invalid_tool_or_arguments"


async def test_revise_swaps_the_files_in_the_bucket(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, fake = install(monkeypatch)
    relay, bucket = install_files(monkeypatch)
    _, _, scope = await assistant_turn(db_session)

    async def tool(name: str, arguments: dict[str, Any]) -> Any:
        ctx = assistant_tools.Context(db_session, None, scope, relay, CFG)  # type: ignore[arg-type]
        out = await assistant_tools.dispatch(ctx, name, arguments, groups=assistant_tools.GROUPS)
        await db_session.commit()
        return out

    first = {"upload_id": relay.add("v1.pdf", b"draft one"), "folder_token": "fld_1"}
    out = await tool("feishu_save_to_drive", first)
    (old_key,) = bucket.objects
    second = {"upload_id": relay.add("v2.pdf", b"draft two"), "folder_token": "fld_1"}
    revised = await tool(
        "approval_revise", {"approval_id": out["approval_id"], "arguments": second}
    )
    assert revised["revised"] is True
    # 旧稿的附件不会再被执行：桶里只剩新的那一份，审批单上的链接也换了。
    (new_key,) = bucket.objects
    assert new_key != old_key and bucket.objects[new_key] == b"draft two"
    assert bucket.deleted == [old_key]
    assert new_key in fake.submitted[1]["content"] and old_key not in fake.submitted[1]["content"]
    # 审批系统没收下改稿：新复制的对象删掉，旧的还在，单子保持原样。
    fake.down = True
    third = {"upload_id": relay.add("v3.pdf", b"draft three"), "folder_token": "fld_1"}
    failed = await tool("approval_revise", {"approval_id": out["approval_id"], "arguments": third})
    assert failed["error"] == "approval_service_unavailable" and list(bucket.objects) == [new_key]
    row = await db_session.get(AssistantApproval, out["approval_id"], populate_existing=True)
    assert row is not None and row.has_files
    assert row.args_enc["calls"][0]["arguments"]["upload_id"]["key"] == new_key  # type: ignore[index]
