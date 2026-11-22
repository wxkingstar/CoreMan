"""个人凭证的表：绑定唯一约束、默认值与级联删除。"""

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from coreman.core.db.models import CredentialBinding, CredentialRequest, PersonalCredential, User
from coreman.core.timeutils import utcnow
from tests.integration.worker_helpers import seed_bot


async def _owner(session):
    bot, _, _ = await seed_bot(session)
    user = User(login_name="owner", display_name="本人", source="sync")
    session.add(user)
    await session.flush()
    return bot, user


async def _credential(session, user, key="DEMO_API_KEY"):
    row = PersonalCredential(user_id=user.id, env_key=key, value_enc="x")
    session.add(row)
    await session.flush()
    return row


async def test_one_binding_per_bot_user_key(db_session):
    bot, user = await _owner(db_session)
    first, second = await _credential(db_session, user), await _credential(db_session, user)
    db_session.add(
        CredentialBinding(credential_id=first.id, bot_id=bot.id, user_id=user.id, env_key="K")
    )
    await db_session.commit()
    db_session.add(
        CredentialBinding(credential_id=second.id, bot_id=bot.id, user_id=user.id, env_key="K")
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_defaults_and_cascades(db_session):
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
    cred = await _credential(db_session, user, "DEMO_PIN")
    db_session.add(
        CredentialBinding(credential_id=cred.id, bot_id=bot.id, user_id=user.id, env_key="DEMO_PIN")
    )
    await db_session.commit()
    row = await db_session.scalar(select(CredentialRequest))
    assert row.status == "open" and row.fields == [] and row.purpose == ""
    cred = await db_session.scalar(select(PersonalCredential))
    assert cred.secret is True and cred.label == ""
    binding = await db_session.scalar(select(CredentialBinding))
    assert binding.last_used_at is None
    # 删凭证级联删绑定；删用户把凭证与请求都删掉。
    await db_session.execute(delete(PersonalCredential))
    await db_session.commit()
    assert await db_session.scalar(select(CredentialBinding)) is None
    await _credential(db_session, user)
    await db_session.commit()
    await db_session.execute(delete(User).where(User.id == user.id))
    await db_session.commit()
    assert await db_session.scalar(select(CredentialRequest)) is None
    assert await db_session.scalar(select(PersonalCredential)) is None
