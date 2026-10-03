"""提交之后在原会话续接：发言者是发起人、消息里只有键名、凭证在 env 里、企微走主动推送。"""

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.bus import tasks
from coreman.core.db.models import (
    ChatLog,
    ChatSession,
    CredentialRequest,
    OutboxItem,
    RelayServer,
    Task,
)
from coreman.core.personal_credentials import service
from coreman.runtime.worker.credential_resume import CredentialResumeHandler
from tests.fakes.fake_relay import FakeRelay
from tests.integration.credential_helpers import (
    BODY,
    DEMO_RELAY_SESSION,
    VALUES,
    cap_for,
    login_app,
    owner,
    seed_chat_session,
)
from tests.integration.test_chat_handler import stream_of
from tests.integration.worker_helpers import build_ctx


async def _submitted(session, platform="wecom"):
    bot, user, task, cipher = await owner(session, platform=platform)
    await login_app(session, platform)
    await seed_chat_session(session, bot, task)
    opened = await service.open_request(
        session, cipher, cap_for(bot, user, task), BODY, base_url="http://localhost"
    )
    await session.commit()
    await service.submit(session, cipher, opened.request_id, actor_id=user.id, values=VALUES)
    await tasks.finish(session, task.id, status="succeeded")
    await session.commit()
    row = await session.get(CredentialRequest, opened.request_id)
    claimed = await tasks.claim(session, lane="normal", instance_id="worker-test")
    await session.commit()
    assert claimed is not None and claimed.id == row.resume_task_id
    return bot, user, cipher, row, claimed


async def test_resume_continues_as_owner_with_keys_only(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, user, cipher, row, claimed = await _submitted(db_session)
    fake = FakeRelay("normal")
    ctx = build_ctx(db_engine, claimed, relay_client_factory=lambda _r: fake.client())
    await CredentialResumeHandler().run(ctx)
    await ctx.chat_logs.drain(5)
    body = fake.requests[0]
    assert "已通过安全表单提交 DEMO_PIN、DEMO_USERNAME" in body["messages"][1]["content"]
    assert "pin-778899" not in body["messages"][1]["content"]
    assert body["env_vars"]["DEMO_PIN"] == "pin-778899"
    # 续接的是提问的那个对话，而不是另起一个空会话。
    assert body["session_id"] == str(DEMO_RELAY_SESSION)
    stream = await stream_of(db_session, claimed.id)
    assert stream.delivery_mode == "proactive"
    done = await db_session.get(Task, claimed.id, populate_existing=True)
    assert done.status == "succeeded"


async def test_resume_is_dropped_when_request_no_longer_points_here(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, user, cipher, row, claimed = await _submitted(db_session)
    row.resume_task_id = None
    await db_session.commit()
    fake = FakeRelay("normal")
    await CredentialResumeHandler().run(
        build_ctx(db_engine, claimed, relay_client_factory=lambda _r: fake.client())
    )
    done = await db_session.get(Task, claimed.id, populate_existing=True)
    assert done.status == "cancelled" and done.error_code == "credential_resume_inactive"
    assert fake.requests == []


async def test_resume_on_feishu_keeps_the_origin_reply_context(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, user, cipher, row, claimed = await _submitted(db_session, "feishu")
    fake = FakeRelay("normal")
    ctx = build_ctx(db_engine, claimed, relay_client_factory=lambda _r: fake.client())
    await CredentialResumeHandler().run(ctx)
    await ctx.chat_logs.drain(5)
    assert fake.requests[0]["env_vars"]["DEMO_PIN"] == "pin-778899"
    stream = await stream_of(db_session, claimed.id)
    assert stream.delivery_mode == "stream"
    assert stream.reply_context["requester_user_id"] == "owner_pid"
    log = (await db_session.scalars(select(ChatLog).where(ChatLog.task_id == claimed.id))).one()
    assert log.message_type == "credential_resume"
    assert "pin-778899" not in (log.message_content or "")
    # 来源事件不是回复按钮的点击：开轮时不会去动原卡片。
    used = await db_session.scalars(
        select(OutboxItem).where(OutboxItem.dedupe_key.like("feishu-reply-used:%"))
    )
    assert list(used) == []


async def test_resume_is_dropped_when_owner_is_no_longer_active(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, user, cipher, row, claimed = await _submitted(db_session)
    user.status = "disabled"
    await db_session.commit()
    fake = FakeRelay("normal")
    await CredentialResumeHandler().run(
        build_ctx(db_engine, claimed, relay_client_factory=lambda _r: fake.client())
    )
    done = await db_session.get(Task, claimed.id, populate_existing=True)
    assert done.status == "cancelled" and done.error_code == "credential_owner_changed"
    assert fake.requests == []
    notice = await db_session.scalars(
        select(OutboxItem).where(
            OutboxItem.dedupe_key == f"credential-request:{row.id}:resume-skipped"
        )
    )
    assert [item.payload["markdown"] for item in notice] == ["凭证已保存，下次对话时生效。"]


async def _saved_notices(session: AsyncSession, row: CredentialRequest) -> list[OutboxItem]:
    found = await session.scalars(
        select(OutboxItem).where(
            OutboxItem.dedupe_key == f"credential-request:{row.id}:resume-skipped"
        )
    )
    return list(found)


async def test_resume_tells_the_user_when_the_bot_was_disabled_after_submit(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, user, cipher, row, claimed = await _submitted(db_session)
    bot.enabled = False
    await db_session.commit()
    fake = FakeRelay("normal")
    await CredentialResumeHandler().run(
        build_ctx(db_engine, claimed, relay_client_factory=lambda _r: fake.client())
    )
    done = await db_session.get(Task, claimed.id, populate_existing=True)
    assert done.status == "cancelled" and done.error_code == "credential_resume_inactive"
    assert fake.requests == []
    [notice] = await _saved_notices(db_session, row)
    assert notice.kind == "send" and notice.target == {"chat_id": row.delivery_chat_id}
    assert notice.payload["markdown"] == "凭证已保存，下次对话时生效。"


async def test_resume_tells_the_user_when_the_relay_is_unavailable(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, user, cipher, row, claimed = await _submitted(db_session)
    relay = await db_session.get(RelayServer, bot.relay_server_id)
    relay.is_active = False
    await db_session.commit()
    fake = FakeRelay("normal")
    await CredentialResumeHandler().run(
        build_ctx(db_engine, claimed, relay_client_factory=lambda _r: fake.client())
    )
    done = await db_session.get(Task, claimed.id, populate_existing=True)
    assert done.status == "failed" and done.error_code == "relay_unavailable"
    assert fake.requests == []
    [notice] = await _saved_notices(db_session, row)
    assert notice.kind == "send" and notice.target == {"chat_id": row.delivery_chat_id}
    assert notice.payload["markdown"] == "凭证已保存，下次对话时生效。"


async def _assert_conversation_reset(
    db_engine: AsyncEngine, db_session: AsyncSession, row: CredentialRequest, claimed: Task
) -> None:
    fake = FakeRelay("normal")
    await CredentialResumeHandler().run(
        build_ctx(db_engine, claimed, relay_client_factory=lambda _r: fake.client())
    )
    done = await db_session.get(Task, claimed.id, populate_existing=True)
    assert done.status == "cancelled" and done.error_code == "credential_resume_session_changed"
    assert fake.requests == []
    [notice] = await _saved_notices(db_session, row)
    assert notice.kind == "send" and notice.target == {"chat_id": row.delivery_chat_id}
    assert notice.payload["markdown"] == "凭证已保存。对话已重置，请重新发起刚才的请求。"


async def test_resume_is_not_dispatched_after_the_session_was_reset(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, user, cipher, row, claimed = await _submitted(db_session)
    chat_session = await db_session.get(ChatSession, (bot.id, claimed.session_key))
    chat_session.relay_session_id = uuid.uuid4()
    await db_session.commit()
    await _assert_conversation_reset(db_engine, db_session, row, claimed)


async def test_resume_is_not_dispatched_after_the_session_was_cleared(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, user, cipher, row, claimed = await _submitted(db_session)
    await db_session.execute(delete(ChatSession).where(ChatSession.bot_id == bot.id))
    await db_session.commit()
    await _assert_conversation_reset(db_engine, db_session, row, claimed)


async def test_resume_needs_the_session_the_request_was_asked_in(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, user, cipher, row, claimed = await _submitted(db_session)
    row.origin_relay_session_id = None
    await db_session.commit()
    await _assert_conversation_reset(db_engine, db_session, row, claimed)
