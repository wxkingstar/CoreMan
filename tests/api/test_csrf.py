"""结构性守卫：spec §10.4 要求 /api/admin/* 一律「cookie 会话 + CSRF」。

M1 会往管理台加几十个写端点，靠每个 handler 自己调 verify_csrf 迟早会漏，
所以直接在路由表上断言：CSRF 校验必须挂在路由器级依赖上。
"""

from collections.abc import Iterator

from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.routing import BaseRoute

from coreman.api.security import verify_csrf


def _api_routes(routes: list[BaseRoute]) -> Iterator[APIRoute]:
    """展平路由表。

    FastAPI 0.141 起 include_router 不再把子路由摊进 app.routes，而是放一个包装对象
    （其 original_router.routes 才是真正的 APIRoute）；老版本则是扁平的。两种都要能走通。
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        nested = getattr(route, "routes", None) or getattr(
            getattr(route, "original_router", None), "routes", None
        )
        if nested:
            yield from _api_routes(nested)


async def test_every_admin_mutating_route_requires_csrf(app: FastAPI) -> None:
    checked = 0
    for route in _api_routes(app.routes):
        if not route.path.startswith("/api/admin/"):
            continue
        if not (route.methods - {"GET", "HEAD", "OPTIONS"}):
            continue
        assert any(d.call is verify_csrf for d in route.dependant.dependencies), route.path
        checked += 1
    assert checked >= 1  # 兜底：路由展平失败时不能假装通过
