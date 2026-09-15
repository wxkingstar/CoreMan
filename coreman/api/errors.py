"""API 错误信封：{code, message, errors?}（spec §10.4）。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError
from starlette.exceptions import HTTPException as StarletteHTTPException

from coreman.core.errors import VERSION_CONFLICT as VERSION_CONFLICT
from coreman.core.errors import ApiError as ApiError
from coreman.core.errors import forbidden as forbidden
from coreman.core.errors import not_found as not_found
from coreman.core.logging import get_logger

log = get_logger(__name__)


def _payload(code: int, message: str, errors: list[Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message}
    if errors:
        body["errors"] = errors
    return body


# 422 明细里只回这几个键。pydantic 的 `input` 是出错字段的原值（整体 dict 校验器失败时就是
# 整个 dict），`ctx` / `url` 也可能带上原值——凭证、环境变量这类字段一旦校验失败就会被原样
# 回显。这里做成全局白名单，任何路由的密钥字段都不必各自防守。
_ERROR_FIELDS = ("loc", "msg", "type")


def safe_validation_errors(errors: Sequence[Any]) -> list[dict[str, Any]]:
    """把 pydantic 的错误明细裁成 {loc, msg, type}：`msg` 只含字段名与规则，不含字段值。"""
    return [{k: err[k] for k in _ERROR_FIELDS if k in err} for err in errors]


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code, content=_payload(exc.code, exc.message, exc.errors)
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        safe = safe_validation_errors(exc.errors())
        return JSONResponse(
            status_code=422, content=_payload(422, "参数校验失败", jsonable_encoder(safe))
        )

    @app.exception_handler(IntegrityError)
    async def _integrity(_: Request, exc: IntegrityError) -> JSONResponse:
        # 求助/审批等历史记录保留外键；不能为删除配置而级联丢失记录。
        # SQL 异常可能含参数，不写出原异常文本或查询正文。
        log.warning("database_integrity_conflict", error=type(exc).__name__)
        return JSONResponse(
            status_code=409,
            content=_payload(409, "记录已存在、被其它记录引用或不满足约束；被引用的配置可先停用"),
        )

    @app.exception_handler(StaleDataError)
    async def _stale_data(_: Request, exc: StaleDataError) -> JSONResponse:
        """乐观锁（version_id_col）冲突：并发写只有一个能赢，输的那个到这里。"""
        return JSONResponse(
            status_code=409, content=_payload(VERSION_CONFLICT, "记录已被他人修改，请刷新后重试")
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code, content=_payload(exc.status_code, str(exc.detail))
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error", error=f"{type(exc).__name__}: {exc}")
        return JSONResponse(status_code=500, content=_payload(500, "服务内部错误"))
