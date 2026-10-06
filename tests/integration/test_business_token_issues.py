"""Issued business tokens are recorded so a downstream token id traces back to its turn."""

from datetime import UTC, datetime

import jwt
import pytest
import respx
from sqlalchemy import select

from coreman.core.auth.provider_config import HTTPTokenProviderConfig
from coreman.core.auth.token_issues import IssuedRecord, record_issues
from coreman.core.auth.token_providers import jwt_id
from coreman.core.db.models import (
    Bot,
    BusinessSystem,
    BusinessTokenIssue,
    ChatLog,
    User,
    UserIdentity,
)
from coreman.core.db.session import make_session_factory
from coreman.runtime.scheduler.cron import run_tick
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from coreman.runtime.worker.cron_handler import CronRunHandler
from tests.fakes.fake_relay import FakeRelay
from tests.integration.test_chat_handler import chat_task
from tests.integration.test_cron_handler import claim
from tests.integration.test_cron_scheduler import job
from tests.integration.worker_helpers import build_ctx, seed_bot

PROVIDERS = {
    "company": HTTPTokenProviderConfig(
        token_url="https://identity.example/token",
        client_id="agent",
        client_secret="worker-service-secret",
    )
}


ISSUER_KEY = "issuer-side-test-key-0123456789abcdef"


def external_jwt(jti: str) -> str:
    # 签名密钥无关紧要：CoreMan 只读 jti 做记录，不验外部令牌。
    return jwt.encode({"sub": "creator", "jti": jti}, ISSUER_KEY, algorithm="HS256")


def test_jwt_id_reads_jti_and_ignores_opaque_tokens():
    assert jwt_id(external_jwt("abc-123")) == "abc-123"
    assert jwt_id("external-opaque-worker-token") is None
    assert jwt_id("a.b.c") is None
    assert jwt_id(jwt.encode({"sub": "x"}, ISSUER_KEY, algorithm="HS256")) is None
    assert jwt_id(jwt.encode({"jti": "x" * 300}, ISSUER_KEY, algorithm="HS256")) is None


async def _worker_turn(db_engine, db_session, kind, *, provider, token_value):
    if kind == "chat":
        bot, _, _ = await seed_bot(db_session)
        user = await db_session.get(User, bot.created_by)
        db_session.add(UserIdentity(user_id=user.id, platform="wecom", platform_user_id="zs"))
    else:
        now = datetime.now(UTC)
        row = await job(db_session, now, target_chats=["test-group"])
        bot = await db_session.get(Bot, row.bot_id)
    db_session.add(
        BusinessSystem(key="erp", name="ERP", default_for_all_bots=True, token_provider=provider)
    )
    await db_session.commit()
    if kind == "chat":
        task = await chat_task(db_session, bot, "查 ERP", sender="zs")
    else:
        await run_tick(make_session_factory(db_engine), now)
        task = await claim(db_session)
    fake = FakeRelay()
    ctx = build_ctx(db_engine, task, relay_client_factory=lambda _: fake.client())
    ctx.business_token_providers = PROVIDERS
    with respx.mock:
        respx.post("https://identity.example/token").respond(
            200, json={"access_token": token_value, "token_type": "Bearer", "expires_in": 600}
        )
        handler = ChatTaskHandler() if kind == "chat" else CronRunHandler()
        await handler.run(ctx)
        await ctx.chat_logs.drain(5)
    return bot, task, fake.requests[0]["env_vars"]


@pytest.mark.parametrize("kind", ["chat", "cron"])
async def test_worker_records_external_token_id_with_turn(db_engine, db_session, kind):
    bot, task, env = await _worker_turn(
        db_engine, db_session, kind, provider="company", token_value=external_jwt("jti-ext-1")
    )
    row = (await db_session.scalars(select(BusinessTokenIssue))).one()
    assert (row.purpose, row.task_id, row.bot_id) == (kind, task.id, bot.id)
    assert (row.subject, row.system_key, row.provider, row.audience) == (
        "creator",
        "erp",
        "company",
        "erp",
    )
    assert row.token_id == "jti-ext-1"
    assert row.expires_at > datetime.now(UTC)
    # 记录不含令牌本身。
    assert env["BOT_TOKEN_ERP"] not in repr(
        {c.key: getattr(row, c.key) for c in BusinessTokenIssue.__table__.columns}
    )
    log = (await db_session.scalars(select(ChatLog).where(ChatLog.task_id == task.id))).one()
    assert log.task_id == row.task_id


async def test_opaque_external_token_is_recorded_without_id(db_engine, db_session):
    await _worker_turn(
        db_engine,
        db_session,
        "chat",
        provider="company",
        token_value="external-opaque-worker-token",
    )
    row = (await db_session.scalars(select(BusinessTokenIssue))).one()
    assert row.token_id is None and row.system_key == "erp"


async def test_builtin_token_id_matches_issued_jwt(db_engine, db_session):
    _, task, env = await _worker_turn(
        db_engine, db_session, "chat", provider="builtin", token_value="unused"
    )
    claims = jwt.decode(env["BOT_TOKEN_ERP"], options={"verify_signature": False})
    row = (await db_session.scalars(select(BusinessTokenIssue))).one()
    assert (row.provider, row.token_id, row.task_id) == ("builtin", claims["jti"], task.id)


async def test_failed_issuance_records_nothing(db_engine, db_session):
    bot, _, _ = await seed_bot(db_session)
    user = await db_session.get(User, bot.created_by)
    db_session.add(UserIdentity(user_id=user.id, platform="wecom", platform_user_id="zs"))
    db_session.add(
        BusinessSystem(key="erp", name="ERP", default_for_all_bots=True, token_provider="company")
    )
    await db_session.commit()
    task = await chat_task(db_session, bot, "查 ERP", sender="zs")
    fake = FakeRelay()
    ctx = build_ctx(db_engine, task, relay_client_factory=lambda _: fake.client())
    ctx.business_token_providers = PROVIDERS
    with respx.mock:
        respx.post("https://identity.example/token").respond(403)
        await ChatTaskHandler().run(ctx)
        await ctx.chat_logs.drain(5)
    assert "BOT_TOKEN_ERP" not in fake.requests[0]["env_vars"]
    assert (await db_session.scalars(select(BusinessTokenIssue))).all() == []


async def test_record_issues_failure_does_not_break_caller(db_session, monkeypatch):
    from coreman.core.auth import token_issues

    def boom(*_args, **_kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(token_issues, "insert", boom)
    await record_issues(
        db_session,
        [IssuedRecord("erp", "builtin", "erp", "j1", int(datetime.now(UTC).timestamp()) + 60)],
        purpose="chat",
        subject="creator",
    )
    await db_session.commit()
    assert (await db_session.scalars(select(BusinessTokenIssue))).all() == []
