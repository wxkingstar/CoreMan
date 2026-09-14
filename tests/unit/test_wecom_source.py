from coreman.core.contacts.wecom_source import parse_wecom_directory

DEPTS = [
    {"id": 1, "name": "公司", "parentid": 0, "order": 100},
    {"id": 2, "name": "技术", "parentid": 1, "order": 90},
]
USERS = [
    {
        "userid": "zhangsan",
        "name": "张三",
        "department": [2],
        "main_department": 2,
        "position": "工程师",
        "mobile": "13800000000",
        "email": "zhangsan@example.com",
        "biz_mail": "zs@corp.wecom.work",
        "avatar": "http://a/1",
        "status": 1,
    },
    {"userid": "gone", "name": "离职", "department": [1], "status": 5},
    {
        "userid": "off",
        "name": "禁用",
        "department": [1],
        "status": 2,
        "biz_mail": "off@corp.wecom.work",
    },
]


def test_parse_directory() -> None:
    d = parse_wecom_directory(DEPTS, USERS)
    assert d.platform == "wecom"
    assert [
        (x.platform_dept_id, x.parent_platform_dept_id, x.sort_order) for x in d.departments
    ] == [("1", None, 100), ("2", "1", 90)]
    by_id = {u.platform_user_id: u for u in d.users}
    assert set(by_id) == {"zhangsan", "off"}  # status 5 退出企业不进目录
    zs = by_id["zhangsan"]
    assert (
        zs.email == "zhangsan@example.com"
        and zs.main_dept_id == "2"
        and zs.dept_ids == ["2"]
        and zs.active
    )
    assert by_id["off"].email == "off@corp.wecom.work" and by_id["off"].active is False
