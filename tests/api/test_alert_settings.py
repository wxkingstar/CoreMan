from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from coreman.core.crypto import Cipher
from coreman.core.db.models import OutboxItem, PlatformApp, UserIdentity
from coreman.core.notifications import NotificationSkipped, send_notification
from coreman.core.observability.alerts import observe
from tests.api.conftest import login_as
from tests.integration.worker_helpers import MASTER


async def test_alert_settings_version_identity_and_delivery_revocation(client, db_session):
    user = await login_as(client, db_session, role="platform_admin")
    app = PlatformApp(
        platform="wecom",
        name="notify",
        capabilities=["notify"],
        corp_id="ww_test",
        app_id="1",
        secret_enc="unused",
    )
    db_session.add(app)
    await db_session.flush()
    body = {"channels": [{"platform_app_id": str(app.id), "user_id": str(user.id)}]}
    await db_session.commit()
    assert (
        await client.put("/api/admin/alert-settings", json=body, headers={"If-Match": '"0"'})
    ).status_code == 422
    db_session.add(UserIdentity(user_id=user.id, platform="wecom", platform_user_id="u1"))
    await db_session.commit()
    result = await client.put("/api/admin/alert-settings", json=body, headers={"If-Match": '"0"'})
    assert result.status_code == 200, result.text
    assert (
        await client.put("/api/admin/alert-settings", json=body, headers={"If-Match": '"0"'})
    ).status_code == 409
    await observe(db_session, "outbox", True, "alert_outbox", datetime.now(UTC))
    await db_session.commit()
    item = await db_session.scalar(select(OutboxItem))
    assert item.target["user_id"] == str(user.id)
    assert item.target["alert_generation"] == 1
    # 撤销收件人后，旧排队通知必须在解密/网络调用前拒绝。
    result = await client.put(
        "/api/admin/alert-settings", json={"channels": []}, headers={"If-Match": '"1"'}
    )
    assert result.status_code == 200
    db_session.expire_all()
    item = await db_session.scalar(select(OutboxItem))
    with pytest.raises(NotificationSkipped):
        await send_notification(db_session, item, Cipher(MASTER))
