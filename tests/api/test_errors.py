import httpx
from fastapi import FastAPI, Request
from pydantic import BaseModel, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from coreman.api.errors import VERSION_CONFLICT, ApiError, forbidden, not_found
from coreman.api.versioning import require_if_match


class Payload(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return v


async def test_validation_error_with_value_error_returns_422_envelope(
    app: FastAPI,
) -> None:
    @app.post("/api/admin/_probe")
    async def _probe(body: Payload) -> dict[str, object]:
        return {"code": 0, "data": body.name}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/admin/_probe", json={"name": "   "})
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == 422 and body["message"] == "参数校验失败"
    assert body["errors"][0]["loc"] == ["body", "name"]
    assert "blank" in body["errors"][0]["msg"]


async def test_stale_data_error_returns_409_envelope(app: FastAPI) -> None:
    """乐观锁冲突（version_id_col）不能变成 500：统一成 409 信封。"""

    # 用 POST：spa 的 /{full_path:path} 兜底路由会遮蔽其后注册的所有 GET 路由
    @app.post("/api/admin/_stale")
    async def _stale() -> dict[str, object]:
        raise StaleDataError("x", None, None)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/admin/_stale")
    assert r.status_code == 409
    assert r.json() == {"code": VERSION_CONFLICT, "message": "记录已被他人修改，请刷新后重试"}


async def test_if_match_conflict_uses_dedicated_code_other_409_do_not(app: FastAPI) -> None:
    """版本冲突带专用 code；业务占用类 409 与唯一约束冲突保持 code=409。"""

    @app.post("/api/admin/_if_match")
    async def _if_match(request: Request) -> dict[str, object]:
        require_if_match(request, 3)
        return {"code": 0, "data": None}

    @app.post("/api/admin/_occupied")
    async def _occupied() -> dict[str, object]:
        raise ApiError(409, 409, "该实例工作目录已属于另一个机器人")

    @app.post("/api/admin/_integrity")
    async def _integrity() -> dict[str, object]:
        raise IntegrityError("x", None, Exception("dup"))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        stale = await c.post("/api/admin/_if_match", headers={"If-Match": '"2"'})
        fresh = await c.post("/api/admin/_if_match", headers={"If-Match": '"3"'})
        occupied = await c.post("/api/admin/_occupied")
        integrity = await c.post("/api/admin/_integrity")
    assert VERSION_CONFLICT != 409
    assert stale.status_code == 409 and stale.json()["code"] == VERSION_CONFLICT
    assert fresh.status_code == 200
    assert occupied.status_code == 409 and occupied.json()["code"] == 409
    assert integrity.status_code == 409 and integrity.json()["code"] == 409


def test_switch_error_carries_version_conflict_code() -> None:
    from coreman.core.bots.switch_relay import SwitchError

    assert SwitchError(409, "机器人已被其他操作修改，请刷新后重试", VERSION_CONFLICT).code == 40901
    assert SwitchError(409, "工作目录被占用").code == 409


def test_error_factories_return_fresh_instances() -> None:
    """工厂每次返回新实例：异常对象不能是跨请求共享的模块级单例。"""
    a, b = forbidden(), forbidden()
    assert a is not b and a.status_code == 403 and a.code == 403
    nf = not_found("机器人不存在")
    assert nf.status_code == 404 and nf.message == "机器人不存在"
