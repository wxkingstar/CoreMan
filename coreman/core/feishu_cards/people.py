"""people 块与表格 person 列的人员解析：邮箱、登录名或姓名 → 飞书 user_id。

只做展示（头像加名字，不发通知），所以解析不到就按文字显示，不猜：

- 只认在职（active）、有飞书身份的成员，引导账号不算；
- 优先级：邮箱（不分大小写）> 登录名 > 姓名；姓名同时对应好几个人时不解析；
- 用 user_id 而不是 open_id：open_id 按应用区分，库里存的未必是当前机器人应用的。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import User, UserIdentity


async def resolve_people(session: AsyncSession, names: Sequence[str]) -> dict[str, str]:
    """返回「模型写的原文 → 飞书 user_id」；解析不到的名字不在结果里。"""
    wanted = {name: name.strip() for name in names if name.strip()}
    if not wanted:
        return {}
    keys = set(wanted.values())
    emails = {key.lower() for key in keys if "@" in key}
    rows = await session.execute(
        select(
            User.id, User.email, User.login_name, User.display_name, UserIdentity.platform_user_id
        )
        .join(UserIdentity, UserIdentity.user_id == User.id)
        .where(
            UserIdentity.platform == "feishu",
            User.status == "active",
            User.source != "bootstrap",
            or_(
                func.lower(User.email).in_(emails),
                User.login_name.in_(keys),
                User.display_name.in_(keys),
            ),
        )
        .order_by(User.id, UserIdentity.platform_user_id)
    )
    by_email: dict[str, str] = {}
    by_login: dict[str, str] = {}
    by_name: dict[str, dict[uuid.UUID, str]] = {}
    for user_id, email, login, display, platform_user_id in rows.all():
        if email:
            by_email.setdefault(email.lower(), platform_user_id)
        if login:
            by_login.setdefault(login, platform_user_id)
        by_name.setdefault(display, {}).setdefault(user_id, platform_user_id)
    out: dict[str, str] = {}
    for original, key in wanted.items():
        found = by_email.get(key.lower()) if "@" in key else None
        found = found or by_login.get(key)
        if found is None and len(by_name.get(key, {})) == 1:
            found = next(iter(by_name[key].values()))
        if found:
            out[original] = found
    return out
