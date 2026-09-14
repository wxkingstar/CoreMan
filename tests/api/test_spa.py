from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.routing import BaseRoute

from coreman.api.main import create_app
from coreman.core.config import Settings


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<html><body>coreman spa</body></html>", encoding="utf-8")
    (d / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    return d


async def test_spa_served_with_fallback(
    api_settings: Settings, dist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEB_DIST_DIR", str(dist))
    app: FastAPI = create_app(Settings())  # type: ignore[call-arg]
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            assert "coreman spa" in (await c.get("/")).text
            assert "coreman spa" in (await c.get("/bots/123")).text
            assert (await c.get("/assets/app.js")).text == "console.log(1)"
            assert (await c.get("/api/x")).status_code == 404


async def test_spa_missing_dist_returns_json(client: httpx.AsyncClient) -> None:
    r = await client.get("/")
    assert r.status_code == 404
    assert r.json()["message"] == "管理台未构建"


def _api_routes(routes: list[BaseRoute]) -> Iterator[APIRoute]:
    """展平路由表（同 tests/api/test_csrf.py 的 _api_routes）：FastAPI 0.141 起
    include_router 不再把子路由摊进 app.routes，而是放一个包装对象，其
    original_router.routes 才是真正的 APIRoute。"""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        nested = getattr(route, "routes", None) or getattr(
            getattr(route, "original_router", None), "routes", None
        )
        if nested:
            yield from _api_routes(nested)


async def test_spa_catchall_is_last_route(app: FastAPI) -> None:
    """main.py 里 mount_spa(app, ...) 必须排在所有 include_router(...) 之后：spa.py 手写的
    `@app.get("/{full_path:path}")` 兜底路由和普通路由一样按注册顺序 first-match-wins，
    排到前面就会抢先命中它之后所有路由器的 GET 请求（见 main.py 该调用上方的注释）。"""
    routes = list(_api_routes(app.router.routes))
    assert routes, "route table is empty"
    catchall_index = len(routes) - 1
    assert routes[catchall_index].path == "/{full_path:path}"
    for i, route in enumerate(routes):
        if i != catchall_index and "GET" in route.methods:
            assert i < catchall_index
