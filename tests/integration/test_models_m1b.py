from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import Bot, BotMember, ModelCatalog, RelayServer, User


async def test_catalog_seeded(db_session: AsyncSession) -> None:
    rows = (await db_session.execute(select(ModelCatalog))).scalars().all()
    by_provider: dict[str, list[ModelCatalog]] = {}
    for r in rows:
        by_provider.setdefault(r.provider, []).append(r)
    assert set(by_provider) == {"claude", "codex", "minimax"}
    assert sum(1 for r in by_provider["claude"] if r.is_default) == 1
    assert next(r.model for r in by_provider["codex"] if r.is_default) == "codex/gpt-5.5"


async def test_relay_unique_name_defaults_and_version(db_session: AsyncSession) -> None:
    db_session.add(RelayServer(name="claude01", model_provider="claude"))
    await db_session.commit()
    got = (await db_session.execute(select(RelayServer))).scalar_one()
    assert (
        got.version == 1
        and got.health_status == "unknown"
        and got.visibility == "all"
        and got.host is None
        and got.clawrelay_port is None
    )
    db_session.add(RelayServer(name="claude01", model_provider="codex"))
    try:
        await db_session.commit()
        raise AssertionError("实例名应唯一")
    except IntegrityError:
        await db_session.rollback()


async def test_bot_key_check_and_members(db_session: AsyncSession) -> None:
    u = User(display_name="创建者", login_name="creator")
    db_session.add(u)
    await db_session.flush()
    bot = Bot(
        bot_key="sales_bot",
        platform="wecom",
        name="销售",
        created_by=u.id,
        model="vllm/claude-sonnet-4-6",
        working_dir="/data/skills/sales_bot",
        credentials_enc="enc:v1:x",
    )
    db_session.add(bot)
    await db_session.commit()
    assert bot.version == 1 and bot.verbosity_level == 1 and bot.sse_timeout_seconds == 3600
    db_session.add(BotMember(bot_id=bot.id, user_id=u.id, added_by=u.id))
    await db_session.commit()
    assert (await db_session.execute(select(BotMember))).scalar_one().role == "admin"
    db_session.add(
        Bot(
            bot_key="Bad Key",
            platform="wecom",
            name="x",
            created_by=u.id,
            model="m",
            working_dir="/d",
            credentials_enc="enc",
        )
    )
    try:
        await db_session.commit()
        raise AssertionError("bot_key 正则 CHECK 应拒绝")
    except IntegrityError:
        await db_session.rollback()
    assert (await db_session.execute(text("SELECT 1"))).scalar_one() == 1
