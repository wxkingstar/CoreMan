"""限频：认出飞书的限频应答、只给代码与等待秒数；哪些接口计入分钟用量；用量多了拉长多少。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from coreman.core.feishu_personal import endpoints, ratelimit, service

SECRET = "机密-片段-7f3a"
URL = service.BASE + "/im/v1/chats"


def test_rate_limited_is_a_personal_error_with_a_code_and_seconds_only() -> None:
    exc = ratelimit.RateLimited(7)
    assert isinstance(exc, service.PersonalError) and exc.code == "rate_limited"
    assert exc.category is None
    assert exc.payload() == {"error": "rate_limited", "upstream_code": 99991400, "retry_after": 7}
    assert ratelimit.RateLimited(0).retry_after == 1
    assert ratelimit.RateLimited(99999).retry_after == 600


@pytest.mark.parametrize(
    ("headers", "seconds"),
    [
        ({"x-ogw-ratelimit-reset": "12"}, 12),
        ({"retry-after": "3"}, 3),
        ({"x-ogw-ratelimit-reset": "0", "retry-after": "5"}, 5),
        ({"x-ogw-ratelimit-reset": "soon"}, 60),
        ({"x-ogw-ratelimit-reset": "9000"}, 600),
        ({}, 60),
    ],
)
def test_wait_comes_from_the_response_headers(headers: dict[str, str], seconds: int) -> None:
    assert ratelimit.retry_after(headers) == seconds


def test_detect_knows_the_status_and_the_error_code() -> None:
    assert ratelimit.detect(429, None, {}) is not None
    found = ratelimit.detect(400, {"code": 99991400}, {"x-ogw-ratelimit-reset": "4"})
    assert found is not None and found.retry_after == 4
    assert ratelimit.detect(200, {"code": 0}, {}) is None
    assert ratelimit.detect(400, {"code": 230001}, {}) is None
    assert ratelimit.detect(502, "<html>", {}) is None


def test_only_listing_chats_and_reading_history_are_budgeted() -> None:
    known = endpoints.ENDPOINTS
    assert ratelimit.category(known["im.chats"]) == "chats"
    assert ratelimit.category(known["im.history"]) == "history"
    # 同一个路径的 POST 是发消息；按编号取消息、单个会话、搜索会话各有自己的限额。
    for key in ("im.send", "im.mget", "im.chat", "im.chat_search", "im.search"):
        assert ratelimit.category(known[key]) is None, key


@pytest.mark.parametrize(
    ("load", "factor"), [(0.0, 1), (0.59, 1), (0.6, 2), (0.79, 2), (0.8, 4), (0.99, 4), (1.0, None)]
)
def test_the_interval_stretches_as_the_budget_fills(load: float, factor: int | None) -> None:
    assert ratelimit.stretch(load) == factor


@respx.mock
async def test_http_raises_rate_limited_on_429_without_the_upstream_text() -> None:
    respx.get(URL).mock(
        return_value=httpx.Response(
            429, headers={"x-ogw-ratelimit-reset": "9"}, text=f"too many requests {SECRET}"
        )
    )
    with pytest.raises(ratelimit.RateLimited) as caught:
        await service._http("GET", URL)
    assert caught.value.retry_after == 9
    assert SECRET not in str(caught.value)
    assert SECRET not in json.dumps(caught.value.payload(), ensure_ascii=False)


@respx.mock
async def test_http_raises_rate_limited_on_the_error_code() -> None:
    respx.get(URL).mock(
        return_value=httpx.Response(
            400, json={"code": 99991400, "msg": f"frequency limit {SECRET}"}
        )
    )
    with pytest.raises(ratelimit.RateLimited) as caught:
        await service._http("GET", URL)
    assert caught.value.retry_after == 60


@respx.mock
async def test_http_is_unchanged_for_everything_else() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={"code": 0, "data": {"items": []}}))
    assert await service._http("GET", URL) == {"code": 0, "data": {"items": []}}
    respx.get(URL).mock(return_value=httpx.Response(502, text="<html>bad gateway</html>"))
    with pytest.raises(service.PersonalError) as caught:
        await service._http("GET", URL)
    assert caught.value.code == "upstream_unavailable"
    assert not isinstance(caught.value, ratelimit.RateLimited)


async def test_only_assistant_calls_are_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """普通员工的调用不记用量（它们各有自己的应用，这条路径上也不该多一条语句）。"""
    recorded: list[tuple[str, str]] = []

    async def authorize(_s: Any, _c: Any, _scope: Any, method: str, path: str, **_kw: Any) -> Any:
        row = SimpleNamespace(app_id="cli_example")
        return endpoints.match(method, path), row, "secret", {"access_token": "t"}

    async def http(_method: str, _url: str, **_kw: Any) -> dict[str, Any]:
        return {"code": 0, "data": {}}

    async def record(self: ratelimit.Budget, _session: Any, category: str, **_kw: Any) -> None:
        recorded.append((self.app_id, category))

    monkeypatch.setattr(service, "_authorize", authorize)
    monkeypatch.setattr(service, "_http", http)
    monkeypatch.setattr(ratelimit.Budget, "record", record)
    for kind in ("standard", "assistant"):
        scope: Any = SimpleNamespace(bot=SimpleNamespace(kind=kind))
        for path in ("/im/v1/chats", "/im/v1/messages", "/im/v1/messages/mget"):
            await service.api_request(None, None, scope, "GET", path)  # type: ignore[arg-type]
    assert recorded == [("cli_example", "chats"), ("cli_example", "history")]
