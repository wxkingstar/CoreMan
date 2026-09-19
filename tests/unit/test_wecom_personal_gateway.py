"""企业微信「可使用权限」网关客户端：签名、信封、令牌失效与授权人解析。"""

import hashlib
import json

import httpx
import pytest
import respx

from coreman.core.wecom_personal import gateway

CONTEXT = (
    "<extra_identity_context>\n机器人身份：\n名字：示例机器人\nID：aib-demo\n"
    "授权真人用户身份：\n名字：张三  \nID：wo-demo-user_1\n说明文字……\n</extra_identity_context>"
)


def envelope(result=None, *, errcode=0, error=None, errmsg="ok"):
    inner = {"result": json.dumps(result, ensure_ascii=False)} if result is not None else {}
    if error is not None:
        inner["error"] = error
    return {"errcode": errcode, "errmsg": errmsg, "results_json": json.dumps(inner)}


def test_signature_matches_the_official_tool():
    expected = hashlib.sha256(b"secretbot-11700000000nonce").hexdigest()
    assert gateway.sign("secret", "bot-1", 1700000000, "nonce") == expected


@respx.mock
async def test_fetch_token_signs_the_request_and_never_leaks_the_secret():
    route = respx.post(gateway.AUTH_URL).mock(
        return_value=httpx.Response(200, json={"errcode": 0, "token": "t-1"})
    )
    assert await gateway.fetch_token("bot-1", "s3cret") == "t-1"
    body = json.loads(route.calls.last.request.content)
    assert body["bot_id"] == "bot-1" and body["bind_source"] == gateway.BIND_SOURCE
    assert body["signature"] == gateway.sign("s3cret", "bot-1", body["time"], body["nonce"])
    assert "s3cret" not in route.calls.last.request.content.decode()

    respx.post(gateway.AUTH_URL).mock(
        return_value=httpx.Response(200, json={"errcode": 40001, "errmsg": "bad s3cret"})
    )
    with pytest.raises(gateway.GatewayError) as exc:
        await gateway.fetch_token("bot-1", "s3cret")
    assert exc.value.code == "credentials_rejected" and "s3cret" not in exc.value.message


@respx.mock
async def test_invoke_unwraps_the_gateway_envelope():
    route = respx.post(gateway.BASE_URL + "/todo/list").mock(
        return_value=httpx.Response(200, json=envelope({"items": [{"title": "周报"}]}))
    )
    result = await gateway.invoke("tok", "/todo/list", {"limit": 5, "keywords": ["周报"]})
    assert result == {"items": [{"title": "周报"}]}
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer tok"
    assert json.loads(json.loads(request.content)["payload"]) == {"limit": 5, "keywords": ["周报"]}


@pytest.mark.parametrize("where", ["outer", "inner"])
@pytest.mark.parametrize("errcode", sorted(gateway.TOKEN_ERRORS))
@respx.mock
async def test_token_errors_are_recognised_at_both_levels(where, errcode):
    body = (
        {"errcode": errcode, "errmsg": "token"}
        if where == "outer"
        else envelope(error={"code": errcode, "message": "token"})
    )
    respx.post(gateway.BASE_URL + "/todo/list").mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(gateway.GatewayError) as exc:
        await gateway.invoke("tok", "/todo/list", {})
    assert exc.value.code == "token_expired"


@respx.mock
async def test_business_errors_keep_the_code_and_redact_the_token():
    respx.post(gateway.BASE_URL + "/mail/search").mock(
        return_value=httpx.Response(
            200, json=envelope(error={"code": 851013, "message": "no auth for tok-secret"})
        )
    )
    with pytest.raises(gateway.GatewayError) as exc:
        await gateway.invoke("tok-secret", "/mail/search", {})
    assert (exc.value.code, exc.value.errcode) == ("wecom_error", 851013)
    assert "tok-secret" not in exc.value.message


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(502, json={}),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=[1]),
        httpx.Response(200, json={"errcode": 0}),
        httpx.Response(200, json={"errcode": 0, "results_json": json.dumps({"result": 3})}),
    ],
)
@respx.mock
async def test_malformed_responses_are_upstream_failures(response):
    respx.post(gateway.BASE_URL + "/todo/list").mock(return_value=response)
    with pytest.raises(gateway.GatewayError) as exc:
        await gateway.invoke("tok", "/todo/list", {})
    assert exc.value.code == "upstream_unavailable"


@respx.mock
async def test_oversized_responses_are_rejected(monkeypatch):
    monkeypatch.setattr(gateway, "MAX_RESPONSE_BYTES", 100)
    respx.post(gateway.BASE_URL + "/todo/list").mock(
        return_value=httpx.Response(200, json=envelope({"x": "y" * 500}))
    )
    with pytest.raises(gateway.GatewayError):
        await gateway.invoke("tok", "/todo/list", {})


def test_identity_is_parsed_from_the_whoami_context():
    identity = gateway.parse_identity(CONTEXT)
    assert identity is not None
    assert (identity.bot_id, identity.authorizer_id) == ("aib-demo", "wo-demo-user_1")
    assert identity.bot_name == "示例机器人"
    unauthorized = "机器人身份：\n名字：示例\nID：aib-demo\n"
    assert gateway.parse_identity(unauthorized) == gateway.Identity("aib-demo", None, "示例")
    assert gateway.parse_identity("something else") is None


@pytest.mark.parametrize(
    ("text", "url"),
    [
        (
            "当前机器人「文档」使用权限已过期\n若你是智能机器人创建者，可以"
            "[点击这里](https://work.weixin.qq.com/ai/auth?bot=aib-demo&biz=doc)授权",
            "https://work.weixin.qq.com/ai/auth?bot=aib-demo&biz=doc",
        ),
        ("去 https://open.work.weixin.qq.com/devtool/query?e=850002 查错误码", None),
        ("[钓鱼](https://example.com/work.weixin.qq.com)", None),
        ("[明文](http://work.weixin.qq.com/ai/auth)", None),
        (None, None),
    ],
)
def test_help_link_only_accepts_wecom_authorization_pages(text, url):
    assert gateway.help_link(text) == url


@respx.mock
async def test_capability_errors_carry_the_renewal_link():
    body = {
        "errcode": 850003,
        "errmsg": "authorization expired",
        "help_message": "若你是智能机器人创建者，可以[点击这里](https://work.weixin.qq.com/ai/x)授权",
    }
    respx.post(gateway.BASE_URL + "/doc/search").mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(gateway.GatewayError) as exc:
        await gateway.invoke("tok", "/doc/search", {})
    assert (exc.value.code, exc.value.errcode) == ("wecom_error", 850003)
    assert exc.value.help_url == "https://work.weixin.qq.com/ai/x"
    inner = {
        "error": {
            "code": 850002,
            "message": "no authorization",
            "help_message": body["help_message"],
        }
    }
    respx.post(gateway.BASE_URL + "/mail/search").mock(
        return_value=httpx.Response(200, json={"errcode": 0, "results_json": json.dumps(inner)})
    )
    with pytest.raises(gateway.GatewayError) as exc:
        await gateway.invoke("tok", "/mail/search", {})
    assert (exc.value.errcode, exc.value.help_url) == (850002, "https://work.weixin.qq.com/ai/x")


@respx.mock
async def test_whoami_refuses_to_guess_when_the_context_changes():
    route = respx.post(gateway.BASE_URL + "/identity/whoami")
    route.mock(return_value=httpx.Response(200, json=envelope({"extra_identity_context": CONTEXT})))
    assert (await gateway.whoami("tok")).authorizer_id == "wo-demo-user_1"
    route.mock(return_value=httpx.Response(200, json=envelope({"extra_identity_context": "改版"})))
    with pytest.raises(gateway.GatewayError):
        await gateway.whoami("tok")
