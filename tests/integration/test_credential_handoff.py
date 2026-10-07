"""一次性交付：值不进个人凭证，只给提交后的那一个续接轮，用完、续不上或超时都擦掉。"""

import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.bus import tasks
from coreman.core.chat.redaction import PLACEHOLDER
from coreman.core.db.models import (
    AuditLog,
    ChatSession,
    CredentialRequest,
    OutboxItem,
    PersonalCredential,
    Task,
)
from coreman.core.db.session import make_session_factory
from coreman.core.personal_credentials import policy, service, store
from coreman.core.personal_credentials.policy import CredentialError
from coreman.core.settings_store import SettingsStore
from coreman.core.timeutils import utcnow
from coreman.runtime.scheduler import reaper
from coreman.runtime.worker.credential_resume import CredentialResumeHandler
from tests.fakes.fake_relay import FakeRelay
from tests.integration.credential_helpers import (
    BODY,
    FIELDS,
    VALUES,
    cap_for,
    cron_cap,
    login_app,
    owner,
    seed_chat_session,
)
from tests.integration.test_chat_handler import chat_task, run, stream_of
from tests.integration.worker_helpers import build_ctx, turn_block, user_input

BASE = "https://coreman.example.com"
ONCE = {"fields": FIELDS, "purpose": "把 SSO 密钥写进服务配置"}


async def _opened(session, platform="feishu", body=ONCE):  # type: ignore[no-untyped-def]
    bot, user, task, cipher = await owner(session, platform=platform)
    if platform == "wecom":
        await login_app(session, "wecom")
    await seed_chat_session(session, bot, task)
    opened = await service.open_request(
        session, cipher, cap_for(bot, user, task), body, base_url=BASE
    )
    row = await session.get(CredentialRequest, opened.request_id)
    item = await session.get(OutboxItem, row.request_outbox_id)
    item.status = "sent"
    item.payload = {**item.payload, "_feishu_message_id": "om_form"}
    await session.commit()
    return bot, user, task, cipher, row


async def _submitted(session, platform="wecom"):  # type: ignore[no-untyped-def]
    bot, user, task, cipher, row = await _opened(session, platform)
    await service.submit(session, cipher, row.id, actor_id=user.id, values=VALUES)
    await tasks.finish(session, task.id, status="succeeded")
    await session.commit()
    await session.refresh(row)
    claimed = await tasks.claim(session, lane="normal", instance_id="worker-test")
    await session.commit()
    assert claimed is not None and claimed.id == row.resume_task_id
    return bot, user, cipher, row, claimed


def test_save_defaults_to_one_time() -> None:
    assert policy.parse_request(ONCE).save is False
    assert policy.parse_request(BODY).save is True


async def test_one_time_form_says_it_is_not_kept(db_session: AsyncSession) -> None:
    bot, user, task, cipher, row = await _opened(db_session)
    assert row.save is False
    item = await db_session.get(OutboxItem, row.request_outbox_id)
    card = item.payload["card"]
    assert card["header"]["title"]["content"] == "🔑 需要你提供一次性密钥"
    assert "不会保存" in json.dumps(card, ensure_ascii=False)


async def test_one_time_and_saved_requests_are_not_deduplicated(db_session: AsyncSession) -> None:
    bot, user, task, cipher = await owner(db_session)
    await seed_chat_session(db_session, bot, task)
    cap = cap_for(bot, user, task)
    once = await service.open_request(db_session, cipher, cap, ONCE, base_url=BASE)
    saved = await service.open_request(db_session, cipher, cap, BODY, base_url=BASE)
    again = await service.open_request(db_session, cipher, cap, ONCE, base_url=BASE)
    assert once.status == saved.status == "form_sent" and once.request_id != saved.request_id
    assert again.status == "already_pending" and again.request_id == once.request_id
    assert (once.save, saved.save, again.save) == (False, True, False)


async def test_scheduled_runs_cannot_ask_for_one_time_values(db_session: AsyncSession) -> None:
    bot, user, task, cipher = await owner(db_session)
    with pytest.raises(CredentialError) as exc:
        await service.open_request(
            db_session, cipher, cron_cap(bot, user, task), ONCE, base_url=BASE
        )
    assert exc.value.code == "invalid_fields" and "save" in exc.value.message


async def test_submit_holds_values_for_the_resume_only(db_session: AsyncSession) -> None:
    bot, user, task, cipher, row = await _opened(db_session)
    result = await service.submit(db_session, cipher, row.id, actor_id=user.id, values=VALUES)
    await db_session.commit()
    await db_session.refresh(row)
    assert result.status == "saved" and result.message == "AI 员工会继续之前的任务。"
    assert row.status == "submitted" and row.resume_task_id is not None
    assert json.loads(cipher.decrypt(row.handoff_enc, policy.handoff_aad(row.id))) == VALUES
    assert (await db_session.scalars(select(PersonalCredential))).all() == []
    audit = await db_session.scalar(select(AuditLog))
    assert audit.action == "personal_credential.handed_off"
    assert audit.diff == {"keys": ["DEMO_PIN", "DEMO_USERNAME"]}
    card = await db_session.scalar(select(OutboxItem).where(OutboxItem.kind == "card_update"))
    assert card.payload["card"]["header"]["title"]["content"] == "✅ 已交付"
    dumped = json.dumps(
        [card.payload, audit.diff, (await db_session.get(Task, row.resume_task_id)).payload],
        ensure_ascii=False,
    )
    assert "pin-778899" not in dumped and "alice" not in dumped


async def test_one_time_submit_discards_when_the_conversation_moved_on(
    db_session: AsyncSession,
) -> None:
    bot, user, task, cipher, row = await _opened(db_session, "wecom")
    chat_session = await db_session.get(ChatSession, (bot.id, task.session_key))
    chat_session.relay_session_id = uuid.uuid4()
    await db_session.commit()
    result = await service.submit(db_session, cipher, row.id, actor_id=user.id, values=VALUES)
    await db_session.commit()
    await db_session.refresh(row)
    assert result.message == service.DISCARDED
    assert row.status == "submitted" and row.resume_task_id is None and row.handoff_enc is None
    notice = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.dedupe_key == f"credential-request:{row.id}:saved")
    )
    assert notice.payload["markdown"] == f"DEMO_PIN、DEMO_USERNAME 没有使用：{service.DISCARDED}"


@pytest.mark.parametrize("first", ["once", "saved"])
async def test_one_time_and_saved_submissions_do_not_settle_each_other(
    db_session: AsyncSession, first: str
) -> None:
    bot, user, task, cipher = await owner(db_session)
    await seed_chat_session(db_session, bot, task)
    cap = cap_for(bot, user, task)
    opened = {
        "once": await service.open_request(db_session, cipher, cap, ONCE, base_url=BASE),
        "saved": await service.open_request(db_session, cipher, cap, BODY, base_url=BASE),
    }
    await db_session.commit()
    await service.submit(
        db_session, cipher, opened[first].request_id, actor_id=user.id, values=VALUES
    )
    await db_session.commit()
    other = opened["saved" if first == "once" else "once"]
    assert (await db_session.get(CredentialRequest, other.request_id)).status == "open"


async def test_resume_injects_the_values_once_and_then_drops_them(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, user, cipher, row, claimed = await _submitted(db_session)
    fake = FakeRelay("leaks_personal")
    ctx = build_ctx(db_engine, claimed, relay_client_factory=lambda _r: fake.client())
    await CredentialResumeHandler().run(ctx)
    await ctx.chat_logs.drain(5)
    body = fake.requests[0]
    assert body["env_vars"]["DEMO_PIN"] == "pin-778899"
    assert "已通过安全表单提交一次性密钥 DEMO_PIN、DEMO_USERNAME" in user_input(body)
    prompt = body["messages"][0]["content"]
    # 续接轮跑在原会话里，Codex 不会重读 system prompt：一次性密钥的说明写进本轮块。
    assert "本轮有用户刚提交的一次性密钥：`$DEMO_PIN`、`$DEMO_USERNAME`" in turn_block(body)
    assert "pin-778899" not in prompt
    # 模型把值打了出来：出站照样替换。
    stream = await stream_of(db_session, claimed.id)
    written = "\n".join(filter(None, [stream.final_text, stream.pending_text]))
    assert "pin-778899" not in written and PLACEHOLDER in written
    await db_session.refresh(row)
    assert row.handoff_enc is None
    assert (await db_session.get(Task, claimed.id, populate_existing=True)).status == "succeeded"
    # 下一轮本人再说话，值已经不在了。
    found = await store.injected(db_session, cipher, bot_id=bot.id, user_id=user.id)
    assert found.env == {}


async def test_next_turn_does_not_get_one_time_values(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, user, cipher, row, claimed = await _submitted(db_session)
    await CredentialResumeHandler().run(
        build_ctx(db_engine, claimed, relay_client_factory=lambda _r: FakeRelay("normal").client())
    )
    later = await chat_task(
        db_session, bot, "再来一次", sender="owner_pid", chat_type="single", chat_id="oc_private"
    )
    fake = FakeRelay("normal")
    await run(db_engine, later, fake)
    assert "DEMO_PIN" not in fake.requests[0]["env_vars"]


async def test_resume_skipped_discards_the_values(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, user, cipher, row, claimed = await _submitted(db_session)
    chat_session = await db_session.get(ChatSession, (bot.id, claimed.session_key))
    chat_session.relay_session_id = uuid.uuid4()
    await db_session.commit()
    fake = FakeRelay("normal")
    await CredentialResumeHandler().run(
        build_ctx(db_engine, claimed, relay_client_factory=lambda _r: fake.client())
    )
    assert fake.requests == []
    await db_session.refresh(row)
    assert row.handoff_enc is None
    notice = await db_session.scalar(
        select(OutboxItem).where(
            OutboxItem.dedupe_key == f"credential-request:{row.id}:resume-skipped"
        )
    )
    assert notice.payload["markdown"] == (
        "对话已重置，刚才提交的一次性密钥已丢弃，请重新发起刚才的请求。"
    )


async def test_resume_lost_values_tell_the_agent_to_ask_again(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, user, cipher, row, claimed = await _submitted(db_session)
    row.handoff_enc = None
    await db_session.commit()
    fake = FakeRelay("normal")
    await CredentialResumeHandler().run(
        build_ctx(db_engine, claimed, relay_client_factory=lambda _r: fake.client())
    )
    assert "DEMO_PIN" not in fake.requests[0]["env_vars"]
    assert "一次性密钥已不可用" in turn_block(fake.requests[0])


async def test_values_stay_while_the_resume_is_still_queued(db_session: AsyncSession) -> None:
    bot, user, cipher, row, claimed = await _submitted(db_session)
    await tasks.defer(db_session, claimed.id, seconds=0)
    await db_session.commit()
    await service.drop_finished_handoff(db_session, row.id, task_id=claimed.id)
    await db_session.commit()
    await db_session.refresh(row)
    assert row.handoff_enc is not None
    await tasks.finish(db_session, claimed.id, status="failed")
    await service.drop_finished_handoff(db_session, row.id, task_id=claimed.id)
    await db_session.commit()
    await db_session.refresh(row)
    assert row.handoff_enc is None


async def test_reaper_wipes_finished_and_stale_handoffs(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, user, cipher, row, claimed = await _submitted(db_session)
    factory = make_session_factory(db_engine)
    counts = await reaper.run_cleanup(factory, SettingsStore(factory), utcnow())
    assert counts["credential_handoffs_wiped"] == 0
    await db_session.execute(
        update(CredentialRequest).values(
            submitted_at=utcnow() - policy.HANDOFF_MAX_AGE - timedelta(minutes=1)
        )
    )
    await db_session.commit()
    counts = await reaper.run_cleanup(factory, SettingsStore(factory), utcnow())
    assert counts["credential_handoffs_wiped"] == 1
    await db_session.refresh(row)
    assert row.handoff_enc is None


async def test_reaper_wipes_handoff_once_the_resume_task_ended(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, user, cipher, row, claimed = await _submitted(db_session)
    await tasks.finish(db_session, claimed.id, status="failed")
    await db_session.commit()
    factory = make_session_factory(db_engine)
    counts = await reaper.run_cleanup(factory, SettingsStore(factory), utcnow())
    assert counts["credential_handoffs_wiped"] == 1
