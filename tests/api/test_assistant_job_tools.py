"""个人工具 MCP 上的子任务工具：主会话能派、能问、能看、能取消；子任务轮次只读加知识库。"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from sqlalchemy import func, select, update

from coreman.core import personal_schedules as schedules
from coreman.core.assistants import jobs, notes
from coreman.core.assistants.approvals import catalog
from coreman.core.bus import tasks
from coreman.core.chat import sessions
from coreman.core.db.models import AssistantApproval, AssistantJob, FeishuPersonalGrant, Task
from coreman.core.feishu_personal import policy
from coreman.core.feishu_personal import tools as personal_tools
from tests.api.test_assistant_approval_gate import listed, owner_turn
from tests.api.test_assistant_tool_groups import call
from tests.api.test_feishu_personal import URL, headers, rpc
from tests.integration.assistant_helpers import SECRET, identified, raw
from tests.integration.test_cron_handler import claim

INVALID = "invalid_tool_or_arguments"
JOB_TOOLS = [
    "assistant_spawn",
    "assistant_job_message",
    "assistant_job_list",
    "assistant_job_cancel",
]
SPAWN = {"title": "查李四的报价", "brief": f"翻一下上周和李四的聊天。{SECRET}"}


async def main_turn(session: Any, app: Any) -> tuple[Any, Any, Any]:
    """所有者私聊的一轮（在跑），助理记下了身份。"""
    bot, user, task = await owner_turn(session, app, assistant=True)
    # 与 owner_turn 造出的通讯录身份、授权一致。
    await identified(
        session, bot, open_id="ou_human", tenant_key="tenant-test", platform_user_id="human"
    )
    return bot, user, task


async def subtask_turn(session: Any, app: Any) -> tuple[Any, Any, AssistantJob, Any]:
    """主会话派出的子任务的第一轮（已认领）；派出它的那一轮已经结束。"""
    bot, user, task = await main_turn(session, app)
    row = await jobs.spawn(session, bot, **SPAWN)
    await tasks.finish(session, task.id, status="succeeded")
    await session.commit()
    turn = await claim(session)
    assert turn.id == row.last_task_id
    return bot, user, row, turn


async def count(session: Any, model: Any) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def test_the_main_session_lists_the_job_tools(
    client: httpx.AsyncClient, app: Any, db_session: Any
) -> None:
    _, user, task = await main_turn(db_session, app)
    listing = await listed(client, app, task, user)
    assert set(JOB_TOOLS) <= set(listing) and "assistant_job_blocked" not in listing
    assert listing["assistant_spawn"]["inputSchema"]["required"] == ["title", "brief"]
    assert listing["assistant_job_message"]["inputSchema"]["required"] == ["id", "text"]
    assert "required" not in listing["assistant_job_list"]["inputSchema"]


async def test_spawn_list_message_and_cancel_from_the_main_session(
    client: httpx.AsyncClient, app: Any, db_session: Any
) -> None:
    _, user, task = await main_turn(db_session, app)
    auth = await headers(app, task, user)
    out = await call(client, auth, "assistant_spawn", SPAWN)
    job = out["job"]
    assert (job["status"], job["title"], job["rounds"]) == ("queued", "查李四的报价", 1)
    # 库里是密文。
    stored = str(
        await raw(
            db_session,
            "SELECT title_enc || brief_enc FROM assistant_jobs WHERE id = :id",
            id=uuid.UUID(job["id"]),
        )
    )
    assert SECRET not in stored and "李四" not in stored
    listing = await call(client, auth, "assistant_job_list", {})
    assert [item["id"] for item in listing["jobs"]] == [job["id"]]
    assert "brief" not in listing["jobs"][0]
    assert listing["limits"] == {"active": 3, "per_day": 100, "rounds": 12}
    one = await call(client, auth, "assistant_job_list", {"id": job["id"]})
    assert one["job"]["brief"].endswith(SECRET) and one["job"]["result"] is None
    # 还在跑的不能追问。
    busy = await call(client, auth, "assistant_job_message", {"id": job["id"], "text": "快点"})
    assert busy["error"] == "job_busy" and "hint" in busy
    # 同时最多 3 个。
    for _ in range(2):
        assert "job" in await call(client, auth, "assistant_spawn", SPAWN)
    full = await call(client, auth, "assistant_spawn", SPAWN)
    assert full["error"] == "too_many_jobs" and full["limit"] == 3
    assert await count(db_session, AssistantJob) == 3
    gone = await call(client, auth, "assistant_job_cancel", {"id": job["id"]})
    assert gone["job"]["status"] == "cancelled"
    assert gone["job"]["error_code"] == "cancelled_by_assistant"
    missing = await call(client, auth, "assistant_job_cancel", {"id": str(uuid.uuid4())})
    assert missing["error"] == "job_not_found"
    bad = await call(client, auth, "assistant_spawn", {"title": "", "brief": "x"})
    assert bad["error"] == INVALID and bad["invalid"]


async def test_job_tools_survive_a_changed_authorization(
    client: httpx.AsyncClient, app: Any, db_session: Any
) -> None:
    """子任务的数据在平台自己的库里、不调飞书：授权换代之后同一轮里照常可用。"""
    bot, user, task = await main_turn(db_session, app)
    auth = await headers(app, task, user)
    grant = await db_session.get(FeishuPersonalGrant, (bot.id, user.id), populate_existing=True)
    grant.context_epoch = uuid.uuid4()
    await db_session.commit()
    assert "job" in await call(client, auth, "assistant_spawn", SPAWN)
    assert len((await call(client, auth, "assistant_job_list", {}))["jobs"]) == 1
    assert await call(client, auth, "approval_list", {}) == {"error": "authorization_changed"}


async def test_a_subtask_turn_lists_only_reads_and_knowledge(
    client: httpx.AsyncClient, app: Any, db_session: Any
) -> None:
    _, user, _, turn = await subtask_turn(db_session, app)
    listing = await listed(client, app, turn, user)
    names = set(listing)
    assert {
        "notes_read",
        "notes_write",
        "followup_add",
        "followup_list",
        "feishu_chat_history",
        "feishu_authorization_status",
        "assistant_job_blocked",
    } <= names
    assert not names & set(JOB_TOOLS)
    assert not [name for name in names if catalog.is_write(name)]
    assert not [name for name in names if name.startswith("approval_")]
    assert "assistant_mark_processing" not in names
    assert not names & {"feishu_authorize", "feishu_revoke_authorization"}
    assert not names & set(schedules.TOOLS)


async def test_a_subtask_turn_cannot_call_what_it_does_not_list(
    client: httpx.AsyncClient, app: Any, db_session: Any
) -> None:
    _, user, _, turn = await subtask_turn(db_session, app)
    auth = await headers(app, turn, user)
    writes = sorted(name for name in personal_tools.TOOLS if catalog.is_write(name))
    assert "feishu_reply_message" in writes
    refused = [
        *JOB_TOOLS,
        "approval_list",
        "approval_revise",
        "approval_withdraw",
        "assistant_mark_processing",
        *writes,
    ]
    for name in refused:
        out = await call(client, auth, name, {})
        assert out["error"] == INVALID, name
    # 授权的发起与撤销、本人定时任务：无人在场的轮次一律不受理。
    for name in ("feishu_authorize", "feishu_revoke_authorization", *sorted(schedules.TOOLS)):
        assert (await call(client, auth, name, {}))["error"] == "owner_turn_required", name
    # 什么都没多出来：没有新的子任务，没有审批单。
    assert await count(db_session, AssistantJob) == 1
    assert await count(db_session, AssistantApproval) == 0


async def test_a_subtask_cannot_touch_pinned_pages(
    client: httpx.AsyncClient, app: Any, db_session: Any
) -> None:
    _, user, _, turn = await subtask_turn(db_session, app)
    auth = await headers(app, turn, user)
    pinned = await call(
        client,
        auth,
        "notes_write",
        {"path": "rules/tone", "body": "以后都这样回", "summary": "口吻", "pinned": True},
    )
    assert pinned["error"] == notes.PIN_REQUIRES_OWNER
    plain = await call(
        client,
        auth,
        "notes_write",
        {"path": "people/li", "body": "李四报价 3 万", "summary": "报价"},
    )
    assert "error" not in plain


async def test_the_blocked_tool_belongs_to_the_subtask_turn(
    client: httpx.AsyncClient, app: Any, db_session: Any
) -> None:
    _, user, row, turn = await subtask_turn(db_session, app)
    auth = await headers(app, turn, user)
    out = await call(
        client, auth, "assistant_job_blocked", {"reason": f"要所有者确认预算。{SECRET}"}
    )
    assert out == {"ok": True}
    stored = str(
        await raw(db_session, "SELECT blocked_enc FROM assistant_jobs WHERE id = :id", id=row.id)
    )
    assert stored.startswith("enc:v2:") and SECRET not in stored
    empty = await call(client, auth, "assistant_job_blocked", {"reason": ""})
    assert empty["error"] == INVALID


async def test_the_main_session_cannot_call_the_blocked_tool(
    client: httpx.AsyncClient, app: Any, db_session: Any
) -> None:
    _, user, task = await main_turn(db_session, app)
    auth = await headers(app, task, user)
    assert (await call(client, auth, "assistant_job_blocked", {"reason": "x"}))["error"] == INVALID


async def test_a_subtask_token_is_fenced_to_the_subtask_session(
    client: httpx.AsyncClient, app: Any, db_session: Any
) -> None:
    """子任务轮次的令牌围的是子任务会话：拿主会话的会话编号签不出能用的令牌。"""
    bot, user, row, turn = await subtask_turn(db_session, app)
    good = await headers(app, turn, user)
    assert (await client.post(URL, headers=good, json=rpc("notes_index"))).status_code == 200
    async with app.state.session_factory() as session:
        main = await sessions.get_or_create(
            session,
            bot_id=bot.id,
            session_key="oc_private",
            backend="claude",
            ttl_hours=72,
            speaker_user_id=user.id,
        )
        await session.commit()
    grant = await db_session.get(FeishuPersonalGrant, (bot.id, user.id), populate_existing=True)
    crossed = {
        **good,
        "Authorization": "Bearer "
        + policy.issue_capability(
            app.state.cipher,
            task_id=turn.id,
            user_id=str(user.id),
            context_epoch=grant.context_epoch,
            base_session_id=main.relay_session_id,
            ttl_seconds=1800,
        ),
    }
    response = await client.post(URL, headers=crossed, json=rpc("notes_index"))
    assert response.status_code == 403 and "conversation changed" in response.text
    # 会话键不是这位助理的私聊派生出来的任务，拿不到任何工具。
    await db_session.execute(
        update(Task).where(Task.id == turn.id).values(session_key=f"oc_someone#job:{row.id}")
    )
    await db_session.commit()
    assert (await client.post(URL, headers=good, json=rpc("notes_index"))).status_code == 403
