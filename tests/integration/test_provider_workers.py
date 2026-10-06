"""Exercise external issuance through actual chat/cron handlers, not just the helper."""

from datetime import UTC, datetime
from urllib.parse import parse_qs

import pytest
import respx
from sqlalchemy import select

from coreman.core.auth.provider_config import HTTPTokenProviderConfig
from coreman.core.chat.redaction import PLACEHOLDER
from coreman.core.db.models import Bot, BusinessSystem, ChatLog, User, UserIdentity
from coreman.core.db.session import make_session_factory
from coreman.runtime.scheduler.cron import run_tick
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from coreman.runtime.worker.cron_handler import CronRunHandler
from tests.fakes.fake_relay import FakeRelay
from tests.integration.test_chat_handler import chat_task, stream_of
from tests.integration.test_cron_handler import claim
from tests.integration.test_cron_scheduler import job
from tests.integration.worker_helpers import build_ctx, seed_bot


@pytest.mark.parametrize("kind", ["chat", "cron"])
async def test_external_issuer_reaches_worker_and_token_is_redacted(db_engine, db_session, kind):
    if kind == "chat":
        bot, _, _ = await seed_bot(db_session, env={"COREMAN_USER_SUBJECT": "forged"})
        user = await db_session.get(User, bot.created_by)
        db_session.add(UserIdentity(user_id=user.id, platform="wecom", platform_user_id="zs"))
    else:
        now = datetime.now(UTC)
        row = await job(db_session, now, target_chats=["test-group"])
        bot = await db_session.get(Bot, row.bot_id)
    bot.sse_timeout_seconds = 43200
    db_session.add(
        BusinessSystem(key="erp", name="ERP", default_for_all_bots=True, token_provider="company")
    )
    await db_session.commit()
    if kind == "chat":
        task = await chat_task(db_session, bot, "查 ERP", sender="zs")
    else:
        await run_tick(make_session_factory(db_engine), now)
        task = await claim(db_session)
    fake = FakeRelay("leaks_credentials")
    ctx = build_ctx(db_engine, task, relay_client_factory=lambda _: fake.client())
    ctx.business_token_providers = {
        "company": HTTPTokenProviderConfig(
            token_url="https://identity.example/token",
            client_id="agent",
            client_secret="worker-service-secret",
            max_token_ttl_seconds=28800,
        )
    }
    with respx.mock:
        route = respx.post("https://identity.example/token").respond(
            200,
            json={
                "access_token": "external-opaque-worker-token",
                "token_type": "Bearer",
                "expires_in": 1200,
            },
        )
        handler = ChatTaskHandler() if kind == "chat" else CronRunHandler()
        await handler.run(ctx)
        await ctx.chat_logs.drain(5)
    assert parse_qs(route.calls.last.request.content.decode()) == {
        "username": ["creator"],
        "audience": ["erp"],
        "expires_in": ["28800"],
    }
    env = fake.requests[0]["env_vars"]
    assert env["COREMAN_USER_SUBJECT"] == "creator"
    assert env["BOT_TOKEN_ERP"] == "external-opaque-worker-token"
    assert "worker-service-secret" not in repr(fake.requests)
    log = (await db_session.scalars(select(ChatLog).where(ChatLog.task_id == task.id))).one()
    assert "external-opaque-worker-token" not in (log.response_content or "")
    assert PLACEHOLDER in (log.response_content or "")
    if kind == "chat":
        stream = await stream_of(db_session, task.id)
        assert "external-opaque-worker-token" not in (stream.final_text or "")
