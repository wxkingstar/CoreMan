from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.chat.identity import resolve_speaker
from coreman.core.db.models import User, UserIdentity
from coreman.core.db.session import make_session_factory


async def test_resolve_by_platform_user_id_open_id_and_unknown(db_session: AsyncSession) -> None:
    u = User(login_name="zhangsan", display_name="张三")
    db_session.add(u)
    await db_session.flush()
    db_session.add(
        UserIdentity(user_id=u.id, platform="wecom", platform_user_id="zs", open_id="woXYZ")
    )
    await db_session.commit()
    s = await resolve_speaker(db_session, platform="wecom", platform_user_id="zs")
    assert s.known and s.user_id == u.id and s.login_name == "zhangsan" and s.display_name == "张三"
    s2 = await resolve_speaker(db_session, platform="wecom", platform_user_id="woXYZ")
    assert s2.known and s2.user_id == u.id and s2.platform_user_id == "woXYZ"
    s3 = await resolve_speaker(db_session, platform="wecom", platform_user_id="nobody")
    assert not s3.known and s3.login_name is None
    s4 = await resolve_speaker(db_session, platform="wecom", platform_user_id="wo" + "A" * 40)
    assert not s4.known


async def test_disabled_identity_is_not_an_authorized_speaker(db_session: AsyncSession) -> None:
    user = User(login_name="disabled", display_name="Disabled", status="disabled")
    db_session.add(user)
    await db_session.flush()
    db_session.add(UserIdentity(user_id=user.id, platform="wecom", platform_user_id="disabled"))
    await db_session.commit()
    speaker = await resolve_speaker(db_session, platform="wecom", platform_user_id="disabled")
    assert not speaker.known and speaker.login_name is None


async def test_identity_refresh_observes_disable_in_another_transaction(db_session, db_engine):
    user = User(login_name="recently-disabled", display_name="User")
    db_session.add(user)
    await db_session.flush()
    db_session.add(UserIdentity(user_id=user.id, platform="wecom", platform_user_id="pid"))
    await db_session.commit()
    assert (await resolve_speaker(db_session, platform="wecom", platform_user_id="pid")).known
    async with make_session_factory(db_engine)() as other:
        await other.execute(update(User).where(User.id == user.id).values(status="disabled"))
        await other.commit()
    assert not (await resolve_speaker(db_session, platform="wecom", platform_user_id="pid")).known
