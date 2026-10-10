"""业务系统：登记、开放范围、操作目录与访问测试。

给某个 AI 员工授权用 `bots system-grant`；这里管系统本身和「开放给哪些员工」。
"""

from __future__ import annotations

import argparse
from typing import Any

from coreman.admin_cli import output
from coreman.admin_cli.base import add_yes, changed, group, leaf, on_off, require_yes
from coreman.admin_cli.client import AdminClient, CliError

SYSTEMS = "/api/admin/systems"
# PUT /systems/{key} 是整份替换：取回现值、改动给出的字段、原样回传其余字段。
SYSTEM_FIELDS = (
    "name",
    "description",
    "base_url",
    "openapi_url",
    "token_provider",
    "token_delivery",
    "token_audience",
    "access_test_url",
    "enabled",
    "sort_order",
    "default_for_all_bots",
    "allowed_bot_ids",
)
OPTIONS = {
    "name": "name",
    "description": "description",
    "base_url": "base_url",
    "openapi_url": "openapi_url",
    "provider": "token_provider",
    "delivery": "token_delivery",
    "audience": "token_audience",
    "test_url": "access_test_url",
    "sort": "sort_order",
    "default_for_all": "default_for_all_bots",
}


def system_options(p: argparse.ArgumentParser, *, create: bool) -> None:
    p.add_argument("--name", required=create, help="显示名称")
    p.add_argument("--description")
    p.add_argument("--base-url", help="系统地址")
    p.add_argument("--openapi-url", help="OpenAPI 描述地址（须与系统地址同源）")
    p.add_argument("--provider", help="令牌签发方，默认 builtin")
    p.add_argument(
        "--delivery", choices=("env", "proxy"), help="env 令牌进运行环境；proxy 只经平台代理"
    )
    p.add_argument("--audience", help="令牌受众，默认为系统标识")
    p.add_argument("--test-url", help="访问测试地址（须与系统地址同源）")
    p.add_argument("--sort", type=int, help="排序")
    p.add_argument(
        "--default-for-all", type=on_off, metavar="on|off", help="新员工默认获得此系统授权"
    )


def register(sub: Any) -> None:
    ops = group(sub, "systems", "业务系统")
    leaf(ops, "list", "列出业务系统", list_systems)
    leaf(ops, "show", "查看业务系统与操作目录", show).add_argument("key", help="系统标识")
    p = leaf(ops, "create", "登记业务系统（默认不开放给任何员工）", create)
    p.add_argument("key", help="系统标识：小写字母开头，字母数字下划线")
    system_options(p, create=True)
    p.add_argument("--disabled", action="store_true", help="登记为停用")
    p = leaf(ops, "set", "修改业务系统（只改给出的字段）", set_system)
    p.add_argument("key")
    system_options(p, create=False)
    leaf(ops, "enable", "启用业务系统", enable).add_argument("key")
    leaf(ops, "disable", "停用业务系统", disable).add_argument("key")
    for name, handler, text in (
        ("allow", allow, "把系统开放给这些 AI 员工"),
        ("disallow", disallow, "不再开放给这些 AI 员工（同时收回其授权）"),
    ):
        p = leaf(ops, name, text, handler)
        p.add_argument("key")
        p.add_argument("bots", nargs="+", metavar="AI员工", help="标识、名称或 id")
    p = leaf(ops, "allow-all", "开放给全部 AI 员工", allow_all)
    p.add_argument("key")
    add_yes(p)
    leaf(ops, "refresh", "以自己的身份重新拉取操作目录", refresh).add_argument("key")
    leaf(ops, "test", "以自己的身份签一枚测试令牌并访问测试地址", test).add_argument("key")
    p = leaf(ops, "delete", "删除业务系统", delete)
    p.add_argument("key")
    add_yes(p)


def scope_text(row: dict[str, Any]) -> str:
    ids = row["allowed_bot_ids"]
    return "全部员工" if ids is None else f"{len(ids)} 个员工"


async def list_systems(c: AdminClient, args: argparse.Namespace) -> int:
    rows = await c.collect(SYSTEMS)
    if args.json:
        output.dump(rows)
    else:
        output.table(
            rows,
            [
                ("标识", "key"),
                ("名称", "name"),
                ("状态", output.enabled),
                ("令牌下发", "token_delivery"),
                ("签发方", "token_provider"),
                ("开放范围", scope_text),
                ("地址", "base_url"),
            ],
        )
    return 0


async def bot_labels(c: AdminClient, ids: list[str]) -> list[str]:
    rows = {b["id"]: b for b in await c.collect("/api/admin/bots", scope="all")}
    return [f"{rows[i]['bot_key']}（{rows[i]['name']}）" if i in rows else i for i in ids]


async def show(c: AdminClient, args: argparse.Namespace) -> int:
    row = await c.system(args.key)
    catalog = await c.get(f"{SYSTEMS}/{args.key}/catalog")
    if args.json:
        output.dump({**row, "catalog": catalog})
        return 0
    ids = row["allowed_bot_ids"]
    output.fields(
        row,
        [
            ("标识", "key"),
            ("名称", "name"),
            ("状态", output.enabled),
            ("系统地址", "base_url"),
            ("OpenAPI 地址", "openapi_url"),
            ("令牌下发", "token_delivery"),
            ("签发方", "token_provider"),
            ("令牌受众", "token_audience"),
            ("测试地址", "access_test_url"),
            ("新员工默认授权", "default_for_all_bots"),
            ("开放给", lambda _: "全部员工" if ids is None else None),
            ("描述", "description"),
        ],
    )
    if ids:
        for label in await bot_labels(c, ids):
            print(f"  - {label}")
    if catalog:
        print("\n操作目录：")
        output.fields(
            catalog,
            [
                ("状态", "status"),
                ("错误", "error"),
                ("模块数", "module_count"),
                ("操作数", "operation_count"),
                ("隐藏操作", "hidden_count"),
                ("契约错误/警告", lambda d: f"{d['lint_errors']} / {d['lint_warnings']}"),
                ("拉取时间", "fetched_at"),
                ("检查时间", "checked_at"),
            ],
        )
    return 0


def report_catalog(data: dict[str, Any]) -> None:
    catalog = data.get("catalog")
    if catalog:
        print(f"操作目录：{catalog.get('status')} {catalog.get('error') or ''}".rstrip())


async def create(c: AdminClient, args: argparse.Namespace) -> int:
    body = {"key": args.key, **changed(args, OPTIONS), "enabled": not args.disabled}
    data = await c.request("POST", SYSTEMS, body=body)
    if args.json:
        output.dump(data)
    else:
        print(f"已登记业务系统 {args.key}，目前不开放给任何员工；用 systems allow 开放")
        report_catalog(data)
    return 0


async def put(c: AdminClient, row: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    body = {key: row[key] for key in SYSTEM_FIELDS} | updates
    return await c.request(  # type: ignore[no-any-return]
        "PUT", f"{SYSTEMS}/{row['key']}", body=body, version=row["version"]
    )


async def set_system(c: AdminClient, args: argparse.Namespace) -> int:
    updates = changed(args, OPTIONS)
    if not updates:
        raise CliError("没有要修改的字段，见 --help")
    data = await put(c, await c.system(args.key), updates)
    if args.json:
        output.dump(data)
    else:
        print(f"已更新业务系统 {args.key}：{'、'.join(updates)}")
        report_catalog(data)
    return 0


async def set_enabled(c: AdminClient, args: argparse.Namespace, want: bool) -> int:
    row = await c.system(args.key)
    word = "启用" if want else "停用"
    if row["enabled"] == want:
        print(f"{args.key} 已经是{word}状态")
        return 0
    data = await put(c, row, {"enabled": want})
    if args.json:
        output.dump(data)
    else:
        print(f"已{word}业务系统 {args.key}")
    return 0


async def enable(c: AdminClient, args: argparse.Namespace) -> int:
    return await set_enabled(c, args, True)


async def disable(c: AdminClient, args: argparse.Namespace) -> int:
    return await set_enabled(c, args, False)


async def save_scope(c: AdminClient, args: argparse.Namespace, ids: list[str] | None) -> None:
    data = await put(c, await c.system(args.key), {"allowed_bot_ids": ids})
    if args.json:
        output.dump(data)
    else:
        print(f"{args.key} 现在开放给：{scope_text(data)}")


async def allow(c: AdminClient, args: argparse.Namespace) -> int:
    row = await c.system(args.key)
    if row["allowed_bot_ids"] is None:
        raise CliError(f"{args.key} 已开放给全部员工")
    added = [(await c.bot(ref))["id"] for ref in args.bots]
    await save_scope(c, args, list(dict.fromkeys([*row["allowed_bot_ids"], *added])))
    return 0


async def disallow(c: AdminClient, args: argparse.Namespace) -> int:
    row = await c.system(args.key)
    if row["allowed_bot_ids"] is None:
        raise CliError(f"{args.key} 开放给全部员工；先用 allow 列出要开放的员工改成白名单")
    removed = {(await c.bot(ref))["id"] for ref in args.bots}
    await save_scope(c, args, [i for i in row["allowed_bot_ids"] if i not in removed])
    return 0


async def allow_all(c: AdminClient, args: argparse.Namespace) -> int:
    require_yes(args, f"把 {args.key} 开放给全部 AI 员工")
    await save_scope(c, args, None)
    return 0


async def refresh(c: AdminClient, args: argparse.Namespace) -> int:
    await c.system(args.key)
    data = await c.request("POST", f"{SYSTEMS}/{args.key}/catalog/refresh")
    if args.json or not data:
        output.dump(data)
    else:
        print(f"{args.key} 操作目录：{data.get('status')}")
        output.fields(data, [(k, k) for k in data if k not in ("status", "lint")])
    return 0


async def test(c: AdminClient, args: argparse.Namespace) -> int:
    data = await c.request("POST", f"{SYSTEMS}/test-access", body={"system_key": args.key})
    if args.json:
        output.dump(data)
        return 0
    output.fields(
        data,
        [
            ("结果", lambda d: "通过" if d["success"] else "未通过"),
            ("说明", "message"),
            ("地址", "url"),
            ("无令牌状态码", "baseline_status_code"),
            ("带令牌状态码", "status_code"),
            ("跳转", "redirect"),
            ("令牌主体", "subject"),
        ],
    )
    return 0 if data["success"] else 1


async def delete(c: AdminClient, args: argparse.Namespace) -> int:
    row = await c.system(args.key)
    require_yes(args, f"删除业务系统 {args.key}（连同所有员工的授权）")
    await c.request("DELETE", f"{SYSTEMS}/{args.key}", version=row["version"])
    print(f"已删除业务系统 {args.key}")
    return 0
