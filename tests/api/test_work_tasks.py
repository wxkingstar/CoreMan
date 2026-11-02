"""任务页接口：所有登录成员都能看列表和详情，包括给 Agent 看的那一层。"""

from __future__ import annotations

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import GroupObserveDecision, WorkTask, WorkTaskEvent
from tests.api.conftest import login_as
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
