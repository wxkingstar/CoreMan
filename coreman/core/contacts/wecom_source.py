"""企微通讯录抓取：department/list 全量 + user/list(department_id=1, fetch_child=1)。"""

from __future__ import annotations

from typing import Any

from coreman.core.contacts.types import Directory, DirectoryDept, DirectoryUser
from coreman.core.platforms.wecom import WeComClient

_LEFT_CORP = 5
_DISABLED = 2


def parse_wecom_directory(
    departments: list[dict[str, Any]], users: list[dict[str, Any]]
) -> Directory:
    """把企微 department/list、user/list 的原始返回转成平台无关的 Directory。

    Args:
        departments: department/list 返回的部门列表
        users: user/list 返回的成员列表

    Returns:
        平台无关的通讯录目录（status 5「退出企业」的成员被过滤掉）
    """
    depts = [
        DirectoryDept(
            platform_dept_id=str(d["id"]),
            parent_platform_dept_id=str(d["parentid"]) if d.get("parentid") else None,
            name=str(d["name"]),
            sort_order=int(d.get("order", 0)),
        )
        for d in departments
    ]
    out: list[DirectoryUser] = []
    for u in users:
        status = int(u.get("status", 1))
        if status == _LEFT_CORP:
            continue
        dept_ids = [str(x) for x in u.get("department", [])]
        email = (u.get("email") or u.get("biz_mail") or "").strip() or None
        out.append(
            DirectoryUser(
                platform_user_id=str(u["userid"]),
                name=str(u.get("name") or u["userid"]),
                dept_ids=dept_ids,
                main_dept_id=str(u["main_department"])
                if u.get("main_department")
                else (dept_ids[0] if dept_ids else None),
                position=(u.get("position") or "").strip() or None,
                mobile=(u.get("mobile") or "").strip() or None,
                email=email,
                avatar_url=u.get("avatar") or None,
                active=status != _DISABLED,
                profile={
                    k: u[k] for k in ("alias", "gender", "telephone", "english_name") if u.get(k)
                },
            )
        )
    return Directory(platform="wecom", departments=depts, users=out)


async def fetch_wecom_directory(client: WeComClient) -> Directory:
    """从企微拉取全量部门与成员并解析为 Directory。

    Args:
        client: 已配置好通讯录同步 secret 的企微客户端

    Returns:
        平台无关的通讯录目录
    """
    departments = await client.department_list()
    users = await client.user_list(department_id=1, fetch_child=True)
    return parse_wecom_directory(departments, users)
