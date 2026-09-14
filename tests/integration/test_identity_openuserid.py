import json
from collections.abc import Iterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.chat.identity import resolve_speaker
from coreman.core.chat.openuserid import OpenUseridResolver
from coreman.core.crypto import Cipher
from coreman.core.db.models import PlatformApp, User, UserIdentity
from coreman.core.db.session import make_session_factory
from coreman.core.platforms import wecom
from tests.fakes.fake_wecom_api import FakeWeComApi
from tests.integration.worker_helpers import MASTER

WO = "wo" + "A" * 40


@pytest.fixture(autouse=True)
def _fresh_token_cache() -> Iterator[None]:
    """`wecom._TOKENS` 是进程级缓存：不清干净，`token_calls` 会被别的用例的令牌顶掉。"""
    wecom._TOKENS.clear()
    yield
    wecom._TOKENS.clear()


async def _app(session: AsyncSession, caps: list[str]) -> PlatformApp:
    cipher = Cipher(MASTER)
    app = PlatformApp(
        platform="wecom",
        name=f"app-{'-'.join(caps)}",
        capabilities=caps,
        corp_id="corp1",
        secret_enc=cipher.encrypt("s3cret", "platform_apps.secret_enc"),
    )
    session.add(app)
    await session.commit()
    return app


async def test_resolves_persists_open_id_and_caches(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    u = User(login_name="zhangsan", display_name="张三")
    db_session.add(u)
    await db_session.flush()
    db_session.add(UserIdentity(user_id=u.id, platform="wecom", platform_user_id="zs"))
    await _app(db_session, ["contact_sync"])
    await _app(db_session, ["login", "notify"])
    await db_session.commit()
    api = FakeWeComApi({WO: "zs"})
    resolver = OpenUseridResolver(
        make_session_factory(db_engine), Cipher(MASTER), http=api.client()
    )
    s = await resolve_speaker(db_session, platform="wecom", platform_user_id=WO, resolver=resolver)
    await db_session.commit()
    assert s.known and s.user_id == u.id and s.platform_user_id == "zs"
    ident = (
        await db_session.execute(select(UserIdentity).where(UserIdentity.user_id == u.id))
    ).scalar_one()
    assert ident.open_id == WO
    assert api.convert_calls == 1 and api.token_calls == 1
    # 第二次：身份表已有 open_id，连 resolver 都不用叫。
    s2 = await resolve_speaker(db_session, platform="wecom", platform_user_id=WO, resolver=resolver)
    assert s2.known and api.convert_calls == 1
    # 缓存命中：另一条身份行不存在时也不再打企微。
    assert await resolver.resolve(WO) == "zs" and api.convert_calls == 1


async def test_invalid_or_unknown_is_negative_cached(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    await _app(db_session, ["login"])
    api = FakeWeComApi({})
    resolver = OpenUseridResolver(
        make_session_factory(db_engine), Cipher(MASTER), http=api.client()
    )
    s = await resolve_speaker(db_session, platform="wecom", platform_user_id=WO, resolver=resolver)
    assert not s.known and s.platform_user_id == WO
    s = await resolve_speaker(db_session, platform="wecom", platform_user_id=WO, resolver=resolver)
    assert not s.known and api.convert_calls == 1  # 5 分钟内不重试
    # 解出了明文但库里没有这个人：同样未知，且不写任何身份行。
    api2 = FakeWeComApi({WO: "ghost"})
    r2 = OpenUseridResolver(make_session_factory(db_engine), Cipher(MASTER), http=api2.client())
    s = await resolve_speaker(db_session, platform="wecom", platform_user_id=WO, resolver=r2)
    assert not s.known
    assert (await db_session.execute(select(UserIdentity))).scalars().all() == []


async def test_without_app_or_resolver_stays_unknown(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    api = FakeWeComApi({WO: "zs"})
    resolver = OpenUseridResolver(
        make_session_factory(db_engine), Cipher(MASTER), http=api.client()
    )
    s = await resolve_speaker(db_session, platform="wecom", platform_user_id=WO, resolver=resolver)
    assert not s.known and api.token_calls == 0
    s = await resolve_speaker(db_session, platform="wecom", platform_user_id=WO)
    assert not s.known
    assert json.dumps({"ok": True})
