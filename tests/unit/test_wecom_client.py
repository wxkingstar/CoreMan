import asyncio
import json
from collections.abc import Iterator
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from coreman.core.platforms import wecom
from coreman.core.platforms.wecom import WeComClient, WeComError, oauth_url, qr_login_url


def _transport(handler):  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://qyapi.weixin.qq.com"
    )


@pytest.fixture(autouse=True)
def _clear_token_cache() -> Iterator[None]:
    wecom.clear_token_cache()
    yield
    wecom.clear_token_cache()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """可手动拨动的单调时钟：now[0] += 秒。"""
    now = [1000.0]
    monkeypatch.setattr(wecom, "_clock", lambda: now[0])
    return now


async def test_notification_duplicate_check_and_invalid_recipient_are_not_false_success():
    def handler(req):
        if req.url.path == "/cgi-bin/gettoken":
            return httpx.Response(200, json={"access_token": "synthetic", "expires_in": 7200})
        body = json.loads(req.content)
        assert body["enable_duplicate_check"] == 1 and body["touser"] == "recipient"
        return httpx.Response(200, json={"errcode": 0, "invaliduser": "recipient"})

    c = WeComClient("ww1", "s1", http=_transport(handler))
    with pytest.raises(WeComError) as exc:
        await c.send_message(agent_id=1, user_id="recipient", content="hi")
    assert exc.value.errcode == -3
    with pytest.raises(ValueError):
        await c.send_message(agent_id=1, user_id="@all", content="hi")


async def test_token_cached_and_used() -> None:
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        if req.url.path == "/cgi-bin/gettoken":
            assert parse_qs(req.url.query.decode()) == {"corpid": ["ww1"], "corpsecret": ["s1"]}
            return httpx.Response(
                200, json={"errcode": 0, "errmsg": "ok", "access_token": "T1", "expires_in": 7200}
            )
        assert parse_qs(req.url.query.decode())["access_token"] == ["T1"]
        return httpx.Response(
            200,
            json={
                "errcode": 0,
                "errmsg": "ok",
                "department": [{"id": 1, "name": "公司", "parentid": 0, "order": 1}],
            },
        )

    c = WeComClient("ww1", "s1", http=_transport(handler))
    assert await c.department_list() == [{"id": 1, "name": "公司", "parentid": 0, "order": 1}]
    await c.department_list()
    assert calls.count("/cgi-bin/gettoken") == 1


async def test_expired_token_refreshed_once() -> None:
    tokens = iter(["T1", "T2"])
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/cgi-bin/gettoken":
            return httpx.Response(
                200, json={"errcode": 0, "access_token": next(tokens), "expires_in": 7200}
            )
        tok = parse_qs(req.url.query.decode())["access_token"][0]
        seen.append(tok)
        if tok == "T1":
            return httpx.Response(200, json={"errcode": 42001, "errmsg": "access_token expired"})
        return httpx.Response(200, json={"errcode": 0, "userlist": []})

    c = WeComClient("ww1", "s1", http=_transport(handler))
    assert await c.user_list() == []
    assert seen == ["T1", "T2"]


async def test_errcode_raises() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/cgi-bin/gettoken":
            return httpx.Response(200, json={"errcode": 0, "access_token": "T", "expires_in": 7200})
        return httpx.Response(200, json={"errcode": 48009, "errmsg": "api forbidden for new ip"})

    c = WeComClient("ww1", "s1", http=_transport(handler))
    with pytest.raises(WeComError) as ei:
        await c.user_list()
    assert ei.value.errcode == 48009 and "48009" in str(ei.value)


async def test_user_info_by_code_and_user_get() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/cgi-bin/gettoken":
            return httpx.Response(200, json={"errcode": 0, "access_token": "T", "expires_in": 7200})
        if req.url.path == "/cgi-bin/auth/getuserinfo":
            assert parse_qs(req.url.query.decode())["code"] == ["c1"]
            return httpx.Response(
                200, json={"errcode": 0, "userid": "zhangsan", "user_ticket": "x"}
            )
        assert req.url.path == "/cgi-bin/user/get"
        return httpx.Response(200, json={"errcode": 0, "userid": "zhangsan", "name": "张三"})

    c = WeComClient("ww1", "s1", http=_transport(handler))
    assert (await c.user_info_by_code("c1"))["userid"] == "zhangsan"
    assert (await c.user_get("zhangsan"))["name"] == "张三"


def test_login_urls() -> None:
    qr = urlparse(
        qr_login_url("ww1", "1000002", "https://x.example.com/api/auth/wecom/callback", "st")
    )
    assert qr.netloc == "login.work.weixin.qq.com" and qr.path == "/wwlogin/sso/login"
    assert parse_qs(qr.query) == {
        "login_type": ["CorpApp"],
        "appid": ["ww1"],
        "agentid": ["1000002"],
        "redirect_uri": ["https://x.example.com/api/auth/wecom/callback"],
        "state": ["st"],
    }
    oa = oauth_url("ww1", "1000002", "https://x.example.com/cb", "st")
    assert oa.startswith(
        "https://open.weixin.qq.com/connect/oauth2/authorize?appid=ww1&redirect_uri=https%3A%2F%2Fx.example.com%2Fcb&response_type=code&scope=snsapi_base&state=st&agentid=1000002"
    )
    assert oa.endswith("#wechat_redirect")


async def test_gettoken_http_error_does_not_leak_secret() -> None:
    """HTTP 错误响应不应该在异常消息中泄漏 corpsecret。"""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="<html>upstream error</html>")

    c = WeComClient("ww1", "TOP-SECRET-VALUE", http=_transport(handler))
    with pytest.raises(WeComError) as ei:
        await c.get_token()
    exc_str = str(ei.value)
    assert (
        ei.value.errcode == -1 and "TOP-SECRET-VALUE" not in exc_str and "corpsecret" not in exc_str
    )


async def test_non_json_response_raises_wecom_error() -> None:
    """非 JSON 响应应该抛出 WeComError 而不是 JSONDecodeError。"""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/cgi-bin/gettoken":
            return httpx.Response(200, json={"errcode": 0, "access_token": "T", "expires_in": 7200})
        return httpx.Response(
            200, text="<html>bad gateway</html>", headers={"content-type": "text/html"}
        )

    c = WeComClient("ww1", "s1", http=_transport(handler))
    with pytest.raises(WeComError) as ei:
        await c.user_list()
    assert ei.value.errcode == -2


async def test_application_media_token_refresh_and_no_redirect() -> None:
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if req.url.path == "/cgi-bin/gettoken":
            return httpx.Response(200, json={"access_token": "T", "expires_in": 7200})
        if calls.count("/cgi-bin/media/get") == 1:
            return httpx.Response(200, json={"errcode": 42001})
        return httpx.Response(200, content=b"media")

    client = WeComClient("ww", "secret", http=_transport(handler))
    assert b"".join([part async for part in client.media_stream("media-id")]) == b"media"
    assert calls.count("/cgi-bin/gettoken") == 2
    await client.aclose()


# ---- gettoken 失败负缓存与并发去重 ----


def _gettoken_replies(*replies: httpx.Response | Exception):  # type: ignore[no-untyped-def]
    """按顺序返回预设的 gettoken 响应（最后一个重复），并记下调用次数。"""
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/cgi-bin/gettoken"
        reply = replies[min(len(calls), len(replies) - 1)]
        calls.append(1)
        if isinstance(reply, Exception):
            raise reply
        return reply

    return handler, calls


def _err(code: int) -> httpx.Response:
    return httpx.Response(200, json={"errcode": code, "errmsg": "synthetic"})


def _ok(token: str = "T") -> httpx.Response:
    return httpx.Response(200, json={"errcode": 0, "access_token": token, "expires_in": 7200})


async def test_credential_error_is_negatively_cached_until_ttl(clock: list[float]) -> None:
    handler, calls = _gettoken_replies(_err(40001), _ok("T2"))
    c = WeComClient("ww1", "bad-secret", http=_transport(handler))
    for _ in range(3):
        with pytest.raises(WeComError) as ei:
            await c.get_token()
        assert ei.value.errcode == 40001
    assert len(calls) == 1
    clock[0] += wecom.CREDENTIAL_FAILURE_TTL - 1
    with pytest.raises(WeComError):
        await c.get_token()
    assert len(calls) == 1
    clock[0] += 2
    assert await c.get_token() == "T2"
    assert len(calls) == 2
    # 成功后失败记录清掉，令牌照常缓存
    assert await c.get_token() == "T2" and len(calls) == 2


async def test_rate_limit_is_cached_briefly(clock: list[float]) -> None:
    handler, calls = _gettoken_replies(_err(45009), _ok())
    c = WeComClient("ww1", "s1", http=_transport(handler))
    with pytest.raises(WeComError):
        await c.get_token()
    with pytest.raises(WeComError):
        await c.get_token()
    assert len(calls) == 1
    clock[0] += wecom.RATE_LIMIT_FAILURE_TTL + 1
    assert await c.get_token() == "T" and len(calls) == 2


@pytest.mark.parametrize(
    "reply",
    [
        _err(-1),  # 企微系统繁忙
        httpx.Response(502, text="bad gateway"),
        httpx.ConnectError("network down"),
    ],
    ids=["busy", "http-502", "network"],
)
async def test_transient_failures_are_not_cached(
    clock: list[float], reply: httpx.Response | Exception
) -> None:
    handler, calls = _gettoken_replies(reply, _ok())
    c = WeComClient("ww1", "s1", http=_transport(handler))
    with pytest.raises((WeComError, httpx.ConnectError)):
        await c.get_token()
    assert await c.get_token() == "T"
    assert len(calls) == 2


async def test_fixed_secret_and_forced_refresh_bypass_the_negative_cache(
    clock: list[float],
) -> None:
    handler, calls = _gettoken_replies(_err(40001), _ok("T-new"))
    bad = WeComClient("ww1", "bad-secret", http=_transport(handler))
    with pytest.raises(WeComError):
        await bad.get_token()
    # 改对 secret 就是新的缓存键，不受旧失败影响
    fixed = WeComClient("ww1", "good-secret", http=_transport(handler))
    assert await fixed.get_token() == "T-new" and len(calls) == 2
    # 管理台「测试连接」用 force：同一 secret 也重新问一次企微
    with pytest.raises(WeComError):
        await bad.get_token()
    assert len(calls) == 2
    assert await bad.get_token(force=True) == "T-new" and len(calls) == 3


async def test_concurrent_token_requests_share_one_gettoken() -> None:
    calls: list[int] = []
    release = asyncio.Event()

    async def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        await release.wait()
        return _ok()

    c = WeComClient("ww1", "s1", http=_transport(handler))
    waiting = [asyncio.create_task(c.get_token()) for _ in range(5)]
    await asyncio.sleep(0.05)
    release.set()
    assert await asyncio.gather(*waiting) == ["T"] * 5
    assert len(calls) == 1


async def test_concurrent_requests_share_one_cached_failure() -> None:
    calls: list[int] = []

    async def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        await asyncio.sleep(0.02)
        return _err(40013)

    c = WeComClient("ww1", "s1", http=_transport(handler))
    results = await asyncio.gather(*(c.get_token() for _ in range(5)), return_exceptions=True)
    assert all(isinstance(r, WeComError) and r.errcode == 40013 for r in results)
    assert len(calls) == 1
