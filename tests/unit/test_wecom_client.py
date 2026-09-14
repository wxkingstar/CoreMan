import json
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
def _clear_token_cache() -> None:
    wecom._TOKENS.clear()


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
