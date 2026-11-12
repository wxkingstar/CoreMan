"""谁能开通个人助理、用哪种 AI：管理台的两个设置项，开通的每一步都按当时的值重新判断。

- `assistant_eligibility`：`{"mode": "all" | "listed", "department_ids": [], "user_ids": []}`。
  `listed` 时本人在名单里，或本人所在的任一部门（含上级部门）在名单里才能开通。
- `assistant_backends`：开通时按顺序挑第一个池里有可用节点的 AI（`claude` / `codex`）。

另外两条不看设置：账号是启用的，且有登录名（审批单的发起人按登录名认人，见审批闸门）。
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import Department, Setting, User, UserDepartment
from coreman.core.settings_schema import SETTING_DEFAULTS, AssistantEligibility

ELIGIBILITY_KEY = "assistant_eligibility"
BACKENDS_KEY = "assistant_backends"
SUPPORTED_BACKENDS = ("claude", "codex")
# 给本人看的原因（不带任何内部信息）。
REASONS = {
    "inactive": "你的账号已停用，开通不了个人助理。",
    "no_login_name": "你的账号还没有登录名，开通不了个人助理，请联系管理员。",
    "not_listed": "你暂时不在个人助理的开通范围内，如需开通请联系管理员。",
}


async def setting(session: AsyncSession, key: str) -> Any:
    """直接读 settings 表（不经进程内缓存）：管理员刚改的范围要立即生效。

    没有这一行才用默认值；有这一行而值是 null 属于写坏了，原样返回 None（调用方一律按坏值处理）。
    """
    found = (await session.execute(select(Setting.value).where(Setting.key == key))).first()
    return SETTING_DEFAULTS[key] if found is None else found[0]


async def backends(session: AsyncSession) -> list[str]:
    raw = await setting(session, BACKENDS_KEY)
    found = [b for b in raw if b in SUPPORTED_BACKENDS] if isinstance(raw, list) else []
    return list(dict.fromkeys(found))


async def _departments_with_ancestors(session: AsyncSession, user_id: uuid.UUID) -> set[str]:
    """本人所在的部门及其全部上级（部门树不会成环，UNION 去重也挡住了万一的环）。"""
    start = (
        select(Department.id, Department.parent_id)
        .join(UserDepartment, UserDepartment.department_id == Department.id)
        .where(UserDepartment.user_id == user_id)
        .cte("up", recursive=True)
    )
    tree = start.union(
        select(Department.id, Department.parent_id).join(start, Department.id == start.c.parent_id)
    )
    return {str(row) for row in await session.scalars(select(tree.c.id))}


async def refusal(session: AsyncSession, user: User) -> str | None:
    """这个人现在不能开通的原因代码（`REASONS` 的键）；能开通返回 None。"""
    if user.status != "active":
        return "inactive"
    if not user.login_name:
        return "no_login_name"
    try:
        # 与管理接口同一套校验：编号格式、名单长度都要对。
        rule = AssistantEligibility.model_validate(await setting(session, ELIGIBILITY_KEY))
    except ValidationError:
        # 设置被写坏（管理接口有校验，坏值只可能来自直接改库）：谁都不放行。
        return "not_listed"
    if rule.mode == "all":
        return None
    if str(user.id) in set(rule.user_ids):
        return None
    wanted = set(rule.department_ids)
    if wanted and wanted & await _departments_with_ancestors(session, user.id):
        return None
    return "not_listed"
