from datetime import UTC, datetime, timedelta

import jwt
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.auth.tokens import KEY_AAD, active_key, issue_token, public_keys, verify_token
from coreman.core.crypto import Cipher
from coreman.core.db.models import JwtKey


async def test_key_rotation_keeps_old_tokens_valid_and_hides_private_key(
    db_session: AsyncSession,
) -> None:
    cipher = Cipher(b"\x01" * 32)
    key = await active_key(db_session, cipher)
    token = issue_token(
        key, cipher, issuer="coreman", login="alice", name="Alice", audience="erp", ttl=3600
    )
    await db_session.commit()
    assert (await verify_token(db_session, token, issuer="coreman", audience="erp"))[
        "sub"
    ] == "alice"
    old_kid = key.kid
    new = await active_key(db_session, cipher, rotate=True)
    await db_session.commit()
    assert new.kid != old_kid
    assert (
        len(list((await db_session.execute(select(JwtKey).where(JwtKey.is_active))).scalars())) == 1
    )
    public = await public_keys(db_session)
    assert len(public) == 2 and all("d" not in k for k in public)
    await verify_token(db_session, token, issuer="coreman", audience="erp")
    key.retired_at = datetime.now(UTC) - timedelta(hours=25)
    await db_session.commit()
    assert len(await public_keys(db_session)) == 1
    with pytest.raises(jwt.InvalidTokenError):
        await verify_token(db_session, token, issuer="coreman", audience="erp")


async def test_token_rejects_scope_issuer_expiry_missing_claims_and_algorithm(
    db_session: AsyncSession,
) -> None:
    cipher = Cipher(b"\x02" * 32)
    key = await active_key(db_session, cipher)
    now = int(datetime.now(UTC).timestamp())
    claims = {
        "iss": "coreman",
        "sub": "alice",
        "name": "Alice",
        "aud": "coreman",
        "scope": "coreman",
        "iat": now,
        "exp": now + 3600,
        "jti": "test",
    }
    pem = cipher.decrypt(key.private_pem_enc, KEY_AAD)
    for changes in (
        {"scope": "erp"},
        {"iss": "other"},
        {"aud": "other"},
        {"iat": now - 100, "exp": now - 1},
        {"sub": ""},
        {"exp": now + 999999},
    ):
        token = jwt.encode({**claims, **changes}, pem, algorithm="ES256", headers={"kid": key.kid})
        with pytest.raises(jwt.InvalidTokenError):
            await verify_token(db_session, token, issuer="coreman", audience="coreman")
    token = jwt.encode({"sub": "alice"}, pem, algorithm="ES256", headers={"kid": key.kid})
    with pytest.raises(jwt.InvalidTokenError):
        await verify_token(db_session, token, issuer="coreman", audience="coreman")
    token = jwt.encode(
        claims,
        "synthetic-secret-for-algorithm-confusion",
        algorithm="HS256",
        headers={"kid": key.kid},
    )
    with pytest.raises(jwt.InvalidTokenError):
        await verify_token(db_session, token, issuer="coreman", audience="coreman")
