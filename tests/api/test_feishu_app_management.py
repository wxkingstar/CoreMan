import base64
import json
import uuid
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.routers import feishu_apps as router
from coreman.core.bots.secrets import CREDENTIALS_AAD, encrypt_json
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    AuditLog,
    Bot,
    Department,
    FeishuAppRegistration,
    FeishuPersonalGrant,
    PlatformApp,
    Team,
    User,
    UserIdentity,
)
from coreman.core.feishu_apps import management, manifest
from coreman.core.platforms.feishu import FeishuClient
from tests.api.conftest import MASTER_KEY, login_as, login_existing

CIPHER = Cipher(base64.b64decode(MASTER_KEY))


class Feishu:
    """最小的飞书模拟：记录请求，按路径返回预设结果。"""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        # 每次建客户端用的是哪个应用：bot（机器人自己）或 platform（飞书登录应用）。
        self.used: list[str] = []
        self.platform_routes: dict[tuple[str, str], tuple[int, dict[str, object]]] = {}
        self.routes: dict[tuple[str, str], tuple[int, dict[str, object]]] = {
            ("GET", management.V6_APP): (
                200,
                {
                    "code": 0,
                    "data": {
                        "app": {
                            "app_id": "cli_bound",
                            "app_name": "销售助手",
                            "description": "卖货",
                            "primary_language": "zh_cn",
                            "online_version_id": "",
                            "unaudit_version_id": "",
                            "scopes": [
                                {"scope": "im:message:readonly", "token_types": ["user"]},
                            ],
                        }
                    },
                },
            ),
            ("GET", management.SLASH): (200, {"code": 0, "data": {"items": []}}),
        }

    def client(self, cipher: Cipher, bot: Bot) -> FeishuClient:
        self.used.append("bot")
        return self._client({})

    def platform_client(self, cipher: Cipher, app: PlatformApp) -> FeishuClient:
        """飞书登录应用的客户端：platform_routes 里有的路径按它返回，其余同机器人。"""
        self.used.append("platform")
        return self._client(self.platform_routes)

    def _client(
        self, overrides: dict[tuple[str, str], tuple[int, dict[str, object]]]
    ) -> FeishuClient:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("tenant_access_token/internal"):
                return httpx.Response(
                    200, json={"code": 0, "tenant_access_token": "t", "expire": 7200}
                )
            self.calls.append(request)
            key = (request.method, request.url.path)
            status, body = overrides.get(key) or self.routes.get(
                key, (200, {"code": 0, "data": {}})
            )
            return httpx.Response(status, json=body)

        http = httpx.AsyncClient(
            base_url="https://open.feishu.cn", transport=httpx.MockTransport(handler)
        )
        return FeishuClient("cli_bound", "secret", http=http)


async def _setup(client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch):  # type: ignore[no-untyped-def]
    feishu = Feishu()
    monkeypatch.setattr(router, "client_for", feishu.client)
    monkeypatch.setattr(router, "platform_client_for", feishu.platform_client)
    team = Team(slug=f"t{uuid.uuid4().hex[:6]}", name_zh="团队")
    db_session.add(team)
    await db_session.commit()
    owner = await login_as(client, db_session, role="member", team_id=team.id, login_name="owner")
    bot = Bot(
        bot_key=f"fs{uuid.uuid4().hex[:6]}",
        platform="feishu",
        name="销售助手",
        description="卖货",
        created_by=owner.id,
        team_id=team.id,
        model="m",
        working_dir="/w",
        credentials_enc=encrypt_json(
            CIPHER, {"app_id": "cli_bound", "app_secret": "secret"}, CREDENTIALS_AAD
        ),
    )
    db_session.add(bot)
    await db_session.commit()
    return feishu, owner, bot


async def test_overview_reports_origin_levels_and_personal_connections(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    feishu, owner, bot = await _setup(client, db_session, monkeypatch)
    db_session.add(
        FeishuAppRegistration(
            user_id=owner.id,
            bot_id=bot.id,
            purpose="create",
            status="consumed",
            app_id="cli_bound",
            expires_at=datetime.now(UTC),
        )
    )
    other = User(login_name="reader", display_name="读者", role="member", source="sync")
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        FeishuPersonalGrant(
            bot_id=bot.id,
            user_id=other.id,
            app_id="cli_bound",
            app_fingerprint="",
            platform_user_id="u",
            open_id="ou",
            tenant_key="tk",
            status="connected",
            scopes=[],
            poll_interval=5,
        )
    )
    await db_session.commit()

    r = await client.get(f"/api/admin/bots/{bot.id}/feishu-app")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["app_id"] == "cli_bound" and data["app"]["name"] == "销售助手"
    assert data["origin"]["one_click"] and data["origin"]["created_by_name"] == "测试用户"
    assert data["employee"] == {"name": "销售助手", "description": "卖货"}
    assert data["personal_connections"] == 1 and data["next_version"] == "1.0.0"
    assert data["personal_levels"]["messages_readonly"] is False
    assert "contact:user.employee_id:readonly" in data["missing_scopes"]["tenant"]
    assert data["console_url"] == "https://open.feishu.cn/app/cli_bound"
    assert "secret" not in r.text


async def test_only_bot_admins_manage_feishu_bots(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    feishu, owner, bot = await _setup(client, db_session, monkeypatch)
    await login_as(client, db_session, role="member", team_id=owner.team_id, login_name="peer")
    assert (await client.get(f"/api/admin/bots/{bot.id}/feishu-app")).status_code == 403
    r = await client.post(
        f"/api/admin/bots/{bot.id}/feishu-app/publish",
        json={"version": "1.0.0", "changelog": "c", "remark": "r"},
    )
    assert r.status_code == 403 and feishu.calls == []

    await login_existing(client, db_session, owner)
    wecom = Bot(
        bot_key="wecom_only",
        platform="wecom",
        name="企微",
        created_by=owner.id,
        team_id=owner.team_id,
        model="m",
        working_dir="/w2",
        credentials_enc=encrypt_json(CIPHER, {"bot_id": "b", "secret": "s"}, CREDENTIALS_AAD),
    )
    db_session.add(wecom)
    await db_session.commit()
    assert (await client.get(f"/api/admin/bots/{wecom.id}/feishu-app")).status_code == 422


async def test_visibility_needs_admin_scopes_then_reads_and_patches_incrementally(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    feishu, owner, bot = await _setup(client, db_session, monkeypatch)
    member = User(login_name="m1", display_name="成员", role="member", source="sync")
    stranger = User(login_name="m2", display_name="无飞书", role="member", source="sync")
    dept = Department(platform="feishu", platform_dept_id="D100", name="销售部", path="/D100")
    db_session.add_all([member, stranger, dept])
    await db_session.flush()
    db_session.add(UserIdentity(user_id=member.id, platform="feishu", platform_user_id="fs_u1"))
    await db_session.commit()
    path = f"/api/admin/bots/{bot.id}/feishu-app/visibility"
    add = {"add_user_ids": [str(member.id)], "add_department_ids": [str(dept.id)]}

    # 应用还没有管理可用范围的权限：只报状态，修改被拒，且不碰飞书的可用范围接口。
    r = await client.get(path)
    assert r.status_code == 200
    assert r.json()["data"] == {"platform_app": None, "permission": "missing", "via": None}
    r = await client.patch(path, json=add)
    assert r.status_code == 409 and "扫码补齐" in r.json()["message"]
    assert not [c for c in feishu.calls if "visibility" in c.url.path]

    app = feishu.routes[("GET", management.V6_APP)][1]["data"]["app"]  # type: ignore[index]
    for scope in manifest.VISIBILITY_SCOPES:
        app["scopes"].append({"scope": scope, "token_types": ["tenant"]})  # type: ignore[index]
    feishu.routes[("GET", management.VISIBILITY_GET)] = (
        200,
        {
            "code": 0,
            "data": {
                "is_visible_to_all": 0,
                "users": [{"user_id": "fs_u1", "open_id": "ou_1"}, {"user_id": "fs_x"}],
                "departments": [{"id": "D100"}, {"id": "D999"}],
                "invisible_users": [{"user_id": "fs_u1"}],
                "has_more_users": 0,
            },
        },
    )
    r = await client.get(path)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["permission"] == "granted" and data["visible_to_all"] is False
    assert data["users"] == [
        {"feishu_id": "fs_u1", "id": str(member.id), "display_name": "成员", "login_name": "m1"},
        {"feishu_id": "fs_x", "id": None, "display_name": None, "login_name": None},
    ]
    assert [d["name"] for d in data["departments"]] == ["销售部", None]
    assert data["invisible"]["users"][0]["display_name"] == "成员"
    assert feishu.calls[-1].url.params["app_id"] == "cli_bound"

    assert (await client.patch(path, json={})).status_code == 422
    r = await client.patch(path, json={"add_user_ids": [str(stranger.id)]})
    assert r.status_code == 422 and "没有飞书账号" in r.json()["message"]

    feishu.calls.clear()
    r = await client.patch(path, json={**add, "remove_user_ids": [str(member.id)]})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["users"][0]["display_name"] == "成员"
    sent = [c for c in feishu.calls if c.method == "PATCH"]
    assert len(sent) == 1
    assert sent[0].url.path == "/open-apis/application/v6/applications/cli_bound/visibility"
    assert json.loads(sent[0].content) == {
        "add_visible_list": {"user_ids": ["fs_u1"], "department_ids": ["D100"]},
        "del_visible_list": {"user_ids": ["fs_u1"]},
    }
    audit = await db_session.scalar(
        select(AuditLog).where(AuditLog.action == "bot.feishu_app_visibility")
    )
    assert audit is not None and audit.target_id == str(bot.id)
    assert audit.diff["users_added"] == [None, ["fs_u1"]]

    # 当前全员可见时改为部分成员，必须同时给出名单。
    feishu.routes[("GET", management.VISIBILITY_GET)] = (
        200,
        {"code": 0, "data": {"is_visible_to_all": 1, "has_more_users": 0}},
    )
    r = await client.patch(path, json={"visible_to_all": False})
    assert r.status_code == 422 and "至少添加" in r.json()["message"]
    r = await client.patch(path, json={"visible_to_all": True})
    assert r.status_code == 200 and r.json()["data"]["visible_to_all"] is True


async def test_visibility_prefers_the_login_app_and_falls_back_to_the_bot(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    feishu, _, bot = await _setup(client, db_session, monkeypatch)
    db_session.add(
        PlatformApp(
            platform="feishu",
            name="飞书登录",
            capabilities=["login"],
            app_id="cli_login",
            secret_enc=CIPHER.encrypt("s", "platform_apps.secret_enc"),
        )
    )
    await db_session.commit()
    path = f"/api/admin/bots/{bot.id}/feishu-app/visibility"

    # 登录应用没开通权限：退回机器人自己的应用，它也没有就报缺权限。
    feishu.platform_routes[("GET", management.VISIBILITY_GET)] = (
        400,
        {"code": 99991672, "msg": "app scope missing"},
    )
    r = await client.get(path)
    assert r.json()["data"] == {"platform_app": "飞书登录", "permission": "missing", "via": None}
    assert feishu.used == ["platform", "bot"]
    r = await client.patch(path, json={"visible_to_all": True})
    assert r.status_code == 409 and "登录应用" in r.json()["message"]

    # 登录应用有权限：查改的都是这个机器人的 app_id，不碰机器人自己的应用。
    feishu.platform_routes[("GET", management.VISIBILITY_GET)] = (
        200,
        {"code": 0, "data": {"is_visible_to_all": 0, "users": [], "has_more_users": 0}},
    )
    feishu.used.clear()
    feishu.calls.clear()
    r = await client.get(path)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["via"] == "platform" and r.json()["data"]["permission"] == "granted"
    assert feishu.used == ["platform"]
    assert feishu.calls[0].url.params["app_id"] == "cli_bound"
    r = await client.patch(path, json={"visible_to_all": True})
    assert r.status_code == 200 and r.json()["data"]["via"] == "platform"
    assert [c.url.path for c in feishu.calls if c.method == "PATCH"] == [
        "/open-apis/application/v6/applications/cli_bound/visibility"
    ]
    assert set(feishu.used) == {"platform"}

    # 别的失败照常报错，不能当成缺权限退回。
    feishu.platform_routes[("GET", management.VISIBILITY_GET)] = (
        500,
        {"code": 1, "msg": "internal"},
    )
    assert (await client.get(path)).status_code == 502


async def test_menu_validation_and_feishu_rejections_are_explained(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    feishu, owner, bot = await _setup(client, db_session, monkeypatch)
    path = f"/api/admin/bots/{bot.id}/feishu-app/bot"
    nested = {
        "menu_enabled": True,
        "menus": [
            {"menu_id": "a", "name": "A", "kind": "submenu"},
            {"menu_id": "b", "parent_menu_id": "a", "name": "B", "kind": "submenu"},
        ],
    }
    assert (await client.patch(path, json=nested)).status_code == 422
    no_link = {"menu_enabled": True, "menus": [{"menu_id": "a", "name": "A", "kind": "link"}]}
    assert (await client.patch(path, json=no_link)).status_code == 422
    assert (await client.patch(path, json={"menu_enabled": True, "menus": []})).status_code == 422
    assert feishu.calls == []

    feishu.routes[("PATCH", "/open-apis/application/v7/applications/cli_bound/ability")] = (
        403,
        {"code": 210021, "msg": "not developer console"},
    )
    ok = {
        "get_started_desc": "直接提问",
        "menu_enabled": True,
        "menus": [{"menu_id": "a", "name": "文档", "kind": "link", "pc_url": "https://doc"}],
    }
    r = await client.patch(path, json=ok)
    assert r.status_code == 409 and "开发者后台" in r.json()["message"]


async def test_publish_commands_and_scope_apply_are_audited(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    feishu, owner, bot = await _setup(client, db_session, monkeypatch)
    feishu.routes[("POST", "/open-apis/application/v7/applications/cli_bound/publish")] = (
        200,
        {"code": 0, "data": {"version_id": "oav_1", "version": "1.0.1"}},
    )
    feishu.routes[("POST", management.SLASH)] = (200, {"code": 0, "data": {"command_id": "9"}})
    base = f"/api/admin/bots/{bot.id}/feishu-app"
    r = await client.post(
        f"{base}/publish", json={"version": "1.0.1", "changelog": "更新", "remark": "CoreMan"}
    )
    assert r.json()["data"] == {"version_id": "oav_1", "version": "1.0.1"}
    assert (
        await client.post(
            f"{base}/publish", json={"version": "1.0", "changelog": "c", "remark": "r"}
        )
    ).status_code == 422
    r = await client.post(
        f"{base}/slash-commands", json={"command": "report", "description": "生成日报"}
    )
    assert r.status_code == 201 and r.json()["data"] == {"command_id": "9"}
    assert (
        await client.patch(f"{base}/slash-commands/9", json={"description": "新"})
    ).status_code == 200
    assert (await client.delete(f"{base}/slash-commands/9")).status_code == 200
    calls = len(feishu.calls)
    assert (await client.delete(f"{base}/slash-commands/..%2Fx")).status_code in (404, 405, 422)
    assert len(feishu.calls) == calls
    assert (await client.post(f"{base}/scopes/apply")).status_code == 200
    actions = set(
        await db_session.scalars(select(AuditLog.action).where(AuditLog.target_id == str(bot.id)))
    )
    assert {
        "bot.feishu_app_publish",
        "bot.feishu_app_command_create",
        "bot.feishu_app_command_update",
        "bot.feishu_app_command_delete",
        "bot.feishu_app_scopes_apply",
    } <= actions


async def test_avatar_upload_checks_type_and_size_before_calling_feishu(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    feishu, owner, bot = await _setup(client, db_session, monkeypatch)
    feishu.routes[("POST", management.AVATAR_UPLOAD)] = (
        200,
        {"code": 0, "data": {"url": "https://s3-imfile.feishucdn.com/new"}},
    )
    path = f"/api/admin/bots/{bot.id}/feishu-app/avatar"
    r = await client.post(path, files={"avatar": ("a.gif", b"GIF89a", "image/gif")})
    assert r.status_code == 422 and feishu.calls == []
    r = await client.post(path, files={"avatar": ("a.png", b"\x89PNG" * 10, "image/png")})
    assert r.status_code == 200, r.text
    patched = json.loads(feishu.calls[-1].content)
    assert patched["avatar_url"] == "https://s3-imfile.feishucdn.com/new"
    assert patched["i18ns"] == [{"i18n_key": "zh_cn", "name": "销售助手", "description": "卖货"}]


async def test_default_commands_endpoint_adds_missing_builtins(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    feishu, owner, bot = await _setup(client, db_session, monkeypatch)
    feishu.routes[("GET", management.SLASH)] = (
        200,
        {"code": 0, "data": {"items": [{"command_id": "1", "command": "new"}]}},
    )
    feishu.routes[("POST", management.SLASH)] = (200, {"code": 0, "data": {"command_id": "9"}})
    r = await client.post(f"/api/admin/bots/{bot.id}/feishu-app/slash-commands/defaults")
    assert r.status_code == 200, r.text
    assert r.json()["data"] == {"created": ["stop", "sessions", "connect", "help"]}
    posted = [json.loads(c.content)["command"] for c in feishu.calls if c.method == "POST"]
    assert posted == ["stop", "sessions", "connect", "help"]
    audit = await db_session.scalar(
        select(AuditLog).where(AuditLog.action == "bot.feishu_app_command_defaults")
    )
    assert audit is not None


async def test_observe_permission_reports_missing_pending_and_granted(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    feishu, _, bot = await _setup(client, db_session, monkeypatch)
    url = f"/api/admin/bots/{bot.id}/feishu-app/observe-permission"
    r = await client.get(url)
    assert r.status_code == 200 and r.json()["data"] == {"status": "missing"}

    app = feishu.routes[("GET", management.V6_APP)][1]["data"]["app"]  # type: ignore[index]
    app["scopes"].append(  # type: ignore[index]
        {"scope": "im:message.group_msg.include_bot:read", "token_types": ["tenant"]}
    )
    grant = {"scope_name": "im:message.group_msg.include_bot:read", "grant_status": 0}
    feishu.routes[("GET", management.V6_SCOPES)] = (200, {"code": 0, "data": {"scopes": [grant]}})
    assert (await client.get(url)).json()["data"] == {"status": "pending"}

    grant["grant_status"] = 1
    assert (await client.get(url)).json()["data"] == {"status": "granted"}
