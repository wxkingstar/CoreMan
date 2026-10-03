"""提交之后在原会话续接：发起人发言、消息只有键名与原始请求、凭证在 env 里、企微走主动推送。"""

import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.bus import tasks
from coreman.core.db.models import (
    Announcement,
    Bot,
    ChatLog,
    ChatSession,
    CredentialRequest,
    OutboxItem,
    RelayServer,
    Task,
    TaskStream,
)
from coreman.core.personal_credentials import service
from coreman.runtime.worker.context import TaskContext
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
from tests.integration.test_chat_handler import chat_task, stream_of
from tests.integration.worker_helpers import build_ctx


async def _newer_turn(session, bot, origin, sender):
    """来源那一轮之后，同一会话里又有人发了一条新请求（已处理完）。"""
    message = origin.payload["message"]
    newer = await chat_task(
        session,
        bot,
        "换一个请求",
        sender=sender,
        chat_type=message["chat_type"],
        chat_id=origin.session_key,
    )
    await tasks.finish(session, newer.id, status="succeeded")
    await session.commit()


async def _submitted(session, platform="wecom", *, chat_type="single", before=None, after=None):
    """before / after：表单提交之前 / 之后，谁在同一会话里发了新请求。"""
    bot, user, task, cipher = await owner(session, platform=platform, chat_type=chat_type)
    await login_app(session, platform)
    await seed_chat_session(session, bot, task)
    opened = await service.open_request(
        session, cipher, cap_for(bot, user, task), BODY, base_url="http://localhost"
    )
    await session.commit()
    if before:
        await _newer_turn(session, bot, task, before)
    await service.submit(session, cipher, opened.request_id, actor_id=user.id, values=VALUES)
    if after:
        await _newer_turn(session, bot, task, after)
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
    # 消息里带着用户的原始请求，续接的是那一条，而不是会话里最近的一条。
    assert body["messages"][1]["content"].endswith("请继续完成用户的这条原始请求：帮我查订单")
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


async def test_resume_gives_the_announcement_instead_of_running_during_maintenance(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, user, cipher, row, claimed = await _submitted(db_session)
    db_session.add(Announcement(scope="global", content="全站维护"))
    await db_session.commit()
    fake = FakeRelay("normal")
    await CredentialResumeHandler().run(
        build_ctx(db_engine, claimed, relay_client_factory=lambda _r: fake.client())
    )
    done = await db_session.get(Task, claimed.id, populate_existing=True)
    assert done.status == "succeeded" and done.result == {"announcement": True}
    assert fake.requests == []
    # 来源那一轮的回复上下文已经过期：不走 reply_once，公告直接发到表单送达的会话。
    streams = await db_session.scalars(select(TaskStream).where(TaskStream.task_id == claimed.id))
    assert list(streams) == []
    [notice] = await _saved_notices(db_session, row)
    assert notice.kind == "send" and notice.target == {"chat_id": row.delivery_chat_id}
    assert notice.payload["markdown"] == "全站维护\n\n凭证已保存，维护结束后请重新发起刚才的请求。"


async def _assert_conversation_reset(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    row: CredentialRequest,
    claimed: Task,
    handler: CredentialResumeHandler | None = None,
) -> None:
    fake = FakeRelay("normal")
    await (handler or CredentialResumeHandler()).run(
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


class _ChangedBeforeOpen(CredentialResumeHandler):
    """让变化恰好落在 `_resolve` 提交之后、`_open` 拿 bot 行锁之前（会话检查已经放行的窗口）。"""

    def __init__(self, change: Callable[[AsyncSession, uuid.UUID, str], Awaitable[None]]) -> None:
        super().__init__()
        self._change = change

    async def _wait_superseded(self, ctx: TaskContext, bot_id: uuid.UUID, session_key: str) -> bool:
        ready = await super()._wait_superseded(ctx, bot_id, session_key)
        async with ctx.session_factory() as session:
            await self._change(session, bot_id, session_key)
            await session.commit()
        return ready


async def test_resume_is_not_opened_when_the_session_is_reset_after_the_check(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, user, cipher, row, claimed = await _submitted(db_session)

    async def reset(session: AsyncSession, bot_id: uuid.UUID, session_key: str) -> None:
        chat_session = await session.get(ChatSession, (bot_id, session_key))
        assert chat_session is not None
        chat_session.relay_session_id = uuid.uuid4()

    await _assert_conversation_reset(db_engine, db_session, row, claimed, _ChangedBeforeOpen(reset))


async def test_resume_does_not_open_a_replacement_session_when_the_session_is_cleared(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, user, cipher, row, claimed = await _submitted(db_session)

    async def clear(session: AsyncSession, bot_id: uuid.UUID, session_key: str) -> None:
        await session.execute(delete(ChatSession).where(ChatSession.bot_id == bot_id))

    await _assert_conversation_reset(db_engine, db_session, row, claimed, _ChangedBeforeOpen(clear))
    # 续接不该为失败的这一轮顺手建一个空会话：下一条消息自己会新建。
    assert (
        await db_session.scalar(select(ChatSession).where(ChatSession.bot_id == bot.id))
    ) is None


async def test_resume_does_not_open_a_replacement_session_when_the_runtime_changes(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, user, cipher, row, claimed = await _submitted(db_session)

    async def switch_runtime(session: AsyncSession, bot_id: uuid.UUID, session_key: str) -> None:
        current = await session.get(Bot, bot_id)
        assert current is not None and current.relay_server_id is not None
        relay = await session.get(RelayServer, current.relay_server_id)
        assert relay is not None
        relay.model_provider = "codex"

    await _assert_conversation_reset(
        db_engine, db_session, row, claimed, _ChangedBeforeOpen(switch_runtime)
    )
    # 原对话的映射原样保留：用户下一条消息按后端变化自行换会话，这里不替它做。
    kept = await db_session.get(ChatSession, (bot.id, claimed.session_key), populate_existing=True)
    assert kept is not None and kept.relay_session_id == DEMO_RELAY_SESSION


async def test_resume_is_not_queued_when_another_user_spoke_after_the_request(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, user, task, cipher = await owner(db_session, platform="feishu", chat_type="group")
    await seed_chat_session(db_session, bot, task)
    opened = await service.open_request(
        db_session, cipher, cap_for(bot, user, task), BODY, base_url="http://localhost"
    )
    await db_session.commit()
    await _newer_turn(db_session, bot, task, "other_pid")
    done = await service.submit(
        db_session, cipher, opened.request_id, actor_id=user.id, values=VALUES
    )
    await db_session.commit()
    # 群里已经转向别人的请求：值照常保存，但不接着做，也不承诺会继续。
    assert done.status == "saved" and done.message == "下次对话时生效。"
    row = await db_session.get(CredentialRequest, opened.request_id)
    assert row is not None and row.status == "submitted" and row.resume_task_id is None
    queued = await db_session.scalars(select(Task).where(Task.kind == service.RESUME_KIND))
    assert list(queued) == []


async def test_resume_is_dropped_when_another_user_speaks_after_the_submit(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, user, cipher, row, claimed = await _submitted(
        db_session, "feishu", chat_type="group", after="other_pid"
    )
    fake = FakeRelay("normal")
    await CredentialResumeHandler().run(
        build_ctx(db_engine, claimed, relay_client_factory=lambda _r: fake.client())
    )
    done = await db_session.get(Task, claimed.id, populate_existing=True)
    assert done.status == "cancelled" and done.error_code == "credential_resume_superseded"
    assert fake.requests == []
    [notice] = await _saved_notices(db_session, row)
    assert notice.kind == "send" and notice.target == {"chat_id": row.delivery_chat_id}
    assert notice.payload["markdown"] == "凭证已保存。对话里已有新的请求，请重新发起刚才的请求。"


async def test_resume_continues_when_the_same_user_spoke_after_the_request(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, user, cipher, row, claimed = await _submitted(db_session, "feishu", before="owner_pid")
    assert row.resume_task_id == claimed.id
    fake = FakeRelay("normal")
    ctx = build_ctx(db_engine, claimed, relay_client_factory=lambda _r: fake.client())
    await CredentialResumeHandler().run(ctx)
    await ctx.chat_logs.drain(5)
    # 同一个人的新消息不挡路；引用的是原始请求，不会续到后来那一条上。
    content = fake.requests[0]["messages"][1]["content"]
    assert content.endswith("请继续完成用户的这条原始请求：帮我查订单")
    assert fake.requests[0]["env_vars"]["DEMO_PIN"] == "pin-778899"
