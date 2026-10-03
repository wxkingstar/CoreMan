"""本人触发的轮次注入本人的凭证；别人、协作轮拿不到；模型复述时出站被拦。"""

import uuid

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.chat.redaction import PLACEHOLDER
from coreman.core.db.models import ChatSession, InboundEvent, User, UserIdentity
from coreman.core.personal_credentials import policy, store
from coreman.core.prompting import Speaker
from coreman.runtime.worker.chat import credentials
from coreman.runtime.worker.chat.models import Intake
from tests.fakes.fake_relay import FakeRelay
from tests.integration.credential_helpers import FIELDS, VALUES, owner
from tests.integration.test_chat_handler import chat_task, run, stream_of
from tests.integration.worker_helpers import build_ctx


async def _saved(session, **kw):  # type: ignore[no-untyped-def]
    bot, user, task, cipher = await owner(session, platform="wecom", **kw)
    await store.save(session, cipher, bot_id=bot.id, user_id=user.id, fields=FIELDS, values=VALUES)
    await session.commit()
    return bot, user, task, cipher


async def test_private_turn_gets_own_credentials_and_echo_is_masked(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, user, task, cipher = await _saved(db_session)
    fake = FakeRelay("leaks_personal")
    await run(db_engine, task, fake)
    env = fake.requests[0]["env_vars"]
    assert env["DEMO_PIN"] == "pin-778899" and env["DEMO_USERNAME"] == "alice"
    assert env["COREMAN_CREDENTIAL_URL"] == "http://localhost/api/runtime/credentials/requests"
    cap = policy.read_capability(cipher, env["COREMAN_CREDENTIAL_TOKEN"])
    assert (cap.user_id, cap.task_id, cap.origin_kind, cap.chat_id) == (
        user.id,
        task.id,
        "chat",
        "oc_private",
    )
    # 令牌记着本轮用的 relay 会话：续接时凭它确认对话没有被重置过。
    chat_session = await db_session.get(ChatSession, (bot.id, task.session_key))
    assert cap.relay_session_id == chat_session.relay_session_id
    assert fake.requests[0]["session_id"] == str(cap.relay_session_id)
    prompt = fake.requests[0]["messages"][0]["content"]
    assert "`$DEMO_PIN`" in prompt and "pin-778899" not in prompt and "alice" not in prompt
    stream = await stream_of(db_session, task.id)
    written = "\n".join(filter(None, [stream.final_text, stream.pending_text]))
    assert "pin-778899" not in written and PLACEHOLDER in written


async def test_group_turn_injects_only_the_speakers_own(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, user, task, cipher = await _saved(db_session, chat_type="group")
    # 群里同一个会话一次只跑一轮：先把本人这一轮跑完，另一个人的轮次才不会被当成会话忙而推迟。
    owner_fake = FakeRelay("normal")
    await run(db_engine, task, owner_fake)
    assert owner_fake.requests[0]["env_vars"]["DEMO_PIN"] == "pin-778899"
    other = User(login_name="other", display_name="别人", source="sync")
    db_session.add(other)
    await db_session.flush()
    db_session.add(UserIdentity(user_id=other.id, platform="wecom", platform_user_id="other_pid"))
    await db_session.commit()
    mine = await chat_task(
        db_session, bot, "我也查一下", sender="other_pid", chat_type="group", chat_id="oc_group"
    )
    fake = FakeRelay("normal")
    await run(db_engine, mine, fake)
    env = fake.requests[0]["env_vars"]
    assert "DEMO_PIN" not in env and "DEMO_USERNAME" not in env
    assert policy.read_capability(cipher, env["COREMAN_CREDENTIAL_TOKEN"]).user_id == other.id


async def test_collaboration_turns_get_nothing(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, user, task, cipher = await _saved(db_session)
    inbound = await db_session.get(InboundEvent, task.inbound_event_id)
    speaker = Speaker("owner_pid", user.id, "owner", "本人")
    intake = Intake(bot, None, inbound, speaker, "oc_private", "single", "oc_private", "x", "text")
    db_session.expunge(task)
    base = dict(task.payload)
    for key in ("collaboration_id", "collaboration_phase", "human_collaboration_id"):
        task.payload = {**base, key: "x"}
        extra, env, secrets = await credentials.configure(
            db_session,
            build_ctx(db_engine, task),
            intake,
            "",
            {"COREMAN_CREDENTIAL_TOKEN": "forged"},
            relay_session_id=uuid.uuid4(),
        )
        assert (extra, env, secrets) == ("", {}, frozenset())


def test_guidance_lists_names_and_rules() -> None:
    text = credentials.guidance(("DEMO_PIN",), "chat")
    assert "`$DEMO_PIN`" in text and "$COREMAN_CREDENTIAL_URL" in text
    assert "不得让用户在聊天里发送密码或密钥" in text
    assert "还没有保存" in credentials.guidance((), "chat")


def test_guidance_promises_resume_only_for_chat_turns() -> None:
    chat = credentials.guidance(("DEMO_PIN",), "chat")
    cron = credentials.guidance(("DEMO_PIN",), "cron")
    assert "系统会自动让你继续" in chat and "下一次定时运行" not in chat
    assert "系统会自动让你继续" not in cron and "不会续接本轮" in cron
    assert "下一次定时运行起生效" in cron and "不得让用户在聊天里发送密码或密钥" in cron
