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

    # 读任务回的是任务页 ?format=md 那份纯文本，不是 JSON。
    response = await client.post(URL, json=_call("get_task", {"code": first.code}), headers=auth)
    text = response.json()["result"]["content"][0]["text"]
    assert response.json()["result"]["isError"] is False
    assert text.startswith(f"# {first.code} 查上周退货率\n")
    assert "## 交给 AI 员工的背景\n\n口径按退货单数除以签收单数" in text
    assert "叫销售开工" in text


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


async def test_whoever_starts_or_restarts_a_task_drives_it(
    client: httpx.AsyncClient, app, db_session: AsyncSession
) -> None:
    bot, alice, turn, first, second = await _setup(db_session)
    carol = User(login_name="carol", display_name="carol", email="carol@example.test")
    dave = User(login_name="dave", display_name="dave", email="dave@example.test")
    db_session.add_all([carol, dave])
    db_session.add(UserIdentity(user_id=alice.id, platform="feishu", platform_user_id="ou_a"))
    db_session.add(UserReached(bot_id=bot.id, user_id=alice.id, platform_chat_id="oc_dm"))
    await db_session.commit()

    async def start(actor, code):
        token = tools.issue_capability(
            app.state.cipher, task_id=turn.id, bot_id=bot.id, actor_id=actor.id, kind="chat"
        )
        response = await client.post(
            URL,
            json=_call("start_task", {"codes": [code]}),
            headers={"Authorization": f"Bearer {token}"},
        )
        return json.loads(response.json()["result"]["content"][0]["text"])["results"][0]

    async def reload():
        return await db_session.scalar(
            select(WorkTask)
            .where(WorkTask.code == second.code)
            .execution_options(populate_existing=True)
        )

    # 指派给这位员工的任务谁都能叫开工，以叫开工的人的身份做：carol 没有飞书身份，开不了工，
    # 代表人也不变。
    result = await start(carol, second.code)
    assert result["started"] is False and "carol没有飞书身份" in result["reason"]
    row = await reload()
    assert row.principal_user_id == alice.id and row.claimed_at is None

    # 有了飞书身份、私聊过它：开工，carol 成为代表人。
    db_session.add(UserIdentity(user_id=carol.id, platform="feishu", platform_user_id="ou_c"))
    db_session.add(UserReached(bot_id=bot.id, user_id=carol.id, platform_chat_id="oc_dm_c"))
    await db_session.commit()
    assert (await start(carol, second.code))["started"] is True
    row = await reload()
    assert row.principal_user_id == carol.id and row.chat_id == "oc_dm_c"
    queued = await db_session.scalar(select(Task).where(Task.session_key == f"work:{second.code}"))
    assert queued.user_id == carol.id

    # 做到一半停下来的：谁叫它接着做就换成谁的身份；dave 没有飞书身份，开不了工，代表人不变。
    row.status, row.waiting_on = "waiting", {"kind": "human", "human_text": "卡住了"}
    await db_session.commit()
    result = await start(dave, second.code)
    assert result["started"] is False and "dave没有飞书身份" in result["reason"]
    assert (await reload()).principal_user_id == carol.id

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


async def test_get_task_reads_any_task_when_a_person_asks(
    client: httpx.AsyncClient, app, db_session: AsyncSession
) -> None:
    """有人把任务链接甩给它：成员在任务页上看得到所有任务，它也能读；没人叫它时只读自己的。"""
    bot, alice, turn, _, _ = await _setup(db_session)
    other = await service.create(db_session, alice, title="别人的事")
    await db_session.commit()

    async def read(actor_id):
        token = tools.issue_capability(
            app.state.cipher, task_id=turn.id, bot_id=bot.id, actor_id=actor_id, kind="chat"
        )
        response = await client.post(
            URL,
            json=_call("get_task", {"code": other.code}),
            headers={"Authorization": f"Bearer {token}"},
        )
        return response.json()["result"]

    asked = await read(alice.id)
    assert asked["isError"] is False
    assert asked["content"][0]["text"].startswith(f"# {other.code} 别人的事")
    alone = await read(None)
    assert alone["isError"] is True
    assert json.loads(alone["content"][0]["text"])["error"] == "not_found"


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


async def test_creator_can_hand_a_task_to_whoever_she_asks_and_cron_starts_as_the_creator(
    client: httpx.AsyncClient, app, db_session: AsyncSession
) -> None:
    from coreman.core.db.models import Bot, WorkTaskEvent

    bot, alice, _, first, second = await _setup(db_session)
    other = Bot(
        bot_key="ops_bot",
        platform="feishu",
        name="运维助手",
        created_by=bot.created_by,
        relay_server_id=bot.relay_server_id,
        model=bot.model,
        working_dir="/data/skills/ops_bot",
        system_prompt="你是运维助手",
        merged_system_prompt="你是运维助手",
        credentials_enc=bot.credentials_enc,
        env_vars_enc=bot.env_vars_enc,
    )
    db_session.add(other)
    db_session.add(UserIdentity(user_id=alice.id, platform="feishu", platform_user_id="ou_a"))
    await db_session.flush()
    db_session.add(UserReached(bot_id=other.id, user_id=alice.id, platform_chat_id="oc_dm_o"))
    own = await tasks.enqueue(
        db_session,
        tasks.NewTask(bot_id=other.id, kind="chat", user_id=alice.id, session_key="oc_dm_o"),
    )
    own.status = "running"
    await db_session.commit()

    def call(actor_id, kind="chat"):
        token = tools.issue_capability(
            app.state.cipher, task_id=own.id, bot_id=other.id, actor_id=actor_id, kind=kind
        )
        return {"Authorization": f"Bearer {token}"}

    # 创建人 alice 叫「运维助手」做派给「销售」的任务：运维助手接手，以 alice 的身份做。
    response = await client.post(
        URL, json=_call("start_task", {"codes": [first.code]}), headers=call(alice.id)
    )
    result = json.loads(response.json()["result"]["content"][0]["text"])["results"][0]
    assert result["started"] is True
    row = await db_session.scalar(
        select(WorkTask)
        .where(WorkTask.code == first.code)
        .execution_options(populate_existing=True)
    )
    assert row.assignee_bot_id == other.id and row.principal_user_id == alice.id
    texts = list(
        await db_session.scalars(
            select(WorkTaskEvent.human_text).where(WorkTaskEvent.work_task_id == row.id)
        )
    )
    assert "alice叫运维助手接手" in texts

    # 别人不行：只能叫任务指派给的那位。
    carol = User(login_name="carol", display_name="carol", email="carol@example.test")
    db_session.add(carol)
    await db_session.commit()
    response = await client.post(
        URL, json=_call("start_task", {"codes": [second.code]}), headers=call(carol.id)
    )
    result = json.loads(response.json()["result"]["content"][0]["text"])["results"][0]
    assert result["started"] is False and "没有指派给「运维助手」" in result["reason"]

    # 定时领取是自动开工：以创建人 alice 的身份做，不是定时任务的所有者 carol。
    second.assignee_bot_id, second.principal_user_id = other.id, carol.id
    await db_session.commit()
    response = await client.post(
        URL,
        json=_call("start_task", {"codes": [second.code]}),
        headers=call(carol.id, kind="cron"),
    )
    result = json.loads(response.json()["result"]["content"][0]["text"])["results"][0]
    assert result["started"] is True
    await db_session.refresh(second)
    assert second.principal_user_id == alice.id


async def test_the_creator_reviews_by_default_even_when_someone_else_assigned_it(
    db_session: AsyncSession,
) -> None:
    bot, alice, _, _, _ = await _setup(db_session)
    bob = User(login_name="bob", display_name="bob", email="bob@example.test")
    db_session.add(bob)
    await db_session.flush()
    row = await service.create(db_session, alice, title="对一下账")
    row.assignee_user_id = bob.id
    await service.assign(db_session, row, bob, service.Assignee("bot", bot.id))
    # 创建人是提需求的人：验收默认归她，不归转交的人。
    assert row.reviewer_user_id == alice.id and row.principal_user_id == bob.id
