"""个人凭证的读写。

一条凭证是本人的一份值，逐条加密，AAD 绑定（凭证, 用户）；绑定（CredentialBinding）决定它以
哪个变量名注入哪个 AI 员工，一份值可以绑给多个 AI 员工。只有开轮注入会解密密文字段的明文。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import delete as sql_delete
from sqlalchemy import exists, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.crypto import Cipher, DecryptError
from coreman.core.db.models import Bot, CredentialBinding, CredentialRequest, PersonalCredential
from coreman.core.logging import get_logger
from coreman.core.personal_credentials.policy import (
    MAX_CHOICES,
    MIN_REDACT,
    handoff_aad,
    key_problem,
    value_aad,
)
from coreman.core.timeutils import utcnow

log = get_logger(__name__)
LAST_USED_EVERY = timedelta(hours=1)


@dataclass(frozen=True)
class Injected:
    """一轮注入的结果。`repr=False` 让任何 `repr`（日志、异常回溯、断言失败）都带不出明文。"""

    env: dict[str, str] = field(repr=False)
    secret_values: frozenset[str] = field(repr=False)
    names: tuple[str, ...]


async def lock(session: AsyncSession, bot_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """同一人同一员工的索取与保存排队：和 service.open_request 用同一把事务级咨询锁。"""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"credential-request:{bot_id}:{user_id}"},
    )


async def _binding(
    session: AsyncSession, bot_id: uuid.UUID, user_id: uuid.UUID, env_key: str
) -> CredentialBinding | None:
    found: CredentialBinding | None = await session.scalar(
        select(CredentialBinding)
        .where(
            CredentialBinding.bot_id == bot_id,
            CredentialBinding.user_id == user_id,
            CredentialBinding.env_key == env_key,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return found


async def _own(
    session: AsyncSession, credential_id: uuid.UUID, user_id: uuid.UUID
) -> PersonalCredential | None:
    row = await session.get(
        PersonalCredential, credential_id, with_for_update=True, populate_existing=True
    )
    return row if row is not None and row.user_id == user_id else None


def _encrypt(cipher: Cipher, row: PersonalCredential, value: str) -> None:
    row.value_enc = cipher.encrypt(value, value_aad(row.id, row.user_id))


async def create(
    session: AsyncSession,
    cipher: Cipher,
    *,
    user_id: uuid.UUID,
    env_key: str,
    label: str,
    secret: bool,
    value: str,
) -> PersonalCredential:
    # AAD 要用行 ID：先在这里定下 ID，再加密。
    row = PersonalCredential(
        id=uuid.uuid4(), user_id=user_id, env_key=env_key, label=label, secret=secret
    )
    _encrypt(cipher, row, value)
    session.add(row)
    await session.flush()
    return row


async def bind(
    session: AsyncSession,
    credential: PersonalCredential,
    *,
    bot_id: uuid.UUID,
    env_key: str,
) -> CredentialBinding:
    """把凭证以 env_key 绑给 AI 员工；这个变量名原来指向别的凭证就改指过来。"""
    found = await _binding(session, bot_id, credential.user_id, env_key)
    if found is None:
        found = CredentialBinding(
            credential_id=credential.id,
            bot_id=bot_id,
            user_id=credential.user_id,
            env_key=env_key,
        )
        session.add(found)
    elif found.credential_id != credential.id:
        found.credential_id, found.last_used_at, found.updated_at = credential.id, None, utcnow()
    await session.flush()
    return found


async def save(
    session: AsyncSession,
    cipher: Cipher,
    *,
    bot_id: uuid.UUID,
    user_id: uuid.UUID,
    fields: list[dict[str, Any]],
    values: dict[str, str],
    picks: dict[str, uuid.UUID] | None = None,
) -> list[str]:
    """表单提交落库。`picks` 里的字段直接绑定本人已有的凭证（调用方已核对归属），其余写值：

    这个变量名已经绑着凭证时原地改值（共用这条凭证的 AI 员工一起生效），否则新建一条。
    """
    picks = picks or {}
    await lock(session, bot_id, user_id)
    # 按键名排序再写：并发提交以同一顺序取行锁，避免 [A, B] 与 [B, A] 互相死锁。
    for spec in sorted(fields, key=lambda f: str(f["key"])):
        key = str(spec["key"])
        if key in picks:
            chosen = await _own(session, picks[key], user_id)
            if chosen is not None:
                await bind(session, chosen, bot_id=bot_id, env_key=key)
            continue
        label, secret = str(spec.get("label") or ""), bool(spec.get("secret", True))
        found = await _binding(session, bot_id, user_id, key)
        current = await _own(session, found.credential_id, user_id) if found else None
        if current is None:
            current = await create(
                session,
                cipher,
                user_id=user_id,
                env_key=key,
                label=label,
                secret=secret,
                value=values[key],
            )
            await bind(session, current, bot_id=bot_id, env_key=key)
            continue
        _encrypt(cipher, current, values[key])
        # 名称本人可能在「我的凭证」里改过：已有就留着，不让 agent 起的名字覆盖。
        current.label = current.label or label
        current.secret, current.updated_at = secret, utcnow()
        await session.flush()
    return sorted(str(f["key"]) for f in fields)


async def injected(
    session: AsyncSession, cipher: Cipher, *, bot_id: uuid.UUID, user_id: uuid.UUID
) -> Injected:
    rows = (
        await session.execute(
            select(CredentialBinding, PersonalCredential)
            .join(PersonalCredential, PersonalCredential.id == CredentialBinding.credential_id)
            .where(
                CredentialBinding.bot_id == bot_id,
                CredentialBinding.user_id == user_id,
                PersonalCredential.user_id == user_id,
            )
            .order_by(CredentialBinding.env_key)
            # expire_on_commit=False：同一会话里 Core UPDATE 改过的 last_used_at 不会回填到
            # identity map，必须强制按库里最新值刷新，节流才读得对。
            .execution_options(populate_existing=True)
        )
    ).all()
    now = utcnow()
    env: dict[str, str] = {}
    secrets: set[str] = set()
    used: list[uuid.UUID] = []
    for binding, credential in rows:
        # 策略收紧后的存量键名不越界。
        if key_problem(binding.env_key):
            continue
        try:
            value = cipher.decrypt(credential.value_enc, value_aad(credential.id, user_id))
        except DecryptError:
            log.warning("personal_credential_unreadable", env_key=binding.env_key)
            continue
        env[binding.env_key] = value
        if credential.secret and len(value) >= MIN_REDACT:
            secrets.add(value)
        if binding.last_used_at is None or binding.last_used_at < now - LAST_USED_EVERY:
            used.append(binding.id)
    if used:
        await session.execute(
            update(CredentialBinding)
            .where(CredentialBinding.id.in_(used))
            .values(last_used_at=now)
            .execution_options(synchronize_session=False)
        )
    return Injected(env, frozenset(secrets), tuple(env))


def handed_off(cipher: Cipher, row: CredentialRequest) -> Injected:
    """一次性交付的值，只在它的续接轮开轮时解密。已擦掉或读不出来时为空。"""
    if row.save or not row.handoff_enc:
        return Injected({}, frozenset(), ())
    try:
        values = json.loads(cipher.decrypt(row.handoff_enc, handoff_aad(row.id)))
    except (DecryptError, ValueError):
        log.warning("credential_handoff_unreadable", request_id=str(row.id))
        return Injected({}, frozenset(), ())
    secret_keys = {str(f["key"]) for f in row.fields if f.get("secret", True)}
    env = {
        key: value
        for key, value in sorted(values.items())
        if isinstance(value, str) and not key_problem(key)
    }
    secrets = frozenset(
        value for key, value in env.items() if key in secret_keys and len(value) >= MIN_REDACT
    )
    return Injected(env, secrets, tuple(env))


@dataclass(frozen=True)
class Owned:
    """「我的凭证」页的一条：凭证本身，加上它绑给了哪些 AI 员工。"""

    credential: PersonalCredential
    bindings: tuple[tuple[CredentialBinding, Bot], ...]


async def list_own(
    session: AsyncSession, user_id: uuid.UUID
) -> list[tuple[CredentialBinding, PersonalCredential, Bot]]:
    """按绑定逐行列出（AI 员工, 变量名）：旧接口与命令行用。"""
    result = await session.execute(
        select(CredentialBinding, PersonalCredential, Bot)
        .join(PersonalCredential, PersonalCredential.id == CredentialBinding.credential_id)
        .join(Bot, Bot.id == CredentialBinding.bot_id)
        .where(CredentialBinding.user_id == user_id, PersonalCredential.user_id == user_id)
        .order_by(Bot.name, CredentialBinding.env_key)
    )
    return [(binding, row, bot) for binding, row, bot in result.all()]


async def list_items(session: AsyncSession, user_id: uuid.UUID) -> list[Owned]:
    """按凭证列出，包括暂时没有绑定任何 AI 员工的。"""
    rows = (
        await session.scalars(
            select(PersonalCredential)
            .where(PersonalCredential.user_id == user_id)
            .order_by(PersonalCredential.label, PersonalCredential.env_key, PersonalCredential.id)
        )
    ).all()
    bound: dict[uuid.UUID, list[tuple[CredentialBinding, Bot]]] = {}
    for binding, bot in (
        await session.execute(
            select(CredentialBinding, Bot)
            .join(Bot, Bot.id == CredentialBinding.bot_id)
            .where(CredentialBinding.user_id == user_id)
            .order_by(Bot.name, CredentialBinding.env_key)
        )
    ).all():
        bound.setdefault(binding.credential_id, []).append((binding, bot))
    return [Owned(row, tuple(bound.get(row.id, ()))) for row in rows]


def plain_value(cipher: Cipher, row: PersonalCredential) -> str | None:
    if row.secret:
        return None
    try:
        return cipher.decrypt(row.value_enc, value_aad(row.id, row.user_id))
    except DecryptError:
        return None


async def bound_credential(
    session: AsyncSession, *, bot_id: uuid.UUID, user_id: uuid.UUID, env_key: str
) -> PersonalCredential | None:
    found = await _binding(session, bot_id, user_id, env_key)
    return await _own(session, found.credential_id, user_id) if found else None


async def update_value(
    session: AsyncSession,
    cipher: Cipher,
    *,
    credential_id: uuid.UUID,
    user_id: uuid.UUID,
    value: str,
) -> PersonalCredential | None:
    row = await _own(session, credential_id, user_id)
    if row is None:
        return None
    _encrypt(cipher, row, value)
    row.updated_at = utcnow()
    await session.flush()
    return row


async def rename(
    session: AsyncSession, *, credential_id: uuid.UUID, user_id: uuid.UUID, label: str
) -> PersonalCredential | None:
    row = await _own(session, credential_id, user_id)
    if row is None:
        return None
    row.label, row.updated_at = label, utcnow()
    await session.flush()
    return row


async def unbind(
    session: AsyncSession, *, bot_id: uuid.UUID, user_id: uuid.UUID, env_key: str
) -> bool:
    """旧的「删除一项」：解除这个变量名；凭证没有别的 AI 员工在用就一并删掉。"""
    found = await _binding(session, bot_id, user_id, env_key)
    if found is None:
        return False
    credential_id = found.credential_id
    await session.delete(found)
    await session.flush()
    others = exists().where(CredentialBinding.credential_id == credential_id)
    await session.execute(
        sql_delete(PersonalCredential)
        .where(
            PersonalCredential.id == credential_id,
            PersonalCredential.user_id == user_id,
            ~others,
        )
        .execution_options(synchronize_session=False)
    )
    return True


async def delete(session: AsyncSession, *, credential_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """删除整条凭证，所有绑定随之级联删除。"""
    result = await session.execute(
        sql_delete(PersonalCredential)
        .where(PersonalCredential.id == credential_id, PersonalCredential.user_id == user_id)
        .returning(PersonalCredential.id)
    )
    return result.first() is not None


async def set_bots(
    session: AsyncSession, credential: PersonalCredential, bot_ids: set[uuid.UUID]
) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """把凭证的绑定改成正好这些 AI 员工；返回（新加的, 去掉的）。

    新加的用凭证自己的变量名；那个 AI 员工上这个变量名原来指向别的凭证时改指过来。
    已有的绑定保留原来的变量名。
    """
    have = set(
        await session.scalars(
            select(CredentialBinding.bot_id).where(CredentialBinding.credential_id == credential.id)
        )
    )
    added = sorted(bot_ids - have, key=str)
    # 与表单保存同样先取（AI 员工, 用户）的咨询锁、再锁行，两条路径不会互相等。
    for bot_id in added:
        await lock(session, bot_id, credential.user_id)
    current = (
        await session.scalars(
            select(CredentialBinding)
            .where(CredentialBinding.credential_id == credential.id)
            .with_for_update()
        )
    ).all()
    removed = sorted((b for b in current if b.bot_id not in bot_ids), key=lambda b: str(b.bot_id))
    for binding in removed:
        await session.delete(binding)
    await session.flush()
    for bot_id in added:
        await bind(session, credential, bot_id=bot_id, env_key=credential.env_key)
    return added, [b.bot_id for b in removed]


@dataclass(frozen=True)
class Choice:
    """表单上「选已有凭证」的一个选项：只有名字，不含值。"""

    id: uuid.UUID
    label: str
    env_key: str
    bots: tuple[str, ...]

    @property
    def title(self) -> str:
        return f"{self.label or self.env_key}（{self.env_key}）"


async def choices(
    session: AsyncSession,
    *,
    bot_id: uuid.UUID,
    user_id: uuid.UUID,
    fields: list[dict[str, Any]],
) -> tuple[dict[str, list[Choice]], dict[str, uuid.UUID]]:
    """每个字段可选的已有凭证，以及默认选中项。

    候选只取保密性相同的凭证，变量名相同的排前面。当前已绑在这个 AI 员工这个变量名上的
    那条不列出：agent 再次索取，说明它多半已经不能用了。只有一条同名候选时默认选中它。
    """
    rows = (
        await session.scalars(
            select(PersonalCredential)
            .where(PersonalCredential.user_id == user_id)
            .order_by(PersonalCredential.updated_at.desc(), PersonalCredential.id)
        )
    ).all()
    if not rows:
        return {}, {}
    names: dict[uuid.UUID, list[str]] = {}
    current: dict[str, uuid.UUID] = {}
    for binding, bot in (
        await session.execute(
            select(CredentialBinding, Bot)
            .join(Bot, Bot.id == CredentialBinding.bot_id)
            .where(CredentialBinding.user_id == user_id)
            .order_by(Bot.name)
        )
    ).all():
        if bot.name not in names.setdefault(binding.credential_id, []):
            names[binding.credential_id].append(bot.name)
        if binding.bot_id == bot_id:
            current[binding.env_key] = binding.credential_id
    out: dict[str, list[Choice]] = {}
    defaults: dict[str, uuid.UUID] = {}
    for spec in fields:
        key, secret = str(spec["key"]), bool(spec.get("secret", True))
        pool = [r for r in rows if r.secret == secret and r.id != current.get(key)]
        pool.sort(key=lambda r: r.env_key != key)
        if not pool:
            continue
        out[key] = [
            Choice(r.id, r.label, r.env_key, tuple(names.get(r.id, ()))) for r in pool[:MAX_CHOICES]
        ]
        same = [r for r in pool if r.env_key == key]
        if len(same) == 1:
            defaults[key] = same[0].id
    return out, defaults
