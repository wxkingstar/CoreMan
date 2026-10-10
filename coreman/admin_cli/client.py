"""管理命令的进程内客户端：在 API 镜像里直接驱动管理后台的同一套接口。

不走网络，也不另写一套业务逻辑：以指定成员的身份建一个短期管理台会话，请求交给 FastAPI
应用本身处理，权限、校验、审计日志和变更通知都与在管理后台点按钮一致；结束时吊销会话。
"""

from __future__ import annotations

import contextlib
import os
import secrets
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select

from coreman.core.config import get_settings
from coreman.core.db.models import AdminSession, User

ACTOR_ENV = "COREMAN_CLI_USER"
# 会话只活一次命令；进程被杀没来得及吊销时，最多再留一小时。
SESSION_TTL = timedelta(hours=1)
PAGE_SIZE = 200


class CliError(Exception):
    """命令失败：消息原样给用户看，进程以 1 退出。"""


def is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


class AdminClient:
    def __init__(self, http: httpx.AsyncClient, actor: User) -> None:
        self.http = http
        self.actor = actor

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        version: int | None = None,
    ) -> Any:
        """发一个管理 API 请求，返回 data；接口报错时抛 CliError（带字段级校验信息）。"""
        headers = {"If-Match": f'"{version}"'} if version is not None else None
        query = {k: v for k, v in (params or {}).items() if v is not None}
        resp = await self.http.request(method, path, params=query, json=body, headers=headers)
        payload = resp.json() if resp.content else None
        if resp.status_code >= 400 or (isinstance(payload, dict) and payload.get("code", 0) != 0):
            message = payload.get("message") if isinstance(payload, dict) else None
            detail = [
                f"{'.'.join(str(p) for p in err.get('loc', [])[1:])}: {err.get('msg')}"
                for err in (payload.get("errors") or [] if isinstance(payload, dict) else [])
                if isinstance(err, dict)
            ]
            text = message or resp.text or f"HTTP {resp.status_code}"
            raise CliError("；".join([text, *detail]))
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    async def get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params=params)

    async def collect(self, path: str, **params: Any) -> list[dict[str, Any]]:
        """分页列表全部取回。"""
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            data = await self.get(path, **params, page=page, per_page=PAGE_SIZE)
            items.extend(data["items"])
            if not data["items"] or len(items) >= data["total"]:
                return items
            page += 1

    # ---- 名称解析：命令行里都用人认得的名字，接口要的是 id ----

    async def bot(self, ref: str) -> dict[str, Any]:
        """按 id、标识（bot_key）或名称找 AI 员工，返回详情。"""
        if is_uuid(ref):
            return await self.get(f"/api/admin/bots/{ref}")  # type: ignore[no-any-return]
        rows = await self.collect("/api/admin/bots", scope="all", keyword=ref)
        found = [b for b in rows if b["bot_key"] == ref] or [b for b in rows if b["name"] == ref]
        bot = one(found, ref, "AI 员工", lambda b: f"{b['bot_key']}（{b['name']}）")
        return await self.get(f"/api/admin/bots/{bot['id']}")  # type: ignore[no-any-return]

    async def user(self, ref: str) -> dict[str, Any]:
        """按 id、登录名、邮箱或姓名找成员。"""
        if is_uuid(ref):
            return await self.get(f"/api/admin/users/{ref}")  # type: ignore[no-any-return]
        rows = await self.collect("/api/admin/users", keyword=ref)
        found = (
            [u for u in rows if u["login_name"] == ref]
            or [u for u in rows if u.get("email") == ref]
            or [u for u in rows if u["display_name"] == ref]
        )
        return one(found, ref, "成员", lambda u: f"{u['login_name']}（{u['display_name']}）")

    async def team(self, ref: str) -> dict[str, Any]:
        """按 id、slug 或中文名找团队。"""
        rows = await self.get("/api/admin/teams")
        found = (
            [t for t in rows if t["id"] == ref]
            or [t for t in rows if t["slug"] == ref]
            or [t for t in rows if t["name_zh"] == ref]
        )
        return one(found, ref, "团队", lambda t: f"{t['slug']}（{t['name_zh']}）")

    async def skill(self, ref: str) -> dict[str, Any]:
        """按 id 或名称找目录里的技能。"""
        rows = await self.collect("/api/admin/skills")
        found = [s for s in rows if str(s["id"]) == ref] or [s for s in rows if s["name"] == ref]
        return one(found, ref, "技能", lambda s: s["name"])

    async def system(self, key: str) -> dict[str, Any]:
        rows = await self.collect("/api/admin/systems")
        return one([s for s in rows if s["key"] == key], key, "业务系统", lambda s: s["key"])


def one(found: list[dict[str, Any]], ref: str, kind: str, label: Any) -> dict[str, Any]:
    if not found:
        raise CliError(f"找不到{kind}：{ref}")
    if len(found) > 1:
        raise CliError(
            f"{kind}「{ref}」匹配到多个：{'、'.join(label(x) for x in found)}，请写得更精确"
        )
    return found[0]


@contextlib.asynccontextmanager
async def connect(actor_login: str | None) -> AsyncIterator[AdminClient]:
    """起一个不监听端口的 API 应用，以 actor_login 的身份建会话，用完吊销。"""
    # 应用只在真正执行命令时才导入：--help 和其它运维命令不必加载整套路由。
    from coreman.api.main import create_app
    from coreman.api.security import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, sign_session_id

    login = actor_login or os.environ.get(ACTOR_ENV)
    if not login:
        raise CliError(f"请用 --as 登录名 指定以谁的身份操作（或设置环境变量 {ACTOR_ENV}）")
    # 只留告警以上的日志：命令行的输出是给人看的结果，不是服务日志。
    settings = get_settings().model_copy(update={"log_level": "WARNING"})
    app = create_app(settings)
    async with contextlib.AsyncExitStack() as stack:
        # lifespan 里配置日志时取的是当时的 sys.stdout；改成 stderr，stdout 只留命令结果。
        with contextlib.redirect_stdout(sys.stderr):
            await stack.enter_async_context(app.router.lifespan_context(app))
        factory = app.state.session_factory
        async with factory() as session:
            actor = await session.scalar(select(User).where(User.login_name == login))
            if actor is None or actor.status != "active":
                raise CliError(f"成员 {login} 不存在或已停用")
            now = datetime.now(UTC)
            row = AdminSession(
                user_id=actor.id,
                auth_method="cli",
                user_agent="coreman-cli",
                expires_at=now + SESSION_TTL,
                last_seen_at=now,
            )
            session.add(row)
            await session.commit()
            session_id = row.id
        csrf = secrets.token_urlsafe(16)
        token = sign_session_id(settings.session_secret, session_id)
        # Cookie 直接写在请求头上：不进 cookie 罐，服务端续期时下发的 Secure cookie 不会把它顶掉。
        headers = {
            "Cookie": f"{SESSION_COOKIE}={token}; {CSRF_COOKIE}={csrf}",
            CSRF_HEADER: csrf,
            "User-Agent": "coreman-cli",
        }
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://coreman-cli", headers=headers, timeout=300
            ) as http:
                yield AdminClient(http, actor)
        finally:
            async with factory() as session:
                stored = await session.get(AdminSession, session_id)
                if stored is not None:
                    stored.revoked_at = datetime.now(UTC)
                    await session.commit()
