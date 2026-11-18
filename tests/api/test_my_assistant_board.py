"""总表与最近落地：待批的单只给摘要与链接，落地的单给本人解密后的摘要与理由；别人拿不到。"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from coreman.core.assistants import followups, keys, sealed
from coreman.core.config import reset_settings_cache
from coreman.core.db.models import AssistantApproval, Bot, User
from coreman.core.timeutils import utcnow
from tests.api.conftest import login_existing
from tests.api.my_assistant_helpers import URL, as_bot_token
from tests.integration.assistant_helpers import SECRET, raw
from tests.integration.job_helpers import owner_with_key, reloaded, spawned

pytestmark = pytest.mark.usefixtures("assistant_master")
BOARD, RECENT = f"{URL}/board", f"{URL}/approvals/recent"
ABSTRACT = f"私聊 → 李四 {SECRET}"
BODY_TEXT = "正文-不该出现在页面接口里-91c2"
# 「最近落地」列全部终态，新近有动静的在前（种子数据里的时刻就是这个顺序）。
FINAL = ["executed", "rejected", "withdrawn", "failed", "expired", "stale"]
STORED = "SELECT string_agg(args_enc::text, chr(32)) FROM assistant_approvals WHERE bot_id = :bot"


def _approval(bot: Bot, status: str, **over: Any) -> AssistantApproval:
    return AssistantApproval(
        bot_id=bot.id,
        tool="feishu_im_send_message",
        args_enc={"abstract": ABSTRACT, "calls": [{"text": BODY_TEXT}]},
        args_hash=uuid.uuid4().hex,
        request_hash=uuid.uuid4().hex,
        channel="p2p",
        external_id=f"cm-pa-{uuid.uuid4().hex}",
        expires_at=utcnow() + timedelta(hours=72),
        status=status,
        **over,
    )


async def _owner(client: httpx.AsyncClient, db_session: AsyncSession) -> tuple[Bot, User]:
    bot, owner = await owner_with_key(db_session)
    await db_session.commit()
    await login_existing(client, db_session, owner)
    return bot, owner


async def _seed(db_session: AsyncSession, bot: Bot) -> dict[str, AssistantApproval]:
    now = utcnow()
    rows = {
        "pending": _approval(bot, "pending"),
        "executed": _approval(bot, "executed", decided_at=now, executed_at=now),
        "rejected": _approval(
            bot, "rejected", decided_at=now - timedelta(hours=1), reason_enc=f"语气太硬 {SECRET}"
        ),
        "withdrawn": _approval(bot, "withdrawn", decided_at=now - timedelta(hours=2)),
        "failed": _approval(
            bot, "failed", decided_at=now - timedelta(hours=3), error_code="feishu_error"
        ),
        "expired": _approval(bot, "expired", decided_at=now - timedelta(hours=4)),
        "stale": _approval(bot, "stale", decided_at=now - timedelta(hours=5)),
        # 已批准、还没执行完的单不是终态：两个分区都不列。
        "approved": _approval(bot, "approved", decided_at=now),
    }
    db_session.add_all(rows.values())
    blocked = await spawned(db_session, bot, title="合同条款", brief="查一下违约条款")
    blocked = await reloaded(db_session, blocked)
    blocked.status, blocked.blocked_enc = "blocked", f"需要你确认范围 {SECRET}"
    await spawned(db_session, bot, title="还在跑的", brief="不该出现在总表里")
    await followups.add(
        db_session,
        bot.id,
        category="remind",
        title=f"续约 {SECRET}",
        due_on=date(2000, 1, 1),
        job_id=blocked.id,
    )
    await followups.add(
        db_session, bot.id, category="waiting", title="以后的事", due_on=date(2099, 1, 1)
    )
    await db_session.commit()
    return rows


async def test_board_lists_pending_approvals_linked_jobs_and_followups(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOWER_WEB_URL", "https://tower.example.com")
    reset_settings_cache()
    bot, owner = await _owner(client, db_session)
    rows = await _seed(db_session, bot)
    with capture_logs() as logs:
        response = await client.get(BOARD)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    data = response.json()["data"]
    assert data["approvals_total"] == 1
    [pending] = data["approvals"]
    assert pending == {
        "id": str(rows["pending"].id),
        "tool": "feishu_im_send_message",
        "summary": ABSTRACT,
        "created_at": pending["created_at"],
        "link": "https://tower.example.com",
    }
    # 过了期限的待跟进挂着被卡住的子任务：子任务在条目的 job 里，不再单独列出。
    [due] = [i for items in data["groups"].values() for i in items if i["id"] == "F001"]
    assert due["title"] == f"续约 {SECRET}" and "notes" not in due
    assert due["job"]["title"] == "合同条款" and due["job"]["status"] == "blocked"
    assert due["job"]["blocked_reason"] == f"需要你确认范围 {SECRET}"
    assert "jobs" not in data and "followups" not in data
    assert sum(data["counts"].values()) == 2
    assert data["counts"] == {name: len(items) for name, items in data["groups"].items()}
    # 暂存的参数（要发出的正文）不经这个接口给出：正文在审批系统里看。
    assert BODY_TEXT not in response.text
    assert SECRET not in (await raw(db_session, STORED, bot=bot.id))
    accesses = [entry for entry in logs if entry["event"] == "assistant_content_access"]
    assert sorted((entry["action"], entry["target"]) for entry in accesses) == [
        ("approvals.read", "pending"),
        ("followups.read", "board"),
        ("jobs.read", "board"),
    ]
    assert {entry["actor_id"] for entry in accesses} == {str(owner.id)}


async def test_board_groups_and_suggestion(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await owner_with_key(db_session)
    await followups.add(
        db_session, bot.id, category="decide", title="选供应商", notes="背景\n建议：选 A"
    )
    await followups.add(
        db_session,
        bot.id,
        category="waiting",
        title="等赵六",
        handler="person",
        handler_name="赵六",
    )
    await db_session.commit()
    await login_existing(client, db_session, owner)
    body = (await client.get(BOARD)).json()["data"]
    assert body["counts"]["owner"] == 1 and body["counts"]["others"] == 1
    (item,) = body["groups"]["owner"]
    assert item["id"] == "F001" and item["suggestion"] == "选 A" and "notes" not in item
    assert item["stage"] == "owner" and item["job"] is None
    assert body["groups"]["others"][0]["handler_name"] == "赵六"


async def test_without_a_configured_entry_there_is_no_link(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner_row = await _owner(client, db_session)
    await _seed(db_session, bot)
    [pending] = (await client.get(BOARD)).json()["data"]["approvals"]
    assert pending["link"] is None and pending["summary"] == ABSTRACT


async def test_recent_lists_every_final_state_newest_first(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner_row = await _owner(client, db_session)
    rows = await _seed(db_session, bot)
    response = await client.get(RECENT)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    items = response.json()["data"]["items"]
    assert [item["status"] for item in items] == FINAL
    assert [item["id"] for item in items] == [str(rows[status].id) for status in FINAL]
    assert items[3]["error"] == "feishu_error"
    assert items[0]["summary"] == ABSTRACT and items[0]["locked"] is False
    assert items[0]["executed_at"] is not None and items[0]["channel"] == "p2p"
    assert items[1]["reason"] == f"语气太硬 {SECRET}"
    assert BODY_TEXT not in response.text


async def test_an_admin_with_their_own_assistant_gets_nothing_of_the_owners(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, owner = await owner_with_key(db_session)
    await _seed(db_session, bot)
    _theirs, admin = await owner_with_key(
        db_session, chat_id="oc_admin", platform_user_id="admin_pid", open_id="ou_admin"
    )
    admin.role = "platform_admin"
    await db_session.commit()
    await login_existing(client, db_session, admin)
    board_resp = await client.get(BOARD)
    assert board_resp.json()["data"]["approvals"] == []
    assert sum(board_resp.json()["data"]["counts"].values()) == 0
    recent = await client.get(RECENT)
    assert recent.json()["data"]["items"] == []
    assert SECRET not in board_resp.text + recent.text
    as_bot_token(client, monkeypatch, owner)
    assert (await client.get(BOARD)).status_code == 403
    assert (await client.get(RECENT)).status_code == 403


async def test_after_the_key_is_destroyed_only_metadata_is_left(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await _owner(client, db_session)
    await _seed(db_session, bot)
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    sealed.forget()
    board_resp = await client.get(BOARD)
    recent = await client.get(RECENT)
    assert board_resp.status_code == 200 and recent.status_code == 200
    assert SECRET not in board_resp.text + recent.text
    [pending] = board_resp.json()["data"]["approvals"]
    assert pending["summary"] is None and pending["tool"] == "feishu_im_send_message"
    items = recent.json()["data"]["items"]
    assert [item["status"] for item in items] == FINAL
    assert all(item["locked"] is True and item["summary"] is None for item in items)
    assert items[1]["reason"] is None


async def test_a_dispatch_waiting_for_approval_shows_among_the_pending(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """等待审批的调度没有子任务行：它只在待批的审批单里，靠工具名认出来。"""
    bot, _owner_row = await _owner(client, db_session)
    row = _approval(bot, "pending")
    row.tool, row.channel = "assistant_ask_bot", "other"
    row.args_enc = {
        "abstract": "调度员工 → demo-bot",
        "calls": [{"arguments": {"question": BODY_TEXT}}],
    }
    db_session.add(row)
    await db_session.commit()
    response = await client.get(BOARD)
    [pending] = response.json()["data"]["approvals"]
    assert pending["tool"] == "assistant_ask_bot" and pending["summary"] == "调度员工 → demo-bot"
    assert BODY_TEXT not in response.text
