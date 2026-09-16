"""业务错误类型，不依赖 HTTP 框架；API 层负责渲染响应。"""

from typing import Any

# 乐观锁版本冲突（If-Match / version 不一致）的专用业务码。其它 409（工作目录被占用、
# 飞书应用已分配、记录已存在）仍用 409，前端据此区分「请刷新」与「原话提示」。
VERSION_CONFLICT = 40901


class ApiError(Exception):
    def __init__(
        self, status_code: int, code: int, message: str, errors: list[Any] | None = None
    ) -> None:
        super().__init__(message)
        self.status_code, self.code, self.message, self.errors = status_code, code, message, errors


def forbidden() -> ApiError:
    """403：统一的「没有权限」。每次返回新实例，异常对象不做跨请求共享。"""
    return ApiError(403, 403, "没有权限执行该操作")


def not_found(message: str) -> ApiError:
    """404：资源不存在，message 由调用方给出具体资源名。"""
    return ApiError(404, 404, message)
