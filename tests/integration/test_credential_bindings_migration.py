"""迁移 0121：旧表的每行搬成凭证 + 绑定并重新加密；同一个人同名同值的合并成一条。"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select, text

from coreman.core.crypto import Cipher
from coreman.core.db.models import CredentialBinding, PersonalCredential, User
from coreman.core.personal_credentials import store
from tests.integration.test_group_dispatch import another_bot
from tests.integration.worker_helpers import MASTER, seed_bot

CIPHER = Cipher(MASTER)
PATH = Path(__file__).resolve().parents[2] / "migrations/versions/0121_credential_bindings.py"


def _migration():
    spec = importlib.util.spec_from_file_location("migration_0121", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _old_row(session, bot, user, key, value, *, label="", secret=True):
    aad = f"personal_credentials.value_enc:{bot.id}:{user.id}:{key}"
    await session.execute(
        text(
            "INSERT INTO personal_credentials (bot_id, user_id, env_key, label, secret, value_enc) "
            "VALUES (:bot, :user, :key, :label, :secret, :enc)"
        ),
        {
            "bot": bot.id,
            "user": user.id,
            "key": key,
            "label": label,
            "secret": secret,
            "enc": CIPHER.encrypt(value, aad),
        },
    )


async def test_existing_rows_are_copied_merged_and_reencrypted(db_session, db_engine, monkeypatch):
    bot, _, _ = await seed_bot(db_session)
    other = await another_bot(db_session, bot, "ops_bot", "运营")
    user = User(login_name="owner", display_name="本人", source="sync")
    db_session.add(user)
    await db_session.flush()
    await _old_row(db_session, bot, user, "DEMO_PIN", "pin-778899", label="PIN")
    await _old_row(db_session, other, user, "DEMO_PIN", "pin-778899", label="新 PIN")
    await _old_row(db_session, other, user, "DEMO_USER", "alice", secret=False)
    await _old_row(db_session, bot, user, "DEMO_USER", "bob", secret=False)
    await db_session.execute(
        text(
            "INSERT INTO personal_credentials (bot_id, user_id, env_key, value_enc) "
            "VALUES (:bot, :user, 'BROKEN', 'enc:v1:AAAA')"
        ),
        {"bot": bot.id, "user": user.id},
    )
    await db_session.commit()
    migration = _migration()
    monkeypatch.setattr(
        "coreman.core.config.get_settings", lambda: SimpleNamespace(build_cipher=lambda: CIPHER)
    )
    async with db_engine.begin() as conn:
        await conn.run_sync(
            lambda sync: (
                monkeypatch.setattr(migration.op, "get_bind", lambda: sync, raising=False),
                migration._copy_existing(),
            )
        )
    rows = (await db_session.scalars(select(PersonalCredential))).all()
    # 同名同值的 PIN 合并成一条（标签取最新），两个不同值的账号各一条，解不开的不搬。
    assert sorted((r.env_key, r.label) for r in rows) == [
        ("DEMO_PIN", "新 PIN"),
        ("DEMO_USER", ""),
        ("DEMO_USER", ""),
    ]
    assert len((await db_session.scalars(select(CredentialBinding))).all()) == 4
    for target, user_value in ((bot, "bob"), (other, "alice")):
        found = await store.injected(db_session, CIPHER, bot_id=target.id, user_id=user.id)
        assert found.env == {"DEMO_PIN": "pin-778899", "DEMO_USER": user_value}
    await db_session.commit()
