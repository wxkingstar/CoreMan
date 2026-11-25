"""AI 员工的任务工具：列出派给自己的、领取开工（以代表人身份排一轮）、读任务。"""

from __future__ import annotations

import json

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import tasks
from coreman.core.db.models import Task, User, UserIdentity, UserReached, WorkTask
from coreman.core.work_tasks import service, tools
from tests.integration.worker_helpers import seed_bot

URL = tools.API_PATH


async def _setup(db_session: AsyncSession):
    bot, _, _ = await seed_bot(db_session)
    bot.platform = "feishu"
    alice = User(login_name="alice", display_name="alice", email="alice@example.test")
    db_session.add(alice)
    await db_session.flush()
    turn = await tasks.enqueue(
        db_session,
        tasks.NewTask(bot_id=bot.id, kind="chat", user_id=alice.id, session_key="oc_dm"),
    )
    turn.status = "running"
    first = await service.create(
        db_session,
        alice,
        title="查上周退货率",
        brief="口径按退货单数除以签收单数",
        assignee=service.Assignee("bot", bot.id),
    )
    second = await service.create(
        db_session,
        alice,
        title="整理退款原因",
        priority="urgent",
        assignee=service.Assignee("bot", bot.id),
    )
    await db_session.commit()
    return bot, alice, turn, first, second


def _call(name: str, arguments: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


async def test_agent_lists_and_claims_tasks_as_the_principal(
    client: httpx.AsyncClient, app, db_session: AsyncSession
) -> None:
    bot, alice, turn, first, second = await _setup(db_session)
    token = tools.issue_capability(
        app.state.cipher, task_id=turn.id, bot_id=bot.id, actor_id=alice.id, kind="chat"
    )
    auth = {"Authorization": f"Bearer {token}"}
    assert (await client.post(URL, json=_call("list_my_tasks", {}))).status_code == 401
    listed = await client.post(
        URL, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=auth
    )
    assert {t["name"] for t in listed.json()["result"]["tools"]} == {
        "list_my_tasks",
        "start_task",
        "search_tasks",
        "get_task",
    }
    response = await client.post(URL, json=_call("list_my_tasks", {}), headers=auth)
    items = json.loads(response.json()["result"]["content"][0]["text"])["tasks"]
    # 紧急的排在前面。
    assert [i["code"] for i in items] == [second.code, first.code]
    assert items[0]["from"] == "alice" and items[0]["waiting_to_be_claimed"] is True

    # 代表人没私聊过这位员工：没处回报，领取失败并说明原因。
    db_session.add(UserIdentity(user_id=alice.id, platform="feishu", platform_user_id="ou_a"))
    await db_session.commit()
    response = await client.post(
        URL, json=_call("start_task", {"codes": [first.code]}), headers=auth
    )
    result = json.loads(response.json()["result"]["content"][0]["text"])["results"][0]
    assert result["started"] is False and "私聊" in result["reason"]

    db_session.add(UserReached(bot_id=bot.id, user_id=alice.id, platform_chat_id="oc_dm"))
    await db_session.commit()
    response = await client.post(
        URL, json=_call("start_task", {"codes": [first.code, "T-999999"]}), headers=auth
    )
    results = json.loads(response.json()["result"]["content"][0]["text"])["results"]
    assert [r["started"] for r in results] == [True, False]
    queued = await db_session.scalar(select(Task).where(Task.session_key == f"work:{first.code}"))
    assert queued.user_id == alice.id and queued.payload["work_trigger"] == "chat"
    assert queued.payload["work_triggered_by"]["name"] == "alice"
    row = await db_session.scalar(
        select(WorkTask)
        .where(WorkTask.code == first.code)
        .execution_options(populate_existing=True)
    )
    assert row.status == "waiting" and row.claimed_at is not None and row.chat_id == "oc_dm"
    # 已经领了：再领就说不用再领。
    response = await client.post(
        URL, json=_call("start_task", {"codes": [first.code]}), headers=auth
    )
    result = json.loads(response.json()["result"]["content"][0]["text"])["results"][0]
    assert result["started"] is False

    response = await client.post(URL, json=_call("get_task", {"code": first.code}), headers=auth)
    task = json.loads(response.json()["result"]["content"][0]["text"])
    assert task["for_agents"]["brief"] == "口径按退货单数除以签收单数"
    assert any("叫销售开工" in line for line in task["for_people"]["timeline"])


async def test_capability_dies_with_the_turn(
    client: httpx.AsyncClient, app, db_session: AsyncSession
) -> None:
    bot, alice, turn, _, _ = await _setup(db_session)
    token = tools.issue_capability(
        app.state.cipher, task_id=turn.id, bot_id=bot.id, actor_id=alice.id, kind="chat"
    )
    turn.status = "succeeded"
    await db_session.commit()
    response = await client.post(
        URL, json=_call("list_my_tasks", {}), headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403


async def test_only_people_who_can_manage_the_task_can_start_it_and_reads_stay_own(
    client: httpx.AsyncClient, app, db_session: AsyncSession
) -> None:
    bot, alice, turn, first, _ = await _setup(db_session)
    carol = User(login_name="carol", display_name="carol", email="carol@example.test")
    db_session.add(carol)
    db_session.add(UserIdentity(user_id=alice.id, platform="feishu", platform_user_id="ou_a"))
    db_session.add(UserReached(bot_id=bot.id, user_id=alice.id, platform_chat_id="oc_dm"))
    await db_session.commit()
    token = tools.issue_capability(
        app.state.cipher, task_id=turn.id, bot_id=bot.id, actor_id=carol.id, kind="chat"
    )
    auth = {"Authorization": f"Bearer {token}"}
    response = await client.post(
        URL, json=_call("start_task", {"codes": [first.code]}), headers=auth
    )
    result = json.loads(response.json()["result"]["content"][0]["text"])["results"][0]
    assert result["started"] is False and "代表人" in result["reason"]

    # 卡住了停下来等代表人：代表人叫它接着做，带上补充的话。
    token = tools.issue_capability(
        app.state.cipher, task_id=turn.id, bot_id=bot.id, actor_id=alice.id, kind="chat"
    )
    auth = {"Authorization": f"Bearer {token}"}
    row = await db_session.scalar(select(WorkTask).where(WorkTask.code == first.code))
    row.status, row.claimed_at = "waiting", row.created_at
    row.waiting_on = {"kind": "human", "user_id": str(alice.id), "human_text": "卡住了：没权限"}
    await db_session.commit()
    listed = await client.post(URL, json=_call("list_my_tasks", {}), headers=auth)
    items = json.loads(listed.json()["result"]["content"][0]["text"])["tasks"]
    assert any(i["code"] == first.code and i["stopped_waiting_for_principal"] for i in items)
    response = await client.post(
        URL,
        json=_call("start_task", {"codes": [first.code], "note": "权限开好了"}),
        headers=auth,
    )
    result = json.loads(response.json()["result"]["content"][0]["text"])["results"][0]
    assert result["started"] is True
    queued = await db_session.scalar(select(Task).where(Task.session_key == f"work:{first.code}"))
    assert queued.payload["work_note"] == "权限开好了"


async def test_get_task_only_reads_the_agents_own_tasks(
    client: httpx.AsyncClient, app, db_session: AsyncSession
) -> None:
    bot, alice, turn, _, _ = await _setup(db_session)
    mine = await service.create(db_session, alice, title="别人的事")
    await db_session.commit()
    token = tools.issue_capability(
        app.state.cipher, task_id=turn.id, bot_id=bot.id, actor_id=alice.id, kind="chat"
    )
    response = await client.post(
        URL,
        json=_call("get_task", {"code": mine.code}),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert json.loads(response.json()["result"]["content"][0]["text"])["error"] == "not_found"


async def test_tools_refuse_a_cancelled_task(
    client: httpx.AsyncClient, app, db_session: AsyncSession
) -> None:
    bot, alice, _, first, _ = await _setup(db_session)
    work_turn = await tasks.enqueue(
        db_session,
        tasks.NewTask(
            bot_id=bot.id, kind="chat", user_id=alice.id, session_key=f"work:{first.code}"
        ),
    )
    work_turn.status = "running"
    row = await db_session.scalar(select(WorkTask).where(WorkTask.code == first.code))
    row.status = "cancelled"
    await db_session.commit()
    token = tools.issue_capability(
        app.state.cipher, task_id=work_turn.id, bot_id=bot.id, actor_id=alice.id, kind="chat"
    )
    response = await client.post(
        URL, json=_call("wait_for", {}), headers={"Authorization": f"Bearer {token}"}
    )
    assert json.loads(response.json()["result"]["content"][0]["text"])["error"] == "not_in_task"
