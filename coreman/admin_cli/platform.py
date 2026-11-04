"""平台管理：平台设置、模型目录与价格、平台应用、接口调用方、审计日志、统计、对话记录与任务跟进。"""

from __future__ import annotations

import argparse
import json
from typing import Any

from coreman.admin_cli import output
from coreman.admin_cli.base import add_yes, changed, group, leaf, on_off, read_text, require_yes
from coreman.admin_cli.client import PAGE_SIZE, AdminClient, CliError
from coreman.core.db.models import CAPABILITIES, WORK_TASK_STATUSES
from coreman.core.relay.models import backend_of
from coreman.core.settings_schema import SettingsPatch

SETTINGS = "/api/admin/settings"
ALERTS = "/api/admin/alert-settings"
SMTP = "/api/admin/notification-settings"
CATALOG = "/api/admin/model-catalog"
PRICES = "/api/admin/model-prices"
APPS = "/api/admin/platform-apps"
CLIENTS = "/api/admin/api-clients"
JWT_KEYS = "/api/admin/jwt-keys"
AUDIT = "/api/admin/audit-logs"
CHATS = "/api/admin/chat-logs"
TASKS = "/api/admin/work-tasks"
# 设置页里篇幅很长的提示词段落：show 只显示字数，全文用 get 看。
LONG_TEXT = 80


def register(sub: Any) -> None:
    register_settings(sub)
    register_models(sub)
    register_apps(sub)
    register_clients(sub)
    register_audit(sub)
    register_stats(sub)
    register_chats(sub)
    register_tasks(sub)


# ---- 平台设置 ----


def register_settings(sub: Any) -> None:
    ops = group(sub, "settings", "平台设置、告警接收人与通知邮箱（平台管理员）")
    leaf(ops, "show", "查看全部平台设置（长文本只显示字数）", settings_show)
    leaf(ops, "get", "查看一项设置的完整值", settings_get).add_argument("key", help="设置项")
    p = leaf(ops, "set", "修改平台设置（只改给出的项）", settings_set)
    p.add_argument(
        "items",
        nargs="*",
        metavar="KEY=VALUE",
        help="文本项原样保存；其余按 JSON 解析（true、3、0.5）；null 表示清空",
    )
    p.add_argument("--file", action="append", metavar="KEY=文件", help="从文件读取文本值，可重复")
    leaf(ops, "defaults", "查看新建 AI 员工的默认值（所有成员可看）", settings_defaults)
    leaf(ops, "alerts", "查看告警接收渠道", alerts_show)
    p = leaf(ops, "alerts-set", "替换全部告警接收渠道", alerts_set)
    p.add_argument(
        "--channel",
        action="append",
        metavar="应用=成员",
        help="通知应用（名称或 id）与接收人（登录名），可重复",
    )
    p.add_argument("--clear", action="store_true", help="清空所有渠道")
    leaf(ops, "notifications", "查看通知邮箱（SMTP）设置", smtp_show)
    p = leaf(ops, "notifications-set", "修改通知邮箱设置（只改给出的字段）", smtp_set)
    p.add_argument("--enabled", type=on_off, metavar="on|off")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--security", choices=("tls", "starttls"))
    p.add_argument("--username")
    p.add_argument("--sender", help="发件地址")
    p.add_argument("--password-file", metavar="文件", help="SMTP 密码；- 表示从标准输入读")


def setting_types(key: str) -> set[str]:
    props = SettingsPatch.model_json_schema()["properties"]
    if key not in props:
        raise CliError(f"没有可修改的设置项 {key}；可修改的有：{'、'.join(props)}")
    prop = props[key]
    return {item.get("type") for item in prop.get("anyOf", [prop])}


def setting_value(key: str, raw: str) -> Any:
    """按接口 schema 解析：文本项原样保存，其余按 JSON 解析。"""
    if raw == "null":
        return None
    if "string" in setting_types(key):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise CliError(f"{key} 的值不是合法的 JSON：{raw}") from None


def short(value: Any) -> Any:
    if isinstance(value, str) and (len(value) > LONG_TEXT or "\n" in value):
        return f"（{len(value)} 字，用 settings get 查看）"
    return value


async def settings_show(c: AdminClient, args: argparse.Namespace) -> int:
    data = await c.get(SETTINGS)
    if args.json:
        output.dump(data)
    else:
        output.fields({key: short(value) for key, value in data.items()}, [(k, k) for k in data])
    return 0


async def settings_get(c: AdminClient, args: argparse.Namespace) -> int:
    data = await c.get(SETTINGS)
    if args.key not in data:
        raise CliError(f"没有设置项 {args.key}")
    value = data[args.key]
    if args.json or not isinstance(value, str):
        output.dump(value)
    else:
        print(value)
    return 0


async def settings_set(c: AdminClient, args: argparse.Namespace) -> int:
    body: dict[str, Any] = {}
    for item in args.items:
        key, sep, raw = item.partition("=")
        if not sep or not key:
            raise CliError(f"参数应为 KEY=VALUE：{item}")
        body[key] = setting_value(key, raw)
    for item in args.file or []:
        key, sep, path = item.partition("=")
        if not sep or not key:
            raise CliError(f"--file 应为 KEY=文件：{item}")
        setting_types(key)
        body[key] = read_text(path)
    if not body:
        raise CliError("没有要修改的设置，见 --help")
    data = await c.request("PUT", SETTINGS, body=body)
    if args.json:
        output.dump(data)
    else:
        print(f"已更新设置：{'、'.join(body)}")
    return 0


async def settings_defaults(c: AdminClient, args: argparse.Namespace) -> int:
    data = await c.get(f"{SETTINGS}/defaults")
    if args.json:
        output.dump(data)
    else:
        output.fields(data, [(key, key) for key in data])
    return 0


async def alerts_show(c: AdminClient, args: argparse.Namespace) -> int:
    data = await c.get(ALERTS)
    if args.json:
        output.dump(data)
        return 0
    apps = {a["id"]: a for a in await c.collect(APPS)}
    rows = []
    for ch in data["channels"]:
        app = apps.get(ch["platform_app_id"])
        try:
            user = await c.user(ch["user_id"])
            person = f"{user['login_name']}（{user['display_name']}）"
        except CliError:
            person = ch["user_id"]
        rows.append({"app": app["name"] if app else ch["platform_app_id"], "user": person})
    output.table(rows, [("通知应用", "app"), ("接收人", "user")], empty="（没有告警接收渠道）")
    return 0


async def alerts_set(c: AdminClient, args: argparse.Namespace) -> int:
    if not args.channel and not args.clear:
        raise CliError("用 --channel 应用=成员 给出全部渠道，或用 --clear 清空")
    if args.channel and args.clear:
        raise CliError("--channel 与 --clear 不能同时使用")
    channels = []
    for item in args.channel or []:
        app_ref, sep, user_ref = item.partition("=")
        if not sep or not app_ref or not user_ref:
            raise CliError(f"--channel 应为 应用=成员：{item}")
        app = await find_app(c, app_ref)
        user = await c.user(user_ref)
        channels.append({"platform_app_id": app["id"], "user_id": user["id"]})
    current = await c.get(ALERTS)
    data = await c.request("PUT", ALERTS, body={"channels": channels}, version=current["version"])
    if args.json:
        output.dump(data)
    else:
        print(f"告警接收渠道已更新：共 {len(data['channels'])} 个")
    return 0


SMTP_FIELDS = ("enabled", "host", "port", "security", "username", "sender")


async def smtp_show(c: AdminClient, args: argparse.Namespace) -> int:
    data = await c.get(SMTP)
    if args.json:
        output.dump(data)
    else:
        output.fields(
            data,
            [
                ("启用", "enabled"),
                ("服务器", "host"),
                ("端口", "port"),
                ("加密方式", "security"),
                ("用户名", "username"),
                ("已设密码", "has_password"),
                ("发件地址", "sender"),
                ("版本", "version"),
            ],
        )
    return 0


async def smtp_set(c: AdminClient, args: argparse.Namespace) -> int:
    updates = changed(args, {name: name for name in SMTP_FIELDS})
    if args.password_file is not None:
        updates["password"] = read_text(args.password_file).strip()
    if not updates:
        raise CliError("没有要修改的字段，见 --help")
    current = await c.get(SMTP)
    # 整份替换：没给密码时不传，服务端保留原密码。
    body = {key: current[key] for key in SMTP_FIELDS} | updates
    data = await c.request("PUT", SMTP, body=body, version=current["version"])
    if args.json:
        output.dump(data)
    else:
        print(f"已更新通知邮箱设置：{'、'.join(updates)}")
    return 0


# ---- 模型目录与价格 ----


def register_models(sub: Any) -> None:
    ops = group(sub, "models", "模型目录与模型价格")
    p = leaf(ops, "list", "列出模型目录", models_list)
    p.add_argument("--provider", help="只看这个提供方")
    p = leaf(ops, "add", "往目录里加一个模型", models_add)
    p.add_argument("provider", help="提供方，如 claude、codex")
    p.add_argument("model", help="模型名")
    p.add_argument("--display-name", help="显示名称")
    p.add_argument("--default", action="store_true", help="设为该提供方的默认模型")
    p.add_argument("--retired", action="store_true", help="登记为已退役")
    p.add_argument("--xhigh", action="store_true", help="支持 xhigh 推理强度")
    p.add_argument("--max", action="store_true", help="支持 max 推理强度")
    p.add_argument("--sort", type=int, default=0, help="排序，大的在前")
    p = leaf(ops, "set", "修改目录里的模型（只改给出的字段）", models_set)
    p.add_argument("provider")
    p.add_argument("model")
    p.add_argument("--display-name", help="显示名称；传空串清除")
    p.add_argument("--default", type=on_off, metavar="on|off", help="是否为默认模型")
    p.add_argument("--retired", type=on_off, metavar="on|off", help="是否退役")
    p.add_argument("--xhigh", type=on_off, metavar="on|off")
    p.add_argument("--max", type=on_off, metavar="on|off")
    p.add_argument("--sort", type=int)
    p = leaf(ops, "delete", "从目录删除模型（仍有 AI 员工在用时会被拒绝）", models_delete)
    p.add_argument("provider")
    p.add_argument("model")
    add_yes(p)
    p = leaf(ops, "prices", "列出模型价格（美元 / 百万 Token）", prices_list)
    p.add_argument("--model", help="只看这个模型")
    p = leaf(ops, "price-set", "新增或修改某个生效日的模型价格", price_set)
    p.add_argument("model")
    p.add_argument("--from", dest="effective_from", required=True, metavar="日期", help="生效日")
    p.add_argument("--provider", help="后端；默认按模型名判断")
    for flag, text in (
        ("--input", "输入"),
        ("--output", "输出"),
        ("--cache-read", "缓存读取"),
        ("--cache-write", "缓存写入"),
    ):
        p.add_argument(flag, metavar="美元", help=f"{text}单价（新增时必填）")


async def models_list(c: AdminClient, args: argparse.Namespace) -> int:
    rows = await c.get(CATALOG, provider=args.provider)
    if args.json:
        output.dump(rows)
    else:
        output.table(
            rows,
            [
                ("提供方", "provider"),
                ("模型", "model"),
                ("显示名称", "display_name"),
                ("默认", "is_default"),
                ("退役", "retired"),
                ("xhigh", "supports_xhigh"),
                ("max", "supports_max"),
                ("排序", "sort_order"),
                ("后端", "backend"),
            ],
        )
    return 0


CATALOG_OPTIONS = {
    "display_name": "display_name",
    "default": "is_default",
    "retired": "retired",
    "xhigh": "supports_xhigh",
    "max": "supports_max",
    "sort": "sort_order",
}


async def models_add(c: AdminClient, args: argparse.Namespace) -> int:
    body = {"provider": args.provider, "model": args.model, **changed(args, CATALOG_OPTIONS)}
    data = await c.request("POST", CATALOG, body=body)
    if args.json:
        output.dump(data)
    else:
        print(f"已添加模型 {args.provider}/{args.model}")
    return 0


async def models_set(c: AdminClient, args: argparse.Namespace) -> int:
    updates = changed(args, CATALOG_OPTIONS)
    if updates.get("display_name") == "":
        updates["display_name"] = None
    if not updates:
        raise CliError("没有要修改的字段，见 --help")
    data = await c.request("PATCH", f"{CATALOG}/{args.provider}/{args.model}", body=updates)
    if args.json:
        output.dump(data)
    else:
        print(f"已更新模型 {args.provider}/{args.model}：{'、'.join(updates)}")
    return 0


async def models_delete(c: AdminClient, args: argparse.Namespace) -> int:
    require_yes(args, f"从目录删除模型 {args.provider}/{args.model}")
    await c.request("DELETE", f"{CATALOG}/{args.provider}/{args.model}")
    print(f"已从目录删除模型 {args.provider}/{args.model}")
    return 0


PRICE_FIELDS = {
    "input": "input_usd",
    "output": "output_usd",
    "cache_read": "cache_read_usd",
    "cache_write": "cache_write_usd",
}


async def prices_list(c: AdminClient, args: argparse.Namespace) -> int:
    rows = await c.get(PRICES)
    if args.model:
        rows = [r for r in rows if r["model"] == args.model]
    if args.json:
        output.dump(rows)
    else:
        output.table(
            rows,
            [
                ("后端", "provider"),
                ("模型", "model"),
                ("生效日", "effective_from"),
                ("输入", "input_usd"),
                ("输出", "output_usd"),
                ("缓存读取", "cache_read_usd"),
                ("缓存写入", "cache_write_usd"),
            ],
        )
    return 0


async def price_set(c: AdminClient, args: argparse.Namespace) -> int:
    provider = args.provider or backend_of(args.model)
    key = (provider, args.model, args.effective_from)
    current = next(
        (r for r in await c.get(PRICES) if (r["provider"], r["model"], r["effective_from"]) == key),
        None,
    )
    updates = changed(args, PRICE_FIELDS)
    if current is None:
        missing = [f"--{k.replace('_', '-')}" for k, f in PRICE_FIELDS.items() if f not in updates]
        if missing:
            raise CliError(f"新增价格需要 {'、'.join(missing)}")
    elif not updates:
        raise CliError("没有要修改的价格，见 --help")
    base = {f: current[f] for f in PRICE_FIELDS.values()} if current else {}
    body = {
        "provider": provider,
        "model": args.model,
        "effective_from": args.effective_from,
        **base,
        **updates,
    }
    data = await c.request("PUT", PRICES, body=body, version=current["version"] if current else 0)
    if args.json:
        output.dump(data)
    else:
        print(f"已保存 {provider}/{args.model} 自 {args.effective_from} 起的价格")
    return 0


# ---- 平台应用 ----


def register_apps(sub: Any) -> None:
    ops = group(sub, "apps", "平台应用：企业微信与飞书应用（平台管理员）")
    p = leaf(ops, "list", "列出平台应用", apps_list)
    p.add_argument("--platform", choices=("wecom", "feishu"))
    leaf(ops, "show", "查看平台应用（密钥已脱敏）", apps_show).add_argument(
        "app", help="应用名称或 id（可只写开头几位）"
    )
    p = leaf(ops, "create", "新建平台应用；密钥从文件或标准输入读", apps_create)
    p.add_argument("--platform", choices=("wecom", "feishu"), required=True)
    app_options(p, create=True)
    p.add_argument("--disabled", action="store_true", help="新建为停用")
    p = leaf(ops, "set", "修改平台应用（只改给出的字段）", apps_set)
    p.add_argument("app")
    app_options(p, create=False)
    p.add_argument("--enabled", type=on_off, metavar="on|off")
    p = leaf(ops, "delete", "删除平台应用", apps_delete)
    p.add_argument("app")
    add_yes(p)
    leaf(ops, "test", "用应用凭证换一次 access_token，检验配置", apps_test).add_argument("app")
    leaf(ops, "sync", "触发一次通讯录同步（后台执行）", apps_sync).add_argument("app")
    p = leaf(ops, "runs", "查看通讯录同步记录", apps_runs)
    p.add_argument("app")
    p.add_argument("--limit", type=int, default=20, help="最多显示几条（1-100）")
    leaf(ops, "run-show", "查看一次通讯录同步", apps_run_show).add_argument(
        "run_id", type=int, help="同步记录 id"
    )


def app_options(p: argparse.ArgumentParser, *, create: bool) -> None:
    p.add_argument("--name", required=create, help="应用名称")
    p.add_argument(
        "--capability",
        action="append",
        required=create,
        choices=CAPABILITIES,
        help="应用能力，可重复；修改时替换全部能力",
    )
    p.add_argument("--corp-id", help="企业微信 corp_id")
    p.add_argument("--app-id", help="飞书 app_id")
    p.add_argument(
        "--secret-file", required=create, metavar="文件", help="应用 Secret；- 表示从标准输入读"
    )
    p.add_argument("--callback-token-file", metavar="文件", help="回调 Token")
    p.add_argument("--callback-aes-key-file", metavar="文件", help="回调 EncodingAESKey")


APP_OPTIONS = {
    "name": "name",
    "capability": "capabilities",
    "corp_id": "corp_id",
    "app_id": "app_id",
}
SECRET_OPTIONS = {
    "secret_file": "secret",
    "callback_token_file": "callback_token",
    "callback_aes_key_file": "callback_aes_key",
}
APP_FIELDS = (
    "platform",
    "name",
    "capabilities",
    "corp_id",
    "app_id",
    "secret",
    "callback_token",
    "callback_aes_key",
    "extra",
    "enabled",
)


def app_changes(args: argparse.Namespace) -> dict[str, Any]:
    updates = changed(args, APP_OPTIONS)
    for dest, field in SECRET_OPTIONS.items():
        path = getattr(args, dest)
        if path is not None:
            updates[field] = read_text(path).strip()
    return updates


async def find_app(c: AdminClient, ref: str) -> dict[str, Any]:
    rows = await c.collect(APPS)
    found = (
        [a for a in rows if a["id"] == ref]
        or [a for a in rows if a["name"] == ref]
        or [a for a in rows if a["id"].startswith(ref)]
    )
    if not found:
        raise CliError(f"找不到平台应用：{ref}")
    if len(found) > 1:
        labels = "、".join(f"{a['name']}（{a['id'][:8]}）" for a in found)
        raise CliError(f"平台应用「{ref}」匹配到多个：{labels}，请写得更精确")
    return found[0]


async def apps_list(c: AdminClient, args: argparse.Namespace) -> int:
    rows = await c.collect(APPS, platform=args.platform)
    if args.json:
        output.dump(rows)
    else:
        output.table(
            rows,
            [
                ("id", lambda r: r["id"][:8]),
                ("名称", "name"),
                ("平台", "platform"),
                ("状态", output.enabled),
                ("能力", "capabilities"),
                ("corp_id / app_id", lambda r: r["corp_id"] or r["app_id"]),
            ],
        )
    return 0


async def apps_show(c: AdminClient, args: argparse.Namespace) -> int:
    app = await find_app(c, args.app)
    if args.json:
        output.dump(app)
    else:
        output.fields(
            app,
            [
                ("名称", "name"),
                ("id", "id"),
                ("平台", "platform"),
                ("状态", output.enabled),
                ("能力", "capabilities"),
                ("corp_id", "corp_id"),
                ("app_id", "app_id"),
                ("Secret", "secret"),
                ("回调 Token", "callback_token"),
                ("回调 AESKey", "callback_aes_key"),
                ("回调地址", "callback_url"),
                ("更新时间", "updated_at"),
            ],
        )
    return 0


async def apps_create(c: AdminClient, args: argparse.Namespace) -> int:
    body = {"platform": args.platform, **app_changes(args), "enabled": not args.disabled}
    data = await c.request("POST", APPS, body=body)
    if args.json:
        output.dump(data)
    else:
        print(f"已新建平台应用 {data['name']}（{data['id']}）")
        if data["callback_url"]:
            print(f"回调地址：{data['callback_url']}")
    return 0


async def apps_set(c: AdminClient, args: argparse.Namespace) -> int:
    updates = app_changes(args) | changed(args, {"enabled": "enabled"})
    if not updates:
        raise CliError("没有要修改的字段，见 --help")
    app = await find_app(c, args.app)
    # 整份替换：未改的密钥回传脱敏值，服务端按「脱敏 = 不变」保留原值。
    body = {key: app[key] for key in APP_FIELDS} | updates
    data = await c.request("PUT", f"{APPS}/{app['id']}", body=body, version=app["version"])
    if args.json:
        output.dump(data)
    else:
        print(f"已更新平台应用 {data['name']}：{'、'.join(updates)}")
    return 0


async def apps_delete(c: AdminClient, args: argparse.Namespace) -> int:
    app = await find_app(c, args.app)
    require_yes(args, f"删除平台应用 {app['name']}")
    await c.request("DELETE", f"{APPS}/{app['id']}")
    print(f"已删除平台应用 {app['name']}")
    return 0


async def apps_test(c: AdminClient, args: argparse.Namespace) -> int:
    app = await find_app(c, args.app)
    data = await c.request("POST", f"{APPS}/{app['id']}/test")
    if args.json:
        output.dump(data)
    else:
        print(f"{app['name']}：{'通过' if data['ok'] else '未通过'}，{data['message']}")
    return 0 if data["ok"] else 1


async def apps_sync(c: AdminClient, args: argparse.Namespace) -> int:
    app = await find_app(c, args.app)
    data = await c.request("POST", f"{APPS}/{app['id']}/sync")
    if args.json:
        output.dump(data)
    else:
        print(
            f"已开始同步 {app['name']} 的通讯录，记录 {data['run_id']}；用 apps run-show 查看进度"
        )
    return 0


RUN_COLUMNS: list[output.Column] = [
    ("记录", "id"),
    ("状态", "status"),
    ("开始", "started_at"),
    ("结束", "finished_at"),
    ("统计", "stats"),
    ("错误", "error"),
]


async def apps_runs(c: AdminClient, args: argparse.Namespace) -> int:
    app = await find_app(c, args.app)
    rows = await c.get(f"{APPS}/{app['id']}/sync-runs", limit=args.limit)
    if args.json:
        output.dump(rows)
    else:
        output.table(rows, RUN_COLUMNS, empty="（还没有同步记录）")
    return 0


async def apps_run_show(c: AdminClient, args: argparse.Namespace) -> int:
    data = await c.get(f"/api/admin/sync-runs/{args.run_id}")
    if args.json:
        output.dump(data)
    else:
        output.fields(data, [*RUN_COLUMNS, ("触发人", "triggered_by")])
    return 0


# ---- 接口调用方与签名密钥 ----


def register_clients(sub: Any) -> None:
    ops = group(sub, "clients", "接口调用方、JWT 签名密钥与令牌签发方")
    leaf(ops, "list", "列出接口调用方（平台管理员）", clients_list)
    p = leaf(ops, "create", "新建接口调用方；密钥只显示这一次", clients_create)
    p.add_argument("app_key", help="调用方标识：字母数字下划线连字符")
    p.add_argument("--name", required=True, help="显示名称")
    p.add_argument("--scope", action="append", metavar="接口组", help="开放的接口组，可重复")
    p.add_argument("--secret-file", metavar="文件", help="指定密钥（至少 32 位）；默认随机生成")
    p.add_argument("--disabled", action="store_true", help="新建为停用")
    p = leaf(ops, "set", "修改接口调用方（只改给出的字段）", clients_set)
    p.add_argument("app_key")
    p.add_argument("--name")
    p.add_argument("--scope", action="append", metavar="接口组", help="替换全部接口组，可重复")
    p.add_argument("--no-scope", action="store_true", help="清空接口组")
    p.add_argument("--enabled", type=on_off, metavar="on|off")
    p = leaf(ops, "rotate-secret", "重新生成调用方密钥（旧密钥立即失效）", clients_rotate)
    p.add_argument("app_key")
    add_yes(p)
    leaf(ops, "jwt-keys", "列出 JWT 签名密钥（平台管理员）", jwt_keys)
    add_yes(leaf(ops, "jwt-rotate", "轮换平台 JWT 签名密钥", jwt_rotate))
    leaf(ops, "token-providers", "列出业务令牌签发方", token_providers)


async def find_client(c: AdminClient, key: str) -> dict[str, Any]:
    found = [r for r in await c.collect(CLIENTS) if r["app_key"] == key]
    if not found:
        raise CliError(f"找不到接口调用方：{key}")
    return found[0]


async def clients_list(c: AdminClient, args: argparse.Namespace) -> int:
    rows = await c.collect(CLIENTS)
    if args.json:
        output.dump(rows)
    else:
        output.table(
            rows,
            [
                ("标识", "app_key"),
                ("名称", "name"),
                ("状态", output.enabled),
                ("接口组", "scopes"),
                ("最近调用", "last_used_at"),
            ],
        )
    return 0


def print_secret(data: dict[str, Any]) -> None:
    print(f"密钥（只显示这一次，请立即妥善保存）：{data['secret']}")


async def clients_create(c: AdminClient, args: argparse.Namespace) -> int:
    body: dict[str, Any] = {
        "app_key": args.app_key,
        "name": args.name,
        "scopes": args.scope or [],
        "enabled": not args.disabled,
    }
    if args.secret_file is not None:
        body["secret"] = read_text(args.secret_file).strip()
    data = await c.request("POST", CLIENTS, body=body)
    if args.json:
        output.dump(data)
    else:
        print(f"已新建接口调用方 {data['app_key']}")
        print_secret(data)
    return 0


async def clients_set(c: AdminClient, args: argparse.Namespace) -> int:
    updates = changed(args, {"name": "name", "scope": "scopes", "enabled": "enabled"})
    if args.no_scope:
        updates["scopes"] = []
    if not updates:
        raise CliError("没有要修改的字段，见 --help")
    row = await find_client(c, args.app_key)
    body = {key: row[key] for key in ("name", "scopes", "enabled")} | updates
    data = await c.request("PUT", f"{CLIENTS}/{args.app_key}", body=body, version=row["version"])
    if args.json:
        output.dump(data)
    else:
        print(f"已更新接口调用方 {args.app_key}：{'、'.join(updates)}")
    return 0


async def clients_rotate(c: AdminClient, args: argparse.Namespace) -> int:
    row = await find_client(c, args.app_key)
    require_yes(args, f"重新生成 {args.app_key} 的密钥（旧密钥立即失效）")
    data = await c.request(
        "POST", f"{CLIENTS}/{args.app_key}/rotate-secret", version=row["version"]
    )
    if args.json:
        output.dump(data)
    else:
        print(f"已重新生成 {args.app_key} 的密钥")
        print_secret(data)
    return 0


async def jwt_keys(c: AdminClient, args: argparse.Namespace) -> int:
    rows = await c.get(JWT_KEYS)
    if args.json:
        output.dump(rows)
    else:
        output.table(
            rows,
            [
                ("kid", "kid"),
                ("当前使用", "is_active"),
                ("外部签发方", "external"),
                ("创建时间", "created_at"),
                ("退役时间", "retired_at"),
            ],
        )
    return 0


async def jwt_rotate(c: AdminClient, args: argparse.Namespace) -> int:
    require_yes(args, "轮换平台 JWT 签名密钥")
    data = await c.request("POST", f"{JWT_KEYS}/rotate")
    if args.json:
        output.dump(data)
    else:
        print(f"已轮换 JWT 签名密钥，新 kid：{data['kid']}")
    return 0


async def token_providers(c: AdminClient, args: argparse.Namespace) -> int:
    rows = await c.get("/api/admin/token-providers")
    if args.json:
        output.dump(rows)
    else:
        output.table(rows, [("签发方", "id"), ("令牌最长有效秒数", "max_token_ttl_seconds")])
    return 0


# ---- 审计日志 ----


async def take(c: AdminClient, path: str, limit: int, **params: Any) -> tuple[list[Any], int]:
    """分页接口取前 limit 条，返回（记录，总数）。"""
    items: list[Any] = []
    page = 1
    while True:
        size = min(PAGE_SIZE, limit)
        data = await c.get(path, **params, page=page, per_page=size)
        items.extend(data["items"])
        if len(items) >= limit or len(data["items"]) < size:
            return items[:limit], data["total"]
        page += 1


def footer(shown: int, total: int) -> None:
    if total > shown:
        print(f"\n共 {total} 条，显示最新 {shown} 条；用 --limit 看更多")


def register_audit(sub: Any) -> None:
    ops = group(sub, "audit", "审计日志（AI 委员会与平台管理员）")
    p = leaf(ops, "list", "查询审计日志，最新的在前", audit_list)
    p.add_argument("--actor", dest="who", help="操作人登录名（模糊匹配）或 id")
    p.add_argument("--action", help="动作前缀，如 bot. 或 settings.update")
    p.add_argument("--target-type", help="资源类型，如 bot、skill_config")
    p.add_argument("--target-id", help="资源 id")
    p.add_argument("--since", metavar="时间", help="起始时间（ISO 8601，如 2026-01-01T00:00:00Z）")
    p.add_argument("--until", metavar="时间", help="截止时间")
    p.add_argument("--limit", type=int, default=50, help="最多显示几条，默认 50")


async def audit_list(c: AdminClient, args: argparse.Namespace) -> int:
    rows, total = await take(
        c,
        AUDIT,
        args.limit,
        actor=args.who,
        action=args.action,
        target_type=args.target_type,
        target_id=args.target_id,
        since=args.since,
        until=args.until,
    )
    if args.json:
        output.dump(rows)
        return 0
    output.table(
        rows,
        [
            ("时间", "created_at"),
            ("操作人", "actor_login"),
            ("动作", "action"),
            ("资源", lambda r: f"{r['target_type']}:{r['target_id']}"),
            ("变更", "diff"),
            ("IP", "ip"),
        ],
        empty="（没有符合条件的审计日志）",
    )
    footer(len(rows), total)
    return 0


# ---- 统计 ----


def register_stats(sub: Any) -> None:
    p = leaf(sub, "stats", "全平台用量统计（所有成员可看）", stats)
    p.add_argument("--start", metavar="日期", help="起始日，默认截止日前 29 天")
    p.add_argument("--end", metavar="日期", help="截止日，默认今天")
    p.add_argument(
        "--timezone", choices=("UTC", "Asia/Shanghai", "Asia/Tokyo"), help="按哪个时区分日"
    )


USAGE_COLUMNS: list[output.Column] = [
    ("消息", "messages"),
    ("输入", "input_tokens"),
    ("输出", "output_tokens"),
    ("缓存读取", "cache_read_tokens"),
    ("缓存写入", "cache_creation_tokens"),
    ("估算费用 $", "cost_usd"),
]


async def stats(c: AdminClient, args: argparse.Namespace) -> int:
    data = await c.get(
        "/api/admin/statistics", start=args.start, end=args.end, timezone=args.timezone
    )
    if args.json:
        output.dump(data)
        return 0
    print(f"{data['start']} 至 {data['end']}（{data['timezone']}，费用为估算）")
    output.fields(data["total"], [("人数", "users"), ("AI 员工数", "bots"), *USAGE_COLUMNS])
    print("\n按日：")
    output.table(data["daily"], [("日期", "day"), *USAGE_COLUMNS])
    print(f"\nAI 员工前 {data['ranking_limit']}：")
    output.table(data["by_bot"], [("AI 员工", "name"), ("人数", "users"), *USAGE_COLUMNS])
    print(f"\n成员前 {data['ranking_limit']}（按 Token 合计）：")
    output.table(data["by_user"], [("成员", "name"), ("Token 合计", "tokens"), *USAGE_COLUMNS])
    return 0


# ---- 对话记录 ----


def chat_filters(p: argparse.ArgumentParser) -> None:
    p.add_argument("--bot", metavar="AI员工", help="标识、名称或 id")
    p.add_argument("--user", help="发言人登录名（模糊匹配）或 id")
    p.add_argument("--status", help="状态，如 success、error、timeout")
    p.add_argument("--chat-type", help="会话类型，如 single、group、cron")
    p.add_argument("--keyword", help="在消息与回复里搜索")
    p.add_argument("--token-id", help="业务系统日志里的令牌 id（jti）")
    p.add_argument("--since", metavar="时间", help="起始时间（ISO 8601）")
    p.add_argument("--until", metavar="时间", help="截止时间")


def register_chats(sub: Any) -> None:
    ops = group(sub, "chats", "对话记录（只看得到自己有份的部分）")
    p = leaf(ops, "list", "列出对话记录摘要，最新的在前", chats_list)
    chat_filters(p)
    p.add_argument("--limit", type=int, default=50, help="最多显示几条，默认 50")
    leaf(ops, "show", "查看一条对话的全文", chats_show).add_argument(
        "id", type=int, help="对话记录 id"
    )
    chat_filters(leaf(ops, "stats", "对话概览：总量、状态分布、Token 与按员工排行", chats_stats))


async def chat_params(c: AdminClient, args: argparse.Namespace) -> dict[str, Any]:
    return {
        "bot_id": (await c.bot(args.bot))["id"] if args.bot else None,
        "user": args.user,
        "status": args.status,
        "chat_type": args.chat_type,
        "keyword": args.keyword,
        "token_id": args.token_id,
        "since": args.since,
        "until": args.until,
    }


async def chats_list(c: AdminClient, args: argparse.Namespace) -> int:
    rows, total = await take(c, CHATS, args.limit, **await chat_params(c, args))
    if args.json:
        output.dump(rows)
        return 0
    output.table(
        rows,
        [
            ("id", "id"),
            ("时间", "request_at"),
            ("AI 员工", lambda r: r["bot_name"] or r["bot_key"]),
            ("发言人", lambda r: r["user_name"] or r["user_login"]),
            ("类型", "chat_type"),
            ("状态", "status"),
            ("耗时 ms", "latency_ms"),
            ("消息", lambda r: (r["message_preview"] or "")[:40]),
        ],
        empty="（没有符合条件的对话）",
    )
    footer(len(rows), total)
    return 0


async def chats_show(c: AdminClient, args: argparse.Namespace) -> int:
    data = await c.get(f"{CHATS}/{args.id}")
    if args.json:
        output.dump(data)
        return 0
    output.fields(
        data,
        [
            ("id", "id"),
            ("AI 员工", lambda d: f"{d['bot_key']}（{d['bot_name']}）"),
            ("平台", "platform"),
            ("发言人", lambda d: f"{d['user_login']}（{d['user_name']}）"),
            ("会话类型", "chat_type"),
            ("会话", "chat_id"),
            ("模型", "model"),
            ("状态", "status"),
            ("错误码", "error_code"),
            ("错误信息", "error_message"),
            ("请求时间", "request_at"),
            ("回复时间", "response_at"),
            ("耗时 ms", "latency_ms"),
            ("输入 / 输出 Token", lambda d: f"{d['input_tokens']} / {d['output_tokens']}"),
            ("估算费用 $", "cost_usd"),
            ("工具", "tools_used"),
            ("任务", "task_id"),
        ],
    )
    for title, key in (("引用", "quoted_content"), ("消息", "message_content")):
        if data[key]:
            print(f"\n{title}：\n{data[key]}")
    print(f"\n回复：\n{data['response_content'] or '-'}")
    if data["business_tokens"]:
        print("\n这一轮签发的业务令牌：")
        output.table(
            data["business_tokens"],
            [
                ("系统", "system_key"),
                ("令牌 id", "token_id"),
                ("主体", "subject"),
                ("签发", "issued_at"),
            ],
        )
    return 0


async def chats_stats(c: AdminClient, args: argparse.Namespace) -> int:
    data = await c.get(f"{CHATS}/stats", **await chat_params(c, args))
    if args.json:
        output.dump(data)
        return 0
    tokens = data["tokens"]
    output.fields(
        data,
        [
            ("对话总数", "total"),
            ("按状态", "by_status"),
            ("平均耗时 ms", "avg_latency_ms"),
            ("输入 Token", lambda _: tokens["input"]),
            ("输出 Token", lambda _: tokens["output"]),
            ("缓存读取", lambda _: tokens["cache_read"]),
            ("缓存写入", lambda _: tokens["cache_creation"]),
        ],
    )
    print()
    output.table(
        data["by_bot"],
        [
            ("AI 员工", lambda r: r["bot_name"] or r["bot_key"]),
            ("总数", "total"),
            ("成功", "success"),
            ("失败", "error"),
            ("平均耗时 ms", "avg_latency_ms"),
        ],
    )
    return 0


# ---- 任务跟进 ----


def register_tasks(sub: Any) -> None:
    ops = group(sub, "tasks", "任务跟进：调度员分派的任务（只读）")
    p = leaf(ops, "list", "列出任务，最新的在前", tasks_list)
    p.add_argument("--status", choices=WORK_TASK_STATUSES)
    p.add_argument("--bot", metavar="AI员工", help="承接任务的 AI 员工")
    p.add_argument("--chat-id", help="群聊 id")
    p.add_argument("--keyword", help="在编号、标题与摘要里搜索")
    p.add_argument("--limit", type=int, default=50, help="最多显示几条（1-200）")
    p.add_argument("--before-id", type=int, metavar="id", help="只看 id 小于它的（翻页用）")
    leaf(ops, "show", "查看任务详情与事件", tasks_show).add_argument("code", help="任务编号")


def bot_name(value: dict[str, Any] | None) -> str | None:
    return value["name"] if value else None


async def tasks_list(c: AdminClient, args: argparse.Namespace) -> int:
    data = await c.get(
        TASKS,
        status=args.status,
        assignee_bot_id=(await c.bot(args.bot))["id"] if args.bot else None,
        chat_id=args.chat_id,
        q=args.keyword,
        limit=args.limit,
        before_id=args.before_id,
    )
    if args.json:
        output.dump(data)
        return 0
    output.table(
        data["items"],
        [
            ("编号", "code"),
            ("状态", "status"),
            ("标题", "title"),
            ("承接", lambda r: bot_name(r["assignee"])),
            ("提出人", "requester_name"),
            ("更新时间", "updated_at"),
        ],
        empty="（没有任务）",
    )
    if data["counts"]:
        counts = "，".join(f"{k} {v}" for k, v in sorted(data["counts"].items()))
        print(f"\n全部任务：{counts}")
    return 0


async def tasks_show(c: AdminClient, args: argparse.Namespace) -> int:
    data = await c.get(f"{TASKS}/{args.code}")
    if args.json:
        output.dump(data)
        return 0
    output.fields(
        data,
        [
            ("编号", "code"),
            ("标题", "title"),
            ("状态", "status"),
            ("承接", lambda d: bot_name(d["assignee"])),
            ("调度员", lambda d: bot_name(d["dispatcher"])),
            ("提出人", "requester_name"),
            ("平台", "platform"),
            ("群聊", "chat_id"),
            ("相关任务", "related_codes"),
            ("创建时间", "created_at"),
            ("关闭时间", "closed_at"),
            ("摘要", "summary"),
        ],
    )
    print("\n事件：")
    output.table(
        data["events"],
        [
            ("时间", "created_at"),
            ("类型", "kind"),
            ("由谁", lambda e: e["actor_name"] or e["actor_type"]),
            ("内容", "human_text"),
        ],
    )
    agent = data["agent"]
    for title, key in (("交接说明（brief）", "brief"), ("记要（digest）", "digest")):
        if agent[key]:
            print(f"\n{title}：\n{agent[key]}")
    return 0
