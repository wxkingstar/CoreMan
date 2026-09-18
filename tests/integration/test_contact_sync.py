import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from coreman.core.contacts.sync import DISABLE_LIMIT, ContactSyncService, SyncAborted
from coreman.core.contacts.types import Directory, DirectoryDept, DirectoryUser
from coreman.core.db.models import Department, Team, TeamRule, User, UserDepartment, UserIdentity
from coreman.core.db.session import make_session_factory


def _user(uid: str, name: str, dept: str, **kw: object) -> DirectoryUser:
    base: dict[str, object] = dict(
        platform_user_id=uid,
        name=name,
        dept_ids=[dept],
        main_dept_id=dept,
        position=None,
        mobile=None,
        email=None,
        avatar_url=None,
        active=True,
        profile={},
    )
    base.update(kw)
    return DirectoryUser(**base)  # type: ignore[arg-type]


DEPTS = [
    DirectoryDept("1", None, "公司", 100),
    DirectoryDept("2", "1", "技术", 90),
    DirectoryDept("3", "2", "服务端", 80),
]


def _dir(users: list[DirectoryUser]) -> Directory:
    return Directory(platform="wecom", departments=DEPTS, users=users)


async def test_create_merge_and_team_rule(db_engine: AsyncEngine) -> None:
    factory = make_session_factory(db_engine)
    async with factory() as s:
        team = Team(slug="backend", name_zh="服务端")
        s.add(team)
        await s.flush()
        s.add(TeamRule(team_id=team.id, dept_path_contains="服务端", sort_order=1))
        s.add(User(display_name="老账号", email="Li@Example.com", source="sync"))  # 按邮箱归并
        await s.commit()
    svc = ContactSyncService(factory)
    stats = await svc.apply(
        _dir(
            [
                _user("zhangsan", "张三", "3", email="zhangsan@example.com", position="工程师"),
                _user("lisi", "李四", "2", email="li@example.com", mobile="13900000000"),
            ]
        )
    )
    assert (stats.departments, stats.created, stats.updated, stats.disabled) == (3, 1, 1, 0)
    async with factory() as s:
        zs = (await s.execute(select(User).where(User.login_name == "zhangsan"))).scalar_one()
        assert zs.team_id == team.id and zs.position == "工程师" and zs.display_name == "张三"
        li = (await s.execute(select(User).where(User.login_name == "li"))).scalar_one()
        assert li.display_name == "李四" and li.mobile == "13900000000" and li.team_id is None
        assert (
            await s.execute(select(UserIdentity).where(UserIdentity.platform_user_id == "lisi"))
        ).scalar_one().user_id == li.id
        paths = {
            d.platform_dept_id: d.path for d in (await s.execute(select(Department))).scalars()
        }
        assert paths == {"1": "公司", "2": "公司/技术", "3": "公司/技术/服务端"}
        ud = (
            await s.execute(select(UserDepartment).where(UserDepartment.user_id == zs.id))
        ).scalar_one()
        assert ud.is_primary is True


async def test_manual_fields_and_login_name_conflict(db_engine: AsyncEngine) -> None:
    factory = make_session_factory(db_engine)
    svc = ContactSyncService(factory)
    await svc.apply(_dir([_user("wangwu", "王五", "2", position="A")]))
    async with factory() as s:
        u = (await s.execute(select(User).where(User.login_name == "wangwu"))).scalar_one()
        u.position, u.manual_fields = "手工职务", ["position"]
        s.add(User(display_name="占名", login_name="zhaoliu", source="manual"))
        await s.commit()
    stats = await svc.apply(
        _dir([_user("wangwu", "王五改", "2", position="B"), _user("zhaoliu", "赵六", "2")])
    )
    assert stats.login_name_conflicts == ["zhaoliu"]
    async with factory() as s:
        u = (await s.execute(select(User).where(User.login_name == "wangwu"))).scalar_one()
        assert u.position == "手工职务" and u.display_name == "王五改"
        zl = (
            await s.execute(select(UserIdentity).where(UserIdentity.platform_user_id == "zhaoliu"))
        ).scalar_one()
        assert (await s.get(User, zl.user_id)).login_name is None  # type: ignore[union-attr]


async def test_field_conflicts_are_skipped_and_recorded(db_engine: AsyncEngine) -> None:
    factory = make_session_factory(db_engine)
    async with factory() as s:
        s.add(
            User(
                display_name="已有",
                email="taken@example.com",
                mobile="13100000000",
                source="manual",
            )
        )
        await s.commit()
    svc = ContactSyncService(factory)
    stats = await svc.apply(
        _dir(
            [
                # 邮箱与他人冲突（大小写不同）
                _user("new1", "新人一", "2", email="Taken@Example.com"),
                # 手机与他人冲突
                _user("new2", "新人二", "2", mobile="13100000000"),
            ]
        )
    )
    assert sorted(stats.field_conflicts) == ["new1:email", "new2:mobile"]
    async with factory() as s:
        # new1 未设 email（被冲突跳过），login_name 按 _login_name_for 取的是目录里 du.email
        # 的前缀（"taken"，与目录传入的 "Taken@Example.com" 对应），而不是 platform_user_id。
        n1 = (await s.execute(select(User).where(User.login_name == "taken"))).scalar_one()
        n2 = (await s.execute(select(User).where(User.login_name == "new2"))).scalar_one()
        assert n1.email is None and n2.mobile is None
        assert (
            await s.execute(select(User).where(User.email == "taken@example.com"))
        ).scalar_one().source == "manual"


async def test_email_merge_uses_equality_not_ilike(db_engine: AsyncEngine) -> None:
    """邮箱归并必须是等值比较：ILIKE 把目录邮箱当 LIKE 模式，`_` 会通配任意字符，
    两个不同的人会被错误地归并成同一行。"""
    factory = make_session_factory(db_engine)
    async with factory() as s:
        s.add(User(display_name="已有X", email="johnXdoe@example.com", source="sync"))
        s.add(User(display_name="已有下划线", email="jane_doe@example.com", source="sync"))
        await s.commit()
    svc = ContactSyncService(factory)
    # 目录邮箱里的 `_` 在 ILIKE 下是通配符，会命中库里的 johnXdoe 那一行
    first = await svc.apply(_dir([_user("john", "John", "2", email="john_doe@example.com")]))
    assert (first.created, first.updated) == (1, 0)
    # 反向：库里带 `_`、目录里是别的字符，同样必须各自成行
    second = await svc.apply(_dir([_user("jane", "Jane", "2", email="janeXdoe@example.com")]))
    assert (second.created, second.updated) == (1, 0)
    async with factory() as s:
        assert {u.email for u in (await s.execute(select(User))).scalars()} == {
            "johnXdoe@example.com",
            "jane_doe@example.com",
            "john_doe@example.com",
            "janeXdoe@example.com",
        }


async def test_disable_missing_and_reactivate(db_engine: AsyncEngine) -> None:
    factory = make_session_factory(db_engine)
    svc = ContactSyncService(factory)
    await svc.apply(_dir([_user("a", "A", "2"), _user("b", "B", "2")]))
    stats = await svc.apply(_dir([_user("a", "A", "2")]))
    assert stats.disabled == 1
    async with factory() as s:
        b = (await s.execute(select(User).where(User.login_name == "b"))).scalar_one()
        assert b.status == "disabled"
    stats = await svc.apply(_dir([_user("a", "A", "2"), _user("b", "B", "2")]))
    assert stats.reactivated == 1


async def test_manual_disable_is_not_reactivated(db_engine: AsyncEngine) -> None:
    """管理员手工停用（manual_fields 含 status）的人，即便还在目录里也不重新激活。"""
    factory = make_session_factory(db_engine)
    svc = ContactSyncService(factory)
    await svc.apply(_dir([_user("a", "A", "2")]))
    async with factory() as s:
        u = (await s.execute(select(User).where(User.login_name == "a"))).scalar_one()
        u.status, u.manual_fields = "disabled", ["status"]
        await s.commit()
    stats = await svc.apply(_dir([_user("a", "A", "2")]))
    assert stats.reactivated == 0
    async with factory() as s:
        row = (await s.execute(select(User).where(User.login_name == "a"))).scalar_one()
        assert row.status == "disabled"


async def test_abort_when_too_many_disabled(db_engine: AsyncEngine) -> None:
    factory = make_session_factory(db_engine)
    svc = ContactSyncService(factory)
    await svc.apply(_dir([_user(f"u{i}", f"U{i}", "2") for i in range(DISABLE_LIMIT + 1)]))
    with pytest.raises(SyncAborted):
        await svc.apply(_dir([]))
    async with factory() as s:
        assert all(u.status == "active" for u in (await s.execute(select(User))).scalars())


async def test_empty_directory_aborts(db_engine: AsyncEngine) -> None:
    """空目录多半是抓取异常，不能当成「全公司都离职了」：直接中止，一行都不改。"""
    factory = make_session_factory(db_engine)
    svc = ContactSyncService(factory)
    await svc.apply(_dir([_user("a", "A", "2")]))
    with pytest.raises(SyncAborted, match="目录为空"):
        await svc.apply(_dir([]))
    async with factory() as s:
        row = (await s.execute(select(User).where(User.login_name == "a"))).scalar_one()
        assert row.status == "active"


async def test_inactive_in_directory_counts_toward_limit(db_engine: AsyncEngine) -> None:
    factory = make_session_factory(db_engine)
    svc = ContactSyncService(factory)
    await svc.apply(_dir([_user(f"u{i}", f"U{i}", "2") for i in range(DISABLE_LIMIT + 1)]))
    with pytest.raises(SyncAborted):
        await svc.apply(
            _dir([_user(f"u{i}", f"U{i}", "2", active=False) for i in range(DISABLE_LIMIT + 1)])
        )
    stats = await svc.apply(
        _dir(
            [_user("u0", "U0", "2", active=False)]
            + [_user(f"u{i}", f"U{i}", "2") for i in range(1, DISABLE_LIMIT + 1)]
        )
    )
    assert stats.disabled == 1
    stats = await svc.apply(
        _dir(
            [_user("u0", "U0", "2", active=False)]
            + [_user(f"u{i}", f"U{i}", "2") for i in range(1, DISABLE_LIMIT + 1)]
        )
    )
    assert stats.disabled == 0  # 已停用者保持停用，不重复计数


async def test_bootstrap_user_untouched(db_engine: AsyncEngine) -> None:
    factory = make_session_factory(db_engine)
    async with factory() as s:
        s.add(User(display_name="引导", role="platform_admin", source="bootstrap"))
        await s.commit()
    await ContactSyncService(factory).apply(_dir([_user("a", "A", "2")]))
    async with factory() as s:
        assert (
            await s.execute(select(User).where(User.source == "bootstrap"))
        ).scalar_one().status == "active"


async def test_feishu_identity_merges_with_wecom_and_preserves_manual_disable(db_engine):
    factory = make_session_factory(db_engine)
    service = ContactSyncService(factory)
    await service.apply(_dir([_user("wecom-a", "员工", "1", email="shared@example.com")]))
    async with factory() as session:
        user = await session.scalar(select(User))
        uid = user.id
        user.status = "disabled"
        user.manual_fields = ["status"]
        await session.commit()
    directory = Directory(
        "feishu",
        DEPTS,
        [
            _user(
                "feishu-a",
                "同一员工",
                "1",
                email="SHARED@example.com",
                profile={"open_id": "ou_a", "union_id": "on_a"},
            )
        ],
    )
    stats = await service.apply(directory)
    assert stats.created == 0
    async with factory() as session:
        users = list(await session.scalars(select(User)))
        assert len(users) == 1 and users[0].id == uid and users[0].status == "disabled"
        identity = await session.scalar(
            select(UserIdentity).where(UserIdentity.platform == "feishu")
        )
        assert (
            identity.user_id == uid and identity.open_id == "ou_a" and identity.union_id == "on_a"
        )


async def test_cross_platform_conflicting_email_and_mobile_aborts(db_engine):
    factory = make_session_factory(db_engine)
    service = ContactSyncService(factory)
    await service.apply(
        _dir(
            [
                _user("a", "A", "1", email="a@example.com", mobile="111"),
                _user("b", "B", "1", email="b@example.com", mobile="222"),
            ]
        )
    )
    with pytest.raises(SyncAborted, match="不同账号"):
        await service.apply(
            Directory("feishu", DEPTS, [_user("c", "C", "1", email="a@example.com", mobile="222")])
        )
    async with factory() as session:
        assert not list(
            await session.scalars(select(UserIdentity).where(UserIdentity.platform == "feishu"))
        )


async def test_merge_one_links_known_departments_and_disables_nobody(db_engine):
    factory = make_session_factory(db_engine)
    service = ContactSyncService(factory)
    async with factory() as session:
        team = Team(slug="backend", name_zh="服务端")
        session.add(team)
        await session.flush()
        session.add(TeamRule(team_id=team.id, platform="feishu", dept_path_contains="服务端"))
        await session.commit()
    await service.apply(
        Directory("feishu", DEPTS, [_user("old", "老员工", "2", email="old@example.com")])
    )
    newcomer = _user(
        "new",
        "新员工",
        "3",
        dept_ids=["3", "missing"],
        email="new@example.com",
        profile={"open_id": "ou_new"},
    )
    stats = await service.merge_one("feishu", newcomer)
    assert (stats.created, stats.disabled) == (1, 0)
    async with factory() as session:
        users = {u.login_name: u for u in await session.scalars(select(User))}
        assert users["old"].status == "active"
        new = users["new"]
        assert new.team_id == team.id
        paths = list(
            await session.scalars(
                select(Department.path)
                .join(UserDepartment, UserDepartment.department_id == Department.id)
                .where(UserDepartment.user_id == new.id)
            )
        )
        assert paths == ["公司/技术/服务端"]
        assert len(list(await session.scalars(select(Department)))) == 3
