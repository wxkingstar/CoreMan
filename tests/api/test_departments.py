import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import Department, User, UserDepartment
from tests.api.conftest import login_as


async def test_department_tree(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    root = Department(
        platform="wecom", platform_dept_id="1", name="公司", path="公司", sort_order=100
    )
    db_session.add(root)
    await db_session.flush()
    tech = Department(
        platform="wecom",
        platform_dept_id="2",
        parent_id=root.id,
        name="技术",
        path="公司/技术",
        sort_order=90,
    )
    sales = Department(
        platform="wecom",
        platform_dept_id="3",
        parent_id=root.id,
        name="销售",
        path="公司/销售",
        sort_order=95,
    )
    db_session.add_all([tech, sales])
    await db_session.flush()
    u = User(display_name="张三")
    db_session.add(u)
    await db_session.flush()
    db_session.add(UserDepartment(user_id=u.id, department_id=tech.id, is_primary=True))
    await db_session.commit()
    await login_as(client, db_session)
    tree = (await client.get("/api/admin/departments", params={"platform": "wecom"})).json()["data"]
    assert len(tree) == 1 and tree[0]["name"] == "公司"
    assert [c["name"] for c in tree[0]["children"]] == ["销售", "技术"]
    assert tree[0]["children"][1]["member_count"] == 1
