from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import Department, PlatformApp, User, UserIdentity


async def test_only_one_bootstrap_user(db_session: AsyncSession) -> None:
    db_session.add(User(display_name="引导 A", role="platform_admin", source="bootstrap"))
    await db_session.commit()
    db_session.add(User(display_name="引导 B", role="platform_admin", source="bootstrap"))
    try:
        await db_session.commit()
        raise AssertionError("第二个 bootstrap 用户应违反 users_bootstrap_uk")
    except IntegrityError:
        await db_session.rollback()


async def test_identity_unique_per_platform(db_session: AsyncSession) -> None:
    u = User(display_name="张三", login_name="zhangsan")
    db_session.add(u)
    await db_session.flush()
    db_session.add(UserIdentity(user_id=u.id, platform="wecom", platform_user_id="zhangsan"))
    await db_session.commit()
    db_session.add(UserIdentity(user_id=u.id, platform="wecom", platform_user_id="zhangsan"))
    try:
        await db_session.commit()
        raise AssertionError("同平台同 userid 应唯一")
    except IntegrityError:
        await db_session.rollback()


async def test_department_tree_and_platform_app_defaults(db_session: AsyncSession) -> None:
    root = Department(platform="wecom", platform_dept_id="1", name="公司", path="公司")
    db_session.add(root)
    await db_session.flush()
    child = Department(
        platform="wecom", platform_dept_id="2", parent_id=root.id, name="技术", path="公司/技术"
    )
    app = PlatformApp(
        platform="wecom",
        name="通讯录同步",
        capabilities=["contact_sync"],
        corp_id="ww1",
        secret_enc="enc:v1:x",
    )
    db_session.add_all([child, app])
    await db_session.commit()
    got = (await db_session.execute(select(PlatformApp))).scalar_one()
    assert got.version == 1 and got.enabled is True and got.extra == {}
    kids = (
        (await db_session.execute(select(Department).where(Department.parent_id == root.id)))
        .scalars()
        .all()
    )
    assert [d.name for d in kids] == ["技术"]


async def test_source_check_constraint(db_session: AsyncSession) -> None:
    db_session.add(User(display_name="x", source="bogus"))
    try:
        await db_session.commit()
        raise AssertionError("source 非法值应被 CHECK 拒绝")
    except IntegrityError:
        await db_session.rollback()
    assert (await db_session.execute(text("SELECT 1"))).scalar_one() == 1
