"""业务系统：登记、开放范围、操作目录与访问测试，以及经平台代理执行的批量写操作。

给某个 AI 员工授权用 `bots system-grant`；这里管系统本身和「开放给哪些员工」。
"""

from __future__ import annotations

import argparse
from typing import Any

from coreman.admin_cli import output
from coreman.admin_cli.base import add_yes, changed, group, leaf, on_off, require_yes
from coreman.admin_cli.client import AdminClient, CliError, one

SYSTEMS = "/api/admin/systems"
DELIVERY = "/api/admin/system-delivery"
BATCHES = "/api/admin/system-batches"
BATCH_PHASES = {
    "awaiting": "等待确认",
    "running": "执行中",
    "paused": "已暂停",
    "finished": "已结束",
    "not_started": "未开始",
    "cancelled": "已取消",
    "expired": "已过期",
}
PAUSE_REASONS = {"pause_trial": "试跑后等待确认", "pause_failures": "连续失败"}
BATCH_RESULTS = (("成功", "ok"), ("失败", "failed"), ("结果未知", "unknown"), ("未执行", "skipped"))
# PUT /systems/{key} 是整份替换：取回现值、改动给出的字段、原样回传其余字段。
SYSTEM_FIELDS = (
    "name",
    "description",
    "base_url",
    "openapi_url",
    "token_provider",
    "token_delivery",
    "mcp_url",
    "companion_skill_id",
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
    "mcp_url": "mcp_url",
    "companion_skill": "companion_skill_id",
    "audience": "token_audience",
    "test_url": "access_test_url",
    "sort": "sort_order",
    "default_for_all": "default_for_all_bots",
    "batch_write_max": "batch_write_max",
    "batch_destructive_max": "batch_destructive_max",
    "proxy_calls": "proxy_calls_per_turn",
    "batch_interval_ms": "batch_interval_ms",
}


def system_options(p: argparse.ArgumentParser, *, create: bool) -> None:
    p.add_argument("--name", required=create, help="显示名称")
    p.add_argument("--description")
    p.add_argument("--base-url", help="系统地址")
    p.add_argument("--openapi-url", help="OpenAPI 描述地址（须与系统地址同源）")
    p.add_argument("--provider", help="令牌签发方，默认 builtin")
    p.add_argument(
        "--delivery",
        choices=("env", "proxy", "mcp"),
        help="env 令牌进运行环境；proxy 只经平台代理；mcp 系统是一个 MCP 服务，每轮按发言者挂载",
    )
    p.add_argument("--mcp-url", help="MCP 地址（--delivery mcp 时必填，HTTPS；传空串清除）")
    p.add_argument(
        "--companion-skill", help="配套技能 id：授权给员工时自动安装，撤销时一并卸载；传空串清除"
    )
    p.add_argument("--audience", help="令牌受众，默认为系统标识")
    p.add_argument("--test-url", help="访问测试地址（须与系统地址同源）")
    p.add_argument("--sort", type=int, help="排序")
    p.add_argument(
        "--default-for-all", type=on_off, metavar="on|off", help="新员工默认获得此系统授权"
    )
    # 以下只对平台代理（--delivery proxy）生效。
    p.add_argument("--batch-write-max", type=int, help="write 每批最多项数，1～50000，默认 1000")
    p.add_argument(
        "--batch-destructive-max", type=int, help="destructive 每批最多项数，1～500，默认 50"
    )
    p.add_argument("--proxy-calls", type=int, help="单轮代理调用次数上限，1～20000，默认 3000")
    p.add_argument(
        "--batch-interval-ms", type=int, help="批量执行时每项发出前的间隔（毫秒），0～60000"
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
    p = leaf(ops, "delivery", "盘点各系统的令牌下发方式和用得到它的 AI 员工", delivery)
    p.add_argument("--mode", choices=("env", "mcp", "proxy"), help="只看这种下发方式的系统")
    leaf(ops, "refresh", "以自己的身份重新拉取操作目录", refresh).add_argument("key")
    leaf(ops, "test", "以自己的身份签一枚测试令牌并访问测试地址", test).add_argument("key")
    p = leaf(ops, "delete", "删除业务系统", delete)
    p.add_argument("key")
    add_yes(p)
    leaf(ops, "batches", "列出近期的批量写操作（结束 24 小时后清理）", batches)
    p = leaf(ops, "batch-stop", "停止一个执行中或已暂停的批量写操作", batch_stop)
    p.add_argument("id", help="批次 id，可以只写开头几位（见 systems batches）")
    add_yes(p)


def proxy_batch_text(row: dict[str, Any]) -> str | None:
    if "batch_write_max" not in row:
        return None
    return f"{row['batch_write_max']} / {row['batch_destructive_max']}"


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
            ("MCP 地址", "mcp_url"),
            ("配套技能", "companion_skill_id"),
            ("签发方", "token_provider"),
            ("令牌受众", "token_audience"),
            ("测试地址", "access_test_url"),
            ("新员工默认授权", "default_for_all_bots"),
            ("批量上限 write / destructive", proxy_batch_text),
            ("单轮代理调用上限", "proxy_calls_per_turn"),
            ("批量间隔（毫秒）", "batch_interval_ms"),
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


async def delivery(c: AdminClient, args: argparse.Namespace) -> int:
    rows = await c.get(DELIVERY, mode=args.mode)
    if args.json:
        output.dump(rows)
        return 0
    output.table(
        rows,
        [
            ("系统", lambda r: f"{r['system_key']}（{r['system_name']}）"),
            ("令牌下发", "token_delivery"),
            ("AI 员工", lambda r: f"{r['bot_key']}（{r['bot_name']}）"),
            ("来源", lambda r: "显式授权" if r["source"] == "grant" else "默认全员"),
            # 允许写入只对平台代理的系统有意义。
            (
                "允许写入",
                lambda r: r["allow_write"] if r["token_delivery"] == "proxy" else None,
            ),
            ("运行时节点", "runtime_node"),
            ("节点支持目录", "catalog_runtime"),
            ("没人点名也下发", "unprompted_system_tokens"),
        ],
        empty="（没有 AI 员工用得到的业务系统）",
    )
    return 0


def report_catalog(data: dict[str, Any]) -> None:
    catalog = data.get("catalog")
    if catalog:
        print(f"操作目录：{catalog.get('status')} {catalog.get('error') or ''}".rstrip())


def system_changes(args: argparse.Namespace) -> dict[str, Any]:
    updates = changed(args, OPTIONS)
    if updates.get("companion_skill_id") == "":
        updates["companion_skill_id"] = None
    return updates


async def create(c: AdminClient, args: argparse.Namespace) -> int:
    body = {"key": args.key, **system_changes(args), "enabled": not args.disabled}
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
    updates = system_changes(args)
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


def batch_operation(row: dict[str, Any]) -> str:
    system = row.get("system_name") or row.get("system") or "-"
    return f"{system} · {row.get('operation_summary') or row.get('operation_id') or '-'}"


def batch_status(row: dict[str, Any]) -> str:
    if row.get("stopping"):
        return "正在停止"
    label = BATCH_PHASES.get(row["phase"], row["phase"])
    reason = PAUSE_REASONS.get(row.get("pause_reason") or "")
    return f"{label}（{reason}）" if row["phase"] == "paused" and reason else label


def batch_progress(row: dict[str, Any]) -> str:
    return f"{row['count'] - row['pending']} / {row['count']}"


def batch_result(row: dict[str, Any]) -> str | None:
    """各结果的项数，为 0 的不列。"""
    return "、".join(f"{label} {row[key]}" for label, key in BATCH_RESULTS if row[key]) or None


async def batches(c: AdminClient, args: argparse.Namespace) -> int:
    rows = await c.collect(BATCHES)
    if args.json:
        output.dump(rows)
    else:
        output.table(
            rows,
            [
                ("批次 id", lambda r: r["id"][:8]),
                ("发起时间", "created_at"),
                ("AI 员工", "bot_name"),
                ("发起人", "user_name"),
                ("系统 · 操作", batch_operation),
                ("状态", batch_status),
                ("进度", batch_progress),
                ("结果", batch_result),
            ],
            empty="（没有近期的批量写操作；批次在结束 24 小时后清理）",
        )
    return 0


async def batch_stop(c: AdminClient, args: argparse.Namespace) -> int:
    rows = await c.collect(BATCHES)
    found = [r for r in rows if r["id"] == args.id] or [
        r for r in rows if r["id"].startswith(args.id)
    ]
    row = one(found, args.id, "批量写操作", lambda r: r["id"][:8])
    short = row["id"][:8]
    require_yes(
        args,
        f"停止批量写操作 {short}（{batch_operation(row)}，还有 {row['pending']} 项未执行）",
    )
    data = await c.request("POST", f"{BATCHES}/{row['id']}/stop")
    if args.json:
        output.dump(data)
    elif data["stopping"]:
        # 执行中的批次要等手上这一项做完才收尾，此刻还没有最终结果。
        print(f"已请求停止批量写操作 {short}：正在执行的一项结束后收尾，其余不再执行")
    else:
        print(f"已停止批量写操作 {short}：{batch_result(data) or '没有执行任何一项'}")
    return 0
