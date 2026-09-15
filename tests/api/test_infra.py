import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import get_session
from coreman.api.pagination import PageParams, paginate
from coreman.api.permissions import require_roles
from coreman.api.versioning import require_if_match, set_etag
from coreman.core.db.models import Team
from tests.api.conftest import login_as


@pytest.fixture
def infra_app(app: FastAPI) -> FastAPI:
    r = APIRouter(prefix="/api/admin/_t", tags=["test"])

    @r.get("/teams")
    async def _list(
        params: PageParams = Depends(), session: AsyncSession = Depends(get_session)
    ) -> dict[str, object]:
        page = await paginate(session, select(Team).order_by(Team.slug), params)
        return {"code": 0, "data": {**page, "items": [t.slug for t in page["items"]]}}

    @r.put("/versioned/{v}")
    async def _put(v: int, request: Request, response: Response) -> dict[str, object]:
        require_if_match(request, v)
        set_etag(response, v + 1)
        return {"code": 0, "data": None}

    @r.get("/admins-only", dependencies=[Depends(require_roles("platform_admin"))])
    async def _admins() -> dict[str, object]:
        return {"code": 0, "data": "ok"}

    app.include_router(r)
    # spa.py 的 `/{full_path:path}` GET 兜底路由是 create_app() 里早注册的普通路由（不是 FastAPI
    # 0.141 新增的 .frontend() 低优先级路由），路由匹配按登记顺序 first-match-wins，
    # 这里新增的 GET 路由若留在表尾会被兜底路由抢先命中；挪到最前面即可正常测试。
    app.router.routes.insert(0, app.router.routes.pop())
    return app


async def _login(client: httpx.AsyncClient) -> None:
    assert (
        await client.post(
            "/api/auth/bootstrap", json={"username": "admin", "password": "pass-1234-bootstrap"}
        )
    ).status_code == 200


async def test_paginate(
    infra_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    db_session.add_all([Team(slug=f"t{i:02d}", name_zh=f"团队{i}") for i in range(7)])
    await db_session.commit()
    await _login(client)
    r = await client.get("/api/admin/_t/teams", params={"page": 2, "per_page": 3})
    assert r.status_code == 200
    assert r.json()["data"] == {
        "items": ["t03", "t04", "t05"],
        "total": 7,
        "page": 2,
        "per_page": 3,
    }
    assert (await client.get("/api/admin/_t/teams", params={"per_page": 999})).status_code == 422


async def test_if_match(infra_app: FastAPI, client: httpx.AsyncClient) -> None:
    await _login(client)
    csrf = {"X-CSRF-Token": client.cookies["coreman_csrf"]}
    assert (await client.put("/api/admin/_t/versioned/3", headers=csrf)).status_code == 428
    assert (
        await client.put("/api/admin/_t/versioned/3", headers={**csrf, "If-Match": '"2"'})
    ).status_code == 409
    ok = await client.put("/api/admin/_t/versioned/3", headers={**csrf, "If-Match": '"3"'})
    assert ok.status_code == 200 and ok.headers["ETag"] == '"4"'


async def test_require_roles(
    infra_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    assert (await client.get("/api/admin/_t/admins-only")).status_code == 401
    await _login(client)
    assert (await client.get("/api/admin/_t/admins-only")).status_code == 200

    await login_as(client, db_session, role="member")
    r = await client.get("/api/admin/_t/admins-only")
    assert r.status_code == 403
    assert r.json() == {"code": 403, "message": "没有权限执行该操作"}

    await login_as(client, db_session, role="ai_committee")
    assert (await client.get("/api/admin/_t/admins-only")).status_code == 403
