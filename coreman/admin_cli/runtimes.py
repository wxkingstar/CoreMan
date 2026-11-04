"""运行时：节点与安装链接、节点上的 AI 实例（席位、探测、用量、会话）、任务总线运行状态。

管理后台的「运行时实例」（relay-servers）就是节点上按 AI 各一行的实例，由节点注册时自动创建，
不单独增删改；这里都用「节点 + AI」指代它，归在 runtimes 组下。
"""

from __future__ import annotations

import argparse
from typing import Any

from coreman.admin_cli import output
from coreman.admin_cli.base import add_yes, changed, group, leaf, on_off, read_text, require_yes
from coreman.admin_cli.client import AdminClient, CliError, one

NODES = "/api/admin/runtime-nodes"
RELAYS = "/api/admin/relay-servers"
BUS = "/api/admin/runtime"
PROVIDERS = ("claude", "codex", "opencode")
VISIBILITY = ("all", "admins", "private")
VISIBILITY_TEXT = {"all": "所有人", "admins": "仅管理员", "private": "仅所有者"}
NODE_OPTIONS = {
    "name": "name",
    "workspace_root": "workspace_root",
    "visibility": "visibility",
    "active": "is_active",
    "draining": "draining",
}


def register(sub: Any) -> None:
    register_runtimes(sub)
    register_queue(sub)


def node_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("node", metavar="节点", help="节点名称、主机名或 id（可只写开头几位）")


def backend_args(p: argparse.ArgumentParser) -> None:
    node_arg(p)
    p.add_argument("provider", metavar="AI", choices=PROVIDERS, help="claude、codex 或 opencode")


def seat_args(p: argparse.ArgumentParser) -> None:
    backend_args(p)
    p.add_argument("seat", metavar="席位", help="席位标识或名称（默认席位写 default）")


def register_runtimes(sub: Any) -> None:
    ops = group(sub, "runtimes", "运行时节点、AI 实例与席位")
    leaf(ops, "list", "列出运行时节点", list_nodes)
    node_arg(leaf(ops, "show", "查看节点详情、AI 实例与席位", show))
    p = leaf(ops, "set", "修改节点（只改给出的字段）", set_node)
    node_arg(p)
    p.add_argument("--name", help="显示名称")
    p.add_argument(
        "--workspace-root", help="项目主目录（节点须在线且没有 AI 员工在用，由节点确认后生效）"
    )
    p.add_argument("--team", help="分配给团队（slug 或名称；传空串改回公共池，仅管理员）")
    p.add_argument(
        "--visibility",
        choices=VISIBILITY,
        help="可见范围：all 所有人、admins 仅管理员（仅管理员可设）、private 仅所有者",
    )
    p.add_argument(
        "--active", type=on_off, metavar="on|off", help="启用或停用（停用会取消节点上进行中的调用）"
    )
    p.add_argument(
        "--draining", type=on_off, metavar="on|off", help="排空：不再接新对话，进行中的跑完"
    )
    p = leaf(ops, "delete", "删除节点及其 AI 实例（仍有 AI 员工在用时拒绝）", delete_node)
    node_arg(p)
    add_yes(p)

    leaf(ops, "links", "列出安装链接", list_links)
    p = leaf(ops, "link-create", "生成安装链接（24 小时有效，只能用一次）", create_link)
    p.add_argument("--name", default="", help="节点名称，默认取主机名")
    p.add_argument("--workspace-root", default="/home/ai", help="项目主目录，默认 /home/ai")
    p.add_argument("--team", help="分配给团队（slug 或名称，仅管理员）")
    p.add_argument("--visibility", choices=VISIBILITY, default="all", help="可见范围，默认 all")
    p.add_argument("--proxy", help="AI 访问外网用的 HTTP(S) 代理")
    p.add_argument("--control-proxy", help="连回平台用的 HTTP(S) 代理")
    p.add_argument(
        "--environment", choices=("auto", "host", "chroot", "nspawn"), help="运行环境，默认 auto"
    )
    p.add_argument("--claude-path", help="claude 命令的绝对路径")
    p.add_argument("--codex-path", help="codex 命令的绝对路径")
    p.add_argument(
        "--git-host", action="append", metavar="主机", help="Git 白名单，可重复；默认 github.com"
    )
    p.add_argument("--max-concurrent", type=int, help="最大并发，默认 10")
    p.add_argument("--ca-file", metavar="文件", help="平台私有 CA 证书（PEM）")
    p = leaf(ops, "link-revoke", "作废安装链接", revoke_link)
    p.add_argument("link", metavar="链接", help="安装链接 id（可只写开头几位）")

    p = leaf(ops, "seats", "列出节点上各 AI 的席位", list_seats)
    node_arg(p)
    p.add_argument("provider", metavar="AI", nargs="?", choices=PROVIDERS, help="只看这种 AI")
    p = leaf(ops, "seat-add", "在节点上添加席位（之后用 seat-login 登录）", seat_add)
    backend_args(p)
    p.add_argument("--label", required=True, help="席位名称，如账号主人")
    seat_args(leaf(ops, "seat-login", "让节点发起登录，打印授权链接与登录会话", seat_login))
    p = leaf(ops, "seat-login-complete", "完成席位登录", seat_login_complete)
    seat_args(p)
    p.add_argument("--session", required=True, metavar="登录会话", help="seat-login 给出的登录会话")
    p.add_argument("--code", help="浏览器页面给出的认证码（Claude）或 API Key（OpenCode）")
    p.add_argument("--code-file", metavar="文件", help="从文件读认证码或 API Key；- 从标准输入读")
    p = leaf(ops, "seat-activate", "整机切换到这个席位（新对话都用它）", seat_activate)
    seat_args(p)
    add_yes(p)
    p = leaf(ops, "seat-remove", "移除席位（节点上的登录一并删除）", seat_remove)
    seat_args(p)
    add_yes(p)

    p = leaf(ops, "backends", "列出 AI 实例（每个节点按 AI 各一个）", list_backends)
    p.add_argument("--provider", choices=PROVIDERS, help="只看这种 AI")
    backend_args(leaf(ops, "models", "查看 AI 实例可用的模型", models))
    backend_args(leaf(ops, "probe", "健康探测并预填模型目录", probe))
    p = leaf(ops, "agent-probe", "让节点上的驱动执行一次探测", agent_probe)
    backend_args(p)
    p.add_argument(
        "operation",
        metavar="探测",
        choices=("rate-limits", "health-check"),
        help="rate-limits 采集额度，health-check 健康检查",
    )
    backend_args(leaf(ops, "usage", "AI 实例今天（UTC）的用量", usage))
    backend_args(leaf(ops, "status", "AI 实例的实时状态与进行中的任务", live_status))
    p = leaf(ops, "session", "跟看一次会话的事件流（Ctrl-C 结束）", session_events)
    backend_args(p)
    p.add_argument("session_id", metavar="会话", help="会话 id（对话记录里的 relay_session_id）")


def register_queue(sub: Any) -> None:
    ops = group(sub, "queue", "任务总线运行状态：队列、任务、进程实例、租约、出站箱")
    leaf(ops, "overview", "队列概况", overview)
    p = leaf(ops, "tasks", "列出进行中的任务", list_tasks)
    p.add_argument("--queued", action="store_true", help="改看排队中的（按抢单顺序）")
    p.add_argument("--limit", type=int, default=200, help="最多列多少条，默认 200")
    p = leaf(ops, "cancel-task", "请求取消任务", cancel_task)
    p.add_argument("task_id", type=int, metavar="任务", help="任务 id")
    p.add_argument("--reason", help="取消原因，默认 admin")
    add_yes(p)
    leaf(ops, "instances", "列出网关与工作进程实例", list_instances)
    leaf(ops, "leases", "列出 AI 员工的网关租约", list_leases)
    p = leaf(ops, "outbox", "列出失败的出站消息", list_outbox)
    p.add_argument("--pending", action="store_true", help="改看待发送的")
    p.add_argument("--limit", type=int, default=200, help="最多列多少条，默认 200")
    p = leaf(ops, "outbox-retry", "重投一条失败的出站消息", outbox_retry)
    p.add_argument("item_id", type=int, metavar="出站项", help="出站项 id")
    p = leaf(ops, "drain", "排空进程实例，或让某个 AI 员工换一个网关", drain)
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--instance", metavar="实例", help="进程实例 id（可只写开头几位）")
    target.add_argument("--bot", metavar="AI员工", help="标识、名称或 id")
    add_yes(p)


# ---- 节点 ----


async def find_node(c: AdminClient, ref: str) -> dict[str, Any]:
    rows = await c.get(NODES)
    found = (
        [n for n in rows if n["id"] == ref]
        or [n for n in rows if n["name"] == ref]
        or [n for n in rows if n["hostname"] == ref]
        or [n for n in rows if n["id"].startswith(ref)]
    )
    return one(found, ref, "运行时节点", lambda n: f"{n['name']}（{n['id'][:8]}）")


def find_backend(node: dict[str, Any], provider: str) -> dict[str, Any]:
    backend: dict[str, Any]
    for backend in node["backends"]:
        if backend["model_provider"] == provider:
            return backend
    raise CliError(f"运行时 {node['name']} 上没有 {provider} 实例")


async def find_relay(c: AdminClient, args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    node = await find_node(c, args.node)
    return node, find_backend(node, args.provider)["id"]


def online_text(row: dict[str, Any]) -> str:
    if not row["is_active"]:
        return "停用"
    state = "在线" if row["online"] else "离线"
    return f"{state}·排空中" if row["draining"] else state


def backends_text(row: dict[str, Any]) -> str:
    return "、".join(f"{b['model_provider']}:{b['health_status']}" for b in row["backends"])


def quota_text(row: dict[str, Any]) -> str:
    five, seven = row.get("rate_limit_5h_used_pct"), row.get("rate_limit_7d_used_pct")
    if five is None and seven is None:
        return "-"
    return f"5h {output.text(five)}% / 7d {output.text(seven)}%"


async def list_nodes(c: AdminClient, args: argparse.Namespace) -> int:
    rows = await c.get(NODES)
    if args.json:
        output.dump(rows)
        return 0
    output.table(
        rows,
        [
            ("名称", "name"),
            ("id", lambda r: r["id"][:8]),
            ("状态", online_text),
            ("主机", lambda r: f"{r['username']}@{r['hostname']}"),
            ("版本", "version"),
            ("可见范围", lambda r: VISIBILITY_TEXT.get(r["visibility"], r["visibility"])),
            ("团队", "team_name"),
            ("所有者", "owner_name"),
            ("并发", lambda r: f"{output.text(r['active_calls'])}/{r['max_concurrent']}"),
            ("AI 实例", backends_text),
        ],
        empty="（没有运行时节点）",
    )
    return 0


def seat_rows(node: dict[str, Any], provider: str) -> list[dict[str, Any]]:
    cap = (node.get("capabilities") or {}).get(provider) or {}
    return list(cap.get("seats") or [])


def print_seats(rows: list[dict[str, Any]]) -> None:
    output.table(
        rows,
        [
            ("席位", "id"),
            ("名称", "label"),
            ("账号", "account"),
            ("登录", "login"),
            ("当前", lambda s: "●" if s.get("active") else ""),
        ],
        empty="（节点没有上报席位，可能需要升级 Runtime）",
    )


async def show(c: AdminClient, args: argparse.Namespace) -> int:
    node = await find_node(c, args.node)
    if args.json:
        output.dump(node)
        return 0
    output.fields(
        node,
        [
            ("名称", "name"),
            ("id", "id"),
            ("状态", online_text),
            ("主机", lambda r: f"{r['username']}@{r['hostname']}"),
            ("IP", "ip"),
            ("平台", lambda r: f"{r['platform']}/{r['architecture']} · {r['environment']}"),
            ("版本", "version"),
            ("项目主目录", "workspace_root"),
            ("主目录修改", "root_change"),
            ("可见范围", lambda r: VISIBILITY_TEXT.get(r["visibility"], r["visibility"])),
            ("团队", "team_name"),
            ("所有者", "owner_name"),
            ("并发", lambda r: f"{output.text(r['active_calls'])}/{r['max_concurrent']}"),
            ("Git 白名单", "git_hosts"),
            ("代理", "proxy"),
            ("最近心跳", "heartbeat_at"),
        ],
    )
    for backend in node["backends"]:
        provider = backend["model_provider"]
        print(f"\n{provider} 实例：")
        output.fields(
            backend,
            [
                ("健康", "health_status"),
                ("说明", "health_detail"),
                ("不可用原因", "unavailable_reason"),
                ("额度", quota_text),
                ("模型", "effective_models"),
                ("AI 员工数", "bot_count"),
            ],
        )
        seats = seat_rows(node, provider)
        if seats:
            print_seats(seats)
    return 0


async def set_node(c: AdminClient, args: argparse.Namespace) -> int:
    node = await find_node(c, args.node)
    body = changed(args, NODE_OPTIONS)
    if args.team is not None:
        body["team_id"] = (await c.team(args.team))["id"] if args.team else None
    if not body:
        raise CliError("没有要修改的字段，见 --help")
    await c.request("PATCH", f"{NODES}/{node['id']}", body=body)
    if args.json:
        output.dump(await find_node(c, node["id"]))
    else:
        print(f"已更新运行时 {node['name']}：{'、'.join(body)}")
        if "workspace_root" in body:
            print("项目主目录要等节点确认后才生效，用 runtimes show 查看进度")
    return 0


async def delete_node(c: AdminClient, args: argparse.Namespace) -> int:
    node = await find_node(c, args.node)
    require_yes(args, f"删除运行时 {node['name']}（节点凭证随之失效，需在主机上卸载）")
    await c.request("DELETE", f"{NODES}/{node['id']}")
    print(f"已删除运行时 {node['name']}")
    return 0


# ---- 安装链接 ----


async def list_links(c: AdminClient, args: argparse.Namespace) -> int:
    rows = await c.get(f"{NODES}/install-links")
    if args.json:
        output.dump(rows)
        return 0
    output.table(
        rows,
        [
            ("链接 id", lambda r: r["id"][:8]),
            ("名称", "name"),
            ("状态", "state"),
            ("项目主目录", "workspace_root"),
            ("可见范围", lambda r: VISIBILITY_TEXT.get(r["visibility"], r["visibility"])),
            ("过期时间", "expires_at"),
            ("注册出的节点", lambda r: r["node_id"][:8] if r["node_id"] else None),
        ],
        empty="（没有安装链接）",
    )
    return 0


async def create_link(c: AdminClient, args: argparse.Namespace) -> int:
    options = changed(
        args,
        {
            "proxy": "proxy",
            "control_proxy": "control_proxy",
            "environment": "environment",
            "claude_path": "claude_path",
            "codex_path": "codex_path",
            "git_host": "git_hosts",
            "max_concurrent": "max_concurrent",
        },
    )
    if args.ca_file:
        options["ca_pem"] = read_text(args.ca_file)
    body: dict[str, Any] = {
        "name": args.name,
        "workspace_root": args.workspace_root,
        "visibility": args.visibility,
        "options": options,
    }
    if args.team:
        body["team_id"] = (await c.team(args.team))["id"]
    data = await c.request("POST", f"{NODES}/install-links", body=body)
    if args.json:
        output.dump(data)
        return 0
    print(f"已生成安装链接 {data['id'][:8]}，{data['expires_at']} 前有效，只能用一次")
    if data.get("ca_hint"):
        print(data["ca_hint"])
    print("在要注册的主机上执行：")
    print(data["command"])
    return 0


async def revoke_link(c: AdminClient, args: argparse.Namespace) -> int:
    rows = await c.get(f"{NODES}/install-links")
    found = [r for r in rows if r["id"] == args.link] or [
        r for r in rows if r["id"].startswith(args.link)
    ]
    link = one(found, args.link, "安装链接", lambda r: r["id"][:12])
    await c.request("DELETE", f"{NODES}/install-links/{link['id']}")
    print(f"已作废安装链接 {link['id'][:8]}")
    return 0


# ---- 席位 ----


def seat_base(node: dict[str, Any], provider: str) -> str:
    return f"{NODES}/{node['id']}/{provider}/seats"


def find_seat(node: dict[str, Any], provider: str, ref: str) -> str:
    rows = seat_rows(node, provider)
    found = [s for s in rows if s["id"] == ref] or [s for s in rows if s.get("label") == ref]
    if not found and ref == "default":
        return ref
    seat = one(found, ref, "席位", lambda s: f"{s['id']}（{s.get('label') or '-'}）")
    return str(seat["id"])


def report_seats(args: argparse.Namespace, data: dict[str, Any], message: str) -> None:
    if args.json:
        output.dump(data)
        return
    print(message)
    print_seats(data["seats"])


async def list_seats(c: AdminClient, args: argparse.Namespace) -> int:
    node = await find_node(c, args.node)
    providers = (
        [args.provider] if args.provider else [b["model_provider"] for b in node["backends"]]
    )
    data = {p: seat_rows(node, p) for p in providers}
    if args.json:
        output.dump(data)
        return 0
    for index, provider in enumerate(providers):
        if index:
            print()
        print(f"{provider}：")
        print_seats(data[provider])
    return 0


async def seat_add(c: AdminClient, args: argparse.Namespace) -> int:
    node = await find_node(c, args.node)
    data = await c.request("POST", seat_base(node, args.provider), body={"label": args.label})
    report_seats(
        args,
        data,
        f"已添加席位 {data['seat_id']}（{args.label}），用 runtimes seat-login 登录",
    )
    return 0


async def seat_login(c: AdminClient, args: argparse.Namespace) -> int:
    node = await find_node(c, args.node)
    seat = find_seat(node, args.provider, args.seat)
    data = await c.request("POST", f"{seat_base(node, args.provider)}/{seat}/login")
    if args.json:
        output.dump(data)
        return 0
    output.fields(
        data,
        [
            ("登录会话", "session_id"),
            ("授权链接", "authorization_url"),
            ("一次性代码", "user_code"),
            ("有效期", lambda d: f"{round((d.get('expires_in') or 0) / 60)} 分钟"),
        ],
    )
    done = (
        f"runtimes seat-login-complete {args.node} {args.provider} {seat} "
        f"--session {data['session_id']}"
    )
    if args.provider == "opencode":
        print(f"\n拿到 API Key 后执行：{done} --code-file -")
    elif data.get("needs_code"):
        print(f"\n在浏览器打开授权链接，登录后把页面给出的认证码填进来：{done} --code 认证码")
    else:
        print(f"\n在浏览器打开授权链接并输入一次性代码，授权完成后执行：{done}")
    return 0


async def seat_login_complete(c: AdminClient, args: argparse.Namespace) -> int:
    node = await find_node(c, args.node)
    seat = find_seat(node, args.provider, args.seat)
    body: dict[str, Any] = {"session_id": args.session}
    code = read_text(args.code_file).strip() if args.code_file else args.code
    if code:
        body["code"] = code
    data = await c.request(
        "POST", f"{seat_base(node, args.provider)}/{seat}/login/complete", body=body
    )
    report_seats(args, data, f"席位 {seat} 已登录")
    return 0


async def seat_activate(c: AdminClient, args: argparse.Namespace) -> int:
    node = await find_node(c, args.node)
    seat = find_seat(node, args.provider, args.seat)
    require_yes(args, f"把 {node['name']} 上全部 {args.provider} 对话切到席位 {seat}")
    data = await c.request("POST", f"{seat_base(node, args.provider)}/{seat}/activate")
    report_seats(args, data, f"已切换到席位 {seat}；进行中的对话在原席位上跑完")
    return 0


async def seat_remove(c: AdminClient, args: argparse.Namespace) -> int:
    node = await find_node(c, args.node)
    seat = find_seat(node, args.provider, args.seat)
    require_yes(args, f"移除 {node['name']} 上的 {args.provider} 席位 {seat}")
    data = await c.request("DELETE", f"{seat_base(node, args.provider)}/{seat}")
    report_seats(args, data, f"已移除席位 {seat}")
    return 0


# ---- AI 实例 ----


async def list_backends(c: AdminClient, args: argparse.Namespace) -> int:
    rows = await c.collect(RELAYS, model_provider=args.provider)
    if args.json:
        output.dump(rows)
        return 0
    output.table(
        rows,
        [
            ("节点", "runtime_name"),
            ("AI", "model_provider"),
            ("在线", "runtime_online"),
            ("启用", "is_active"),
            ("健康", "health_status"),
            ("额度", quota_text),
            ("团队", "team_name"),
            ("AI 员工数", "bot_count"),
            ("不可用原因", "unavailable_reason"),
        ],
        empty="（没有 AI 实例）",
    )
    return 0


async def models(c: AdminClient, args: argparse.Namespace) -> int:
    _, relay_id = await find_relay(c, args)
    data = await c.get(f"{RELAYS}/{relay_id}/models")
    if args.json:
        output.dump(data)
        return 0
    print(f"默认模型：{output.text(data['default'])}（{data['mode']}）")
    for model in data["models"]:
        print(f"  - {model}")
    return 0


async def probe(c: AdminClient, args: argparse.Namespace) -> int:
    _, relay_id = await find_relay(c, args)
    data = await c.request("POST", f"{RELAYS}/{relay_id}/probe")
    if args.json:
        output.dump(data)
        return 0
    output.fields(
        data["relay"],
        [("健康", "health_status"), ("说明", "health_detail"), ("延迟 ms", "health_latency_ms")],
    )
    if data["added_models"]:
        print(f"新加入目录的模型：{output.text(data['added_models'])}")
    return 0 if data["relay"]["health_status"] == "healthy" else 1


async def agent_probe(c: AdminClient, args: argparse.Namespace) -> int:
    _, relay_id = await find_relay(c, args)
    operation = "probe-rate-limits" if args.operation == "rate-limits" else "health-check"
    data = await c.request(
        "POST", f"{RELAYS}/{relay_id}/agent-probe", body={"operation": operation}
    )
    if args.json:
        output.dump(data)
    else:
        print(f"已让节点在后台执行 {args.operation}，结果随下一次心跳更新，用 runtimes show 查看")
    return 0


async def usage(c: AdminClient, args: argparse.Namespace) -> int:
    _, relay_id = await find_relay(c, args)
    data = await c.get(f"{RELAYS}/{relay_id}/today-usage")
    if args.json:
        output.dump(data)
        return 0
    output.fields(
        data,
        [
            ("起始（UTC）", "since"),
            ("对话数", "total"),
            ("输入 token", "input_tokens"),
            ("输出 token", "output_tokens"),
            ("缓存读 token", "cache_read_tokens"),
            ("缓存写 token", "cache_creation_tokens"),
            ("费用 USD", "cost_usd"),
            ("上报 token 的对话", "token_reported"),
            ("上报费用的对话", "cost_reported"),
        ],
    )
    return 0


async def live_status(c: AdminClient, args: argparse.Namespace) -> int:
    _, relay_id = await find_relay(c, args)
    data = await c.get(f"{RELAYS}/{relay_id}/live-status")
    if args.json:
        output.dump(data)
        return 0
    tasks = data.get("active_tasks") or []
    print(f"主机 {output.text(data.get('hostname'))}，用户 {output.text(data.get('user'))}")
    output.table(
        tasks,
        [
            ("AI 员工", lambda t: t.get("bot_name") or t.get("bot_key")),
            ("发言人", "user"),
            ("会话", "chat_id"),
            ("请求", "request_id"),
            ("进程", "pid"),
        ],
        empty="（没有进行中的任务）",
    )
    return 0


async def session_events(c: AdminClient, args: argparse.Namespace) -> int:
    node, _ = await find_relay(c, args)
    path = f"{NODES}/{node['id']}/{args.provider}/session/{args.session_id}/events"
    async with c.http.stream("GET", path, headers={"Accept": "text/event-stream"}) as resp:
        if resp.status_code >= 400:
            await resp.aread()
            payload = resp.json() if resp.content else {}
            raise CliError(payload.get("message") or f"HTTP {resp.status_code}")
        event = ""
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                if event == "unavailable":
                    raise CliError("运行时上已经没有这次会话的过程（节点离线或会话已清理）")
                print(line[5:].strip(), flush=True)
            elif not line:
                event = ""
    return 0


# ---- 任务总线 ----


async def overview(c: AdminClient, args: argparse.Namespace) -> int:
    data = await c.get(f"{BUS}/queue")
    if args.json:
        output.dump(data)
        return 0
    output.fields(
        data,
        [
            ("排队（普通/快速）", lambda d: f"{d['queued']['normal']} / {d['queued']['fast']}"),
            ("已领取", "claimed"),
            ("执行中", "running"),
            ("出站待发", "outbox_pending"),
            ("出站失败", "outbox_failed"),
            ("流式回复中", "streams_active"),
        ],
    )
    return 0


def bot_text(row: dict[str, Any]) -> str | None:
    return f"{row['bot_key']}（{row['bot_name']}）" if row.get("bot_key") else None


async def list_tasks(c: AdminClient, args: argparse.Namespace) -> int:
    status = "queued" if args.queued else "active"
    rows = await c.get(f"{BUS}/tasks", status=status, limit=args.limit)
    if args.json:
        output.dump(rows)
        return 0
    output.table(
        rows,
        [
            ("任务", "id"),
            ("AI 员工", bot_text),
            ("类型", "kind"),
            ("通道", "lane"),
            ("状态", "status"),
            ("执行者", "claimed_by"),
            ("开始", "started_at"),
            ("心跳", "heartbeat_at"),
            ("取消", "cancel_reason"),
            ("次数", "attempts"),
        ],
        empty="（没有排队中的任务）" if args.queued else "（没有进行中的任务）",
    )
    return 0


async def cancel_task(c: AdminClient, args: argparse.Namespace) -> int:
    require_yes(args, f"取消任务 {args.task_id}")
    body = {"reason": args.reason} if args.reason else None
    data = await c.request("POST", f"{BUS}/tasks/{args.task_id}/cancel", body=body)
    if args.json:
        output.dump(data)
    else:
        print(f"已请求取消任务 {args.task_id}，当前状态 {data['status']}")
    return 0


async def list_instances(c: AdminClient, args: argparse.Namespace) -> int:
    rows = await c.get(f"{BUS}/instances")
    if args.json:
        output.dump(rows)
        return 0
    output.table(
        rows,
        [
            ("实例", "id"),
            ("服务", "service"),
            ("版本", "version"),
            ("存活", "alive"),
            ("运行/容量", lambda r: f"{output.text(r['running'])}/{output.text(r['capacity'])}"),
            ("心跳", "heartbeat_at"),
            ("排空", "drain_requested_at"),
            ("已停止", "stopped_at"),
        ],
        empty="（没有进程实例）",
    )
    return 0


async def list_leases(c: AdminClient, args: argparse.Namespace) -> int:
    rows = await c.get(f"{BUS}/leases")
    if args.json:
        output.dump(rows)
        return 0
    output.table(
        rows,
        [
            ("AI 员工", bot_text),
            ("平台", "platform"),
            ("持有网关", "holder_instance"),
            ("代数", "generation"),
            ("连接", "connection_state"),
            ("心跳", "heartbeat_at"),
            ("排空", "drain_requested_by"),
            ("释放", "released_at"),
        ],
        empty="（没有租约）",
    )
    return 0


async def list_outbox(c: AdminClient, args: argparse.Namespace) -> int:
    status = "pending" if args.pending else "failed"
    rows = await c.get(f"{BUS}/outbox", status=status, limit=args.limit)
    if args.json:
        output.dump(rows)
        return 0
    output.table(
        rows,
        [
            ("出站项", "id"),
            ("AI 员工", bot_text),
            ("类型", "kind"),
            ("状态", "status"),
            ("次数", "attempts"),
            ("最早发送", "not_before"),
            ("错误", "last_error"),
            ("创建", "created_at"),
        ],
        empty="（没有待发送的出站消息）" if args.pending else "（没有失败的出站消息）",
    )
    return 0


async def outbox_retry(c: AdminClient, args: argparse.Namespace) -> int:
    data = await c.request("POST", f"{BUS}/outbox/{args.item_id}/retry")
    if args.json:
        output.dump(data)
    else:
        print(f"已重投出站项 {args.item_id}，当前状态 {data['status']}")
    return 0


async def drain(c: AdminClient, args: argparse.Namespace) -> int:
    if args.instance:
        rows = await c.get(f"{BUS}/instances")
        found = [r for r in rows if r["id"] == args.instance] or [
            r for r in rows if r["id"].startswith(args.instance) and not r["stopped_at"]
        ]
        instance = one(found, args.instance, "进程实例", lambda r: r["id"])
        what = f"进程实例 {instance['id']}（不再接新活）"
        body: dict[str, Any] = {"instance_id": instance["id"]}
    else:
        bot = await c.bot(args.bot)
        what = f"AI 员工 {bot['bot_key']} 的网关（断开后由其它网关接手）"
        body = {"bot_key": bot["bot_key"]}
    require_yes(args, f"排空{what}")
    data = await c.request("POST", f"{BUS}/drain", body=body)
    if args.json:
        output.dump(data)
    else:
        print(f"已请求排空{what}")
    return 0
