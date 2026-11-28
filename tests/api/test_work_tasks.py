"""任务页接口：所有登录成员都能看列表和详情，包括给 Agent 看的那一层。"""

from __future__ import annotations

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import (
    GroupObserveDecision,
    Task,
    User,
    UserIdentity,
    UserReached,
    WorkTask,
    WorkTaskEvent,
)
from coreman.core.work_tasks import service
from tests.api.conftest import _attach_session, login_as
from tests.integration.worker_helpers import seed_bot


async def _seed(db_session: AsyncSession) -> WorkTask:
    bot, _, _ = await seed_bot(db_session)
    decision = GroupObserveDecision(
        bot_id=bot.id,
        chat_id="oc_1",
        mode="shadow",
        outcome="dispatched",
        probability=0.9,
        state={"recent_messages": [{"sender": "李雷", "sender_kind": "human", "text": "退货率?"}]},
        dispatch={"plan": {"action": "create"}},
    )
    db_session.add(decision)
    await db_session.flush()
    task = WorkTask(
        title="查上周退货率",
        summary="算出上周退货率",
        status="shadow",
        platform="feishu",
        chat_id="oc_1",
        requester_name="李雷",
        assignee_bot_id=bot.id,
        dispatcher_bot_id=bot.id,
        decision_id=decision.id,
        brief="口径按退货单数除以签收单数",
    )
    db_session.add(task)
    await db_session.flush()
    db_session.add(
        WorkTaskEvent(
            work_task_id=task.id,
            kind="created",
            actor_type="bot",
            actor_id=str(bot.id),
            human_text="试运行：调度员会交给销售",
            agent_detail={"reason": "数据问题"},
        )
    )
    await db_session.commit()
    return task


async def test_any_member_sees_tasks_and_the_agent_layer(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    task = await _seed(db_session)
    await login_as(client, db_session, role="member")
    listed = await client.get("/api/admin/work-tasks", params={"status": "shadow"})
    assert listed.status_code == 200
    data = listed.json()["data"]
    assert [i["code"] for i in data["items"]] == [task.code]
    assert data["items"][0]["assignee"]["name"] == "销售"
    assert data["counts"] == {"shadow": 1}
    assert (await client.get("/api/admin/work-tasks", params={"q": "库存"})).json()["data"][
        "items"
    ] == []
    detail = (await client.get(f"/api/admin/work-tasks/{task.code}")).json()["data"]
    assert detail["events"][0]["human_text"] == "试运行：调度员会交给销售"
    assert detail["events"][0]["actor_name"] == "销售"
    assert detail["agent"]["brief"] == "口径按退货单数除以签收单数"
    assert detail["agent"]["decision"]["recent_messages"][0]["text"] == "退货率?"
    assert detail["agent"]["decision"]["dispatch"] == {"plan": {"action": "create"}}
    assert (await client.get("/api/admin/work-tasks/T-999999")).status_code == 404
    assert (await client.get("/api/admin/work-tasks", params={"status": "x"})).status_code == 400


# ---- 任务平台：人在网页上的操作 --------------------------------------------------------------


async def _member(db_session: AsyncSession, name: str) -> User:
    user = User(login_name=name, display_name=name, email=f"{name}@example.test", role="member")
    db_session.add(user)
    await db_session.flush()
    return user


async def _feishu_bot(db_session: AsyncSession):
    bot, _, _ = await seed_bot(db_session)
    bot.platform = "feishu"
    await db_session.commit()
    return bot


async def test_person_to_person_accept_and_deliver(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bob = await _member(db_session, "bob")
    alice = await login_as(client, db_session, display_name="alice")
    made = await client.post(
        "/api/admin/work-tasks",
        json={
            "title": "整理供应商名单",
            "summary": "列出上季度合作过的",
            "assignee": {"type": "human", "id": str(bob.id)},
        },
    )
    assert made.status_code == 200, made.text
    task = made.json()["data"]
    assert task["status"] == "todo" and task["assignee"]["name"] == "bob"
    assert task["ask"]["kind"] == "accept" and task["ask"]["mine"] is False
    assert task["now"].startswith("等bob回答")
    code = task["code"]

    await _attach_session(client, db_session, bob)
    mine = (await client.get("/api/admin/work-tasks", params={"view": "needs_me"})).json()["data"]
    assert [i["code"] for i in mine["items"]] == [code] and mine["needs_me"] == 1
    ask = mine["items"][0]["ask"]
    assert ask["mine"] is True
    # 拒绝要写理由。
    bad = await client.post(
        f"/api/admin/work-tasks/{code}/answer", json={"ask_id": ask["id"], "option": "decline"}
    )
    assert bad.status_code == 422
    # 选「其他」写一句话：记下来，问题还在。
    other = await client.post(
        f"/api/admin/work-tasks/{code}/answer",
        json={"ask_id": ask["id"], "option": "other", "text": "明天再看可以吗"},
    )
    assert other.json()["data"]["ask"]["id"] == ask["id"]
    ok = await client.post(
        f"/api/admin/work-tasks/{code}/answer", json={"ask_id": ask["id"], "option": "accept"}
    )
    assert ok.json()["data"]["status"] == "in_progress" and ok.json()["data"]["ask"] is None
    # 同一个问题再答一次（例如卡片上又点了）：已经变了。
    again = await client.post(
        f"/api/admin/work-tasks/{code}/answer", json={"ask_id": ask["id"], "option": "accept"}
    )
    assert again.status_code == 409
    done = await client.post(
        f"/api/admin/work-tasks/{code}/complete", json={"text": "名单已放进共享表"}
    )
    data = done.json()["data"]
    assert data["status"] == "done" and data["summary"] == "名单已放进共享表"
    texts = [e["human_text"] for e in data["events"]]
    assert "bob：其他，明天再看可以吗" in texts and "bob交付：名单已放进共享表" in texts
    assert alice.id != bob.id


async def test_handing_to_an_agent_waits_to_be_driven_and_review_reject_reruns(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot = await _feishu_bot(db_session)
    alice = await login_as(client, db_session, display_name="alice")
    made = await client.post(
        "/api/admin/work-tasks",
        json={
            "title": "查上周退货率",
            "brief": "口径按退货单数除以签收单数",
            "assignee": {"type": "bot", "id": str(bot.id)},
        },
    )
    task = made.json()["data"]
    assert task["needs_drive"] is True and task["start_mode"] == "manual"
    assert task["review_required"] is True and task["reviewer"]["id"] == str(alice.id)
    assert "还没开工" in task["now"] and f"做 {task['code']}" in task["now"]
    mine = (await client.get("/api/admin/work-tasks", params={"view": "needs_me"})).json()["data"]
    assert [i["code"] for i in mine["items"]] == [task["code"]]
    # 没有被叫开工：不会排任何一轮。
    assert (await db_session.scalar(select(func.count()).select_from(Task))) == 0

    # 交付后待验收，退回：要排一轮重做（代表人和员工私聊过、有飞书身份）。
    db_session.add(UserIdentity(user_id=alice.id, platform="feishu", platform_user_id="ou_alice"))
    db_session.add(UserReached(bot_id=bot.id, user_id=alice.id, platform_chat_id="oc_dm"))
    row = await db_session.scalar(select(WorkTask).where(WorkTask.code == task["code"]))
    service.to_review(row, "上周退货率 4.2%")
    row.claimed_at = row.created_at
    await db_session.commit()
    detail = (await client.get(f"/api/admin/work-tasks/{task['code']}")).json()["data"]
    assert detail["status"] == "review" and detail["ask"]["mine"] is True
    rejected = await client.post(
        f"/api/admin/work-tasks/{task['code']}/answer",
        json={"ask_id": detail["ask"]["id"], "option": "reject", "text": "要按渠道拆开"},
    )
    data = rejected.json()["data"]
    assert data["status"] == "waiting" and data["now"] == f"排队等{bot.name}开始"
    queued = await db_session.scalar(select(Task))
    assert queued.session_key == f"work:{task['code']}" and queued.user_id == alice.id
    assert (
        queued.payload["work_trigger"] == "rerun" and queued.payload["work_note"] == "要按渠道拆开"
    )


async def test_only_participants_change_a_task_but_anyone_can_comment(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await login_as(client, db_session, display_name="alice")
    code = (await client.post("/api/admin/work-tasks", json={"title": "订团建场地"})).json()[
        "data"
    ]["code"]
    long = await client.post("/api/admin/work-tasks", json={"title": "x" * 31})
    assert long.status_code == 422
    await login_as(client, db_session, display_name="carol")
    assert (
        await client.post(f"/api/admin/work-tasks/{code}/cancel", json={"text": "不需要了"})
    ).status_code == 403
    commented = await client.post(
        f"/api/admin/work-tasks/{code}/comment", json={"text": "推荐 A 场地"}
    )
    assert commented.json()["data"]["can_manage"] is False
    watched = await client.post(f"/api/admin/work-tasks/{code}/watch", json={"on": True})
    assert watched.json()["data"]["watching"] is True
    listed = (await client.get("/api/admin/work-tasks", params={"view": "watching"})).json()
    assert [i["code"] for i in listed["data"]["items"]] == [code]


async def test_reject_still_counts_when_the_agent_cannot_take_it_back(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """代表人没私聊过这位员工：退回照样生效，改成等人叫它开工。"""
    bot = await _feishu_bot(db_session)
    await login_as(client, db_session, display_name="alice")
    code = (
        await client.post(
            "/api/admin/work-tasks",
            json={"title": "查上周退货率", "assignee": {"type": "bot", "id": str(bot.id)}},
        )
    ).json()["data"]["code"]
    row = await db_session.scalar(select(WorkTask).where(WorkTask.code == code))
    service.to_review(row, "4.2%")
    row.claimed_at = row.created_at
    await db_session.commit()
    ask = (await client.get(f"/api/admin/work-tasks/{code}")).json()["data"]["ask"]
    assert ask["can_answer"] is True
    rejected = await client.post(
        f"/api/admin/work-tasks/{code}/answer",
        json={"ask_id": ask["id"], "option": "reject", "text": "按渠道拆开"},
    )
    data = rejected.json()["data"]
    assert rejected.status_code == 200 and data["status"] == "todo" and data["needs_drive"] is True
    assert any("没能自动交回" in e["human_text"] for e in data["events"])


async def test_changing_the_reviewer_moves_the_question(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bob = await _member(db_session, "bob")
    await login_as(client, db_session, display_name="alice")
    code = (
        await client.post(
            "/api/admin/work-tasks",
            json={"title": "写周报", "assignee": {"type": "human", "id": str(bob.id)}},
        )
    ).json()["data"]["code"]
    row = await db_session.scalar(select(WorkTask).where(WorkTask.code == code))
    service.to_review(row, "写好了")
    await db_session.commit()
    carol = await _member(db_session, "carol")
    await db_session.commit()
    moved = await client.patch(
        f"/api/admin/work-tasks/{code}", json={"reviewer_user_id": str(carol.id)}
    )
    assert moved.json()["data"]["ask"]["to"]["id"] == str(carol.id)


async def test_stats_show_backlog_and_first_pass_rate(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bob = await _member(db_session, "bob")
    await login_as(client, db_session, display_name="alice")
    for title in ("写周报", "订场地"):
        await client.post(
            "/api/admin/work-tasks",
            json={"title": title, "assignee": {"type": "human", "id": str(bob.id)}},
        )
    stats = (await client.get("/api/admin/work-tasks/stats", params={"days": 7})).json()["data"]
    assert stats["backlog"][0]["assignee"]["name"] == "bob" and stats["backlog"][0]["open"] == 2
    assert {s["source"]: s["created"] for s in stats["by_source"]} == {"web": 2}
    assert stats["agent_first_pass_rate"] is None


async def test_detail_lists_status_of_every_task_code_on_the_page(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    task = await _seed(db_session)
    related = WorkTask(title="对接约定", status="done", requester_name="李雷")
    child = WorkTask(title="拉退货明细", status="in_progress", requester_name="李雷")
    mentioned = WorkTask(title="重推单据", status="waiting", requester_name="李雷")
    db_session.add_all([related, mentioned])
    await db_session.flush()
    child.parent_id = task.id
    db_session.add(child)
    await db_session.flush()
    task.related_codes = [related.code]
    db_session.add(
        WorkTaskEvent(
            work_task_id=task.id,
            kind="subtask",
            actor_type="system",
            human_text=f"等{mentioned.code}、T-99999 和 {task.code} 的结果",
        )
    )
    await db_session.commit()
    await login_as(client, db_session, role="member")
    detail = (await client.get(f"/api/admin/work-tasks/{task.code}")).json()["data"]
    assert detail["linked"] == {
        related.code: {"title": "对接约定", "status": "done"},
        child.code: {"title": "拉退货明细", "status": "in_progress"},
        mentioned.code: {"title": "重推单据", "status": "waiting"},
    }
