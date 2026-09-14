"""完整抓取成功后才把飞书目录交给公共归并服务。"""

from __future__ import annotations

import asyncio
from typing import Any

from coreman.core.contacts.types import Directory, DirectoryDept, DirectoryUser
from coreman.core.platforms.feishu import FeishuClient, FeishuError


def parse_feishu_directory(
    departments: list[dict[str, Any]], users: list[dict[str, Any]]
) -> Directory:
    depts = []
    ids: set[str] = set()
    for row in departments:
        did = row.get("department_id")
        if not isinstance(did, str) or not did or did in ids:
            raise FeishuError(-2, "invalid or duplicate department id")
        ids.add(did)
        depts.append(
            DirectoryDept(
                did,
                row.get("parent_department_id") or None,
                str(row.get("name") or did),
                int(row.get("order") or 0),
            )
        )
    members: dict[str, DirectoryUser] = {}
    for row in users:
        uid = row.get("user_id")
        if not isinstance(uid, str) or not uid:
            raise FeishuError(-2, "user_id permission required")
        status = row.get("status") or {}
        if status.get("is_resigned") or status.get("is_exited"):
            continue
        departments_of_user = [str(v) for v in row.get("department_ids") or []]
        user = DirectoryUser(
            platform_user_id=uid,
            name=str(row.get("name") or uid),
            dept_ids=departments_of_user,
            main_dept_id=departments_of_user[0] if departments_of_user else None,
            position=row.get("job_title") or None,
            mobile=row.get("mobile") or None,
            email=(row.get("enterprise_email") or row.get("email") or "").strip() or None,
            avatar_url=(row.get("avatar") or {}).get("avatar_240") or None,
            active=not status.get("is_frozen", False) and not status.get("is_unjoin", False),
            profile={"open_id": row.get("open_id"), "union_id": row.get("union_id")},
        )
        previous = members.get(uid)
        if previous and previous != user:
            raise FeishuError(-2, "inconsistent user across directory pages")
        members[uid] = user
    return Directory("feishu", depts, list(members.values()))


async def fetch_feishu_directory(client: FeishuClient) -> Directory:
    async with asyncio.timeout(600):
        params = {"department_id_type": "department_id", "user_id_type": "user_id"}
        # An individual-only grant does not authorize reading department 0.
        roots: set[str] = set()
        direct_users: set[str] = set()
        page_token = ""
        seen: set[str] = set()
        for _ in range(2000):
            body = await client.call(
                "GET",
                "/open-apis/contact/v3/scopes",
                params={**params, "page_size": 100, "page_token": page_token},
            )
            data = body.get("data")
            if not isinstance(data, dict):
                raise FeishuError(-2, "invalid scope response")
            if data.get("group_ids"):
                raise FeishuError(-2, "group-based directory scope is not supported")
            for key, target in (("department_ids", roots), ("user_ids", direct_users)):
                values = data.get(key, [])
                if not isinstance(values, list) or any(
                    not isinstance(v, str) or not v or "/" in v or "?" in v or "#" in v
                    for v in values
                ):
                    raise FeishuError(-2, "invalid directory scope identifiers")
                target.update(values)
            if len(roots) + len(direct_users) > 100_000:
                raise FeishuError(-2, "directory scope limit exceeded")
            if data.get("has_more") is False:
                break
            next_token = data.get("page_token")
            if not isinstance(next_token, str) or not next_token or next_token in seen:
                raise FeishuError(-2, "incomplete scope pagination")
            seen.add(next_token)
            page_token = next_token
        else:
            raise FeishuError(-2, "scope page limit exceeded")
        departments: dict[str, dict[str, Any]] = {}
        for did in sorted(roots):
            if did != "0":
                body = await client.call(
                    "GET", f"/open-apis/contact/v3/departments/{did}", params=params
                )
                row = (body.get("data") or {}).get("department")
                if not isinstance(row, dict) or row.get("department_id") != did:
                    raise FeishuError(-2, "invalid scoped department")
                departments[did] = row
            for row in await client.pages(
                f"/open-apis/contact/v3/departments/{did}/children",
                {**params, "fetch_child": "true"},
            ):
                identity = row.get("department_id")
                if not isinstance(identity, str) or not identity:
                    raise FeishuError(-2, "invalid department id")
                if identity in departments and departments[identity] != row:
                    raise FeishuError(-2, "inconsistent scoped department")
                departments[identity] = row
        users: list[dict[str, Any]] = []
        for did in sorted(set(departments) | roots):
            users.extend(
                await client.pages(
                    "/open-apis/contact/v3/users/find_by_department",
                    {**params, "department_id": did},
                )
            )
            if len(users) > 200_000:
                raise FeishuError(-2, "directory membership limit exceeded")
        already = {row.get("user_id") for row in users}
        for uid in sorted(direct_users - already):
            body = await client.call("GET", f"/open-apis/contact/v3/users/{uid}", params=params)
            row = (body.get("data") or {}).get("user")
            if not isinstance(row, dict) or row.get("user_id") != uid:
                raise FeishuError(-2, "invalid scoped user")
            users.append(row)
        return parse_feishu_directory(list(departments.values()), users)
