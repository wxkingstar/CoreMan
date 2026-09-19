import json

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.auth.external_key import load_external_key
from coreman.core.auth.system_access import (
    SubjectUnavailable,
    build_system_access,
    token_subject,
    user_for_subject,
)
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
    bob = User(login_name="bob", display_name="Bob", email="bob@example.test")
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
            # 业务系统账号名（= 令牌 sub）随令牌一起下发，技能不必再从 login 猜。
            "COREMAN_USER_SUBJECT",
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
        subject = await token_subject(db_session, user)
        assert access.env["COREMAN_USER_SUBJECT"] == subject
        assert (
            claims["sub"] == subject
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
    # sub 是邮箱前缀，不是 login_name。
    assert claims["sub"] == await token_subject(db_session, alice)
    assert claims["scope"] == "erp" and "aud" not in claims
    # 平台自己的密钥没有被创建或使用，CoreMan 验签也不认外部签发方的令牌。
    assert (await db_session.execute(select(JwtKey))).first() is None
    with pytest.raises(jwt.InvalidTokenError):
        await verify_token(db_session, token, issuer="legacy-issuer", audience="erp")


async def test_token_subject_prefers_a_unique_email_prefix(db_session: AsyncSession) -> None:
    bot, _, cipher = await seed_bot(db_session)
    alice = await db_session.get(User, bot.created_by)
    assert alice
    alice.login_name, alice.email = "ou_1a2b3c", "Alice.W@example.com"
    db_session.add(BusinessSystem(key="erp", name="ERP", default_for_all_bots=True))
    await db_session.commit()
    speaker = Speaker(alice.login_name, alice.id, alice.login_name, alice.display_name)

    async def subject() -> str:
        access = await build_system_access(
            db_session, cipher, bot=bot, speaker=speaker, issuer="coreman", external_key=None
        )
        await db_session.commit()
        claims = await verify_token(
            db_session, access.env["BOT_TOKEN_ERP"], issuer="coreman", audience="erp"
        )
        assert claims["sub"] == await token_subject(db_session, alice)
        return str(claims["sub"])

    async def no_token() -> str:
        """算不出唯一 sub 时：一个令牌都不发，并给出可执行的提示。"""
        access = await build_system_access(
            db_session, cipher, bot=bot, speaker=speaker, issuer="coreman", external_key=None
        )
        assert access.env == {} and access.prompt
        with pytest.raises(SubjectUnavailable) as err:
            await token_subject(db_session, alice)
        return str(err.value.reason)

    assert await subject() == "alice.w"
    # 另一个账号的邮箱前缀相同（不同域名）：sub 不能对应两个人，谁都不给。
    other = User(login_name="other", display_name="Other", email="alice.w@other.test")
    db_session.add(other)
    await db_session.commit()
    assert await no_token() == "email_prefix_ambiguous"
    # 没有邮箱就没有 sub：不再退回 login_name（见下一条测试），同样一个令牌都不发。
    await db_session.delete(other)
    alice.email = None
    await db_session.commit()
    assert await no_token() == "email_missing"


async def test_login_name_is_never_a_subject_so_it_cannot_collide_with_an_email_prefix(
    db_session: AsyncSession,
) -> None:
    """回归：sub 曾经在没有邮箱时退回 login_name，而两者共用一个命名空间。

    甲的 login_name（首次同步时取的平台 userid）完全可能等于乙的邮箱前缀，当时的冲突
    检测只比对别人的**邮箱**，于是两个人拿到同一个 sub，业务系统分不出谁是谁。
    """
    bot, _, cipher = await seed_bot(db_session)
    alice = await db_session.get(User, bot.created_by)
    assert alice
    # 甲：没有邮箱，login_name 恰好是乙的邮箱前缀。
    alice.login_name, alice.email = "zhangsan", None
    bob = User(login_name="ou_9z8y7x", display_name="Bob", email="zhangsan@example.com")
    db_session.add_all([bob, BusinessSystem(key="erp", name="ERP", default_for_all_bots=True)])
    await db_session.commit()

    # 甲拿不到令牌（没有邮箱），所以不可能再顶着 "zhangsan" 这个 sub 出现。
    with pytest.raises(SubjectUnavailable, match="email_missing"):
        await token_subject(db_session, alice)
    alice_access = await build_system_access(
        db_session,
        cipher,
        bot=bot,
        speaker=Speaker("zhangsan", alice.id, alice.login_name, alice.display_name),
        issuer="coreman",
        external_key=None,
    )
    assert alice_access.env == {} and "补全企业邮箱" in alice_access.prompt
    # 乙照常拿到自己的 sub：甲的 login_name 不再参与这个命名空间。
    assert await token_subject(db_session, bob) == "zhangsan"
    assert await user_for_subject(db_session, "zhangsan") == bob
