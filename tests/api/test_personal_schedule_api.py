"""技能管理本人定时任务：凭据只在本人已验证私聊里下发，只能管自己的任务，生效要本人点卡片。"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from coreman.core import personal_schedules as schedules
from coreman.core.bus import tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.chat import sessions
from coreman.core.db.models import (
    CronJob,
    CronRun,
    InboundEvent,
    InteractionState,
    OutboxItem,
    User,
    UserIdentity,
    UserReached,
)
from coreman.core.prompting.env_vars import is_reserved_key
from coreman.runtime.worker.card_actions import CardActionHandler
from coreman.runtime.worker.chat import schedules as chat_schedules
from tests.api.test_feishu_personal import setup as feishu_setup
from tests.api.test_personal_schedules import click as feishu_click
from tests.api.test_personal_schedules import run_due
from tests.integration.test_chat_handler import chat_task
from tests.integration.worker_helpers import build_ctx

URL = "/api/runtime/personal-schedules"
PRECHECK = 'def should_trigger(ctx):\n    return {"trigger": True, "reason": "ok"}\n'


async def auth(app, task, user, *, session_id=None):
    async with app.state.session_factory() as session:
        info = await sessions.get_or_create(
            session,
            bot_id=task.bot_id,
            session_key=task.session_key,
            backend="claude",
            ttl_hours=72,
            speaker_user_id=user.id,
        )
        await session.commit()
    token = schedules.issue_capability(
        app.state.cipher,
        task_id=task.id,
        user_id=str(user.id),
        base_session_id=session_id or info.relay_session_id,
    )
    return {"Authorization": "Bearer " + token}


async def colleague(session, bot, *, reached=True, platform="feishu", name="同事"):
    user = User(login_name=f"c-{uuid.uuid4().hex[:6]}", display_name=name, source="sync")
    session.add(user)
    await session.flush()
    session.add(
        UserIdentity(user_id=user.id, platform=platform, platform_user_id=f"p-{user.login_name}")
    )
    if reached:
        session.add(UserReached(bot_id=bot.id, user_id=user.id, platform_chat_id=f"dm-{user.id}"))
    await session.commit()
    return user


async def spoke_in_group(session, bot, sender, chat_id="oc_team", platform="feishu"):
    session.add(
        InboundEvent(
            bot_id=bot.id,
            platform=platform,
            platform_msg_id=str(uuid.uuid4()),
            kind="message",
            chat_type="group",
            chat_id=chat_id,
            sender_platform_user_id=sender,
            payload={},
            reply_context={},
        )
    )
    await session.commit()


def body(**kw):
    return {"name": "日报汇总", "prompt": "汇总今天的日报", "cron_expression": "0 18 * * 1-5", **kw}


async def test_only_a_verified_private_chat_gets_the_capability(db_session, app, db_engine):
    from tests.api.test_feishu_personal_worker import intake_for

    bot, user, task = await feishu_setup(db_session, app)
    intake = await intake_for(db_session, bot, task, "每天提醒我写日报")
    ctx = build_ctx(db_engine, task)
    forged = {"COREMAN_SCHEDULE_TOKEN": "forged", "KEEP": "1"}
    prompt, env = await chat_schedules.configure(
        db_session, ctx, intake, uuid.uuid4(), "你是助手", forged
    )
    assert "## 本人定时任务" in prompt and "coreman-cron" in prompt
    assert env["COREMAN_SCHEDULE_URL"] == "http://localhost" + URL
    capability = schedules.read_capability(app.state.cipher, env["COREMAN_SCHEDULE_TOKEN"])
    assert (capability.task_id, capability.actor) == (task.id, str(user.id))
    assert env["KEEP"] == "1"

    group_task = await chat_task(
        db_session, bot, "每天提醒我", sender="human", chat_type="group", chat_id="oc_group"
    )
    group_intake = await intake_for(db_session, bot, group_task, "每天提醒我")
    prompt, env = await chat_schedules.configure(
        db_session, build_ctx(db_engine, group_task), group_intake, uuid.uuid4(), "", forged
    )
    assert prompt == "" and "COREMAN_SCHEDULE_TOKEN" not in env
    # 机器人自己配的同名变量不得冒充这枚凭据，定时执行也就拿不到它。
    assert is_reserved_key("COREMAN_SCHEDULE_TOKEN") and is_reserved_key("COREMAN_SCHEDULE_URL")


async def test_capability_is_bound_to_the_live_private_turn(client, app, db_session):
    bot, user, task = await feishu_setup(db_session, app)
    assert (await client.get(URL)).status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert (await client.get(URL, headers=bad)).status_code == 401
    stale = await auth(app, task, user, session_id=uuid.uuid4())
    assert (await client.get(URL, headers=stale)).status_code == 403
    headers = await auth(app, task, user)
    listed = await client.get(URL, headers=headers)
    assert listed.status_code == 200 and listed.json()["data"]["items"] == []
    task.status = "succeeded"
    await db_session.commit()
    assert (await client.get(URL, headers=headers)).status_code == 403
    group_task = await chat_task(
        db_session, bot, "每天提醒我", sender="human", chat_type="group", chat_id="oc_group"
    )
    group = await auth(app, group_task, user)
    assert (await client.get(URL, headers=group)).status_code == 403


async def test_draft_validates_recipients_and_sends_only_a_card(client, app, db_session):
    bot, user, task = await feishu_setup(db_session, app)
    headers = await auth(app, task, user)
    stranger = await colleague(db_session, bot, reached=False)
    response = await client.post(
        URL + "/drafts", headers=headers, json=body(recipient_user_ids=[str(stranger.id)])
    )
    assert response.status_code == 422
    assert response.json()["errors"] == [{"type": "recipient_unreachable"}]
    response = await client.post(
        URL + "/drafts", headers=headers, json=body(recipient_chat_ids=["oc_team"])
    )
    assert response.json()["errors"] == [{"type": "chat_not_allowed"}]
    for bad in (
        body(cron_expression="*/5 * * * *"),
        body(precheck_script="import os"),
        body(include_self=False),
        body(user_id=str(stranger.id)),
    ):
        assert (await client.post(URL + "/drafts", headers=headers, json=bad)).status_code == 422
    assert await db_session.scalar(select(InteractionState)) is None

    mate = await colleague(db_session, bot, name="小王")
    await spoke_in_group(db_session, bot, "human")
    options = (await client.get(URL + "/recipients", headers=headers, params={"q": "小王"})).json()
    assert [u["id"] for u in options["data"]["users"]] == [str(mate.id)]
    assert [c["id"] for c in options["data"]["chats"]] == []
    everything = (await client.get(URL + "/recipients", headers=headers)).json()["data"]
    assert [c["id"] for c in everything["chats"]] == ["oc_team"]
    response = await client.post(
        URL + "/drafts",
        headers=headers,
        json=body(
            recipient_user_ids=[str(mate.id)],
            recipient_chat_ids=["oc_team"],
            precheck_script=PRECHECK,
        ),
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "awaiting_confirmation"
    assert data["recipients"] == ["我（私聊）", "小王", "群 oc_team"]
    assert await db_session.scalar(select(CronJob)) is None
    card = (await db_session.scalars(select(OutboxItem))).one()
    content = str(card.payload["card"])
    assert "小王" in content and "执行前检查" in content and "不会使用你的飞书" in content


async def confirm(db_session, db_engine, bot, task):
    state = await db_session.scalar(
        select(InteractionState).where(InteractionState.status == "open")
    )
    pressed = await feishu_click(db_session, bot, task, "confirm", state.id)
    await CardActionHandler().run(build_ctx(db_engine, pressed))
    await db_session.refresh(state)
    return state


async def test_confirmed_draft_runs_as_owner_and_delivers_to_recipients(
    client, app, db_session, db_engine
):
    bot, user, task = await feishu_setup(db_session, app)
    headers = await auth(app, task, user)
    mate = await colleague(db_session, bot)
    await spoke_in_group(db_session, bot, "human")
    response = await client.post(
        URL + "/drafts",
        headers=headers,
        json=body(
            recipient_user_ids=[str(mate.id)],
            recipient_chat_ids=["oc_team"],
            precheck_script=PRECHECK,
        ),
    )
    assert response.status_code == 200
    state = await confirm(db_session, db_engine, bot, task)
    assert state.status == "submitted"
    job = await db_session.scalar(select(CronJob))
    assert job.created_by == user.id and job.execution_mode == schedules.MODE
    assert job.target_users == [user.id, mate.id] and job.target_chats == ["oc_team"]
    assert job.precheck_script == PRECHECK and job.enabled

    listed = (await client.get(URL, headers=headers)).json()["data"]
    assert listed["items"][0]["recipient_user_ids"] == [str(mate.id)]
    assert listed["items"][0]["include_self"] is True

    await db_session.execute(OutboxItem.__table__.delete())
    await db_session.commit()
    run_task, fake = await run_due(db_session, db_engine, job)
    assert run_task.user_id == user.id
    run = await db_session.scalar(select(CronRun))
    assert run.status == "success", run.error_message
    targets = {
        item.target.get("chat_id") for item in (await db_session.scalars(select(OutboxItem))).all()
    }
    assert targets == {"oc_private", f"dm-{mate.id}", "oc_team"}
    # 发给别人的任务不挂本人的飞书个人工具。
    assert not any(k.startswith("COREMAN_FEISHU_PERSONAL_") for k in fake.requests[0]["env_vars"])


async def test_group_membership_is_rechecked_before_each_run(client, app, db_session, db_engine):
    bot, user, task = await feishu_setup(db_session, app)
    await spoke_in_group(db_session, bot, "human")
    headers = await auth(app, task, user)
    await client.post(URL + "/drafts", headers=headers, json=body(recipient_chat_ids=["oc_team"]))
    await confirm(db_session, db_engine, bot, task)
    job = await db_session.scalar(select(CronJob))
    await db_session.execute(
        InboundEvent.__table__.delete().where(InboundEvent.chat_id == "oc_team")
    )
    await db_session.commit()
    user_row = await db_session.get(User, user.id)
    with pytest.raises(Exception, match="personal_schedule_chat_unavailable"):
        await schedules.require_personal(db_session, job, bot, user_row)


async def test_update_pause_run_delete_and_history(client, app, db_session, db_engine):
    bot, user, task = await feishu_setup(db_session, app)
    headers = await auth(app, task, user)
    await client.post(URL + "/drafts", headers=headers, json=body())
    await confirm(db_session, db_engine, bot, task)
    job = await db_session.scalar(select(CronJob))
    job_id = str(job.id)

    # 修改也要确认；确认前任务被改过（版本变了）就不生效。
    response = await client.post(
        URL + "/drafts", headers=headers, json=body(job_id=job_id, name="周报汇总")
    )
    assert response.status_code == 200
    job.prompt = "别人改过"
    await db_session.commit()
    state = await confirm(db_session, db_engine, bot, task)
    assert state.status == "cancelled"
    await db_session.refresh(job)
    assert job.name == "日报汇总"
    await client.post(URL + "/drafts", headers=headers, json=body(job_id=job_id, name="周报汇总"))
    await confirm(db_session, db_engine, bot, task)
    await db_session.refresh(job)
    assert (job.name, job.prompt) == ("周报汇总", "汇总今天的日报")

    paused = (await client.post(f"{URL}/{job_id}/pause", headers=headers)).json()["data"]
    assert paused["enabled"] is False
    assert (await client.post(f"{URL}/{job_id}/run", headers=headers)).status_code == 409
    resumed = await client.post(f"{URL}/{job_id}/resume", headers=headers)
    assert resumed.json()["data"]["status"] == "awaiting_confirmation"
    await db_session.refresh(job)
    assert job.enabled is False
    await confirm(db_session, db_engine, bot, task)
    await db_session.refresh(job)
    assert job.enabled is True

    ran = (await client.post(f"{URL}/{job_id}/run", headers=headers)).json()["data"]
    assert ran["run_requested"] is True
    history = (await client.get(f"{URL}/{job_id}/runs", headers=headers)).json()["data"]
    assert history["items"] == [] and history["job"]["id"] == job_id

    tested = (
        await client.post(URL + "/precheck/test", headers=headers, json={"script": PRECHECK})
    ).json()["data"]
    assert tested == {"status": "success", "trigger": True, "reason": "ok", "prompt_appendix": ""}
    broken = (
        await client.post(URL + "/precheck/test", headers=headers, json={"script": "import os"})
    ).json()["data"]
    assert broken["status"] == "failed_precheck"

    assert (await client.delete(f"{URL}/{job_id}", headers=headers)).status_code == 200
    assert await db_session.scalar(select(CronJob)) is None


async def test_cannot_touch_someone_elses_job(client, app, db_session, db_engine):
    bot, user, task = await feishu_setup(db_session, app)
    other = await colleague(db_session, bot)
    job = CronJob(
        bot_id=bot.id,
        created_by=other.id,
        name="别人的",
        execution_mode=schedules.MODE,
        reminder_chat_id=f"dm-{other.id}",
        cron_expression="0 9 * * *",
        timezone=schedules.TIMEZONE,
        prompt="x",
        target_users=[other.id],
        next_run_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.add(job)
    await db_session.commit()
    headers = await auth(app, task, user)
    for method, path in (
        ("post", f"/{job.id}/pause"),
        ("post", f"/{job.id}/run"),
        ("post", f"/{job.id}/resume"),
        ("get", f"/{job.id}/runs"),
        ("delete", f"/{job.id}"),
    ):
        assert (await getattr(client, method)(URL + path, headers=headers)).status_code == 404
    response = await client.post(URL + "/drafts", headers=headers, json=body(job_id=str(job.id)))
    assert response.status_code == 404
    assert (await client.get(URL, headers=headers)).json()["data"]["items"] == []


async def wecom_click(db_session, db_engine, bot, card_task_id, verb, clicker):
    from tests.api.test_wecom_personal import WECOM_BOT

    body_ = {
        "msgid": f"e-{uuid.uuid4()}",
        "aibotid": WECOM_BOT,
        "chattype": "single",
        "from": {"userid": clicker},
        "msgtype": "event",
        "event": {"eventtype": "template_card_event"},
    }
    action = {"task_id": card_task_id, "card_type": "button_interaction", "event_key": verb}
    event = InboundEvent(
        bot_id=bot.id,
        platform="wecom",
        platform_msg_id=body_["msgid"],
        kind="card_action",
        chat_type="single",
        chat_id=clicker,
        sender_platform_user_id=clicker,
        payload={"card_action": action, "raw": {"cmd": "aibot_event_callback", "body": body_}},
        reply_context={"req_id": "click-req"},
    )
    db_session.add(event)
    await db_session.flush()
    await tasks.enqueue(
        db_session,
        NewTask(
            bot_id=bot.id,
            kind="card_action",
            lane="fast",
            payload={"card_action": action, "platform_user_id": clicker, "chat_id": clicker},
            session_key=clicker,
            inbound_event_id=event.id,
            dedupe_key=f"inbound:{event.id}",
        ),
    )
    await db_session.commit()
    task = await tasks.claim(db_session, lane="fast", instance_id="worker-test")
    await db_session.commit()
    await CardActionHandler().run(build_ctx(db_engine, task))
    await db_session.refresh(task)
    return task.result["card"]


@pytest.mark.parametrize("clicker", ["owner", "other"])
async def test_wecom_private_chat_confirms_with_a_button_card(
    client, app, db_session, db_engine, clicker
):
    from tests.api.test_wecom_personal import SENDER
    from tests.api.test_wecom_personal import setup as wecom_setup

    bot, user, task = await wecom_setup(db_session, app)
    headers = await auth(app, task, user)
    await spoke_in_group(db_session, bot, SENDER, chat_id="wr-group", platform="wecom")
    response = await client.post(
        URL + "/drafts",
        headers=headers,
        json=body(run_at=None, cron_expression="0 9 * * 1", recipient_chat_ids=["wr-group"]),
    )
    assert response.status_code == 200, response.text
    details, card = (await db_session.scalars(select(OutboxItem).order_by(OutboxItem.id))).all()
    assert "汇总今天的日报" in details.payload["markdown"]
    template = card.payload["card"]
    assert template["card_type"] == "button_interaction"
    assert [b["key"] for b in template["button_list"]] == ["confirm", "cancel"]
    assert len(template["task_id"].encode()) <= 128
    who = SENDER if clicker == "owner" else "wo-someone-else"
    result = await wecom_click(db_session, db_engine, bot, template["task_id"], "confirm", who)
    job = await db_session.scalar(select(CronJob))
    if clicker == "other":
        assert result == "ignored" and job is None
        return
    assert result == "personal_schedule_created"
    assert job.created_by == user.id and job.target_chats == ["wr-group"]
    assert job.reminder_chat_id == SENDER
    update = await db_session.scalar(select(OutboxItem).where(OutboxItem.kind == "card_update"))
    assert update.payload["card"]["main_title"]["title"] == "定时任务已生效"
    assert len(update.payload["card"]["sub_title_text"]) <= 110
