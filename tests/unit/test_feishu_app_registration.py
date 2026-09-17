from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from lark_oapi.scene.registration import _encode_addons  # type: ignore[attr-defined]

from coreman.core.feishu_apps import manifest, registration
from coreman.core.feishu_personal import service as personal


def test_addons_encoding_matches_official_sdk():
    value = manifest.addons()
    assert manifest.encode_addons(value) == _encode_addons(value)


def test_manifest_covers_identity_and_every_personal_tier():
    assert "contact:user.employee_id:readonly" in manifest.TENANT_SCOPES
    assert set(personal.SCOPES.split()) <= set(manifest.USER_SCOPES)
    assert {"im:message", "im:message.send_as_user"} <= set(manifest.USER_SCOPES)
    assert "im.message.receive_v1" in manifest.TENANT_EVENTS
    # 协议层授权项不会出现在应用权限列表里，不能算缺失。
    assert manifest.missing_scopes(["offline_access"], kind="user") == sorted(
        set(manifest.USER_SCOPES) - manifest.PROTOCOL_SCOPES
    )
    granted = [
        s for s in manifest.USER_SCOPES if s not in ("vc:meeting:readonly", "auth:user.id:read")
    ]
    assert manifest.missing_scopes(granted, kind="user") == []
    assert {"im:chat.members:read", "application:app_slash_command:write"} <= set(
        manifest.TENANT_SCOPES
    )


def test_default_slash_commands_match_builtin_commands_and_icon_catalog():
    from coreman.core.chat.commands import classify_command
    from coreman.core.chat.session_switch import is_sessions_command

    names = [name for name, _, _ in manifest.DEFAULT_SLASH_COMMANDS]
    assert names == ["new", "stop", "sessions", "help"]
    for name in names:
        assert classify_command(f"/{name}") or is_sessions_command(f"/{name}")
    for _, description, icon in manifest.DEFAULT_SLASH_COMMANDS:
        assert description and icon.endswith("_outlined")


def _client(handler):
    return httpx.AsyncClient(
        base_url=registration.ACCOUNTS_URL, transport=httpx.MockTransport(handler)
    )


async def test_begin_follows_sdk_protocol_and_prefills_create_only_page():
    forms = []

    def handler(request):
        assert request.url.path == registration.ENDPOINT
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        forms.append(form)
        if form["action"] == "init":
            return httpx.Response(200, json={"supported_auth_methods": ["client_secret"]})
        return httpx.Response(
            200,
            json={
                "device_code": "dc",
                "verification_uri_complete": "https://open.feishu.cn/page/launcher?user_code=AB-CD",
                "interval": 3,
                "expires_in": 600,
            },
        )

    async with _client(handler) as http:
        began = await registration.begin(
            name="销售助手", desc="卖货", avatars=["https://x/a.png"], http=http
        )
    assert [f["action"] for f in forms] == ["init", "begin"]
    assert forms[1] == {
        "action": "begin",
        "archetype": "PersonalAgent",
        "auth_method": "client_secret",
        "request_user_info": "open_id",
    }
    assert (began.device_code, began.interval, began.expires_in) == ("dc", 3, 600)
    query = parse_qs(urlparse(began.url).query)
    assert query["user_code"] == ["AB-CD"] and query["createOnly"] == ["true"]
    assert query["name"] == ["销售助手"] and query["desc"] == ["卖货"]
    assert query["avatar"] == ["https://x/a.png"] and "clientID" not in query
    assert query["addons"] == [manifest.encode_addons(manifest.addons())]


async def test_update_targets_bound_app_without_create_only():
    url = registration.build_url(
        "https://accounts.feishu.cn/oauth/v1/device?user_code=X", app_id="cli_1"
    )
    query = parse_qs(urlparse(url).query)
    assert query["clientID"] == ["cli_1"] and "createOnly" not in query


@pytest.mark.parametrize(
    "uri", ["http://open.feishu.cn/x", "https://evil.example/x", "https://u@open.feishu.cn/x"]
)
def test_rejects_untrusted_confirmation_links(uri):
    with pytest.raises(registration.RegistrationError):
        registration.build_url(uri)


async def test_begin_requires_client_secret_method():
    async with _client(lambda r: httpx.Response(200, json={"supported_auth_methods": []})) as http:
        with pytest.raises(registration.RegistrationError, match="registration_unsupported"):
            await registration.begin(http=http)


@pytest.mark.parametrize(
    ("body", "status", "expected"),
    [
        ({"error": "authorization_pending"}, 400, "pending"),
        ({"error": "slow_down"}, 400, "slow_down"),
        ({"error": "access_denied"}, 400, "denied"),
        ({"error": "expired_token"}, 400, "expired"),
        ({"error": "anything"}, 400, "failed"),
        (
            {"error": "authorization_pending", "user_info": {"tenant_brand": "lark"}},
            400,
            "lark_unsupported",
        ),
        (
            {"client_id": "cli_a", "client_secret": "s", "user_info": {"tenant_brand": "lark"}},
            200,
            "lark_unsupported",
        ),
    ],
)
async def test_poll_maps_protocol_errors(body, status, expected):
    async with _client(lambda r: httpx.Response(status, json=body)) as http:
        assert (await registration.poll("dc", http=http)).status == expected


async def test_poll_success_returns_credentials_and_owner():
    body = {"client_id": "cli_a", "client_secret": "sec", "user_info": {"open_id": "ou_1"}}
    async with _client(lambda r: httpx.Response(200, json=body)) as http:
        result = await registration.poll("dc", http=http)
    assert (result.status, result.app_id, result.app_secret, result.open_id) == (
        "succeeded",
        "cli_a",
        "sec",
        "ou_1",
    )


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(502, json={"error": "x"}),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, content=b"x" * (300 * 1024)),
    ],
)
async def test_poll_sanitizes_upstream_failures(response):
    async with _client(lambda r: response) as http:
        with pytest.raises(registration.RegistrationError, match="upstream_unavailable"):
            await registration.poll("dc", http=http)
