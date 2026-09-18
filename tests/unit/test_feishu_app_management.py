import json

import httpx
import pytest

from coreman.core.errors import ApiError
from coreman.core.feishu_apps import management, manifest
from coreman.core.platforms.feishu import FeishuClient, FeishuError

TOKEN = "/open-apis/auth/v3/tenant_access_token/internal"
APP = {
    "app_id": "cli_a",
    "app_name": "销售助手",
    "description": "卖货",
    "avatar_url": "https://x/a.png",
    "status": 1,
    "primary_language": "zh_cn",
    "i18n": [{"i18n_key": "zh_cn", "help_use": "https://help"}],
    "online_version_id": "oav_online",
    "unaudit_version_id": "",
    "scopes": [
        {"scope": "im:message:send_as_bot", "token_types": ["tenant"], "level": 1},
        {"scope": "contact:user.employee_id:readonly", "token_types": ["tenant"], "level": 2},
        *[
            {"scope": s, "token_types": ["user"], "level": 1}
            for s in manifest.USER_SCOPES
            if s not in ("im:message", "im:message.send_as_user")
        ],
    ],
}
VERSION = {
    "version_id": "oav_online",
    "version": "1.0.3",
    "status": 1,
    "remark": {"visibility": {"is_all": False, "visible_list": {"open_ids": ["ou_1"]}}},
    "events": ["im.message.receive_v1"],
    "ability": {
        "bot": {
            "bot_menu_enable": True,
            "bot_menu_display_strategy": 1,
            "bot_menus": [
                {"menu_id": "m1", "default_name": "帮助", "menu_content_type": 3},
                {
                    "menu_id": "m2",
                    "parent_menu_id": "m1",
                    "default_name": "文档",
                    "menu_content_type": 1,
                    "redirect_link": {"pc_url": "https://doc"},
                },
            ],
        }
    },
}


def _client(routes, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN:
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t", "expire": 7200})
        if calls is not None:
            calls.append(request)
        key = (request.method, request.url.path)
        status, body = routes.get(key, (200, {"code": 0, "data": {}}))
        return httpx.Response(status, json=body)

    http = httpx.AsyncClient(
        base_url="https://open.feishu.cn", transport=httpx.MockTransport(handler)
    )
    return FeishuClient("cli_a", "secret", http=http)


async def test_overview_combines_app_versions_grants_and_commands():
    client = _client(
        {
            ("GET", management.V6_APP): (200, {"code": 0, "data": {"app": APP}}),
            ("GET", f"{management.V6_APP}/app_versions/oav_online"): (
                200,
                {"code": 0, "data": {"app_version": VERSION}},
            ),
            ("GET", management.V6_SCOPES): (
                200,
                {
                    "code": 0,
                    "data": {
                        "scopes": [
                            {"scope_name": "im:message:send_as_bot", "grant_status": 1},
                            {"scope_name": "contact:user.employee_id:readonly", "grant_status": 2},
                        ]
                    },
                },
            ),
            ("GET", management.SLASH): (400, {"code": 99991672, "msg": "denied"}),
        }
    )
    data = await management.overview(client)
    assert data["app"]["name"] == "销售助手" and data["app"]["help_use"] == "https://help"
    assert data["app"]["under_review"] is False
    assert data["pending_grants"] == ["contact:user.employee_id:readonly"]
    granted = {s["scope"]: s["granted"] for s in data["scopes"]}
    assert granted["im:message:send_as_bot"] is True
    assert "contact:user.employee_id:readonly" not in data["missing_scopes"]["tenant"]
    assert data["missing_scopes"]["user"] == ["im:message", "im:message.send_as_user"]
    assert data["personal_levels"] == {
        "messages_readonly": True,
        "all_except_send": True,
        "all": False,
    }
    online = data["versions"]["online"]
    assert online["version"] == "1.0.3" and online["visibility"]["open_ids"] == ["ou_1"]
    assert [m["kind"] for m in online["bot"]["menus"]] == ["submenu", "link"]
    assert online["bot"]["menus"][1]["parent_menu_id"] == "m1"
    assert data["slash_commands"] is None and data["errors"]["slash_commands"] == 99991672


async def test_bot_menus_visibility_publish_and_commands_use_documented_bodies():
    calls: list[httpx.Request] = []
    client = _client(
        {
            ("POST", "/open-apis/application/v7/applications/cli_a/publish"): (
                200,
                {"code": 0, "data": {"version_id": "oav_new", "version": "1.0.4"}},
            ),
            ("POST", management.SLASH): (200, {"code": 0, "data": {"command_id": "7374"}}),
        },
        calls,
    )
    await management.update_bot(
        client,
        "cli_a",
        language="zh_cn",
        get_started_desc="直接提问即可",
        menu_enabled=True,
        menu_display_strategy=1,
        menus=[
            {"menu_id": "m1", "name": "帮助", "kind": "submenu"},
            {
                "menu_id": "m2",
                "parent_menu_id": "m1",
                "name": "文档",
                "kind": "link",
                "pc_url": "https://doc",
                "sort": 2,
            },
        ],
    )
    await management.update_bot(
        client,
        "cli_a",
        language="zh_cn",
        get_started_desc=None,
        menu_enabled=False,
        menu_display_strategy=None,
        menus=None,
    )
    await management.update_visibility(
        client, "cli_a", visible_to_all=False, user_ids=["u1"], department_ids=["d1"]
    )
    assert await management.publish(
        client, "cli_a", version="1.0.4", changelog="更新菜单", remark="CoreMan"
    ) == {"version_id": "oav_new", "version": "1.0.4"}
    assert (
        await management.create_command(
            client, command="report", description="生成日报", icon_key="ai-doc_outlined"
        )
        == "7374"
    )
    await management.delete_command(client, "7374")
    bodies = [(c.method, c.url.path, json.loads(c.content) if c.content else None) for c in calls]
    assert bodies[0][2] == {
        "bot": {
            "enable": True,
            "i18ns": [{"i18n_key": "zh_cn", "get_started_desc": "直接提问即可"}],
            "bot_menu_enable": True,
            "bot_menus": [
                {"menu_id": "m1", "default_name": "帮助", "sort": 0, "menu_content_type": 3},
                {
                    "menu_id": "m2",
                    "default_name": "文档",
                    "sort": 2,
                    "menu_content_type": 1,
                    "parent_menu_id": "m1",
                    "redirect_link": {"pc_url": "https://doc", "mobile_url": "https://doc"},
                },
            ],
            "bot_menu_display_strategy": 1,
        }
    }
    assert bodies[1][2] == {"bot": {"enable": True, "bot_menu_enable": False, "bot_menus": []}}
    assert calls[2].url.params["user_id_type"] == "user_id"
    assert calls[2].url.params["department_id_type"] == "department_id"
    assert bodies[2][2] == {
        "visibility": {
            "is_visible_to_all": False,
            "visible_list": {"user_ids": ["u1"], "department_ids": ["d1"]},
        }
    }
    assert bodies[3][2]["pc_default_ability"] == "bot" and bodies[3][2]["version"] == "1.0.4"
    # 图标必须在顶层：放在 description 里会被飞书忽略（实测）。
    assert bodies[4][2] == {
        "command": "report",
        "description": {"default_value": "生成日报", "i18n": {"zh_cn": "生成日报"}},
        "icon": {"icon_key": "ai-doc_outlined"},
    }
    assert bodies[5][:2] == ("DELETE", f"{management.SLASH}/7374")


async def test_avatar_upload_is_multipart_and_requires_https_url():
    calls: list[httpx.Request] = []
    client = _client(
        {
            ("POST", management.AVATAR_UPLOAD): (
                200,
                {"code": 0, "data": {"url": "https://s3-imfile.feishucdn.com/x"}},
            )
        },
        calls,
    )
    url = await management.upload_avatar(client, b"\x89PNG", "a.png", "image/png")
    assert url == "https://s3-imfile.feishucdn.com/x"
    assert calls[0].headers["content-type"].startswith("multipart/form-data")
    assert b'name="avatar"; filename="a.png"' in calls[0].content


@pytest.mark.parametrize(
    ("code", "status", "text"),
    [
        (210021, 409, "开发者后台"),
        (210001, 409, "开发者后台"),
        (210040, 409, "审核中"),
        (99991672, 409, "扫码补齐权限"),
        (210303, 422, "X.Y.Z"),
        (212004, 422, "已经申请过"),
        (40000000, 409, "指令"),
        (123456, 502, "123456"),
    ],
)
def test_feishu_errors_are_mapped_to_actionable_messages(code, status, text):
    error = management.api_error(FeishuError(code))
    assert isinstance(error, ApiError) and error.status_code == status and text in error.message


async def test_writes_surface_mapped_errors_and_reject_malformed_app_ids():
    client = _client(
        {("PATCH", "/open-apis/application/v7/applications/cli_a/base"): (403, {"code": 210021})}
    )
    with pytest.raises(ApiError, match="开发者后台"):
        await management.update_base(client, "cli_a", language="zh_cn", name="n", description="d")
    with pytest.raises(ApiError):
        management.v7("cli_a/../x", "base")


def test_next_version_increments_patch():
    assert management.next_version("1.2.9") == "1.2.10"
    assert management.next_version(None) == "1.0.0"
    assert management.next_version("v1") == "1.0.0"


async def test_default_commands_only_add_missing_builtins():
    calls: list[httpx.Request] = []
    client = _client(
        {
            ("GET", management.SLASH): (
                200,
                {"code": 0, "data": {"items": [{"command_id": "1", "command": "help"}]}},
            ),
            ("POST", management.SLASH): (200, {"code": 0, "data": {"command_id": "2"}}),
        },
        calls,
    )
    missing = ["new", "stop", "sessions", "connect"]
    assert await management.ensure_default_commands(client) == missing
    created = [json.loads(c.content) for c in calls if c.method == "POST"]
    assert [c["command"] for c in created] == missing
    assert created[0]["icon"] == {"icon_key": "add-chat-ai_outlined"}
    assert "icon" not in created[0]["description"]


def test_personal_levels_ignore_protocol_scopes():
    user = [
        s
        for s in manifest.USER_SCOPES
        if s not in ("auth:user.id:read", "offline_access", "im:message.send_as_user")
    ]
    assert management.personal_levels(user) == {
        "messages_readonly": True,
        "all_except_send": True,
        "all": False,
    }
