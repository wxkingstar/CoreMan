"""清理任务：到点的请求置为过期；保留期外的已结束请求删除。"""

from datetime import timedelta

from sqlalchemy import update

from coreman.core.db.models import CredentialRequest
from coreman.core.db.session import make_session_factory
from coreman.core.personal_credentials import service
from coreman.core.settings_store import SettingsStore
from coreman.core.timeutils import utcnow
from coreman.runtime.scheduler import reaper
from tests.integration.credential_helpers import BODY, cap_for, owner


async def test_requests_expire_then_age_out(db_engine, db_session):
    bot, user, task, cipher = await owner(db_session)
    await service.open_request(
        db_session, cipher, cap_for(bot, user, task), BODY, base_url="http://localhost"
    )
    await db_session.execute(
        update(CredentialRequest).values(expires_at=utcnow() - timedelta(minutes=1))
    )
    await db_session.commit()
    factory = make_session_factory(db_engine)
    counts = await reaper.run_cleanup(factory, SettingsStore(factory), utcnow())
    assert counts["credential_requests_expired"] == 1
    await db_session.execute(
        update(CredentialRequest).values(created_at=utcnow() - timedelta(days=91))
    )
    await db_session.commit()
    kept = await reaper.run_retention(factory, utcnow())
    assert kept["old_credential_requests"] == 1
