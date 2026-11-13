"""平台来源的轮次拿个人工具：身份来自开通时记下的设置，每次用到都重新核对，对不上就不放行。"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import platform_turns
from coreman.core.assistants.kinds import HANDOVER_KIND, PLATFORM_TURN_KINDS, SUBTASK_KIND
from coreman.core.bus import tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import Bot, FeishuPersonalGrant, PlatformApp, Task, User, UserIdentity
from coreman.core.feishu_personal import policy
from tests.integration.assistant_helpers import identified, seed_standard
from tests.integration.lifecycle_helpers import APP_ID, TENANT, active_assistant
from tests.integration.test_cron_handler import claim

pytestmark = pytest.mark.usefixtures("assistant_master")


async def running(session: AsyncSession, bot: Bot, kind: str = HANDOVER_KIND, **kw: Any) -> Task:
    """排一轮平台来源的任务并认领（scope 只给在跑的任务）。"""
    queued = await platform_turns.enqueue(
        session, bot, kind, {"note": "x"}, dedupe_key=f"t:{uuid.uuid4().hex}", **kw
    )
    await session.commit()
    assert queued is not None
    claimed = await claim(session)
    assert claimed.id == queued.id
    return claimed


async def ready(session: AsyncSession) -> tuple[Bot, User, Task]:
    bot, owner, _ = await active_assistant(session)
    await identified(session, bot)
    return bot, owner, await running(session, bot)


def test_platform_kinds_count_as_private_turns() -> None:
    assert set(PLATFORM_TURN_KINDS) <= set(policy.PRIVATE_TURN_KINDS)


async def test_a_platform_turn_acts_as_the_owner(db_session: AsyncSession) -> None:
    bot, owner, task = await ready(db_session)
    scope = await policy.task_scope(db_session, task.id, str(owner.id))
    assert (
        scope.user_id,
        scope.tenant_key,
        scope.app_id,
        scope.open_id,
        scope.platform_user_id,
        scope.chat_id,
    ) == (owner.id, TENANT, APP_ID, "ou_owner", "owner_pid", "oc_private")
    assert scope.task.id == task.id and scope.bot.id == bot.id
    # 没有入站事件，但不是定时任务：和私聊一轮一样按会话围住、能发起授权。
    assert scope.event is None and scope.platform and not scope.scheduled
    # MCP 入口走的 capability_scope 是同一条路。
    again = await policy.capability_scope(db_session, task.id, str(owner.id))
    assert again.platform and again.user_id == owner.id


async def test_a_job_session_qualifies(db_session: AsyncSession) -> None:
    bot, owner, _ = await active_assistant(db_session)
    await identified(db_session, bot)
    task = await running(db_session, bot, SUBTASK_KIND, session_key="oc_private#job:9")
    scope = await policy.task_scope(db_session, task.id, str(owner.id))
    assert scope.platform and scope.task.session_key == "oc_private#job:9"
    assert scope.chat_id == "oc_private"


async def test_only_the_owner_is_the_actor(db_session: AsyncSession) -> None:
    _, _, task = await ready(db_session)
    with pytest.raises(ValueError, match="personal_actor_mismatch"):
        await policy.task_scope(db_session, task.id, str(uuid.uuid4()))


@pytest.mark.parametrize(
    "change",
    [
        "deprovisioning",
        "disabled",
        "no_identity",
        "owner_disabled",
        "rebound",
        "foreign_session",
        "other_user",
        "not_running",
    ],
)
async def test_any_broken_condition_refuses(db_session: AsyncSession, change: str) -> None:
    bot, owner, task = await ready(db_session)
    if change == "deprovisioning":
        bot.assistant_state = "deprovisioning"
    elif change == "disabled":
        bot.enabled = False
    elif change == "no_identity":
        await db_session.execute(text("UPDATE assistant_settings SET tenant_key = ''"))
    elif change == "owner_disabled":
        owner.status = "disabled"
    elif change == "rebound":
        # 通讯录里这个人的飞书身份换了：记下的身份不再是他。
        await db_session.execute(
            update(UserIdentity)
            .where(UserIdentity.user_id == owner.id)
            .values(platform_user_id="someone_else")
        )
    elif change == "foreign_session":
        await db_session.execute(
            update(Task).where(Task.id == task.id).values(session_key="oc_other")
        )
    elif change == "other_user":
        await db_session.execute(
            update(Task).where(Task.id == task.id).values(user_id=uuid.uuid4())
        )
    else:
        await db_session.execute(update(Task).where(Task.id == task.id).values(status="queued"))
    await db_session.commit()
    with pytest.raises(ValueError):
        await policy.task_scope(db_session, task.id, str(owner.id))


async def test_a_grant_for_another_identity_is_refused_and_left_alone(
    db_session: AsyncSession,
) -> None:
    """授权行与记下的身份对不上：拒绝，但不去动授权行（放行的话后面的锁定步骤会把令牌清掉）。"""
    bot, owner, task = await ready(db_session)
    grant = await db_session.get(FeishuPersonalGrant, (bot.id, owner.id))
    assert grant is not None
    grant.open_id = "ou_someone_else"
    await db_session.commit()
    with pytest.raises(ValueError, match="personal_grant_required"):
        await policy.task_scope(db_session, task.id, str(owner.id))
    kept = await db_session.get(FeishuPersonalGrant, (bot.id, owner.id), populate_existing=True)
    assert kept is not None and kept.status == "connected" and kept.token_enc


async def test_without_a_grant_the_app_comes_from_the_assistant_app(
    db_session: AsyncSession,
) -> None:
    """授权被撤销后这一轮仍认得出是谁（工具会如实报没有授权），应用取自助理应用。"""
    bot, owner, task = await ready(db_session)
    grant = await db_session.get(FeishuPersonalGrant, (bot.id, owner.id))
    await db_session.delete(grant)
    await db_session.commit()
    scope = await policy.task_scope(db_session, task.id, str(owner.id))
    app = await db_session.get(PlatformApp, bot.platform_app_id)
    assert app is not None and scope.app_id == app.app_id and scope.platform


async def test_a_forged_platform_task_on_a_standard_bot_is_refused(
    db_session: AsyncSession,
) -> None:
    standard = await seed_standard(db_session)
    forged = await tasks.enqueue(
        db_session,
        NewTask(
            bot_id=standard.id,
            kind=HANDOVER_KIND,
            user_id=standard.created_by,
            session_key="oc_private",
        ),
    )
    await db_session.commit()
    assert forged is not None
    claimed = await claim(db_session)
    with pytest.raises(ValueError, match="platform_turn_required"):
        await policy.task_scope(db_session, claimed.id, str(standard.created_by))


async def test_a_chat_task_without_an_inbound_event_is_refused(db_session: AsyncSession) -> None:
    bot, owner, _ = await active_assistant(db_session)
    await identified(db_session, bot)
    bare = await tasks.enqueue(
        db_session,
        NewTask(bot_id=bot.id, kind="chat", user_id=owner.id, session_key="oc_private"),
    )
    await db_session.commit()
    assert bare is not None
    claimed = await claim(db_session)
    with pytest.raises(ValueError, match="platform_turn_required"):
        await policy.task_scope(db_session, claimed.id, str(owner.id))
    # 卡片回调直接调的这一条同样不放行，也不去按空主键查入站事件。
    with pytest.raises(ValueError, match="feishu_private_chat_required"):
        await policy.verified_origin_scope(db_session, claimed, str(owner.id))
