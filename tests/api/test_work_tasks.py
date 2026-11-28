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


async def test_the_list_shows_goals_only_and_counts_what_is_inside(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """列表默认只列顶层的事；拆出来的步骤和问人的决策点收在它下面，只写个数。"""
    root = await _seed(db_session)
    root.status, root.root_id = "in_progress", root.id
    asker = await _member(db_session, "asked")
    rows = [
        WorkTask(title="对齐接口文档", status="done", parent_id=root.id, root_id=root.id, depth=1),
        WorkTask(
            title="仓储侧开发", status="in_progress", parent_id=root.id, root_id=root.id, depth=1
        ),
        WorkTask(
            title="问：结算口径按哪种？",
            status="todo",
            parent_id=root.id,
            root_id=root.id,
            depth=1,
            code=f"{root.code}-Q1",
            brief="[问题]\n背景",
            assignee_user_id=asker.id,
        ),
    ]
    db_session.add_all(rows)
    await db_session.commit()
    await login_as(client, db_session, role="member")

    listed = (await client.get("/api/admin/work-tasks")).json()["data"]
    assert [i["code"] for i in listed["items"]] == [root.code]
    assert {k: listed["items"][0][k] for k in ("steps", "steps_done", "decisions")} == {
        "steps": 2,
        "steps_done": 1,
        "decisions": 1,
    }
    assert listed["items"][0]["decisions_open"] == 1
    assert listed["counts"] == {"in_progress": 1}

    # 要逐条看、或者按关键词找时，步骤和决策点照样列得出来。
    everything = (await client.get("/api/admin/work-tasks", params={"scope": "all"})).json()
    assert len(everything["data"]["items"]) == 4
    found = (await client.get("/api/admin/work-tasks", params={"q": "仓储侧"})).json()["data"]
    assert [i["title"] for i in found["items"]] == ["仓储侧开发"]


async def test_the_home_list_tells_me_what_is_coming_my_way(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """还在进行的事里，后续步骤点到我名字的：现在不用我处理，但提前让我看到。"""
    await login_as(client, db_session, display_name="alice")
    root = WorkTask(title="订单与仓储系统对接", status="in_progress")
    db_session.add(root)
    await db_session.flush()
    root.root_id = root.id
    db_session.add_all(
        [
            WorkTask(title="两边联调", status="in_progress", parent_id=root.id, root_id=root.id),
            WorkTaskEvent(
                work_task_id=root.id,
                kind="plan",
                actor_type="bot",
                human_text="",
                agent_detail={"steps": ["两边联调", "请 alice 同意合并", "合并后发测试环境"]},
            ),
        ]
    )
    await db_session.commit()

    home = (await client.get("/api/admin/work-tasks", params={"view": "needs_me"})).json()["data"]
    assert home["items"] == []
    assert home["upcoming"] == [
        {"code": root.code, "title": "订单与仓储系统对接", "step": "请 alice 同意合并"}
    ]
    # 别的栏不算这个；事情办完了也不再预告。
    assert (await client.get("/api/admin/work-tasks")).json()["data"]["upcoming"] == []
    root.status = "done"
    await db_session.commit()
    home = (await client.get("/api/admin/work-tasks", params={"view": "needs_me"})).json()["data"]
    assert home["upcoming"] == []


async def test_follow_ups_left_after_delivery_can_be_ticked_off(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    alice = await login_as(client, db_session, display_name="alice")
    made = (await client.post("/api/admin/work-tasks", json={"title": "对接仓储系统"})).json()
    code = made["data"]["code"]
    row = await db_session.scalar(select(WorkTask).where(WorkTask.code == code))
    row.status, row.summary = "done", "测试环境联调通过"
    db_session.add(
        WorkTaskEvent(
            work_task_id=row.id,
            kind="submitted",
            actor_type="human",
            actor_id=str(alice.id),
            human_text="交付",
            agent_detail={"followups": ["合并 PR #12", "发到正式环境"]},
        )
    )
    await db_session.commit()

    ticked = await client.post(f"/api/admin/work-tasks/{code}/followups/0", json={"done": True})
    left = ticked.json()["data"]["overview"]["followups"]
    assert [(f["text"], f["done"], f["by"]) for f in left] == [
        ("合并 PR #12", True, "alice"),
        ("发到正式环境", False, ""),
    ]
    assert "alice办了：合并 PR #12" in [e["human_text"] for e in ticked.json()["data"]["events"]]
    turned = await client.post(f"/api/admin/work-tasks/{code}/followups/1", json={"as_task": True})
    task_code = turned.json()["data"]["overview"]["followups"][1]["task"]
    assert task_code and task_code in turned.json()["data"]["related_codes"]
    missing = await client.post(f"/api/admin/work-tasks/{code}/followups/9", json={"done": True})
    assert missing.status_code == 404


async def test_the_list_leaves_out_automatic_records_unless_asked(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """自动记下的接口任务、求助默认不列：它们是机器之间的流水，一多就把人建的事淹没了。"""
    alice = await login_as(client, db_session, display_name="alice")
    made = (await client.post("/api/admin/work-tasks", json={"title": "整理供应商名单"})).json()
    mine = made["data"]["code"]
    db_session.add_all(
        [
            WorkTask(
                title="业务系统发起的私聊任务",
                status="done",
                source="api",
                origin_ref="api:1",
                requester_user_id=alice.id,
                principal_user_id=alice.id,
            ),
            WorkTask(
                title="求助：库存口径",
                status="in_progress",
                source="collaboration",
                origin_ref="bot_help:1",
                principal_user_id=alice.id,
            ),
        ]
    )
    await db_session.commit()

    for view in ("all", "created"):
        listed = (await client.get("/api/admin/work-tasks", params={"view": view})).json()["data"]
        assert [i["code"] for i in listed["items"]] == [mine], view
        assert listed["counts"] == {"todo": 1}
    # 要看时带 records；按关键词找时不用带也找得到。
    everything = (await client.get("/api/admin/work-tasks", params={"records": True})).json()
    assert {i["title"] for i in everything["data"]["items"]} == {
        "整理供应商名单",
        "业务系统发起的私聊任务",
        "求助：库存口径",
    }
    found = (await client.get("/api/admin/work-tasks", params={"q": "库存口径"})).json()["data"]
    assert [i["mirror"] for i in found["items"]] == [True]
    # 只看还没结束的：首页「我发起、还在进行的」用。
    params = {"view": "created", "unfinished": True, "records": True}
    going = (await client.get("/api/admin/work-tasks", params=params)).json()["data"]
    assert {i["status"] for i in going["items"]} == {"todo", "in_progress"}


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


async def test_starting_an_agent_from_the_task_page(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot = await _feishu_bot(db_session)
    alice = await login_as(client, db_session, display_name="alice")
    body = {"title": "查上周退货率", "assignee": {"type": "bot", "id": str(bot.id)}}

    # 选了现在开工，但她没有飞书身份、没法以她的身份做：任务照样建好，告诉她原因，留着等人叫。
    made = (await client.post("/api/admin/work-tasks", json={**body, "start": True})).json()["data"]
    assert "没有飞书身份" in made["start_error"] and made["needs_drive"] is True
    assert made["status"] == "todo" and made["principal"]["id"] == str(alice.id)
    assert [e["human_text"] for e in made["events"] if e["kind"] == "start_failed"] == [
        f"没能直接开工：{made['start_error']}"
    ]
    assert (await db_session.scalar(select(func.count()).select_from(Task))) == 0
    refused = await client.post(f"/api/admin/work-tasks/{made['code']}/start")
    assert refused.status_code == 409 and "没有飞书身份" in refused.text

    # 有了飞书身份、还没和这位员工私聊过：照样能开工，这一轮不出声，结果记在任务上。
    db_session.add(UserIdentity(user_id=alice.id, platform="feishu", platform_user_id="ou_alice"))
    await db_session.commit()
    started = (await client.post(f"/api/admin/work-tasks/{made['code']}/start")).json()["data"]
    assert started["status"] == "waiting" and started["now"] == f"排队等{bot.name}开始"
    assert started["needs_drive"] is False and "start_error" not in started
    claimed = next(e for e in started["events"] if e["kind"] == "claimed")
    assert claimed["human_text"] == f"alice在任务页叫{bot.name}开工，以alice的身份做"
    assert claimed["agent_detail"]["reply"] == "task_page"
    queued = await db_session.scalar(select(Task))
    assert queued.session_key == f"work:{made['code']}" and queued.user_id == alice.id
    assert queued.payload["work_trigger"] == "web" and queued.payload["work_silent"] is True
    again = await client.post(f"/api/admin/work-tasks/{made['code']}/start")
    assert again.status_code == 409

    # 私聊过以后：回复发到她和它的私聊。新建时选现在开工，建好就排上；不选（接口默认）照旧等人叫。
    db_session.add(UserReached(bot_id=bot.id, user_id=alice.id, platform_chat_id="oc_dm"))
    await db_session.commit()
    now = (await client.post("/api/admin/work-tasks", json={**body, "start": True})).json()["data"]
    assert now["status"] == "waiting" and "start_error" not in now
    loud = await db_session.scalar(select(Task).order_by(Task.id.desc()).limit(1))
    assert loud.payload["work_silent"] is False
    later = (await client.post("/api/admin/work-tasks", json=body)).json()["data"]
    assert later["status"] == "todo" and later["needs_drive"] is True

    # 转交给 AI 员工时选现在开工也一样；交给人的任务没有开工这回事。
    plain = (await client.post("/api/admin/work-tasks", json={"title": "订团建场地"})).json()[
        "data"
    ]
    no_agent = await client.post(f"/api/admin/work-tasks/{plain['code']}/start")
    assert no_agent.status_code == 409 and "没有派给 AI 员工" in no_agent.text
    handed = await client.post(
        f"/api/admin/work-tasks/{plain['code']}/assign",
        json={"assignee": {"type": "bot", "id": str(bot.id)}, "start": True},
    )
    assert handed.json()["data"]["status"] == "waiting"
    # 两种转交的写法都写明是谁做的。
    assert "alice交给" in [e["human_text"] for e in handed.json()["data"]["events"]][1]
    assert (await db_session.scalar(select(func.count()).select_from(Task))) == 3


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


async def test_detail_as_markdown_for_ai(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    task = await _seed(db_session)
    child = WorkTask(title="拉退货|明细", status="done", requester_name="李雷", parent_id=task.id)
    db_session.add(child)
    await db_session.commit()
    await login_as(client, db_session, role="member")
    resp = await client.get(f"/api/admin/work-tasks/{task.code}", params={"format": "md"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    text = resp.text
    assert text.startswith(f"# {task.code} 查上周退货率\n")
    assert "- 状态：试运行" in text
    assert "- 执行人：销售（AI）" in text
    assert "## 子任务" in text
    assert f"| {child.code} | 拉退货\\|明细 | 已完成 |" in text
    assert "## 交给 AI 员工的背景\n\n口径按退货单数除以签收单数" in text
    assert "销售：试运行：调度员会交给销售" in text
    assert '细节：{"reason":"数据问题"}' in text
    assert "退货率?" in text
    bad = await client.get(f"/api/admin/work-tasks/{task.code}", params={"format": "x"})
    assert bad.status_code in (400, 422)


def test_timeline_keeps_milestones_for_people() -> None:
    from coreman.core.work_tasks.view import milestone

    def event(kind: str, text: str, detail: dict | None = None) -> WorkTaskEvent:
        return WorkTaskEvent(kind=kind, human_text=text, agent_detail=detail or {})

    assert milestone(event("created", "调度员自己牵头"))
    assert milestone(event("progress", "退货链路已走通"))
    assert milestone(event("subtask", "问王五（T-9）：要走哪种寄售？"))
    assert milestone(event("claimed", "李雷叫销售开工", {"trigger": "chat"}))
    assert not milestone(event("claimed", "等的事都有结果了，销售接着做", {"trigger": "resume"}))
    assert not milestone(event("started", "销售继续处理"))
    assert not milestone(event("subtask", "拆出 T-9：拉退货明细（销售）"))
    assert not milestone(event("waiting", ""))
