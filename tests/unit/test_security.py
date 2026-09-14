import uuid

import pytest
from fastapi import Request

from coreman.api.errors import ApiError
from coreman.api.security import (
    CSRF_COOKIE,
    CSRF_HEADER,
    sign_session_id,
    unsign_session_id,
    verify_csrf,
)

SECRET = "x" * 32


def test_sign_unsign_roundtrip_and_tamper() -> None:
    sid = uuid.uuid4()
    token = sign_session_id(SECRET, sid)
    assert unsign_session_id(SECRET, token) == sid
    assert unsign_session_id(SECRET, token + "a") is None
    assert unsign_session_id("y" * 32, token) is None
    assert unsign_session_id(SECRET, token, max_age=-1) is None


def _request(method: str, cookies: dict[str, str], headers: dict[str, str]) -> Request:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    if cookies:
        raw_headers.append((b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()))
    scope = {
        "type": "http",
        "method": method,
        "path": "/api/admin/x",
        "headers": raw_headers,
        "query_string": b"",
        "scheme": "http",
        "server": ("t", 80),
    }
    return Request(scope)


async def test_verify_csrf() -> None:
    await verify_csrf(_request("GET", {}, {}))  # 安全方法不校验
    await verify_csrf(_request("POST", {CSRF_COOKIE: "tok"}, {CSRF_HEADER: "tok"}))
    with pytest.raises(ApiError) as e:
        await verify_csrf(_request("POST", {CSRF_COOKIE: "tok"}, {CSRF_HEADER: "other"}))
    assert e.value.status_code == 403
    with pytest.raises(ApiError):
        await verify_csrf(_request("DELETE", {}, {}))
