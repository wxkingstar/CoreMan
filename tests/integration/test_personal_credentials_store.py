"""个人凭证的存取：逐条加密、覆盖写、注入过滤与使用时间。"""

from datetime import timedelta

from sqlalchemy import select, update

from coreman.core.crypto import Cipher
from coreman.core.db.models import PersonalCredential, User
from coreman.core.personal_credentials import store
from coreman.core.timeutils import utcnow
from tests.integration.worker_helpers import MASTER, seed_bot

CIPHER = Cipher(MASTER)
FIELDS = [
    {"key": "DEMO_USERNAME", "label": "账号", "secret": False},
    {"key": "DEMO_PIN", "label": "PIN", "secret": True},
]


async def _owner(session):
    bot, _, _ = await seed_bot(session)
    user = User(login_name="owner", display_name="本人", source="sync")
    session.add(user)
    await session.flush()
    return bot, user


async def test_save_encrypts_each_value_and_overwrites(db_session):
    bot, user = await _owner(db_session)
    keys = await store.save(
        db_session,
        CIPHER,
        bot_id=bot.id,
        user_id=user.id,
        fields=FIELDS,
        values={"DEMO_USERNAME": "alice", "DEMO_PIN": "pin-778899"},
    )
    await db_session.commit()
    assert keys == ["DEMO_PIN", "DEMO_USERNAME"]
    rows = (await db_session.scalars(select(PersonalCredential))).all()
    assert all("pin-778899" not in r.value_enc and "alice" not in r.value_enc for r in rows)
    await store.save(
        db_session,
        CIPHER,
        bot_id=bot.id,
        user_id=user.id,
        fields=FIELDS[1:],
        values={"DEMO_PIN": "pin-000111"},
    )
    await db_session.commit()
    found = await store.injected(db_session, CIPHER, bot_id=bot.id, user_id=user.id)
    assert found.env == {"DEMO_PIN": "pin-000111", "DEMO_USERNAME": "alice"}
    assert found.names == ("DEMO_PIN", "DEMO_USERNAME")
    # 只有 secret 字段进脱敏集合；账号不进。
    assert found.secret_values == frozenset({"pin-000111"})


async def test_injection_is_scoped_and_skips_bad_rows(db_session):
    bot, user = await _owner(db_session)
    other = User(login_name="other", display_name="别人", source="sync")
    db_session.add(other)
    await db_session.flush()
    await store.save(
        db_session,
        CIPHER,
        bot_id=bot.id,
        user_id=other.id,
        fields=FIELDS[1:],
        values={"DEMO_PIN": "pin-of-other"},
    )
    # 被挪过来的密文（AAD 不符）与策略收紧后不再允许的键名都跳过。
    moved = (await db_session.scalars(select(PersonalCredential))).one()
    db_session.add(
        PersonalCredential(
            bot_id=bot.id, user_id=user.id, env_key="DEMO_PIN", value_enc=moved.value_enc
        )
    )
    db_session.add(
        PersonalCredential(
            bot_id=bot.id,
            user_id=user.id,
            env_key="PATH",
            value_enc=CIPHER.encrypt(
                "/tmp", f"personal_credentials.value_enc:{bot.id}:{user.id}:PATH"
            ),
        )
    )
    await db_session.commit()
    found = await store.injected(db_session, CIPHER, bot_id=bot.id, user_id=user.id)
    assert found.env == {} and found.names == ()


async def test_last_used_is_throttled_and_does_not_touch_updated_at(db_session):
    bot, user = await _owner(db_session)
    await store.save(
        db_session,
        CIPHER,
        bot_id=bot.id,
        user_id=user.id,
        fields=FIELDS[1:],
        values={"DEMO_PIN": "pin-778899"},
    )
    await db_session.commit()
    row = await db_session.scalar(select(PersonalCredential))
    updated = row.updated_at
    await store.injected(db_session, CIPHER, bot_id=bot.id, user_id=user.id)
    await db_session.commit()
    await db_session.refresh(row)
    first = row.last_used_at
    assert first is not None and row.updated_at == updated
    await store.injected(db_session, CIPHER, bot_id=bot.id, user_id=user.id)
    await db_session.commit()
    await db_session.refresh(row)
    assert row.last_used_at == first
    await db_session.execute(
        update(PersonalCredential).values(last_used_at=utcnow() - timedelta(hours=2))
    )
    await db_session.commit()
    await store.injected(db_session, CIPHER, bot_id=bot.id, user_id=user.id)
    await db_session.commit()
    await db_session.refresh(row)
    assert row.last_used_at >= first


async def test_list_update_delete_own(db_session):
    bot, user = await _owner(db_session)
    await store.save(
        db_session,
        CIPHER,
        bot_id=bot.id,
        user_id=user.id,
        fields=FIELDS,
        values={"DEMO_USERNAME": "alice", "DEMO_PIN": "pin-778899"},
    )
    await db_session.commit()
    rows = await store.list_own(db_session, user.id)
    assert [(r.env_key, b.id) for r, b in rows] == [
        ("DEMO_PIN", bot.id),
        ("DEMO_USERNAME", bot.id),
    ]
    assert [store.plain_value(CIPHER, r) for r, _ in rows] == [None, "alice"]
    row = await store.update_value(
        db_session, CIPHER, bot_id=bot.id, user_id=user.id, env_key="DEMO_PIN", value="pin-222333"
    )
    assert row is not None
    assert (
        await store.update_value(
            db_session, CIPHER, bot_id=bot.id, user_id=user.id, env_key="MISSING", value="x"
        )
        is None
    )
    assert await store.delete(db_session, bot_id=bot.id, user_id=user.id, env_key="DEMO_USERNAME")
    assert not await store.delete(
        db_session, bot_id=bot.id, user_id=user.id, env_key="DEMO_USERNAME"
    )
    await db_session.commit()
    found = await store.injected(db_session, CIPHER, bot_id=bot.id, user_id=user.id)
    assert found.env == {"DEMO_PIN": "pin-222333"}


def test_injected_repr_does_not_leak_values():
    # 日志、异常回溯、断言失败都会渲染 repr：明文不能跟着出去。
    found = store.Injected({"DEMO_PIN": "pin-778899"}, frozenset({"pin-778899"}), ("DEMO_PIN",))
    assert "pin-778899" not in repr(found)
    assert found.names == ("DEMO_PIN",)
