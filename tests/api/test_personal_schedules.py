"""Owner-created scheduled AI tasks, and jobs that act as their owner on Feishu."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from coreman.core import personal_schedules as schedules
from coreman.core.bus import instances, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import (
    BotMember,
    ChatLog,
    CronJob,
    CronRun,
    InboundEvent,
    InteractionState,
    OutboxItem,
    RelayServer,
    RuntimeNode,
    Task,
    UserReached,
)
from coreman.core.db.session import make_session_factory
from coreman.core.feishu_personal import policy
from coreman.runtime.scheduler.cron import run_tick
from coreman.runtime.worker.card_actions import CardActionHandler
from coreman.runtime.worker.cron_handler import CronRunHandler
from tests.api.test_feishu_personal import URL, grant, headers, rpc, setup, value
from tests.fakes.fake_relay import FakeRelay
from tests.integration.worker_helpers import build_ctx

NOW = datetime(2026, 9, 18, 0, 0, tzinfo=UTC)


def test_plan_enforces_interval_and_window():
    weekdays = schedules.plan("0 9 * * 1-5", None, NOW)
    assert weekdays.schedule_kind == "recurring" and len(weekdays.next_runs) == 3
    assert schedules.shown(weekdays.next_runs[0]) == "2026-09-18 09:00"
    for expression in ("*/30 * * * *", "0,30 9 * * *", "5,10 9 * * *"):
        with pytest.raises(schedules.ScheduleError, match="1 小时"):
            schedules.plan(expression, None, NOW)
    with pytest.raises(schedules.ScheduleError):
        schedules.plan("not cron", None, NOW)
    once = schedules.plan(None, "2026-09-19T09:00:00+08:00", NOW)
    assert once.schedule_kind == "once" and once.run_at == datetime(2026, 9, 19, 1, tzinfo=UTC)
    for bad in ("2026-09-18T08:00:30+08:00", "2026-11-01T09:00:00+08:00", "2026-09-19T09:00:00"):
        with pytest.raises(schedules.ScheduleError):
            schedules.plan(None, bad, NOW)


async def supported(session, bot):
    relay = await session.get(RelayServer, bot.relay_server_id)
    node = await session.get(RuntimeNode, relay.runtime_node_id)
    node.capabilities = {"claude": {"feishu_personal_tools_v1": True}}
    await session.commit()


async def propose(client, app, db_session, **arguments):
    bot, user, task = await setup(db_session, app)
    auth = await headers(app, task, user)
    arguments = {"name": "未读汇总", "prompt": "总结我今天的飞书未读消息", **arguments}
    if "run_at" not in arguments:
        arguments.setdefault("cron_expression", "0 9 * * 1-5")
    result = value(await client.post(URL, headers=auth, json=rpc("schedule_propose", arguments)))
    return bot, user, task, result


async def test_propose_sends_a_card_and_creates_nothing(client, app, db_session):
    bot, user, task, result = await propose(client, app, db_session)
    assert result["status"] == "awaiting_confirmation" and len(result["next_runs"]) == 3
    assert await db_session.scalar(select(CronJob)) is None
    state = await db_session.scalar(select(InteractionState))
    assert state.kind == "personal_schedule" and state.status == "open"
    assert state.state["user_id"] == str(user.id) and state.state["chat_id"] == "oc_private"
    card = (await db_session.scalars(select(OutboxItem))).one()
    assert card.target == {"chat_id": "oc_private"}
    assert card.payload["card"]["task_id"] == f"personal:{task.id}"
    buttons = [e for e in card.payload["card"]["body"]["elements"] if e["tag"] == "button"]
    values = [b["behaviors"][0]["value"] for b in buttons]
    assert [v["level"] for v in values] == [f"confirm:{state.id}", f"cancel:{state.id}"]
    assert {v["event_key"] for v in values} == {"personal_schedule"}


@pytest.mark.parametrize(
    "arguments,error",
    [
        ({"cron_expression": "*/10 * * * *"}, "interval_too_short"),
        ({"run_at": "2030-01-01T09:00:00+08:00"}, "run_at_out_of_range"),
        ({"cron_expression": "0 9 * * *", "run_at": "2026-09-19T09:00:00+08:00"}, None),
    ],
)
async def test_invalid_proposals_are_explained(client, app, db_session, arguments, error):
    *_, result = await propose(client, app, db_session, **arguments)
    assert result["error"] == (error or "invalid_tool_or_arguments")
    assert await db_session.scalar(select(InteractionState)) is None


async def test_active_limit_blocks_new_proposals(client, app, db_session):
    bot, user, task = await setup(db_session, app)
    for index in range(schedules.MAX_ACTIVE):
        db_session.add(personal_job(bot, user, name=f"t{index}"))
    await db_session.commit()
    auth = await headers(app, task, user)
    arguments = {"name": "x", "prompt": "y", "cron_expression": "0 9 * * *"}
    result = value(await client.post(URL, headers=auth, json=rpc("schedule_propose", arguments)))
    assert result["error"] == "too_many_schedules"


def personal_job(bot, user, **kw):
    values = {
        "bot_id": bot.id,
        "created_by": user.id,
        "name": "未读汇总",
        "execution_mode": "personal_ai",
        "reminder_chat_id": "oc_private",
        "cron_expression": "0 9 * * *",
        "timezone": "Asia/Shanghai",
        "prompt": "总结我今天的飞书未读消息",
        "target_users": [user.id],
        "next_run_at": datetime.now(UTC),
    }
    return CronJob(**{**values, **kw})


async def click(session, bot, original, verb, state_id, *, operator="ou_human"):
    card = await session.scalar(select(OutboxItem).where(OutboxItem.kind == "send"))
    card.status = "sent"
    card.payload = {**card.payload, "_feishu_message_id": "om_card"}
    action = {
        "task_id": f"personal:{original.id}",
        "event_key": "personal_schedule",
        "level": f"{verb}:{state_id}",
    }
    event = InboundEvent(
        bot_id=bot.id,
        platform="feishu",
        platform_msg_id=str(uuid.uuid4()),
        kind="card_action",
        chat_type="group",
        chat_id="oc_private",
        sender_platform_user_id="human",
        sender_open_id=operator,
        reply_context={"chat_id": "oc_private", "message_id": "om_card"},
        payload={
            "raw": {
                "header": {
                    "event_type": "card.action.trigger",
                    "app_id": "cli_test",
                    "tenant_key": "tenant-test",
                },
                "event": {
                    "operator": {"user_id": "human", "open_id": operator},
                    "context": {"open_chat_id": "oc_private", "open_message_id": "om_card"},
                    "action": {"value": action},
                },
            }
        },
    )
    session.add(event)
    await session.flush()
    task = await tasks.enqueue(
        session,
        NewTask(
            bot_id=bot.id,
            kind="card_action",
            lane="fast",
            payload={"card_action": action, "platform_user_id": "human"},
            session_key="oc_private",
            inbound_event_id=event.id,
            dedupe_key=f"click:{event.id}",
        ),
    )
    task.status = "running"
    await session.commit()
    return task


@pytest.mark.parametrize("case", ["confirm", "cancel", "other_user", "expired", "full"])
async def test_only_the_owner_confirms_on_the_card(client, app, db_session, db_engine, case):
    bot, user, task, _ = await propose(client, app, db_session)
    state = await db_session.scalar(select(InteractionState))
    if case == "expired":
        state.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    if case == "full":
        for index in range(schedules.MAX_ACTIVE):
            db_session.add(personal_job(bot, user, name=f"t{index}"))
    await db_session.commit()
    verb = "cancel" if case == "cancel" else "confirm"
    operator = "ou_other" if case == "other_user" else "ou_human"
    pressed = await click(db_session, bot, task, verb, state.id, operator=operator)
    await CardActionHandler().run(build_ctx(db_engine, pressed))
    await db_session.refresh(state)
    jobs = (await db_session.scalars(select(CronJob).where(CronJob.name == "未读汇总"))).all()
    updates = (
        await db_session.scalars(select(OutboxItem).where(OutboxItem.kind == "card_update"))
    ).all()
    if case != "confirm":
        assert jobs == []
        assert (
            state.status
            == {
                "cancel": "cancelled",
                "other_user": "open",
                "expired": "expired",
                "full": "cancelled",
            }[case]
        )
        assert bool(updates) == (case != "other_user")
        return
    [job] = jobs
    assert state.status == "submitted"
    assert (job.execution_mode, job.created_by, job.target_users) == (
        "personal_ai",
        user.id,
        [user.id],
    )
    assert job.reminder_chat_id == "oc_private" and job.enabled and job.next_run_at
    assert not (job.target_chats or job.notify_emails or job.notify_webhook)
    reached = await db_session.get(UserReached, (bot.id, user.id))
    assert reached.platform_chat_id == "oc_private"
    assert "已创建定时任务" in updates[0].payload["card"]["body"]["elements"][0]["content"]


async def claim(session):
    await instances.register(
        session, instance_id="worker-test", service="worker", version="dev", capacity=8
    )
    task = await tasks.claim(session, lane="normal", instance_id="worker-test")
    await session.commit()
    assert task is not None
    return task


async def run_due(session, engine, job):
    await run_tick(make_session_factory(engine), job.next_run_at)
    task = await claim(session)
    fake = FakeRelay("normal")
    await CronRunHandler().run(
        build_ctx(engine, task, relay_client_factory=lambda _: fake.client())
    )
    return task, fake


@pytest.mark.parametrize("connected", [True, False])
async def test_member_schedule_runs_as_owner_and_stays_private(
    app, db_session, db_engine, connected
):
    bot, user, _ = await setup(db_session, app)
    await supported(db_session, bot)
    if connected:
        await grant(db_session, app, bot, user)
    db_session.add(UserReached(bot_id=bot.id, user_id=user.id, platform_chat_id="oc_private"))
    job = personal_job(bot, user)
    db_session.add(job)
    await db_session.commit()
    task, fake = await run_due(db_session, db_engine, job)
    assert task.user_id == user.id
    run = await db_session.scalar(select(CronRun))
    assert run.status == "success" and run.private and run.executed_by == user.id
    request = fake.requests[0]
    env = request["env_vars"]
    assert env["COREMAN_USER_LOGIN"] == user.login_name
    assert (policy.PREFIX + "TOKEN" in env) is connected
    assert ("## 本人飞书" in request["messages"][0]["content"]) is connected
    if connected:
        capability = policy.read_capability(app.state.cipher, env[policy.PREFIX + "TOKEN"])
        assert capability.session_id is None and capability.task_id == task.id
    [item] = (await db_session.scalars(select(OutboxItem))).all()
    assert item.target["chat_id"] == "oc_private"


async def test_member_schedule_is_skipped_once_owner_loses_access(app, db_session, db_engine):
    from coreman.core.db.models import BotAllowedUser, User

    bot, user, _ = await setup(db_session, app)
    other = User(login_name="someone", display_name="Someone", source="sync")
    db_session.add(other)
    await db_session.flush()
    db_session.add(BotAllowedUser(bot_id=bot.id, user_id=other.id))
    db_session.add(UserReached(bot_id=bot.id, user_id=user.id, platform_chat_id="oc_private"))
    job = personal_job(bot, user)
    db_session.add(job)
    await db_session.commit()
    await run_tick(make_session_factory(db_engine), job.next_run_at)
    await db_session.refresh(job)
    assert not job.enabled
    assert await db_session.scalar(select(Task).where(Task.kind == "cron_run")) is None


@pytest.mark.parametrize("variation", ["owner", "group_target", "forced_by_other"])
async def test_admin_job_acts_as_owner_only_when_owned_and_private(
    app, db_session, db_engine, variation
):
    from coreman.core.db.models import User, UserIdentity

    bot, user, _ = await setup(db_session, app)
    await supported(db_session, bot)
    await grant(db_session, app, bot, user)
    db_session.add(BotMember(bot_id=bot.id, user_id=user.id))
    db_session.add(UserReached(bot_id=bot.id, user_id=user.id, platform_chat_id="oc_private"))
    job = CronJob(
        bot_id=bot.id,
        created_by=user.id,
        name="日报",
        cron_expression="* * * * *",
        prompt="总结我的会议",
        next_run_at=datetime.now(UTC),
        target_chats=["oc_group"] if variation == "group_target" else [],
    )
    if variation == "forced_by_other":
        other = User(login_name="other-admin", display_name="Other", source="sync")
        db_session.add(other)
        await db_session.flush()
        db_session.add(BotMember(bot_id=bot.id, user_id=other.id))
        db_session.add(UserIdentity(user_id=other.id, platform="feishu", platform_user_id="other"))
        job.force_run_at, job.force_run_by = job.next_run_at, other.id
    db_session.add(job)
    await db_session.commit()
    _, fake = await run_due(db_session, db_engine, job)
    env = fake.requests[0]["env_vars"]
    run = await db_session.scalar(select(CronRun))
    assert (policy.PREFIX + "TOKEN" in env) is (variation == "owner")
    assert run.private is (variation == "owner")


async def test_private_runs_hidden_from_other_admins(app, client, db_session, db_engine):
    from tests.api.conftest import login_as, login_existing

    bot, user, _ = await setup(db_session, app)
    await supported(db_session, bot)
    await grant(db_session, app, bot, user)
    db_session.add(BotMember(bot_id=bot.id, user_id=user.id))
    db_session.add(UserReached(bot_id=bot.id, user_id=user.id, platform_chat_id="oc_private"))
    job = CronJob(
        bot_id=bot.id,
        created_by=user.id,
        name="日报",
        cron_expression="* * * * *",
        prompt="总结我的会议",
        next_run_at=datetime.now(UTC),
    )
    db_session.add(job)
    await db_session.commit()
    await run_due(db_session, db_engine, job)
    log = await db_session.scalar(select(ChatLog))
    assert log.chat_type == "cron"
    from coreman.core.db.models import UserIdentity

    admin = await login_as(client, db_session, role="platform_admin")
    db_session.add(BotMember(bot_id=bot.id, user_id=admin.id))
    db_session.add(UserIdentity(user_id=admin.id, platform="feishu", platform_user_id="admin"))
    await db_session.commit()
    listed = (await client.get("/api/admin/chat-logs")).json()["data"]["items"]
    assert [item["id"] for item in listed] == []
    runs = (await client.get(f"/api/admin/cron-jobs/{job.id}/runs")).json()["data"]["items"]
    assert runs[0]["private"] and runs[0]["reply"] is None and runs[0]["prompt"] == ""
    await login_existing(client, db_session, user)
    listed = (await client.get("/api/admin/chat-logs")).json()["data"]["items"]
    assert [item["id"] for item in listed] == [log.id]
    runs = (await client.get(f"/api/admin/cron-jobs/{job.id}/runs")).json()["data"]["items"]
    assert runs[0]["reply"] == "你好，世界。"


async def test_admins_see_member_schedules_only_to_disable_them(app, client, db_session):
    from tests.api.conftest import login_as

    bot, user, _ = await setup(db_session, app)
    job = personal_job(bot, user)
    db_session.add(job)
    await db_session.commit()
    admin = await login_as(client, db_session, role="member")
    db_session.add(BotMember(bot_id=bot.id, user_id=admin.id))
    from coreman.core.db.models import UserIdentity

    db_session.add(UserIdentity(user_id=admin.id, platform="feishu", platform_user_id="admin"))
    await db_session.commit()
    [item] = (await client.get("/api/admin/cron-jobs")).json()["data"]["items"]
    assert item["personal"] and item["prompt"] == "" and not item["can_edit"]
    assert (await client.get(f"/api/admin/cron-jobs/{job.id}/runs")).status_code == 404
    assert (await client.post(f"/api/admin/cron-jobs/{job.id}/run")).status_code == 404
    response = await client.post(
        f"/api/admin/cron-jobs/{job.id}/disable", headers={"If-Match": f'"{job.version}"'}
    )
    assert response.status_code == 200, response.text
    await db_session.refresh(job)
    assert not job.enabled


async def test_owner_manages_schedules_on_my_schedules_page(app, client, db_session):
    from tests.api.conftest import login_existing

    bot, user, _ = await setup(db_session, app)
    db_session.add(UserReached(bot_id=bot.id, user_id=user.id, platform_chat_id="oc_private"))
    job = personal_job(bot, user)
    db_session.add(job)
    await db_session.commit()
    await login_existing(client, db_session, user)
    [item] = (await client.get("/api/self-reminders")).json()["data"]
    assert item["type"] == "schedule" and item["name"] == "未读汇总" and item["can_pause"]
    paused = (await client.post(f"/api/self-reminders/{job.id}/pause")).json()["data"]
    assert not paused["enabled"] and paused["can_resume"]
    resumed = (await client.post(f"/api/self-reminders/{job.id}/resume")).json()["data"]
    assert resumed["enabled"] and resumed["next_run_at"]
    assert (await client.post(f"/api/self-reminders/{job.id}/cancel")).status_code == 404
    assert (await client.delete(f"/api/self-reminders/{job.id}")).status_code == 200
    assert await db_session.scalar(select(CronJob)) is None


async def test_scheduled_capability_revalidates_the_job(app, client, db_session, db_engine):
    bot, user, _ = await setup(db_session, app)
    await supported(db_session, bot)
    await grant(db_session, app, bot, user)
    db_session.add(UserReached(bot_id=bot.id, user_id=user.id, platform_chat_id="oc_private"))
    job = personal_job(bot, user)
    db_session.add(job)
    await db_session.commit()
    await run_tick(make_session_factory(db_engine), job.next_run_at)
    task = await claim(db_session)
    await db_session.refresh(job)
    token = policy.issue_capability(
        app.state.cipher,
        task_id=task.id,
        user_id=str(user.id),
        context_epoch=(await grant_epoch(db_session, bot, user)),
        base_session_id=None,
    )
    auth = {"Authorization": "Bearer " + token}
    listed = await client.post(
        URL, headers=auth, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    names = {tool["name"] for tool in listed.json()["result"]["tools"]}
    assert "feishu_search_messages" in names
    assert not names & {"feishu_authorize", "schedule_propose", "schedule_list"}
    assert value(await client.post(URL, headers=auth, json=rpc("feishu_authorize")))["error"]
    # Once the job targets a group, the owner's grant is off limits for this run.
    task.payload = {**task.payload, "config": {**task.payload["config"], "target_chats": ["g"]}}
    await db_session.commit()
    assert (await client.post(URL, headers=auth, json=rpc("schedule_list"))).status_code == 403


async def grant_epoch(session, bot, user):
    from coreman.core.db.models import FeishuPersonalGrant

    row = await session.get(FeishuPersonalGrant, (bot.id, user.id), populate_existing=True)
    return row.context_epoch
