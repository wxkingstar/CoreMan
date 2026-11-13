"""助理记录的开通状态：只有 active 能跑轮次；新建的记录从 provisioning 开始；普通记录没有这一列。"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import runtime
from coreman.core.assistants.records import LOCKED_FIELDS, locked_changes
from tests.integration.assistant_helpers import seed_assistant, seed_standard


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (None, "provisioning"),
        ("provisioning", "provisioning"),
        ("deprovisioning", "deprovisioning"),
        ("deprovisioned", "deprovisioning"),
    ],
)
async def test_only_an_active_assistant_can_run_a_turn(
    db_session: AsyncSession, state: str | None, reason: str
) -> None:
    bot, _ = await seed_assistant(db_session)
    bot.assistant_state = state
    assert await runtime.turn_blocker(db_session, bot) == reason
    assert "暂停" not in runtime.pause_message(reason)


async def test_an_active_assistant_goes_on_to_the_runtime_checks(db_session: AsyncSession) -> None:
    bot, _ = await seed_assistant(db_session)
    assert bot.assistant_state == "active"
    # 没绑运行时：落到原有的运行时检查。
    assert await runtime.turn_blocker(db_session, bot) == "runtime_missing"


async def test_new_records_start_as_provisioning_and_the_state_is_locked(
    db_session: AsyncSession,
) -> None:
    from coreman.core.assistants.records import new_assistant_bot
    from coreman.core.db.models import PlatformApp, User

    owner = User(login_name="zhangsan", display_name="张三")
    app = PlatformApp(
        platform="feishu", name="个人助理", capabilities=["assistant"], secret_enc="x"
    )
    db_session.add_all([owner, app])
    await db_session.flush()
    bot = new_assistant_bot(
        owner, app, relay_server_id=None, model="m", working_dir="/w", credentials_enc=""
    )
    assert bot.assistant_state == "provisioning"
    assert "assistant_state" in LOCKED_FIELDS
    assert locked_changes(bot, {"assistant_state": "active"}) == ["assistant_state"]


async def test_ordinary_bots_cannot_carry_a_state(db_session: AsyncSession) -> None:
    standard = await seed_standard(db_session)
    assert standard.assistant_state is None
    standard.assistant_state = "active"
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_one_unfinished_lifecycle_per_person(db_session: AsyncSession) -> None:
    from coreman.core.db.models import AssistantLifecycle
    from coreman.core.timeutils import utcnow

    bot, owner = await seed_assistant(db_session)
    app_id, owner_id = bot.platform_app_id, owner.id
    assert app_id is not None

    def flow(**kw: object) -> AssistantLifecycle:
        values: dict[str, object] = {"action": "provision", "step": "offered", **kw}
        return AssistantLifecycle(owner_id=owner_id, platform_app_id=app_id, **values)

    db_session.add(flow(finished_at=utcnow(), outcome="expired"))
    db_session.add(flow())
    await db_session.flush()
    db_session.add(flow(action="deprovision", step="drain"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
    db_session.add(flow(step="nowhere"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
