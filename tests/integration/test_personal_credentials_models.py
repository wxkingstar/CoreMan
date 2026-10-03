"""个人凭证两张表：唯一约束、默认值与级联删除。"""

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from coreman.core.db.models import CredentialRequest, PersonalCredential, User
from coreman.core.timeutils import utcnow
from tests.integration.worker_helpers import seed_bot


async def _owner(session):
    bot, _, _ = await seed_bot(session)
    user = User(login_name="owner", display_name="本人", source="sync")
    session.add(user)
    await session.flush()
    return bot, user


async def test_one_value_per_bot_user_key(db_session):
    bot, user = await _owner(db_session)
    db_session.add(
        PersonalCredential(bot_id=bot.id, user_id=user.id, env_key="DEMO_API_KEY", value_enc="a")
    )
    await db_session.commit()
    db_session.add(
        PersonalCredential(bot_id=bot.id, user_id=user.id, env_key="DEMO_API_KEY", value_enc="b")
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_request_defaults_and_user_cascade(db_session):
    bot, user = await _owner(db_session)
    db_session.add(
        CredentialRequest(
            bot_id=bot.id,
            user_id=user.id,
            origin_kind="chat",
            origin_chat_id="oc_private",
            origin_chat_type="single",
            expires_at=utcnow(),
        )
    )
    db_session.add(
        PersonalCredential(bot_id=bot.id, user_id=user.id, env_key="DEMO_PIN", value_enc="x")
    )
    await db_session.commit()
    row = await db_session.scalar(select(CredentialRequest))
    assert row.status == "open" and row.fields == [] and row.purpose == ""
    cred = await db_session.scalar(select(PersonalCredential))
    assert cred.secret is True and cred.label == "" and cred.last_used_at is None
    await db_session.execute(delete(User).where(User.id == user.id))
    await db_session.commit()
    assert await db_session.scalar(select(CredentialRequest)) is None
    assert await db_session.scalar(select(PersonalCredential)) is None
