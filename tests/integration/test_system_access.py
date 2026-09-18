import json

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.auth.external_key import load_external_key
from coreman.core.auth.system_access import build_system_access
from coreman.core.auth.tokens import verify_token
from coreman.core.db.models import BotSystemGrant, BusinessSystem, JwtKey, User
from coreman.core.prompting.system_prompt import Speaker
from tests.integration.worker_helpers import seed_bot


async def test_only_current_user_receives_enabled_whitelisted_system_tokens(
    db_session: AsyncSession,
) -> None:
    bot, _, cipher = await seed_bot(db_session)
    alice = await db_session.get(User, bot.created_by)
    assert alice
    bob = User(login_name="bob", display_name="Bob")
    db_session.add(bob)
    db_session.add_all(
        [
            BusinessSystem(
                key="erp", name="ERP", base_url="https://erp.example", allowed_bot_ids=[bot.id]
            ),
            BusinessSystem(key="cloud", name="Cloud", default_for_all_bots=True),
            # 历史库里的保留 key：audience=coreman 的令牌能调用管理 API，永不签发。
            BusinessSystem(key="coreman", name="Legacy", default_for_all_bots=True),
            BusinessSystem(
                key="blocked", name="Blocked", default_for_all_bots=True, allowed_bot_ids=[]
            ),
            BusinessSystem(
                key="disabled", name="Disabled", default_for_all_bots=True, enabled=False
            ),
        ]
    )
    await db_session.flush()
    db_session.add(BotSystemGrant(bot_id=bot.id, system_key="erp", granted_by=alice.id))
    await db_session.commit()
    for user in (alice, bob):
        speaker = Speaker(user.login_name or "", user.id, user.login_name, user.display_name)
        access = await build_system_access(
            db_session, cipher, bot=bot, speaker=speaker, issuer="coreman", external_key=None
        )
        await db_session.commit()
        assert set(access.env) == {
            "BOT_TOKEN_ERP",
            "BOT_TOKEN_CLOUD",
            "COREMAN_SYSTEMS",
            "BOT_SYSTEMS_CONFIG",
        }
        assert access.env["COREMAN_SYSTEMS"] == access.env["BOT_SYSTEMS_CONFIG"]
        assert [s["key"] for s in json.loads(access.env["COREMAN_SYSTEMS"])] == ["cloud", "erp"]
        claims = await verify_token(
            db_session, access.env["BOT_TOKEN_ERP"], issuer="coreman", audience="erp"
        )
        assert (
            claims["sub"] == user.login_name
            and claims["exp"] - claims["iat"] == bot.sse_timeout_seconds + 300
        )
    erp = (
        await db_session.execute(select(BusinessSystem).where(BusinessSystem.key == "erp"))
    ).scalar_one()
    erp.allowed_bot_ids = []
    await db_session.commit()
    speaker = Speaker("bob", bob.id, bob.login_name, bob.display_name)
    assert (
        "BOT_TOKEN_ERP"
        not in (
            await build_system_access(
                db_session, cipher, bot=bot, speaker=speaker, issuer="coreman", external_key=None
            )
        ).env
    )
    bob.status = "disabled"
    await db_session.commit()
    assert not (
        await build_system_access(
            db_session, cipher, bot=bot, speaker=speaker, issuer="coreman", external_key=None
        )
    ).env
    assert not (
        await build_system_access(
            db_session,
            cipher,
            bot=bot,
            speaker=Speaker("unknown", None, None, None),
            issuer="coreman",
            external_key=None,
        )
    ).env


async def test_external_key_signs_system_tokens_in_issuer_format(
    db_session: AsyncSession,
) -> None:
    bot, _, cipher = await seed_bot(db_session)
    alice = await db_session.get(User, bot.created_by)
    assert alice
    db_session.add(BusinessSystem(key="erp", name="ERP", default_for_all_bots=True))
    await db_session.commit()
    private = ec.generate_private_key(ec.SECP256R1())
    external = load_external_key(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        public_pem=None,
        kid="legacy-2024",
        issuer="legacy-issuer",
    )
    speaker = Speaker("alice", alice.id, alice.login_name, alice.display_name)
    access = await build_system_access(
        db_session, cipher, bot=bot, speaker=speaker, issuer="coreman", external_key=external
    )
    await db_session.commit()
    token = access.env["BOT_TOKEN_ERP"]
    assert jwt.get_unverified_header(token)["kid"] == "legacy-2024"
    # 字段与外部签发方一致：iss 用它的、范围只在 scope、不带 aud；按其公钥即可验签。
    claims = jwt.decode(token, private.public_key(), algorithms=["ES256"], issuer="legacy-issuer")
    assert claims["sub"] == alice.login_name and claims["scope"] == "erp" and "aud" not in claims
    # 平台自己的密钥没有被创建或使用，CoreMan 验签也不认外部签发方的令牌。
    assert (await db_session.execute(select(JwtKey))).first() is None
    with pytest.raises(jwt.InvalidTokenError):
        await verify_token(db_session, token, issuer="legacy-issuer", audience="erp")
