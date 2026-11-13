"""Tower 的决定落到助理的审批单上：回调与对账共用，只从 pending 往前走。"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import keys
from coreman.core.assistants.kinds import APPROVAL_EXECUTE_KIND, APPROVAL_RESULT_KIND
from coreman.core.cloud_proxy import reconcile
from coreman.core.db.models import AssistantApproval
from tests.api.test_tower_callback import payload, post, secured  # noqa: F401
from tests.integration.approval_helpers import assistant_turn, install, pending, queued
from tests.integration.assistant_helpers import SECRET, raw

pytestmark = pytest.mark.usefixtures("secured")
REASON = "SELECT reason_enc FROM assistant_approvals WHERE id = :id"


async def waiting(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> AssistantApproval:
    install(monkeypatch)
    _, _, scope = await assistant_turn(session)
    return await pending(session, scope)


def body(row: AssistantApproval, status: str, **kw: Any) -> bytes:
    return payload(row.external_id, status, approval_id=row.approval_id, **kw)


async def reload(session: AsyncSession, row: AssistantApproval) -> AssistantApproval:
    found = await session.get(AssistantApproval, row.id, populate_existing=True)
    assert found is not None
    return found


async def test_approval_queues_the_execution_once(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = await waiting(db_session, monkeypatch)
    first = await post(client, body(row, "approved", reason="可以发"))
    assert first.status_code == 200 and first.json() == {"ok": True, "resumed": True}
    row = await reload(db_session, row)
    assert row.status == "approved" and row.decided_at is not None
    # 批准时填的话只是备注：留给助理看，执行内容不变。
    assert row.reason_enc == "可以发"
    tasks = await queued(db_session, APPROVAL_EXECUTE_KIND)
    assert len(tasks) == 1 and tasks[0].payload["approval_id"] == str(row.id)
    assert tasks[0].bot_id == row.bot_id and tasks[0].session_key is None
    # 回调没有调飞书，也没有直接开结果回合。
    assert await queued(db_session, APPROVAL_RESULT_KIND) == []
    # 同一个决定再来一次（窗口内重放、对账补送）：无事发生。
    again = await post(client, body(row, "approved"))
    assert again.status_code == 200
    assert await reconcile.apply_item(
        db_session,
        {"approval_id": row.approval_id, "external_id": row.external_id, "status": "approved"},
    )
    await db_session.commit()
    assert len(await queued(db_session, APPROVAL_EXECUTE_KIND)) == 1


@pytest.mark.parametrize("status", ["rejected", "expired"])
async def test_rejection_and_expiry_go_back_to_the_assistant(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    row = await waiting(db_session, monkeypatch)
    done = await post(client, body(row, status, reason=f"语气太硬 <b>{SECRET}</b>"))
    assert done.json() == {"ok": True, "resumed": True}
    row = await reload(db_session, row)
    assert row.status == status
    # 审批人的话：加密入库，尖括号已换掉。
    assert row.reason_enc == f"语气太硬 ‹b›{SECRET}‹/b›"
    assert SECRET not in str(await raw(db_session, REASON, id=row.id))
    assert await queued(db_session, APPROVAL_EXECUTE_KIND) == []
    turns = await queued(db_session, APPROVAL_RESULT_KIND)
    assert len(turns) == 1
    turn = turns[0]
    assert turn.payload["approval_id"] == str(row.id) and turn.payload["status"] == status
    assert turn.payload["serialize_session"] is True
    assert (turn.session_key, turn.inbound_event_id) == (row.session_key, row.origin_event_id)


async def test_reconcile_delivers_a_decision_the_callback_missed(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = await waiting(db_session, monkeypatch)
    item = {
        "approval_id": row.approval_id,
        "external_id": row.external_id,
        "status": "rejected",
        "reason": "先别发",
    }
    assert await reconcile.apply_item(db_session, item) is True
    await db_session.commit()
    assert (await reload(db_session, row)).status == "rejected"


async def test_decisions_on_a_closed_or_foreign_approval_do_nothing(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = await waiting(db_session, monkeypatch)
    # 编号对不上：不是这张单的决定。
    other = await post(client, payload(row.external_id, "approved", approval_id="apv-other"))
    assert other.json() == {"ignored": True}
    assert (await reload(db_session, row)).status == "pending"
    # 已经撤回的单之后被批准：不执行。
    row.status = "withdrawn"
    await db_session.commit()
    late = await post(client, body(row, "approved"))
    assert late.status_code == 200
    assert (await reload(db_session, row)).status == "withdrawn"
    assert await queued(db_session, APPROVAL_EXECUTE_KIND) == []
    # 根本不存在的外部编号。
    unknown = await post(client, payload("cm-pa-" + "0" * 32, "approved", approval_id="apv-x"))
    assert unknown.json() == {"ignored": True}


async def test_destroyed_key_or_disabled_assistant_never_executes(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    bot, owner, scope = await assistant_turn(db_session)
    locked = await pending(db_session, scope)
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    done = await post(client, body(locked, "approved"))
    # 不抛错、不重试：这张单按撤回收场。
    assert done.status_code == 200 and done.json() == {"ok": True, "resumed": False}
    locked = await reload(db_session, locked)
    assert (locked.status, locked.error_code) == ("withdrawn", "assistant_inactive")
    assert await queued(db_session, APPROVAL_EXECUTE_KIND) == []
    assert await queued(db_session, APPROVAL_RESULT_KIND) == []
