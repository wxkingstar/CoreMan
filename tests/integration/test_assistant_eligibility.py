"""开通范围：全员 / 名单里的人与部门（含下级部门）；账号停用或没有登录名的不能开通；可选的 AI。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import eligibility
from coreman.core.db.models import Department, Setting, User, UserDepartment


async def person(session: AsyncSession, **kw: object) -> User:
    user = User(login_name=f"u{uuid.uuid4().hex[:8]}", display_name="张三", **kw)
    session.add(user)
    await session.flush()
    return user


async def department(session: AsyncSession, parent: Department | None = None) -> Department:
    code = uuid.uuid4().hex[:8]
    row = Department(
        platform="feishu",
        platform_dept_id=code,
        parent_id=parent.id if parent else None,
        name=f"部门{code}",
        path=f"/{code}",
    )
    session.add(row)
    await session.flush()
    return row


async def scope(session: AsyncSession, **rule: object) -> None:
    session.add(Setting(key="assistant_eligibility", value={"mode": "listed", **rule}))
    await session.flush()


async def test_everyone_is_eligible_by_default(db_session: AsyncSession) -> None:
    assert await eligibility.refusal(db_session, await person(db_session)) is None
    assert await eligibility.backends(db_session) == ["claude", "codex"]


async def test_disabled_accounts_and_accounts_without_a_login_name_are_refused(
    db_session: AsyncSession,
) -> None:
    assert await eligibility.refusal(db_session, await person(db_session, status="disabled")) == (
        "inactive"
    )
    nameless = await person(db_session)
    nameless.login_name = None
    assert await eligibility.refusal(db_session, nameless) == "no_login_name"


async def test_a_listed_person_is_eligible(db_session: AsyncSession) -> None:
    listed, other = await person(db_session), await person(db_session)
    await scope(db_session, user_ids=[str(listed.id)], department_ids=[])
    assert await eligibility.refusal(db_session, listed) is None
    assert await eligibility.refusal(db_session, other) == "not_listed"


async def test_a_listed_department_covers_its_sub_departments(db_session: AsyncSession) -> None:
    top = await department(db_session)
    child = await department(db_session, top)
    grandchild = await department(db_session, child)
    sibling = await department(db_session)
    inside, outside = await person(db_session), await person(db_session)
    db_session.add_all(
        [
            UserDepartment(user_id=inside.id, department_id=grandchild.id),
            UserDepartment(user_id=outside.id, department_id=sibling.id),
        ]
    )
    await scope(db_session, user_ids=[], department_ids=[str(child.id)])
    assert await eligibility.refusal(db_session, inside) is None
    assert await eligibility.refusal(db_session, outside) == "not_listed"


@pytest.mark.parametrize("value", [["all"], {"mode": "everyone"}, "all", {"mode": None}])
async def test_a_broken_setting_lets_nobody_in(db_session: AsyncSession, value: object) -> None:
    db_session.add(Setting(key="assistant_eligibility", value=value))
    await db_session.flush()
    assert await eligibility.refusal(db_session, await person(db_session)) == "not_listed"


async def test_backends_keep_only_supported_ones_in_order(db_session: AsyncSession) -> None:
    db_session.add(
        Setting(key="assistant_backends", value=["codex", "opencode", "codex", "claude"])
    )
    await db_session.flush()
    assert await eligibility.backends(db_session) == ["codex", "claude"]


async def test_a_null_setting_row_is_broken_not_missing(db_session: AsyncSession) -> None:
    """没有这一行用默认值（全员）；有这一行而值是 null 属于写坏了：谁都不放行，也没有 AI 可选。"""
    db_session.add_all(
        [
            Setting(key="assistant_eligibility", value=None),
            Setting(key="assistant_backends", value=None),
        ]
    )
    await db_session.flush()
    assert await eligibility.refusal(db_session, await person(db_session)) == "not_listed"
    assert await eligibility.backends(db_session) == []


@pytest.mark.parametrize(
    "rule",
    [
        {"mode": "listed", "user_ids": 5},
        {"mode": "listed", "department_ids": 5},
        {"mode": "all", "user_ids": 5},
    ],
)
async def test_a_rule_with_wrong_types_lets_nobody_in(
    db_session: AsyncSession, rule: dict[str, object]
) -> None:
    db_session.add(Setting(key="assistant_eligibility", value=rule))
    await db_session.flush()
    assert await eligibility.refusal(db_session, await person(db_session)) == "not_listed"


async def test_ids_written_directly_are_read_like_the_admin_api_reads_them(
    db_session: AsyncSession,
) -> None:
    """大写的编号按管理接口的规则归一成小写；带空白或名单超长的整条规则作废。"""
    listed = await person(db_session)
    await scope(db_session, user_ids=[str(listed.id).upper()])
    assert await eligibility.refusal(db_session, listed) is None
    row = await db_session.get(Setting, "assistant_eligibility")
    assert row is not None
    row.value = {"mode": "listed", "user_ids": [f" {listed.id} "]}
    await db_session.flush()
    assert await eligibility.refusal(db_session, listed) == "not_listed"
    row.value = {"mode": "listed", "user_ids": [str(listed.id)] * 5001}
    await db_session.flush()
    assert await eligibility.refusal(db_session, listed) == "not_listed"
