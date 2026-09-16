"""技能列表是只读接口：不能对 bots 行加 FOR UPDATE，被写操作或换机持锁时也要立即返回。"""

import asyncio

from sqlalchemy import select

from coreman.core.db.models import Bot
from coreman.core.db.session import make_session_factory
from tests.api.test_bot_skills import prepare


async def test_listing_skills_does_not_wait_for_bot_row_lock(client, db_session, db_engine):
    bot, _, _ = await prepare(client, db_session)
    factory = make_session_factory(db_engine)
    async with factory() as holder:
        # 另一事务持有 bots 行锁（模拟进行中的安装或换机）。
        await holder.execute(select(Bot).where(Bot.id == bot.id).with_for_update())
        result = await asyncio.wait_for(client.get(f"/api/admin/bots/{bot.id}/skills"), timeout=5)
        assert result.status_code == 200, result.text
        assert result.json()["data"]["items"] == []
        await holder.rollback()
