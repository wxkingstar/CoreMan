"""对话轮的云服务代理注入：就绪时有地址与令牌，审批人名单总是下发。"""

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.cloud_proxy import policy
from coreman.core.cloud_proxy.approvers import approver_logins
from coreman.core.db.models import BotApprover, User
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from tests.fakes.fake_relay import FakeRelay
from tests.integration.credential_helpers import owner
from tests.integration.worker_helpers import build_ctx


async def _approvers(session: AsyncSession, bot) -> None:  # type: ignore[no-untyped-def]
    users = [
        User(login_name="zeta", display_name="Z", source="sync"),
        User(login_name="alpha", display_name="A", source="sync"),
        User(login_name="gone", display_name="G", source="sync", status="disabled"),
        User(login_name="boot", display_name="B", source="bootstrap"),
    ]
    session.add_all(users)
    await session.flush()
    session.add_all(BotApprover(bot_id=bot.id, user_id=u.id) for u in users)
    await session.commit()


async def test_approver_logins_active_sorted(db_session: AsyncSession) -> None:
    bot, _, _, _ = await owner(db_session)
    assert await approver_logins(db_session, bot.id) == []
    await _approvers(db_session, bot)
    assert await approver_logins(db_session, bot.id) == ["alpha", "zeta"]


@pytest.mark.parametrize("ready", [True, False])
async def test_chat_turn_env(db_engine: AsyncEngine, db_session: AsyncSession, ready: bool) -> None:
    bot, user, task, cipher = await owner(db_session)
    await _approvers(db_session, bot)
    fake = FakeRelay("normal")
    ctx = build_ctx(db_engine, task, relay_client_factory=lambda _r: fake.client())
    ctx.cloud_proxy_ready = ready
    await ChatTaskHandler().run(ctx)
    env = fake.requests[0]["env_vars"]
    assert env["COREMAN_APPROVERS"] == "alpha,zeta"
    if not ready:
        assert not any(k.startswith("COREMAN_CLOUD_") for k in env)
        return
    assert env[policy.URL_ENV] == "http://localhost/api/runtime/cloud"
    cap = policy.read_capability(cipher, env[policy.TOKEN_ENV])
    assert (cap.user_id, cap.task_id, cap.bot_id, cap.origin_kind) == (
        user.id,
        task.id,
        bot.id,
        "chat",
    )


async def test_cron_run_env(db_engine: AsyncEngine, db_session: AsyncSession) -> None:
    from datetime import UTC, datetime

    from coreman.core.crypto import Cipher
    from coreman.core.db.session import make_session_factory
    from coreman.runtime.scheduler.cron import run_tick
    from coreman.runtime.worker.cron_handler import CronRunHandler
    from tests.integration.test_cron_handler import claim
    from tests.integration.test_cron_scheduler import job
    from tests.integration.worker_helpers import MASTER

    now = datetime.now(UTC)
    row = await job(db_session, now, target_chats=["test-group"])
    await run_tick(make_session_factory(db_engine), now)
    task = await claim(db_session)
    fake = FakeRelay("normal")
    ctx = build_ctx(db_engine, task, relay_client_factory=lambda _: fake.client())
    ctx.cloud_proxy_ready = True
    await CronRunHandler().run(ctx)
    env = fake.requests[0]["env_vars"]
    assert env["COREMAN_APPROVERS"] == ""
    cap = policy.read_capability(Cipher(MASTER), env[policy.TOKEN_ENV])
    assert (cap.origin_kind, cap.cron_job_id, cap.user_id) == ("cron", row.id, row.created_by)
