"""个人凭证测试公共件：造发起人、来源任务、本轮令牌与网页登录应用。"""

from __future__ import annotations

import uuid

from coreman.core.db.models import PlatformApp, User, UserIdentity, UserReached
from coreman.core.personal_credentials import policy
from tests.integration.test_chat_handler import chat_task
from tests.integration.worker_helpers import seed_bot

FIELDS = [
    {"key": "DEMO_USERNAME", "label": "账号", "secret": False},
    {"key": "DEMO_PIN", "label": "PIN", "secret": True},
]
BODY = {"fields": FIELDS, "purpose": "查询你在 Demo 系统里的订单"}
VALUES = {"DEMO_USERNAME": "alice", "DEMO_PIN": "pin-778899"}


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


def cap_for(bot, user, task) -> policy.Capability:  # type: ignore[no-untyped-def]
    message = task.payload["message"]
    return policy.Capability(
        task_id=task.id,
        bot_id=bot.id,
        user_id=user.id,
        origin_kind="chat",
        chat_id=message["chat_id"],
        chat_type=message["chat_type"],
        session_key=task.session_key,
        event_id=task.inbound_event_id,
        cron_job_id=None,
    )


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
