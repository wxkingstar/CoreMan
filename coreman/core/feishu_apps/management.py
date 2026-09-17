"""机器人所属飞书应用的管理：以应用自身身份（tenant_access_token）读取与修改自己。

飞书的限制（均来自官方文档）：
- v7 修改接口只允许应用改自己，且只支持「开发者后台创建」的自建应用；
- 应用有版本在审核中时不能修改；
- 基础信息、机器人能力与菜单、可用范围等要提交发布并审核通过后才在线上生效；
- 斜杠指令不需要发布，客户端约 5 分钟后生效。
这里只做参数整形与脱敏的错误映射，权限判断由路由层负责。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from coreman.core.errors import ApiError
from coreman.core.feishu_apps import manifest
from coreman.core.feishu_personal import permissions as personal_permissions
from coreman.core.platforms.feishu import FeishuClient, FeishuError

V6_APP = "/open-apis/application/v6/applications/me"
V6_SCOPES = "/open-apis/application/v6/scopes"
SLASH = "/open-apis/application/v7/app_slash_commands"
AVATAR_UPLOAD = "/open-apis/application/v7/app_avatar/upload"
MENU_TYPES = {"link": 1, "event": 2, "submenu": 3, "message": 4}
MENU_KINDS = {value: key for key, value in MENU_TYPES.items()}
_VERSION = re.compile(r"^(\d{1,4})\.(\d{1,4})\.(\d{1,4})$")

# 飞书错误码 → 给管理员看的原因。未列出的一律按「飞书返回错误」处理并附带错误码。
_NOT_DEVELOPER_CONSOLE = {210001, 210005, 210015, 210021, 210035, 210043}
_UNDER_REVIEW = {210010, 210020, 210040, 210302}
_MISSING_SCOPE = {99991672, 99991640, 210017, 210007, 210037}


def v7(app_id: str, action: str) -> str:
    if not re.fullmatch(r"cli_[A-Za-z0-9]+", app_id):
        raise ApiError(422, 422, "飞书应用 ID 格式不正确")
    return f"/open-apis/application/v7/applications/{app_id}/{action}"


def api_error(exc: FeishuError) -> ApiError:
    code = exc.code
    if code in _NOT_DEVELOPER_CONSOLE:
        message = (
            "飞书不允许通过接口修改这个应用（仅支持开发者后台创建的应用），请在飞书开发者后台修改"
        )
        return ApiError(409, 409, message)
    if code in _UNDER_REVIEW:
        return ApiError(409, 409, "应用有版本正在审核中，审核完成前不能修改或再次发布")
    if code in _MISSING_SCOPE:
        return ApiError(409, 409, f"应用缺少所需权限（{code}），请先扫码补齐权限并发布版本")
    known = {
        210303: "版本号须为 X.Y.Z 格式且高于当前版本",
        210304: "发布信息缺少必填项",
        210019: "消息卡片回调地址校验失败",
        210011: "飞书认为参数不合法，请检查菜单层级、数量与链接",
        210031: "飞书认为参数不合法，请检查所选人员与部门",
        212001: "剩余未授权的都是高敏权限，无法向管理员申请",
        212002: "应用已获得全部权限，无需申请",
        212003: "当前版本向管理员申请授权的次数已达上限",
        212004: "已经申请过，请等待管理员处理",
        40000031: "图标不在飞书支持的范围内",
        99991400: "请求过于频繁，请稍后再试",
    }
    if code in known:
        return ApiError(422, 422, known[code])
    if code == 40000000:
        return ApiError(409, 409, "指令已存在或已被删除，请刷新后重试")
    return ApiError(502, 502, f"飞书接口返回错误（{code}）")


async def _read(coro: Any) -> tuple[dict[str, Any] | None, int | None]:
    try:
        body = await coro
    except FeishuError as exc:
        return None, exc.code
    return _dict(body.get("data")), None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _strings(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _version_out(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    bot = _dict(raw.get("ability")).get("bot")
    remark = _dict(raw.get("remark"))
    visibility = remark.get("visibility")
    visible_list = _dict(_dict(visibility).get("visible_list"))
    return {
        "version_id": raw.get("version_id"),
        "version": raw.get("version"),
        "status": raw.get("status"),
        "create_time": raw.get("create_time"),
        "publish_time": raw.get("publish_time"),
        "remark": remark.get("remark"),
        "update_remark": remark.get("update_remark"),
        "visibility": {
            "is_all": bool(_dict(visibility).get("is_all")),
            "open_ids": _strings(visible_list.get("open_ids")),
            "department_ids": _strings(visible_list.get("department_ids")),
        }
        if isinstance(visibility, dict)
        else None,
        "events": _strings(raw.get("events")),
        "bot": {
            "menu_enabled": bool(bot.get("bot_menu_enable")),
            "menu_display_strategy": bot.get("bot_menu_display_strategy"),
            "menus": [_menu_out(m) for m in bot.get("bot_menus") or [] if isinstance(m, dict)],
        }
        if isinstance(bot, dict)
        else None,
    }


def _menu_out(node: dict[str, Any]) -> dict[str, Any]:
    link = _dict(node.get("redirect_link"))
    kind = node.get("menu_content_type")
    return {
        "menu_id": node.get("menu_id") or "",
        "parent_menu_id": node.get("parent_menu_id") or None,
        "name": node.get("default_name") or "",
        "kind": MENU_KINDS.get(kind, "link") if isinstance(kind, int) else "link",
        "pc_url": link.get("pc_url") or None,
        "mobile_url": link.get("mobile_url") or None,
        "event_key": node.get("event_key") or None,
        "sort": node.get("sort") or 0,
    }


def personal_levels(user_scopes: list[str]) -> dict[str, bool]:
    """应用当前开通的用户权限能支撑「连接飞书」的哪些档位（与个人授权选档时的校验一致）。"""
    available = set(user_scopes) | {"offline_access"}
    try:
        broad = set(personal_permissions.select_scopes("all_except_send", sorted(available)))
    except personal_permissions.PermissionsError:
        broad = set()
    broad -= manifest.PROTOCOL_SCOPES
    # 第 3 档按固定消息范围申请；auth:user.id:read 是协议层授权项，应用权限列表里本来就没有。
    readonly = personal_permissions.MESSAGE_SCOPES - manifest.PROTOCOL_SCOPES
    return {
        "messages_readonly": readonly <= available,
        "all_except_send": bool(broad),
        "all": bool(broad) and {"im:message", "im:message.send_as_user"} <= available,
    }


def _app_out(app: dict[str, Any]) -> dict[str, Any]:
    language = app.get("primary_language") or "zh_cn"
    help_use = next(
        (
            item.get("help_use")
            for item in app.get("i18n") or []
            if isinstance(item, dict) and item.get("i18n_key") == language
        ),
        None,
    )
    return {
        "app_id": app.get("app_id"),
        "name": app.get("app_name") or "",
        "description": app.get("description") or "",
        "avatar_url": app.get("avatar_url") or None,
        "status": app.get("status"),
        "create_source": app.get("create_source") or None,
        "primary_language": language,
        "help_use": help_use or None,
        "online_version_id": app.get("online_version_id") or None,
        "under_review": bool(app.get("unaudit_version_id")),
    }


def _command_out(item: dict[str, Any]) -> dict[str, Any]:
    description = _dict(item.get("description"))
    icon = _dict(item.get("icon") or description.get("icon"))
    return {
        "command_id": str(item.get("command_id") or ""),
        "command": item.get("command") or "",
        "description": description.get("default_value") or "",
        "icon_key": icon.get("icon_key"),
        "update_time": item.get("update_time"),
    }


async def overview(client: FeishuClient) -> dict[str, Any]:
    params = {"lang": "zh_cn", "user_id_type": "open_id"}
    app_data, app_error = await _read(client.call("GET", V6_APP, params=params))
    raw_app = _dict(app_data).get("app")
    app = raw_app if isinstance(raw_app, dict) else None
    (grant_data, grant_error), (command_data, command_error) = await asyncio.gather(
        _read(client.call("GET", V6_SCOPES)), _read(client.call("GET", SLASH))
    )
    versions: dict[str, Any] = {"online": None, "under_review": None}
    version_error = None
    for key, field in (("online", "online_version_id"), ("under_review", "unaudit_version_id")):
        version_id = app.get(field) if app else None
        if not isinstance(version_id, str) or not re.fullmatch(r"oav_[A-Za-z0-9]+", version_id):
            continue
        path = f"{V6_APP}/app_versions/{version_id}"
        data, error = await _read(client.call("GET", path, params=params))
        versions[key] = _version_out(_dict(data).get("app_version"))
        version_error = version_error or error
    grants: dict[str, bool] = {
        str(item.get("scope_name")): item.get("grant_status") == 1
        for item in _dict(grant_data).get("scopes") or []
        if isinstance(item, dict)
    }
    scopes: list[dict[str, Any]] = []
    tenant: set[str] = set()
    user: set[str] = set()
    for item in _dict(app).get("scopes") or []:
        if not isinstance(item, dict) or not isinstance(item.get("scope"), str):
            continue
        name: str = item["scope"]
        token_types = [t for t in _strings(item.get("token_types")) if t in ("tenant", "user")]
        if "tenant" in token_types:
            tenant.add(name)
        if "user" in token_types:
            user.add(name)
        scopes.append(
            {
                "scope": name,
                "token_types": token_types,
                "level": item.get("level"),
                "granted": grants.get(name),
            }
        )
    commands = (
        [_command_out(item) for item in command_data.get("items") or [] if isinstance(item, dict)]
        if command_data is not None
        else None
    )
    return {
        "app": _app_out(app) if app else None,
        "scopes": scopes,
        # 读不到应用时权限列表是空的，不能据此说「缺少全部权限」。
        "missing_scopes": {
            "tenant": manifest.missing_scopes(sorted(tenant), kind="tenant") if app else [],
            "user": manifest.missing_scopes(sorted(user), kind="user") if app else [],
        },
        "pending_grants": sorted(scope for scope, granted in grants.items() if not granted),
        "personal_levels": personal_levels(sorted(user)),
        "versions": versions,
        "slash_commands": commands,
        "errors": {
            "app": app_error,
            "grants": grant_error,
            "versions": version_error,
            "slash_commands": command_error,
        },
    }


async def _write(coro: Any) -> dict[str, Any]:
    try:
        body = await coro
    except FeishuError as exc:
        raise api_error(exc) from exc
    data = body.get("data")
    return data if isinstance(data, dict) else {}


async def update_base(
    client: FeishuClient,
    app_id: str,
    *,
    language: str,
    name: str,
    description: str,
    help_use: str | None = None,
    avatar_url: str | None = None,
) -> None:
    item: dict[str, Any] = {"i18n_key": language, "name": name, "description": description}
    if help_use:
        item["help_use"] = help_use
    body: dict[str, Any] = {"i18ns": [item]}
    if avatar_url:
        body["avatar_url"] = avatar_url
    await _write(client.call("PATCH", v7(app_id, "base"), json=body))


async def upload_avatar(
    client: FeishuClient, content: bytes, filename: str, media_type: str
) -> str:
    data = await _write(
        client.call("POST", AVATAR_UPLOAD, files={"avatar": (filename, content, media_type)})
    )
    url = data.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ApiError(502, 502, "飞书没有返回图标地址")
    return url


def menu_node(menu: dict[str, Any]) -> dict[str, Any]:
    node: dict[str, Any] = {
        "menu_id": menu["menu_id"],
        "default_name": menu["name"],
        "sort": menu.get("sort", 0),
        "menu_content_type": MENU_TYPES[menu["kind"]],
    }
    if menu.get("parent_menu_id"):
        node["parent_menu_id"] = menu["parent_menu_id"]
    if menu["kind"] == "link":
        node["redirect_link"] = {
            "pc_url": menu.get("pc_url") or menu.get("mobile_url"),
            "mobile_url": menu.get("mobile_url") or menu.get("pc_url"),
        }
    if menu["kind"] == "event":
        node["event_key"] = menu["event_key"]
    return node


async def update_bot(
    client: FeishuClient,
    app_id: str,
    *,
    language: str,
    get_started_desc: str | None,
    menu_enabled: bool | None,
    menu_display_strategy: int | None,
    menus: list[dict[str, Any]] | None,
) -> None:
    bot: dict[str, Any] = {"enable": True}
    if get_started_desc:
        bot["i18ns"] = [{"i18n_key": language, "get_started_desc": get_started_desc}]
    if menu_enabled is not None:
        # 飞书规定：开启菜单时菜单不能为空；关闭时一并清空，避免下次开启带出旧菜单。
        bot["bot_menu_enable"] = menu_enabled
        bot["bot_menus"] = [menu_node(m) for m in menus or []] if menu_enabled else []
        if menu_display_strategy:
            bot["bot_menu_display_strategy"] = menu_display_strategy
    await _write(client.call("PATCH", v7(app_id, "ability"), json={"bot": bot}))


async def update_visibility(
    client: FeishuClient,
    app_id: str,
    *,
    visible_to_all: bool,
    user_ids: list[str],
    department_ids: list[str],
) -> None:
    visibility: dict[str, Any] = {"is_visible_to_all": visible_to_all}
    if not visible_to_all:
        visibility["visible_list"] = {"user_ids": user_ids, "department_ids": department_ids}
    await _write(
        client.call(
            "PATCH",
            v7(app_id, "config"),
            params={"user_id_type": "user_id", "department_id_type": "department_id"},
            json={"visibility": visibility},
        )
    )


def next_version(current: str | None) -> str:
    match = _VERSION.match(current or "")
    if not match:
        return "1.0.0"
    major, minor, patch = (int(part) for part in match.groups())
    return f"{major}.{minor}.{patch + 1}"


async def publish(
    client: FeishuClient, app_id: str, *, version: str, changelog: str, remark: str
) -> dict[str, Any]:
    data = await _write(
        client.call(
            "POST",
            v7(app_id, "publish"),
            json={
                "mobile_default_ability": "bot",
                "pc_default_ability": "bot",
                "version": version,
                "changelog": changelog,
                "remark": remark,
            },
        )
    )
    return {"version_id": data.get("version_id"), "version": data.get("version") or version}


async def apply_scopes(client: FeishuClient) -> None:
    await _write(client.call("POST", f"{V6_SCOPES}/apply"))


def _command_body(description: str, icon_key: str | None) -> dict[str, Any]:
    # 飞书指南示例把 icon 放在 description 里，实测会被忽略；与列表返回一致放在顶层才生效。
    body: dict[str, Any] = {
        "description": {"default_value": description, "i18n": {"zh_cn": description}}
    }
    if icon_key:
        body["icon"] = {"icon_key": icon_key}
    return body


async def create_command(
    client: FeishuClient, *, command: str, description: str, icon_key: str | None
) -> str:
    body = {"command": command, **_command_body(description, icon_key)}
    data = await _write(client.call("POST", SLASH, json=body))
    return str(data.get("command_id") or "")


async def update_command(
    client: FeishuClient, command_id: str, *, description: str, icon_key: str | None
) -> None:
    body = _command_body(description, icon_key)
    await _write(client.call("PATCH", f"{SLASH}/{command_id}", json=body))


async def delete_command(client: FeishuClient, command_id: str) -> None:
    await _write(client.call("DELETE", f"{SLASH}/{command_id}"))


async def ensure_default_commands(client: FeishuClient) -> list[str]:
    """补齐 CoreMan 内置斜杠指令；已存在的同名指令不改动。返回本次新建的指令名。"""
    data = await _write(client.call("GET", SLASH))
    existing = {item.get("command") for item in data.get("items") or [] if isinstance(item, dict)}
    created = []
    for command, description, icon in manifest.DEFAULT_SLASH_COMMANDS:
        if command in existing:
            continue
        await create_command(client, command=command, description=description, icon_key=icon)
        created.append(command)
    return created
