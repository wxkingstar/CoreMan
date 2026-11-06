"""AI 员工的其余管理操作：新建与扫码、记忆、工作目录、切换运行时、审批人、协作同事、
群聊旁听、飞书应用管理、运行环境检查、技能 MCP 登录。

都挂在 `bots` 命令组下，命名沿用 bots.py 的 `名词-动词` 风格。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from coreman.admin_cli import output
from coreman.admin_cli.base import (
    add_yes,
    changed,
    leaf,
    on_off,
    pairs,
    read_text,
    require_yes,
)
from coreman.admin_cli.client import AdminClient, CliError, one

BOTS = "/api/admin/bots"
RELAYS = "/api/admin/relay-servers"
FEISHU_SCAN = "/api/admin/feishu-app-registrations"
WECOM_SCAN = "/api/admin/wecom-bot-provisions"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
READ_CHUNK = 262144
AVATAR_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


def register(ops: Any) -> None:
    _register_create(ops)
    _register_memories(ops)
    _register_workspace(ops)
    _register_switch(ops)
    _register_collaboration(ops)
    _register_observe(ops)
    _register_feishu_app(ops)
    _register_misc(ops)


# ---- 通用小工具 -------------------------------------------------------------------------


def _bot_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("bot", help="标识、名称或 id")


def _data(resp: httpx.Response) -> Any:
    """不经 AdminClient.request 发的请求（上传文件、流式输出）按同一口径取 data、报错。"""
    payload = resp.json() if resp.content else None
    if resp.status_code >= 400 or (isinstance(payload, dict) and payload.get("code", 0) != 0):
        message = payload.get("message") if isinstance(payload, dict) else None
        raise CliError(message or resp.text or f"HTTP {resp.status_code}")
    return payload.get("data") if isinstance(payload, dict) else payload


def _done(args: argparse.Namespace, data: Any, message: str) -> int:
    if args.json:
        output.dump(data)
    else:
        print(message)
    return 0


async def _relay(c: AdminClient, ref: str) -> dict[str, Any]:
    """按 id、名称或节点名找运行时实例。"""
    rows = await c.collect(RELAYS)
    found = (
        [r for r in rows if r["id"] == ref]
        or [r for r in rows if r["name"] == ref]
        or [r for r in rows if r["runtime_name"] == ref]
    )
    return one(found, ref, "运行时", lambda r: f"{r['name']}（{r['id'][:8]}）")


# ---- 新建与扫码 -------------------------------------------------------------------------


def _register_create(ops: Any) -> None:
    p = leaf(ops, "create", "新建 AI 员工", create)
    p.add_argument("bot_key", metavar="标识", help="小写字母、数字、下划线或连字符")
    p.add_argument("--platform", choices=("wecom", "feishu"), required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--model", required=True, help="模型名，须是所选运行时支持的")
    p.add_argument("--working-dir", required=True, metavar="目录", help="运行时上的工作目录")
    p.add_argument("--runtime", help="运行时名称或 id；不填则暂不分配")
    p.add_argument("--team", help="团队 slug 或名称（只有经理能替别的团队建）")
    p.add_argument("--description")
    p.add_argument("--prompt-file", metavar="文件", help="系统提示词；- 为标准输入")
    p.add_argument("--effort", choices=EFFORTS)
    p.add_argument("--verbosity", type=int, choices=(1, 2, 3, 4))
    p.add_argument("--timeout", type=int, metavar="秒", help="单次任务超时（1800–43200）")
    p.add_argument("--welcome", help="欢迎语")
    p.add_argument("--rich-cards", type=on_off, metavar="on|off", help="飞书富卡片回复")
    p.add_argument("--dev-mode", type=on_off, metavar="on|off", help="开发模式")
    p.add_argument("--disabled", action="store_true", help="建好后先停用")
    p.add_argument("--env", action="append", metavar="KEY=VALUE", help="环境变量，可重复")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--cred",
        action="append",
        metavar="KEY=VALUE",
        help="手填平台凭证，可重复：企微 bot_id、secret；飞书 app_id、app_secret"
        "（可选 encrypt_key、verification_token）",
    )
    source.add_argument("--scan", action="store_true", help="扫码创建：打印扫码链接并等待完成")
    source.add_argument("--registration", metavar="id", help="用已完成的飞书扫码会话")
    source.add_argument("--provision", metavar="id", help="用已完成的企业微信扫码会话")
    _scan_options(p)

    p = leaf(ops, "feishu-scan-start", "发起扫码创建飞书应用，打印扫码链接", feishu_scan_start)
    p.add_argument("--name", help="应用名称")
    p.add_argument("--description", help="应用描述")
    p.add_argument("--no-reuse", action="store_true", help="不复用之前扫码建好、还没用掉的应用")
    p = leaf(
        ops, "wecom-scan-start", "发起扫码创建企业微信机器人，打印二维码内容", wecom_scan_start
    )
    p.add_argument("--no-reuse", action="store_true", help="不复用之前扫码建好、还没用掉的机器人")
    for kind, text in (("feishu", "飞书"), ("wecom", "企业微信")):
        leaf(ops, f"{kind}-scan-status", f"查看{text}扫码会话的状态", scan_status).add_argument(
            "id", help="扫码会话 id"
        )
        leaf(ops, f"{kind}-scan-cancel", f"取消{text}扫码会话", scan_cancel).add_argument("id")


def _scan_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--wait", type=int, default=600, metavar="秒", help="最长等待扫码的秒数")
    p.add_argument("--no-reuse", action="store_true", help="不复用之前扫码建好、还没用掉的应用")


def _scan_path(args: argparse.Namespace) -> str:
    return FEISHU_SCAN if args.operation.startswith("feishu") else WECOM_SCAN


def _print_scan(row: dict[str, Any]) -> None:
    output.fields(
        row,
        [
            ("会话 id", "id"),
            ("状态", "status"),
            ("扫码链接", "url"),
            ("应用", lambda r: r.get("app_id") or r.get("wecom_bot_id")),
            ("AI 员工", "bot_id"),
            ("有效期至", "expires_at"),
            ("错误", "error"),
        ],
    )


async def _wait_scan(c: AdminClient, path: str, row: dict[str, Any], wait: int) -> dict[str, Any]:
    """打印扫码链接，按服务端给的间隔轮询到结束；超时取消会话。"""
    if row.get("reused") and row["status"] == "succeeded":
        app = row.get("app_id") or row.get("wecom_bot_id")
        print(f"复用之前扫码建好、还没用掉的应用 {app}（不想复用请加 --no-reuse）")
        return row
    if row["status"] == "pending":
        print(f"请在 {wait} 秒内用手机扫码或在浏览器打开下面的链接完成授权：\n{row['url']}")
        sys.stdout.flush()
    deadline = time.monotonic() + wait
    while row["status"] == "pending":
        if time.monotonic() >= deadline:
            await c.request("DELETE", f"{path}/{row['id']}")
            raise CliError(f"等待扫码超时，已取消扫码会话 {row['id']}")
        await asyncio.sleep(max(1, row.get("retry_after") or 3))
        row = await c.get(f"{path}/{row['id']}")
    if row["status"] not in ("succeeded", "consumed"):
        raise CliError(f"扫码没有完成：{row['status']}（{row.get('error') or '无详情'}）")
    return row


async def create(c: AdminClient, args: argparse.Namespace) -> int:
    body: dict[str, Any] = {
        "bot_key": args.bot_key,
        "platform": args.platform,
        "name": args.name,
        "model": args.model,
        "working_dir": args.working_dir,
        "enabled": not args.disabled,
        **changed(
            args,
            {
                "description": "description",
                "effort": "effort_level",
                "verbosity": "verbosity_level",
                "timeout": "sse_timeout_seconds",
                "welcome": "welcome_message",
                "rich_cards": "rich_cards",
                "dev_mode": "dev_mode",
            },
        ),
    }
    if args.runtime:
        body["relay_server_id"] = (await _relay(c, args.runtime))["id"]
    if args.team:
        body["team_id"] = (await c.team(args.team))["id"]
    if args.prompt_file:
        body["system_prompt"] = read_text(args.prompt_file)
    if args.env:
        body["env_vars"] = pairs(args.env)
    feishu = args.platform == "feishu"
    if (args.registration and not feishu) or (args.provision and feishu):
        raise CliError("--registration 只用于飞书，--provision 只用于企业微信")
    if args.cred:
        body["credentials"] = pairs(args.cred)
    elif args.registration:
        body["feishu_registration_id"] = args.registration
    elif args.provision:
        body["wecom_provision_id"] = args.provision
    else:
        # 先校验员工配置，免得扫完码才发现标识重复、目录被占，平台侧白留一个应用。
        await c.request("POST", BOTS, params={"dry_run": "true"}, body=body)
        path = FEISHU_SCAN if feishu else WECOM_SCAN
        start: dict[str, Any] = {"reuse": not args.no_reuse}
        if feishu:
            start |= {"purpose": "create", "name": args.name, "description": args.description}
        row = await _wait_scan(c, path, await c.request("POST", path, body=start), args.wait)
        body["feishu_registration_id" if feishu else "wecom_provision_id"] = row["id"]
    data = await c.request("POST", BOTS, body=body)
    return _done(args, data, f"已新建 {data['name']}（{data['bot_key']}），id {data['id']}")


async def feishu_scan_start(c: AdminClient, args: argparse.Namespace) -> int:
    body = {
        "purpose": "create",
        "name": args.name,
        "description": args.description,
        "reuse": not args.no_reuse,
    }
    data = await c.request("POST", FEISHU_SCAN, body=body)
    if args.json:
        output.dump(data)
    else:
        _print_scan(data)
    return 0


async def wecom_scan_start(c: AdminClient, args: argparse.Namespace) -> int:
    data = await c.request("POST", WECOM_SCAN, body={"reuse": not args.no_reuse})
    if args.json:
        output.dump(data)
    else:
        _print_scan(data)
    return 0


async def scan_status(c: AdminClient, args: argparse.Namespace) -> int:
    data = await c.get(f"{_scan_path(args)}/{args.id}")
    if args.json:
        output.dump(data)
    else:
        _print_scan(data)
    return 0


async def scan_cancel(c: AdminClient, args: argparse.Namespace) -> int:
    data = await c.request("DELETE", f"{_scan_path(args)}/{args.id}")
    if args.json:
        output.dump(data)
    elif data["status"] == "cancelled":
        print(f"已取消扫码会话 {args.id}")
    else:
        # 企微取消前会再查一次：刚好扫完的留着下次复用。
        print(f"扫码会话 {args.id} 已是 {data['status']} 状态，没有取消")
    return 0


# ---- 记忆 -------------------------------------------------------------------------------


def _register_memories(ops: Any) -> None:
    p = leaf(ops, "memories", "列出记忆文件", memories)
    _bot_arg(p)
    p.add_argument("--deleted", action="store_true", help="包括已删除的")
    p = leaf(ops, "memory-show", "输出记忆全文与元数据", memory_show)
    _bot_arg(p)
    p.add_argument("memory", help="文件名或 id（可只写开头几位）")
    p = leaf(ops, "memory-add", "新建记忆文件", memory_add)
    _bot_arg(p)
    p.add_argument("file_name", metavar="文件名")
    p.add_argument("source", metavar="内容文件", help="- 为标准输入")
    _memory_meta(p)
    p = leaf(ops, "memory-set", "修改记忆（只改给出的字段；修改已删除的记忆会恢复它）", memory_set)
    _bot_arg(p)
    p.add_argument("memory")
    p.add_argument("--content-file", metavar="文件", help="用文件内容替换正文；- 为标准输入")
    _memory_meta(p)
    p = leaf(ops, "memory-delete", "删除记忆（部署后运行时上的文件也会删除）", memory_delete)
    _bot_arg(p)
    p.add_argument("memory")
    add_yes(p)
    leaf(ops, "memory-collect", "从运行时回收记忆到平台", memory_collect).add_argument("bot")
    p = leaf(ops, "memory-deploy", "把平台上的记忆部署到运行时（覆盖与删除文件）", memory_deploy)
    _bot_arg(p)
    add_yes(p)


def _memory_meta(p: argparse.ArgumentParser) -> None:
    p.add_argument("--name", help="记忆名称")
    p.add_argument("--description", help="描述")
    p.add_argument("--type", help="类型")


async def _memory(c: AdminClient, bot: dict[str, Any], ref: str) -> dict[str, Any]:
    rows = await c.get(f"{BOTS}/{bot['id']}/memories", include_deleted="true")
    found = (
        [m for m in rows if m["file_name"] == ref]
        or [m for m in rows if m["id"] == ref]
        or [m for m in rows if m["id"].startswith(ref)]
    )
    row = one(found, ref, "记忆", lambda m: f"{m['file_name']}（{m['id'][:8]}）")
    return await c.get(f"{BOTS}/{bot['id']}/memories/{row['id']}")  # type: ignore[no-any-return]


async def memories(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    params = {"include_deleted": "true"} if args.deleted else {}
    rows = await c.get(f"{BOTS}/{bot['id']}/memories", **params)
    if args.json:
        output.dump(rows)
        return 0
    output.table(
        rows,
        [
            ("文件名", "file_name"),
            ("id", lambda m: m["id"][:8]),
            ("名称", "name"),
            ("类型", "type"),
            ("来源", "collected_from"),
            ("修改时间", "file_mtime"),
            ("已删除", lambda m: m["deleted_at"] is not None),
        ],
        empty="（没有记忆）",
    )
    return 0


async def memory_show(c: AdminClient, args: argparse.Namespace) -> int:
    row = await _memory(c, await c.bot(args.bot), args.memory)
    if args.json:
        output.dump(row)
        return 0
    output.fields(
        row,
        [
            ("文件名", "file_name"),
            ("id", "id"),
            ("名称", "name"),
            ("描述", "description"),
            ("类型", "type"),
            ("来源", "collected_from"),
            ("修改时间", "file_mtime"),
            ("删除时间", "deleted_at"),
        ],
    )
    print()
    print(row["content"])
    return 0


def _memory_body(args: argparse.Namespace, base: dict[str, Any]) -> dict[str, Any]:
    return {**base, **changed(args, {"name": "name", "description": "description", "type": "type"})}


async def memory_add(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    body = _memory_body(args, {"file_name": args.file_name, "content": read_text(args.source)})
    data = await c.request("POST", f"{BOTS}/{bot['id']}/memories", body=body)
    return _done(args, data, f"已为 {bot['name']} 新建记忆 {data['file_name']}，部署后生效")


async def memory_set(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    row = await _memory(c, bot, args.memory)
    body = _memory_body(
        args,
        {
            "file_name": row["file_name"],
            "content": row["content"],
            "name": row["name"],
            "description": row["description"],
            "type": row["type"],
        },
    )
    if args.content_file is not None:
        body["content"] = read_text(args.content_file)
    data = await c.request(
        "PUT", f"{BOTS}/{bot['id']}/memories/{row['id']}", body=body, version=row["version"]
    )
    return _done(args, data, f"已更新 {bot['name']} 的记忆 {data['file_name']}，部署后生效")


async def memory_delete(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    row = await _memory(c, bot, args.memory)
    require_yes(args, f"删除记忆 {row['file_name']}")
    await c.request("DELETE", f"{BOTS}/{bot['id']}/memories/{row['id']}", version=row["version"])
    print(f"已删除 {bot['name']} 的记忆 {row['file_name']}，部署后运行时上的文件也会删除")
    return 0


async def memory_collect(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    data = await c.request("POST", f"{BOTS}/{bot['id']}/memories/collect")
    return _done(args, data, f"已从运行时回收 {bot['name']} 的记忆：{data.get('count', 0)} 个文件")


async def memory_deploy(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    require_yes(args, f"把记忆部署到 {bot['name']} 的运行时（覆盖同名文件、删除已删记忆）")
    data = await c.request("POST", f"{BOTS}/{bot['id']}/memories/deploy")
    return _done(args, data, f"已部署 {bot['name']} 的记忆：{data.get('count', 0)} 个文件")


# ---- 工作目录 ---------------------------------------------------------------------------


def _register_workspace(ops: Any) -> None:
    leaf(ops, "workspace", "查看工作目录状态", workspace).add_argument("bot")
    p = leaf(ops, "workspace-files", "列出工作目录里的文件", workspace_files)
    _bot_arg(p)
    p.add_argument("path", nargs="?", default="", help="相对工作目录的子目录，默认根目录")
    p = leaf(ops, "workspace-cat", "把工作目录里的文件原样输出到标准输出", workspace_cat)
    _bot_arg(p)
    p.add_argument("path", help="相对工作目录的路径")
    p = leaf(ops, "workspace-write", "写入工作目录里的文本文件", workspace_write)
    _bot_arg(p)
    p.add_argument("path", help="相对工作目录的路径")
    p.add_argument("source", nargs="?", default="-", metavar="内容文件", help="默认读标准输入")
    p.add_argument("--new", action="store_true", help="新建文件（目标已存在时失败）")
    leaf(ops, "workspace-git-status", "查看工作目录的 Git 状态", workspace_git_status).add_argument(
        "bot"
    )
    p = leaf(ops, "workspace-git-diff", "查看单个文件的未提交改动", workspace_git_diff)
    _bot_arg(p)
    p.add_argument("path")
    leaf(ops, "workspace-init", "初始化或重试初始化工作目录", workspace_init).add_argument("bot")


def _workspace_url(bot: dict[str, Any]) -> str:
    return f"{BOTS}/{bot['id']}/workspace"


async def _ws_get(c: AdminClient, url: str, **params: Any) -> Any:
    """工作目录接口的查询参数里有 path，不能走 c.get 的关键字参数。"""
    return await c.request("GET", url, params=params)


def _print_workspace(data: dict[str, Any]) -> None:
    output.fields(
        data,
        [
            ("目录", "directory"),
            ("状态", "state"),
            ("阶段", "phase"),
            ("错误", "error"),
            ("记忆快照", "memory_snapshot_at"),
        ],
    )


async def workspace(c: AdminClient, args: argparse.Namespace) -> int:
    data = await c.get(_workspace_url(await c.bot(args.bot)))
    if args.json:
        output.dump(data)
    else:
        _print_workspace(data)
    return 0


async def workspace_files(c: AdminClient, args: argparse.Namespace) -> int:
    data = await _ws_get(c, f"{_workspace_url(await c.bot(args.bot))}/files", path=args.path)
    if args.json:
        output.dump(data)
        return 0
    output.table(
        data["entries"],
        [("路径", "path"), ("类型", "type"), ("大小", "size")],
        empty="（空目录）",
    )
    if data.get("truncated"):
        print("（条目过多，只列出前一部分）")
    return 0


async def _read_file(c: AdminClient, url: str, path: str) -> tuple[bytes, dict[str, Any]]:
    """分块读完整个文件；读的过程中文件被改了（哈希变化）就报错。"""
    chunks: list[bytes] = []
    offset = 0
    first: dict[str, Any] | None = None
    while True:
        part = await _ws_get(c, f"{url}/file", path=path, offset=offset, length=READ_CHUNK)
        first = first or part
        if part["hash"] != first["hash"]:
            raise CliError("读取过程中文件被修改，请重试")
        chunk = base64.b64decode(part["data"])
        chunks.append(chunk)
        offset += len(chunk)
        if part["eof"] or offset >= part["size"] or not chunk:
            return b"".join(chunks), first


async def workspace_cat(c: AdminClient, args: argparse.Namespace) -> int:
    content, info = await _read_file(c, _workspace_url(await c.bot(args.bot)), args.path)
    if args.json:
        output.dump({k: info[k] for k in ("size", "hash", "editable")})
        return 0
    sys.stdout.flush()
    sys.stdout.buffer.write(content)
    sys.stdout.buffer.flush()
    return 0


async def workspace_write(c: AdminClient, args: argparse.Namespace) -> int:
    url = _workspace_url(await c.bot(args.bot))
    expected = None
    if not args.new:
        # 乐观锁：带上当前哈希，读完到写入之间被别人改过会被拒绝。
        current = await _ws_get(c, f"{url}/file", path=args.path, length=1)
        if not current["editable"]:
            raise CliError(f"{args.path} 不是可编辑的文本文件（UTF-8 且不超过 1 MiB）")
        expected = current["hash"]
    body = {"path": args.path, "content": read_text(args.source), "expected_hash": expected}
    data = await c.request("PUT", f"{url}/file", body=body)
    return _done(args, data, f"已写入 {args.path}")


async def workspace_git_status(c: AdminClient, args: argparse.Namespace) -> int:
    data = await c.get(f"{_workspace_url(await c.bot(args.bot))}/git/status")
    if args.json:
        output.dump(data)
        return 0
    if not data.get("initialized"):
        print("工作目录还不是 Git 仓库")
    else:
        head = data.get("head") or {}
        output.fields(
            data,
            [
                ("远程", "remote"),
                ("分支", "branch"),
                ("上游", "upstream"),
                ("领先提交", "ahead"),
                ("最新提交", lambda d: f"{head['hash']} {head['subject']}" if head else None),
            ],
        )
        print()
    output.table(
        data.get("files") or [], [("状态", "status"), ("路径", "path")], empty="（没有改动）"
    )
    return 0


async def workspace_git_diff(c: AdminClient, args: argparse.Namespace) -> int:
    data = await _ws_get(c, f"{_workspace_url(await c.bot(args.bot))}/git/diff", path=args.path)
    if args.json:
        output.dump(data)
        return 0
    print(data["diff"] or "（没有改动）")
    if data.get("truncated"):
        print("（改动过长，已截断）")
    return 0


async def workspace_init(c: AdminClient, args: argparse.Namespace) -> int:
    data = await c.request("POST", f"{_workspace_url(await c.bot(args.bot))}/initialize")
    if args.json:
        output.dump(data)
    else:
        print("初始化完成：")
        _print_workspace(data)
    return 0


# ---- 切换运行时与审批人 -----------------------------------------------------------------


def _register_switch(ops: Any) -> None:
    p = leaf(
        ops,
        "switch-relay",
        "切换运行时或模型：不加 --yes 只预览目标目录，加 --yes 才执行",
        switch_relay,
    )
    _bot_arg(p)
    p.add_argument("runtime", help="目标运行时名称或 id；只换模型时写当前运行时")
    p.add_argument("--model", help="目标模型；不填沿用当前模型（目标运行时须支持）")
    p.add_argument(
        "--mode",
        choices=("copy", "existing"),
        default="copy",
        help="copy 把工作目录复制过去（默认）；existing 使用目标上已有的目录",
    )
    p.add_argument("--dir", metavar="目录", help="目标运行时上的工作目录，默认按原目录名")
    p.add_argument(
        "--allow-stored-memory",
        action="store_true",
        help="原运行时离线时用平台上的记忆快照（只能配合 --mode existing）",
    )
    add_yes(p)

    leaf(ops, "approvers", "列出审批人（空 = 由发起人自己确认）", approvers).add_argument("bot")
    p = leaf(ops, "approvers-set", "整体替换审批人；不写成员即清空", approvers_set)
    _bot_arg(p)
    p.add_argument("users", nargs="*", metavar="成员", help="登录名、邮箱、姓名或 id")


async def switch_relay(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    target = await _relay(c, args.runtime)
    moving = target["id"] != bot["relay_server_id"]
    model = args.model or bot["model"]
    preview = None
    if moving:
        body = {"relay_server_id": target["id"], "target_directory": args.dir}
        preview = await c.request("POST", f"{BOTS}/{bot['id']}/workspace/switch-preview", body=body)
    if not args.yes:
        if args.json:
            output.dump({"target": target, "model": model, "preview": preview})
            return 0
        print(f"{bot['name']}：{bot['relay_name'] or '未分配'} → {target['name']}，模型 {model}")
        if preview is not None:
            output.fields(
                preview,
                [
                    ("目标目录", "directory"),
                    ("目录已存在", "exists"),
                    ("目录为空", "empty"),
                    ("属于本员工", "owned"),
                    ("有归属标记", "marked"),
                    ("原运行时在线", "source_online"),
                    ("与原目录共享", "shared"),
                    ("记忆快照", "memory_snapshot_at"),
                ],
            )
        print("确认无误后加 --yes 执行切换")
        return 0
    body = {"relay_server_id": target["id"], "model": model}
    if moving:
        body |= {
            "workspace_mode": args.mode,
            "target_directory": args.dir or preview["directory"],  # type: ignore[index]
            "allow_stored_memory": args.allow_stored_memory,
        }
    data = await c.request(
        "POST", f"{BOTS}/{bot['id']}/switch-relay", body=body, version=bot["version"]
    )
    return _done(
        args,
        data,
        f"已把 {bot['name']} 切换到 {target['name']}，模型 {data['new_model']}"
        f"（记忆：{data['memory_status']}）",
    )


async def approvers(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    rows = await c.get(f"{BOTS}/{bot['id']}/approvers")
    if args.json:
        output.dump(rows)
    else:
        output.table(
            rows,
            [("登录名", "login_name"), ("姓名", "display_name")],
            empty="（没有审批人：由发起人自己确认）",
        )
    return 0


async def approvers_set(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    ids = [(await c.user(ref))["id"] for ref in args.users]
    rows = await c.request("PUT", f"{BOTS}/{bot['id']}/approvers", body={"user_ids": ids})
    names = "、".join(r["display_name"] for r in rows) or "空（由发起人自己确认）"
    return _done(args, rows, f"{bot['name']} 的审批人现为：{names}")


# ---- 协作：真人同事与 AI 员工互助 -------------------------------------------------------


def _register_collaboration(ops: Any) -> None:
    leaf(ops, "humans", "列出可以求助的真人同事", humans).add_argument("bot")
    p = leaf(ops, "human-options", "搜索可以加为协作同事的成员（须已绑定飞书）", human_options)
    _bot_arg(p)
    p.add_argument("--keyword", default="", help="按姓名或登录名搜索")
    p = leaf(ops, "human-add", "添加可以求助的真人同事", human_add)
    _bot_arg(p)
    p.add_argument("user", metavar="成员", help="登录名、邮箱、姓名或 id")
    p.add_argument("--responsibility", default="", help="负责什么，帮 AI 员工判断找谁")
    p = leaf(ops, "human-set", "修改协作同事（只改给出的字段）", human_set)
    _bot_arg(p)
    p.add_argument("user", metavar="成员")
    p.add_argument("--responsibility")
    p.add_argument(
        "--enabled", type=on_off, metavar="on|off", help="暂停或恢复求助；暂停会取消进行中的求助"
    )
    p = leaf(ops, "human-remove", "移除协作同事（进行中的求助一并取消）", human_remove)
    _bot_arg(p)
    p.add_argument("user", metavar="成员")
    add_yes(p)

    leaf(ops, "bot-help", "查看是否允许其他 AI 员工求助", bot_help).add_argument("bot")
    p = leaf(ops, "bot-help-set", "开关「允许其他 AI 员工求助」", bot_help_set)
    _bot_arg(p)
    p.add_argument("enabled", type=on_off, metavar="on|off", help="关闭会取消进行中的求助")

    leaf(ops, "api-access", "查看「允许 API 驱动」和群别名", api_access).add_argument("bot")
    p = leaf(ops, "api-access-set", "开关「允许 API 驱动」", api_access_set)
    _bot_arg(p)
    p.add_argument(
        "enabled", type=on_off, metavar="on|off", help="关闭后新调用被拒，排队中的 API 轮次不再执行"
    )
    p = leaf(ops, "api-alias-remove", "解绑一个群别名", api_alias_remove)
    _bot_arg(p)
    p.add_argument("alias", help="别名")
    add_yes(p)


HUMAN_COLUMNS: list[output.Column] = [
    ("姓名", "name"),
    ("部门", "department"),
    ("职位", "position"),
    ("负责", "responsibility"),
    ("状态", output.enabled),
    ("可触达", "reachable"),
    ("进行中", "active_count"),
]


async def humans(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    rows = await c.get(f"{BOTS}/{bot['id']}/human-collaborators")
    if args.json:
        output.dump(rows)
    else:
        output.table(rows, HUMAN_COLUMNS, empty="（没有协作同事）")
    return 0


async def human_options(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    rows = await c.get(f"{BOTS}/{bot['id']}/human-collaborator-options", q=args.keyword)
    if args.json:
        output.dump(rows)
    else:
        output.table(
            rows,
            [
                ("登录名", "login_name"),
                ("姓名", "name"),
                ("部门", "department"),
                ("职位", "position"),
                ("已添加", "added"),
            ],
            empty="（没有可选的成员）",
        )
    return 0


async def _partner(c: AdminClient, bot: dict[str, Any], ref: str) -> dict[str, Any]:
    user = await c.user(ref)
    rows = await c.get(f"{BOTS}/{bot['id']}/human-collaborators")
    found: list[dict[str, Any]] = [r for r in rows if r["user_id"] == user["id"]]
    if not found:
        raise CliError(f"{user['display_name']} 不是 {bot['name']} 的协作同事")
    return found[0]


async def human_add(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    user = await c.user(args.user)
    body = {"user_id": user["id"], "responsibility": args.responsibility}
    data = await c.request("POST", f"{BOTS}/{bot['id']}/human-collaborators", body=body)
    return _done(args, data, f"已把 {data['name']} 加为 {bot['name']} 可以求助的同事")


async def human_set(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    body = changed(args, {"responsibility": "responsibility", "enabled": "enabled"})
    if not body:
        raise CliError("没有要修改的字段，见 --help")
    partner = await _partner(c, bot, args.user)
    data = await c.request(
        "PATCH",
        f"{BOTS}/{bot['id']}/human-collaborators/{partner['id']}",
        body=body,
        version=partner["version"],
    )
    return _done(args, data, f"已更新协作同事 {data['name']}：{'、'.join(body)}")


async def human_remove(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    partner = await _partner(c, bot, args.user)
    require_yes(args, f"移除协作同事 {partner['name']}")
    await c.request(
        "DELETE",
        f"{BOTS}/{bot['id']}/human-collaborators/{partner['id']}",
        version=partner["version"],
    )
    print(f"已移除 {bot['name']} 的协作同事 {partner['name']}")
    return 0


def _print_bot_help(data: dict[str, Any]) -> None:
    permission = {"granted": "已具备", "missing": "缺少，需扫码补齐", "pending": "待管理员审核"}
    output.fields(
        data,
        [
            ("允许其他 AI 员工求助", "accept_bot_help"),
            ("飞书权限", lambda d: permission.get(d["permission"], "无法检查")),
            ("进行中的求助", "active_count"),
        ],
    )


async def bot_help(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    data = await c.get(f"{BOTS}/{bot['id']}/bot-help")
    if args.json:
        output.dump(data)
    else:
        _print_bot_help(data)
    return 0


async def bot_help_set(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    data = await c.request("PATCH", f"{BOTS}/{bot['id']}/bot-help", body={"enabled": args.enabled})
    if args.json:
        output.dump(data)
    else:
        _print_bot_help(data)
    return 0


# ---- API 驱动 ---------------------------------------------------------------------------


def _print_api_access(data: dict[str, Any]) -> None:
    output.fields(data, [("允许 API 驱动", "enabled")])
    print()
    output.table(
        data["aliases"],
        [
            ("群别名", "alias"),
            ("群 chat_id", "chat_id"),
            ("绑定人", "created_by"),
            ("绑定时间", "created_at"),
        ],
        empty="（还没有群别名：在群里 @ AI 员工发「/api 绑定 <别名>」）",
    )


async def api_access(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    data = await c.get(f"{BOTS}/{bot['id']}/api-access")
    if args.json:
        output.dump(data)
    else:
        _print_api_access(data)
    return 0


async def api_access_set(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    data = await c.request(
        "PATCH", f"{BOTS}/{bot['id']}/api-access", body={"enabled": args.enabled}
    )
    if args.json:
        output.dump(data)
    else:
        _print_api_access(data)
    return 0


async def api_alias_remove(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    data = await c.get(f"{BOTS}/{bot['id']}/api-access")
    row = one(
        [a for a in data["aliases"] if a["alias"] == args.alias],
        args.alias,
        "群别名",
        lambda a: a["alias"],
    )
    require_yes(args, f"解绑群别名 {row['alias']}")
    await c.request("DELETE", f"{BOTS}/{bot['id']}/api-aliases/{row['id']}")
    print(f"已解绑 {bot['name']} 的群别名 {row['alias']}")
    return 0


# ---- 群聊旁听 ---------------------------------------------------------------------------


def _register_observe(ops: Any) -> None:
    p = leaf(ops, "observe-set", "修改群聊旁听设置（只改给出的字段）", observe_set)
    _bot_arg(p)
    p.add_argument(
        "--mode", choices=("off", "shadow", "on"), help="off 关闭；shadow 只记录判断；on 插话"
    )
    p.add_argument("--scope", help="旁听时关注的话题范围；空字符串为清除")
    p.add_argument("--role", choices=("member", "dispatcher"), help="群里的角色：成员或调度员")
    p = leaf(ops, "observe-decisions", "列出群聊旁听的判断记录（新的在前）", observe_decisions)
    _bot_arg(p)
    p.add_argument("--chat", metavar="群 id", help="只看这个群")
    speak = p.add_mutually_exclusive_group()
    speak.add_argument("--would-speak", action="store_true", help="只看判断要插话的")
    speak.add_argument("--silent", action="store_true", help="只看判断不插话的")
    p.add_argument("--before", type=int, metavar="记录 id", help="翻页：只看比它早的")
    p.add_argument("--limit", type=int, default=50, help="条数（1–200，默认 50）")
    p.add_argument("--messages", action="store_true", help="同时打印判断时看到的群消息")
    p = leaf(ops, "observe-review", "标注一条判断该不该插话", observe_review)
    _bot_arg(p)
    p.add_argument("decision", type=int, metavar="记录 id")
    p.add_argument("review", choices=("should", "should_not", "none"), help="none 为清除标注")


async def observe_set(c: AdminClient, args: argparse.Namespace) -> int:
    body = changed(
        args, {"mode": "group_observe", "scope": "group_observe_scope", "role": "group_role"}
    )
    if not body:
        raise CliError("没有要修改的字段，见 --help")
    bot = await c.bot(args.bot)
    data = await c.request("PATCH", f"{BOTS}/{bot['id']}", body=body, version=bot["version"])
    return _done(args, data, f"已更新 {data['name']} 的群聊旁听设置：{'、'.join(body)}")


async def observe_decisions(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    speak = True if args.would_speak else False if args.silent else None
    data = await c.get(
        f"{BOTS}/{bot['id']}/group-observe/decisions",
        chat_id=args.chat,
        would_speak=None if speak is None else str(speak).lower(),
        before_id=args.before,
        limit=args.limit,
    )
    if args.json:
        output.dump(data)
        return 0
    if not args.chat:
        output.table(
            data["chats"],
            [
                ("群", "chat_id"),
                ("判断次数", "total"),
                ("要插话", "would_speak"),
                ("最近", "last_at"),
            ],
            empty="（还没有旁听记录）",
        )
        print()
    output.table(
        data["items"],
        [
            ("记录 id", "id"),
            ("时间", "created_at"),
            ("群", "chat_id"),
            ("模式", "mode"),
            ("结果", "outcome"),
            ("概率", "probability"),
            ("标注", "review"),
        ],
        empty="（没有判断记录）",
    )
    if args.messages:
        for item in data["items"]:
            print(f"\n#{item['id']} {item['outcome']}")
            for msg in item["messages"]:
                print(f"  {msg['sender']}：{msg['text']}")
    return 0


async def observe_review(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    review = None if args.review == "none" else args.review
    data = await c.request(
        "PATCH",
        f"{BOTS}/{bot['id']}/group-observe/decisions/{args.decision}",
        body={"review": review},
    )
    return _done(args, data, f"已标注判断记录 {args.decision}：{review or '清除'}")


# ---- 飞书应用管理 -----------------------------------------------------------------------


def _register_feishu_app(ops: Any) -> None:
    p = leaf(ops, "feishu-app", "查看飞书应用概况：信息、权限、版本、斜杠指令", feishu_app)
    _bot_arg(p)
    p = leaf(
        ops,
        "feishu-app-base",
        "修改飞书应用名称、描述与帮助链接（需发布版本生效）",
        feishu_app_base,
    )
    _bot_arg(p)
    p.add_argument("--name")
    p.add_argument("--description")
    p.add_argument("--help-url", metavar="https 链接", help="帮助文档链接；空字符串为清除")
    p.add_argument("--language", choices=("zh_cn", "en_us", "ja_jp"), help="默认为应用主语言")
    p = leaf(
        ops, "feishu-app-bot", "修改机器人能力：欢迎语与菜单（需发布版本生效）", feishu_app_bot
    )
    _bot_arg(p)
    p.add_argument("--get-started", metavar="文字", help="新会话的引导语")
    menu = p.add_mutually_exclusive_group()
    menu.add_argument(
        "--menus-file",
        metavar="文件",
        help="菜单 JSON 数组（字段同 feishu-app --json 里 versions.*.bot.menus），整体替换并开启",
    )
    menu.add_argument("--no-menu", action="store_true", help="关闭并清空菜单")
    p.add_argument("--menu-strategy", type=int, choices=(1, 2, 3), help="菜单展示方式")
    p.add_argument("--language", choices=("zh_cn", "en_us", "ja_jp"), help="默认为应用主语言")
    p = leaf(
        ops,
        "feishu-app-avatar",
        "上传飞书应用图标（PNG/JPEG，≤2 MB，需发布版本生效）",
        feishu_app_avatar,
    )
    _bot_arg(p)
    p.add_argument("file", metavar="图片文件")
    leaf(
        ops, "feishu-app-scopes-apply", "为飞书应用申请缺少的权限", feishu_app_scopes_apply
    ).add_argument("bot")
    p = leaf(ops, "feishu-app-publish", "发布飞书应用新版本（提交企业审核）", feishu_app_publish)
    _bot_arg(p)
    p.add_argument("--version", help="版本号 x.y.z，默认在线版本加一")
    p.add_argument("--changelog", required=True, help="更新说明（给使用者看）")
    p.add_argument("--remark", help="申请理由（给审核人看），默认同更新说明")
    add_yes(p)
    p = leaf(
        ops,
        "feishu-app-scan",
        "扫码补齐飞书应用权限（打印扫码链接并等待完成）",
        feishu_app_scan,
    )
    _bot_arg(p)
    p.add_argument("--observe", action="store_true", help="同时申请群聊旁听要的权限")
    p.add_argument("--visibility", action="store_true", help="同时申请管理可用范围的权限")
    p.add_argument("--wait", type=int, default=600, metavar="秒", help="最长等待扫码的秒数")
    leaf(
        ops, "observe-permission", "查看飞书应用有没有群聊旁听要的权限", observe_permission
    ).add_argument("bot")

    leaf(ops, "feishu-commands", "列出飞书斜杠指令", feishu_commands).add_argument("bot")
    p = leaf(ops, "feishu-command-add", "新增飞书斜杠指令", feishu_command_add)
    _bot_arg(p)
    p.add_argument("command", help="指令名，不带斜杠")
    p.add_argument("description", help="指令说明")
    p.add_argument("--icon", metavar="icon_key", help="图标，形如 xxx_outlined")
    p = leaf(ops, "feishu-command-set", "修改飞书斜杠指令", feishu_command_set)
    _bot_arg(p)
    p.add_argument("command")
    p.add_argument("--description")
    p.add_argument("--icon", metavar="icon_key", help="空字符串为清除")
    p = leaf(ops, "feishu-command-delete", "删除飞书斜杠指令", feishu_command_delete)
    _bot_arg(p)
    p.add_argument("command")
    add_yes(p)
    leaf(
        ops,
        "feishu-command-defaults",
        "补齐平台内置斜杠指令（已有的同名指令不改）",
        feishu_command_defaults,
    ).add_argument("bot")


def _app_url(bot: dict[str, Any]) -> str:
    return f"{BOTS}/{bot['id']}/feishu-app"


async def _overview(c: AdminClient, bot: dict[str, Any]) -> dict[str, Any]:
    return await c.get(_app_url(bot))  # type: ignore[no-any-return]


def _version(v: dict[str, Any] | None) -> str | None:
    return f"{v['version']}（状态 {v['status']}）" if v else None


async def feishu_app(c: AdminClient, args: argparse.Namespace) -> int:
    data = await _overview(c, await c.bot(args.bot))
    if args.json:
        output.dump(data)
        return 0
    app = data["app"] or {}
    output.fields(
        data,
        [
            ("App ID", "app_id"),
            ("名称", lambda _: app.get("name")),
            ("描述", lambda _: app.get("description")),
            ("主语言", lambda _: app.get("primary_language")),
            ("帮助链接", lambda _: app.get("help_use")),
            ("图标", lambda _: app.get("avatar_url")),
            ("扫码创建", lambda d: d["origin"]["one_click"]),
            ("在线版本", lambda d: _version(d["versions"]["online"])),
            ("审核中版本", lambda d: _version(d["versions"]["under_review"])),
            ("下个版本号", "next_version"),
            ("缺少的应用权限", lambda d: d["missing_scopes"]["tenant"]),
            ("缺少的用户权限", lambda d: d["missing_scopes"]["user"]),
            ("待审核权限", "pending_grants"),
            ("个人连接数", "personal_connections"),
            ("开发者后台", "console_url"),
        ],
    )
    errors = {k: v for k, v in data["errors"].items() if v}
    if errors:
        print(f"\n部分信息没读到（飞书错误码）：{json.dumps(errors, ensure_ascii=False)}")
    return 0


async def feishu_app_base(c: AdminClient, args: argparse.Namespace) -> int:
    if args.name is None and args.description is None and args.help_url is None:
        raise CliError("没有要修改的字段，见 --help")
    bot = await c.bot(args.bot)
    app = (await _overview(c, bot))["app"] or {}
    body = {
        "language": args.language or app.get("primary_language") or "zh_cn",
        "name": args.name if args.name is not None else app.get("name"),
        "description": args.description if args.description is not None else app.get("description"),
        "help_use": (args.help_url or None) if args.help_url is not None else app.get("help_use"),
    }
    data = await c.request("PATCH", f"{_app_url(bot)}/base", body=body)
    return _done(args, data, "已更新飞书应用基础信息，发布新版本后生效（feishu-app-publish）")


async def feishu_app_bot(c: AdminClient, args: argparse.Namespace) -> int:
    if args.get_started is None and not args.menus_file and not args.no_menu:
        raise CliError("没有要修改的字段，见 --help")
    bot = await c.bot(args.bot)
    language = args.language
    if language is None:
        language = ((await _overview(c, bot))["app"] or {}).get("primary_language") or "zh_cn"
    body: dict[str, Any] = {
        "language": language,
        "get_started_desc": args.get_started,
        "menu_display_strategy": args.menu_strategy,
    }
    if args.menus_file:
        try:
            body["menus"] = json.loads(read_text(args.menus_file))
        except json.JSONDecodeError as exc:
            raise CliError(f"{args.menus_file} 不是合法 JSON：{exc}") from None
        body["menu_enabled"] = True
    elif args.no_menu:
        body["menu_enabled"] = False
    data = await c.request("PATCH", f"{_app_url(bot)}/bot", body=body)
    return _done(args, data, "已更新飞书机器人能力，发布新版本后生效（feishu-app-publish）")


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise CliError(f"读不了文件 {path}：{exc.strerror}") from None


async def feishu_app_avatar(c: AdminClient, args: argparse.Namespace) -> int:
    path = Path(args.file)
    media = AVATAR_TYPES.get(path.suffix.lower())
    if media is None:
        raise CliError("图标只支持 PNG 或 JPEG")
    content = _read_bytes(path)
    bot = await c.bot(args.bot)
    resp = await c.http.post(
        f"{_app_url(bot)}/avatar", files={"avatar": (path.name, content, media)}
    )
    data = _data(resp)
    return _done(args, data, "已上传飞书应用图标，发布新版本后生效（feishu-app-publish）")


async def feishu_app_scopes_apply(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    data = await c.request("POST", f"{_app_url(bot)}/scopes/apply")
    return _done(args, data, "已提交权限申请，等待企业管理员审核")


async def feishu_app_publish(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    version = args.version or (await _overview(c, bot))["next_version"]
    require_yes(args, f"发布 {bot['name']} 的飞书应用版本 {version}")
    body = {
        "version": version,
        "changelog": args.changelog,
        "remark": args.remark or args.changelog,
    }
    data = await c.request("POST", f"{_app_url(bot)}/publish", body=body)
    return _done(args, data, f"已提交飞书应用版本 {data['version']}")


async def feishu_app_scan(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    body = {
        "purpose": "update",
        "bot_id": bot["id"],
        "observe": args.observe,
        "visibility": args.visibility,
    }
    row = await c.request("POST", FEISHU_SCAN, body=body)
    row = await _wait_scan(c, FEISHU_SCAN, row, args.wait)
    return _done(args, row, f"已补齐 {bot['name']} 的飞书应用权限")


async def observe_permission(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    data = await c.get(f"{_app_url(bot)}/observe-permission")
    text = {
        "granted": "已具备群聊旁听要的权限",
        "missing": "缺少「获取群组中所有消息」权限，可用 feishu-app-scan --observe 扫码补齐",
        "pending": "「获取群组中所有消息」权限还在等企业管理员审核",
    }
    return _done(args, data, text.get(data["status"], data["status"]))


async def _command(c: AdminClient, bot: dict[str, Any], name: str) -> dict[str, Any]:
    rows = (await _overview(c, bot))["slash_commands"]
    if rows is None:
        raise CliError("读不到飞书斜杠指令，请检查应用权限")
    found = [r for r in rows if r["command"] == name.lstrip("/")]
    return one(found, name, "斜杠指令", lambda r: r["command"])


async def feishu_commands(c: AdminClient, args: argparse.Namespace) -> int:
    rows = (await _overview(c, await c.bot(args.bot)))["slash_commands"]
    if rows is None:
        raise CliError("读不到飞书斜杠指令，请检查应用权限")
    if args.json:
        output.dump(rows)
    else:
        output.table(
            rows,
            [
                ("指令", lambda r: f"/{r['command']}"),
                ("说明", "description"),
                ("图标", "icon_key"),
                ("id", "command_id"),
            ],
            empty="（没有斜杠指令）",
        )
    return 0


async def feishu_command_add(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    body = {
        "command": args.command.lstrip("/"),
        "description": args.description,
        "icon_key": args.icon,
    }
    data = await c.request("POST", f"{_app_url(bot)}/slash-commands", body=body)
    return _done(args, data, f"已新增斜杠指令 /{body['command']}")


async def feishu_command_set(c: AdminClient, args: argparse.Namespace) -> int:
    if args.description is None and args.icon is None:
        raise CliError("没有要修改的字段，见 --help")
    bot = await c.bot(args.bot)
    row = await _command(c, bot, args.command)
    body = {
        "description": args.description if args.description is not None else row["description"],
        "icon_key": (args.icon or None) if args.icon is not None else row["icon_key"],
    }
    data = await c.request(
        "PATCH", f"{_app_url(bot)}/slash-commands/{row['command_id']}", body=body
    )
    return _done(args, data, f"已更新斜杠指令 /{row['command']}")


async def feishu_command_delete(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    row = await _command(c, bot, args.command)
    require_yes(args, f"删除斜杠指令 /{row['command']}")
    await c.request("DELETE", f"{_app_url(bot)}/slash-commands/{row['command_id']}")
    print(f"已删除斜杠指令 /{row['command']}")
    return 0


async def feishu_command_defaults(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    data = await c.request("POST", f"{_app_url(bot)}/slash-commands/defaults")
    created = "、".join(f"/{name}" for name in data["created"]) or "无（都已存在）"
    return _done(args, data, f"已补齐内置斜杠指令，新增：{created}")


# ---- 运行环境检查与技能 MCP 登录 --------------------------------------------------------


def _register_misc(ops: Any) -> None:
    leaf(
        ops, "health-report", "让 AI 员工只读检查运行环境并输出报告（最长约 5 分钟）", health_report
    ).add_argument("bot")
    p = leaf(ops, "mcp-login", "发起技能 MCP 登录，打印授权链接", mcp_login)
    _bot_arg(p)
    p.add_argument("skill", help="技能名称或 id")
    p = leaf(
        ops,
        "mcp-login-complete",
        "提交授权后浏览器跳转到的回调地址，完成 MCP 登录",
        mcp_login_complete,
    )
    _bot_arg(p)
    p.add_argument("skill")
    p.add_argument("session_id", metavar="会话", help="mcp-login 打印的会话")
    p.add_argument("callback_url", metavar="回调地址", help="授权后浏览器地址栏里的完整地址")
    p = leaf(ops, "mcp-logout", "退出技能 MCP 登录", mcp_logout)
    _bot_arg(p)
    p.add_argument("skill")


def _frame(raw: str) -> tuple[str, dict[str, Any] | None]:
    lines = raw.split("\n")
    kind = next((ln[6:].strip() for ln in lines if ln.startswith("event:")), "message")
    payload = "\n".join(ln[5:].lstrip() for ln in lines if ln.startswith("data:"))
    return kind, json.loads(payload) if payload else None


async def health_report(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    text: list[str] = []
    tools: list[str] = []
    done = False
    async with c.http.stream("POST", f"{BOTS}/{bot['id']}/health-report") as resp:
        if resp.status_code >= 400:
            await resp.aread()
            _data(resp)
        buffer = ""
        async for chunk in resp.aiter_text():
            buffer += chunk.replace("\r\n", "\n")
            while "\n\n" in buffer:
                raw, buffer = buffer.split("\n\n", 1)
                kind, data = _frame(raw)
                if data is None:
                    continue
                if kind == "error":
                    raise CliError(data.get("message") or "运行环境检查未完整完成")
                if kind == "done":
                    done = True
                elif kind == "text":
                    text.append(data["text"])
                    if not args.json:
                        print(data["text"], end="", flush=True)
                elif kind == "tool":
                    tools.append(data["name"])
                    if not args.json:
                        print(f"[调用工具 {data['name']}]", file=sys.stderr)
    if not done:
        raise CliError("运行环境检查未完整完成，上面的内容只是部分结果")
    if args.json:
        output.dump({"text": "".join(text), "tools": tools})
    else:
        print()
    return 0


async def _skill_url(c: AdminClient, args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    bot = await c.bot(args.bot)
    skill = await c.skill(args.skill)
    return skill, f"{BOTS}/{bot['id']}/skills/{skill['id']}/mcp-login"


async def mcp_login(c: AdminClient, args: argparse.Namespace) -> int:
    skill, url = await _skill_url(c, args)
    data = await c.request("POST", url)
    if args.json:
        output.dump(data)
        return 0
    print(
        f"请在 {data['expires_in']} 秒内在浏览器打开下面的链接授权：\n{data['authorization_url']}"
    )
    print(
        "\n授权后浏览器会跳到一个打不开的回调地址，复制地址栏里的完整地址，执行：\n"
        f"coreman bots mcp-login-complete {args.bot} {skill['name']} "
        f"{data['session_id']} '回调地址'"
    )
    return 0


async def mcp_login_complete(c: AdminClient, args: argparse.Namespace) -> int:
    skill, url = await _skill_url(c, args)
    body = {"session_id": args.session_id, "callback_url": args.callback_url}
    data = await c.request("POST", f"{url}/complete", body=body)
    return _done(args, data, f"{skill['name']} 的 MCP 登录已完成")


async def mcp_logout(c: AdminClient, args: argparse.Namespace) -> int:
    skill, url = await _skill_url(c, args)
    data = await c.request("DELETE", url)
    return _done(args, data, f"已退出 {skill['name']} 的 MCP 登录")
