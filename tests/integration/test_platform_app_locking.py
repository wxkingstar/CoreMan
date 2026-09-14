"""platform_apps 的乐观锁：并发写只能有一个赢（F2）。"""

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm.exc import StaleDataError

from coreman.core.crypto import Cipher
from coreman.core.db.models import PlatformApp
from coreman.core.db.session import make_session_factory

KEY = b"\x07" * 32


async def test_concurrent_update_raises_stale_data(db_engine: AsyncEngine) -> None:
    """两个会话读到同一版本，先提交的赢；后提交的 UPDATE 匹配不到行 → StaleDataError。"""
    factory = make_session_factory(db_engine)
    async with factory() as s:
        app = PlatformApp(
            platform="wecom",
            name="原名",
            capabilities=["login"],
            corp_id="ww1",
            secret_enc=Cipher(KEY).encrypt("s", "platform_apps.secret_enc"),
        )
        s.add(app)
        await s.commit()
        await s.refresh(app)
        app_id, version = app.id, app.version
    assert version == 1
    async with factory() as sa, factory() as sb:
        a = await sa.get(PlatformApp, app_id)
        b = await sb.get(PlatformApp, app_id)
        assert a is not None and b is not None
        assert a.version == 1 and b.version == 1
        a.name = "A 改的"
        await sa.commit()
        b.name = "B 改的"
        with pytest.raises(StaleDataError):
            await sb.commit()
    async with factory() as s:
        row = await s.get(PlatformApp, app_id)
        assert row is not None and row.name == "A 改的" and row.version == 2
