"""乐观锁：PUT 必须带 If-Match: "<version>"（spec §10.4，冲突 409）。"""

from __future__ import annotations

from fastapi import Request, Response

from coreman.api.errors import ApiError


def require_if_match(request: Request, current_version: int) -> None:
    raw = request.headers.get("if-match")
    if raw is None:
        raise ApiError(428, 428, "缺少 If-Match 版本号")
    if raw.strip().strip('"') != str(current_version):
        raise ApiError(409, 409, "记录已被他人修改，请刷新后重试")


def set_etag(response: Response, version: int) -> None:
    response.headers["ETag"] = f'"{version}"'
