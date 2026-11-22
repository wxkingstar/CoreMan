"""个人凭证的存取：逐条加密、覆盖写、多个 AI 员工共用、注入过滤与使用时间。"""

from datetime import timedelta

from sqlalchemy import select, update

from coreman.core.crypto import Cipher
from coreman.core.db.models import CredentialBinding, PersonalCredential, User
from coreman.core.personal_credentials import store
from coreman.core.timeutils import utcnow
from tests.integration.test_group_dispatch import another_bot
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


async def test_save_writes_in_key_order_so_concurrent_forms_cannot_deadlock(
    db_session, monkeypatch
):
    bot, user = await _owner(db_session)
    written: list[str] = []
    create = store.create

    async def recording_create(*args, **kwargs):
        written.append(kwargs["env_key"])
        return await create(*args, **kwargs)

    monkeypatch.setattr(store, "create", recording_create)
    # 两张表单字段顺序相反时，行锁必须按同一顺序取，否则并发提交会互相等对方的行锁。
    values = {"DEMO_USERNAME": "alice", "DEMO_PIN": "pin-778899"}
    await store.save(
        db_session, CIPHER, bot_id=bot.id, user_id=user.id, fields=FIELDS[::-1], values=values
    )
    assert written == ["DEMO_PIN", "DEMO_USERNAME"]


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
    stolen = PersonalCredential(
        user_id=user.id, env_key="DEMO_PIN", secret=True, value_enc=moved.value_enc
    )
    path = await store.create(
        db_session, CIPHER, user_id=user.id, env_key="PATH", label="", secret=False, value="/tmp"
    )
    db_session.add(stolen)
    await db_session.flush()
    db_session.add_all(
        [
            CredentialBinding(
                credential_id=stolen.id, bot_id=bot.id, user_id=user.id, env_key="DEMO_PIN"
            ),
            CredentialBinding(
                credential_id=path.id, bot_id=bot.id, user_id=user.id, env_key="PATH"
            ),
            # 指向别人凭证的绑定也不注入。
            CredentialBinding(
                credential_id=moved.id, bot_id=bot.id, user_id=user.id, env_key="DEMO_OTHER"
            ),
        ]
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
    cred = await db_session.scalar(select(PersonalCredential))
    updated = cred.updated_at
    row = await db_session.scalar(select(CredentialBinding))
    await store.injected(db_session, CIPHER, bot_id=bot.id, user_id=user.id)
    await db_session.commit()
    await db_session.refresh(row)
    first = row.last_used_at
    await db_session.refresh(cred)
    assert first is not None and cred.updated_at == updated
    await store.injected(db_session, CIPHER, bot_id=bot.id, user_id=user.id)
    await db_session.commit()
    await db_session.refresh(row)
    assert row.last_used_at == first
    await db_session.execute(
        update(CredentialBinding).values(last_used_at=utcnow() - timedelta(hours=2))
    )
    await db_session.commit()
    await store.injected(db_session, CIPHER, bot_id=bot.id, user_id=user.id)
    await db_session.commit()
    await db_session.refresh(row)
    assert row.last_used_at >= first


async def test_list_update_unbind_own(db_session):
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
    assert [(b.env_key, bot_row.id) for b, _, bot_row in rows] == [
        ("DEMO_PIN", bot.id),
        ("DEMO_USERNAME", bot.id),
    ]
    assert [store.plain_value(CIPHER, r) for _, r, _ in rows] == [None, "alice"]
    pin = await store.bound_credential(
        db_session, bot_id=bot.id, user_id=user.id, env_key="DEMO_PIN"
    )
    assert pin is not None
    assert await store.update_value(
        db_session, CIPHER, credential_id=pin.id, user_id=user.id, value="pin-222333"
    )
    # 别人的凭证 ID 改不了。
    assert (
        await store.update_value(
            db_session, CIPHER, credential_id=pin.id, user_id=bot.created_by, value="x"
        )
        is None
    )
    assert await store.unbind(db_session, bot_id=bot.id, user_id=user.id, env_key="DEMO_USERNAME")
    assert not await store.unbind(
        db_session, bot_id=bot.id, user_id=user.id, env_key="DEMO_USERNAME"
    )
    await db_session.commit()
    found = await store.injected(db_session, CIPHER, bot_id=bot.id, user_id=user.id)
    assert found.env == {"DEMO_PIN": "pin-222333"}
    # 解除最后一个绑定时凭证一起删掉。
    assert [o.credential.env_key for o in await store.list_items(db_session, user.id)] == [
        "DEMO_PIN"
    ]


async def test_one_credential_shared_by_two_bots(db_session):
    bot, user = await _owner(db_session)
    other = await another_bot(db_session, bot, "ops_bot", "运营")
    await store.save(
        db_session,
        CIPHER,
        bot_id=bot.id,
        user_id=user.id,
        fields=FIELDS[1:],
        values={"DEMO_PIN": "pin-778899"},
    )
    cred = await db_session.scalar(select(PersonalCredential))
    added, removed = await store.set_bots(db_session, cred, {bot.id, other.id})
    await db_session.commit()
    assert added == [other.id] and removed == []
    for target in (bot, other):
        found = await store.injected(db_session, CIPHER, bot_id=target.id, user_id=user.id)
        assert found.env == {"DEMO_PIN": "pin-778899"}
    # 在任一个 AI 员工的表单里重新填写，改的是同一份值，两边一起生效。
    await store.save(
        db_session,
        CIPHER,
        bot_id=other.id,
        user_id=user.id,
        fields=FIELDS[1:],
        values={"DEMO_PIN": "pin-000111"},
    )
    await db_session.commit()
    assert (await db_session.scalars(select(PersonalCredential))).all() == [cred]
    found = await store.injected(db_session, CIPHER, bot_id=bot.id, user_id=user.id)
    assert found.env == {"DEMO_PIN": "pin-000111"}
    # 去掉一个绑定不删凭证；全部去掉也留着，在「我的凭证」里显示为未绑定。
    await store.set_bots(db_session, cred, {other.id})
    await store.set_bots(db_session, cred, set())
    await db_session.commit()
    items = await store.list_items(db_session, user.id)
    assert [(o.credential.id, o.bindings) for o in items] == [(cred.id, ())]
    assert (await store.injected(db_session, CIPHER, bot_id=other.id, user_id=user.id)).env == {}


async def test_binding_a_bot_replaces_its_variable_of_the_same_name(db_session):
    bot, user = await _owner(db_session)
    other = await another_bot(db_session, bot, "ops_bot", "运营")
    for target, pin in ((bot, "pin-aaaaaa"), (other, "pin-bbbbbb")):
        await store.save(
            db_session,
            CIPHER,
            bot_id=target.id,
            user_id=user.id,
            fields=FIELDS[1:],
            values={"DEMO_PIN": pin},
        )
    first = await store.bound_credential(
        db_session, bot_id=bot.id, user_id=user.id, env_key="DEMO_PIN"
    )
    await store.set_bots(db_session, first, {bot.id, other.id})
    await db_session.commit()
    found = await store.injected(db_session, CIPHER, bot_id=other.id, user_id=user.id)
    assert found.env == {"DEMO_PIN": "pin-aaaaaa"}


async def test_save_with_pick_binds_an_existing_credential(db_session):
    bot, user = await _owner(db_session)
    other = await another_bot(db_session, bot, "ops_bot", "运营")
    await store.save(
        db_session,
        CIPHER,
        bot_id=bot.id,
        user_id=user.id,
        fields=FIELDS[1:],
        values={"DEMO_PIN": "pin-778899"},
    )
    cred = await db_session.scalar(select(PersonalCredential))
    # 另一个 AI 员工要的变量名不同：选中已有凭证，按它要的名字注入。
    fields = [{"key": "OPS_PIN", "label": "运营 PIN", "secret": True}]
    await store.save(
        db_session,
        CIPHER,
        bot_id=other.id,
        user_id=user.id,
        fields=fields,
        values={},
        picks={"OPS_PIN": cred.id},
    )
    await db_session.commit()
    assert (await db_session.scalars(select(PersonalCredential))).all() == [cred]
    found = await store.injected(db_session, CIPHER, bot_id=other.id, user_id=user.id)
    assert found.env == {"OPS_PIN": "pin-778899"}


async def test_choices_list_same_secrecy_and_default_to_the_one_with_the_same_name(db_session):
    bot, user = await _owner(db_session)
    other = await another_bot(db_session, bot, "ops_bot", "运营")
    await store.save(
        db_session,
        CIPHER,
        bot_id=other.id,
        user_id=user.id,
        fields=FIELDS,
        values={"DEMO_USERNAME": "alice", "DEMO_PIN": "pin-778899"},
    )
    await db_session.commit()
    options, defaults = await store.choices(
        db_session, bot_id=bot.id, user_id=user.id, fields=FIELDS
    )
    assert [c.env_key for c in options["DEMO_PIN"]] == ["DEMO_PIN"]
    assert [c.env_key for c in options["DEMO_USERNAME"]] == ["DEMO_USERNAME"]
    assert options["DEMO_PIN"][0].bots == ("运营",)
    assert options["DEMO_PIN"][0].title == "PIN（DEMO_PIN）"
    assert defaults["DEMO_PIN"] == options["DEMO_PIN"][0].id
    # 已经绑在这个 AI 员工这个变量名上的不再列出：再次索取说明它多半不能用了。
    own, own_defaults = await store.choices(
        db_session, bot_id=other.id, user_id=user.id, fields=FIELDS
    )
    assert own == {} and own_defaults == {}
    # 别人看不到。
    assert await store.choices(
        db_session, bot_id=bot.id, user_id=bot.created_by, fields=FIELDS
    ) == ({}, {})


def test_injected_repr_does_not_leak_values():
    # 日志、异常回溯、断言失败都会渲染 repr：明文不能跟着出去。
    found = store.Injected({"DEMO_PIN": "pin-778899"}, frozenset({"pin-778899"}), ("DEMO_PIN",))
    assert "pin-778899" not in repr(found)
    assert found.names == ("DEMO_PIN",)


async def test_resubmitting_keeps_a_name_the_owner_chose(db_session):
    bot, user = await _owner(db_session)
    await store.save(
        db_session,
        CIPHER,
        bot_id=bot.id,
        user_id=user.id,
        fields=FIELDS[1:],
        values={"DEMO_PIN": "pin-778899"},
    )
    cred = await db_session.scalar(select(PersonalCredential))
    await store.rename(db_session, credential_id=cred.id, user_id=user.id, label="Demo 系统 PIN")
    # agent 再次索取时起了别的名字：值更新，本人改过的名称不变。
    await store.save(
        db_session,
        CIPHER,
        bot_id=bot.id,
        user_id=user.id,
        fields=[{"key": "DEMO_PIN", "label": "PIN 码", "secret": True}],
        values={"DEMO_PIN": "pin-000111"},
    )
    await db_session.commit()
    await db_session.refresh(cred)
    assert cred.label == "Demo 系统 PIN"
    found = await store.injected(db_session, CIPHER, bot_id=bot.id, user_id=user.id)
    assert found.env == {"DEMO_PIN": "pin-000111"}
    assert (
        await store.rename(db_session, credential_id=cred.id, user_id=bot.created_by, label="x")
        is None
    )
