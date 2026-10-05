"""个人凭证测试公共件：造发起人、来源任务、本轮令牌与网页登录应用。"""

from __future__ import annotations

import uuid

from coreman.core.bus import tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import ChatSession, PlatformApp, Task, User, UserIdentity, UserReached
from coreman.core.personal_credentials import policy, service
from tests.integration.test_chat_handler import chat_task
from tests.integration.worker_helpers import seed_bot

FIELDS = [
    {"key": "DEMO_USERNAME", "label": "账号", "secret": False},
    {"key": "DEMO_PIN", "label": "PIN", "secret": True},
]
BODY = {"fields": FIELDS, "purpose": "查询你在 Demo 系统里的订单", "save": True}
VALUES = {"DEMO_USERNAME": "alice", "DEMO_PIN": "pin-778899"}
DEMO_RELAY_SESSION = uuid.UUID("00000000-0000-4000-8000-0000000000a1")


async def owner(session, *, platform="feishu", chat_type="single", reached=True):  # type: ignore[no-untyped-def]
    bot, _, cipher = await seed_bot(session)
    bot.platform = platform
    user = User(login_name="owner", display_name="本人", email="owner@example.test", source="sync")
    session.add(user)
    await session.flush()
    session.add(
        UserIdentity(
            user_id=user.id, platform=platform, platform_user_id="owner_pid", open_id="ou_owner"
        )
    )
    if reached:
        session.add(UserReached(bot_id=bot.id, user_id=user.id, platform_chat_id="oc_private"))
    await session.commit()
    chat_id = "oc_private" if chat_type == "single" else "oc_group"
    task = await chat_task(
        session, bot, "帮我查订单", sender="owner_pid", chat_type=chat_type, chat_id=chat_id
    )
    return bot, user, task, cipher


def cap_for(  # type: ignore[no-untyped-def]
    bot, user, task, relay_session_id: uuid.UUID | None = DEMO_RELAY_SESSION
) -> policy.Capability:
    message = task.payload["message"]
    return policy.Capability(
        task_id=task.id,
        bot_id=bot.id,
        user_id=user.id,
        origin_kind="chat",
        chat_id=message["chat_id"],
        chat_type=message["chat_type"],
        session_key=task.session_key,
        relay_session_id=relay_session_id,
        event_id=task.inbound_event_id,
        cron_job_id=None,
    )


async def seed_chat_session(  # type: ignore[no-untyped-def]
    session, bot, task, relay_session_id: uuid.UUID = DEMO_RELAY_SESSION
) -> None:
    """来源那一轮开轮时留下的会话映射：续接只在它仍指向同一个 relay 会话时才进行。"""
    session.add(
        ChatSession(
            bot_id=bot.id,
            session_key=task.session_key,
            relay_session_id=relay_session_id,
            backend="claude",
        )
    )
    await session.commit()


async def resume_task(session, origin: Task, user: User) -> Task:  # type: ignore[no-untyped-def]
    """续接任务：沿用来源那一轮的入站事件与会话，状态为运行中。"""
    task = await tasks.enqueue(
        session,
        NewTask(
            bot_id=origin.bot_id,
            kind=service.RESUME_KIND,
            user_id=user.id,
            session_key=origin.session_key,
            inbound_event_id=origin.inbound_event_id,
            payload={"credential_request_id": str(uuid.uuid4())},
        ),
    )
    assert task is not None
    task.status = "running"
    await session.commit()
    return task


def cron_cap(bot, user, task) -> policy.Capability:  # type: ignore[no-untyped-def]
    job_id = uuid.uuid4()
    return policy.Capability(
        task_id=task.id,
        bot_id=bot.id,
        user_id=user.id,
        origin_kind="cron",
        chat_id=f"cron:{job_id}",
        chat_type="cron",
        session_key=None,
        relay_session_id=None,
        event_id=None,
        cron_job_id=job_id,
    )


async def login_app(session, platform: str) -> None:  # type: ignore[no-untyped-def]
    session.add(
        PlatformApp(
            platform=platform,
            name=f"{platform}-login",
            capabilities=["login"],
            corp_id="ww_demo" if platform == "wecom" else None,
            app_id="cli_login" if platform == "feishu" else None,
            secret_enc="unused",
        )
    )
    await session.commit()
