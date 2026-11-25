"""管理后台的命令行版本：覆盖管理后台各页面的操作，按页面分模块注册命令组。

在 API 镜像里运行，以 --as 指定的成员身份操作，权限与审计和管理后台一致，见 client.py。
没有专用命令的接口可以用 `api` 命令直接调。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

import httpx

from coreman.admin_cli import (
    approvals,
    bots,
    org,
    output,
    platform,
    remote,
    runtimes,
    schedules,
    skills,
    systems,
)
from coreman.admin_cli.base import group, leaf, read_text
from coreman.admin_cli.client import AdminClient, CliError, connect

__all__ = ["register", "register_skills", "run"]


def register(sub: Any) -> None:
    """注册除 `skills` 以外的命令组；`skills` 已有 sync，见 register_skills。"""
    bots.register(sub)
    approvals.register(sub)
    systems.register(sub)
    org.register(sub)
    schedules.register(sub)
    runtimes.register(sub)
    platform.register(sub)
    p = sub.add_parser(
        "login",
        help="在自己电脑上登录远程 CoreMan，之后的管理命令以你本人的身份执行",
        description="浏览器里登录并确认授权，令牌存在本机配置目录（权限 0600）。",
    )
    p.add_argument(
        "--server",
        default=os.environ.get(remote.SERVER_ENV),
        help="CoreMan 地址，如 https://coreman.example.com",
    )
    p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器，只打印地址")
    p.set_defaults(local=_login)
    p = sub.add_parser("logout", help="吊销本机的登录并删掉保存的令牌")
    p.set_defaults(local=_logout)
    leaf(sub, "whoami", "当前以谁的身份操作", whoami)
    ops = group(sub, "api", "直接调用管理 API（覆盖其余管理后台操作）")
    for method in ("get", "post", "put", "patch", "delete"):
        p = leaf(ops, method, f"{method.upper()} 请求，输出返回的 data", raw)
        p.add_argument("path", help="接口路径，如 /api/admin/cron-jobs")
        p.add_argument("--param", action="append", metavar="KEY=VALUE", help="查询参数，可重复")
        p.add_argument("--data", help="JSON 请求体；@文件 从文件读，@- 从标准输入读")
        p.add_argument("--if-match", type=int, metavar="版本", help="乐观锁版本号")


def register_skills(ops: Any) -> None:
    skills.register(ops)


def _login(args: argparse.Namespace) -> int:
    if not args.server:
        print(
            f"请用 --server 指定 CoreMan 地址（或设置环境变量 {remote.SERVER_ENV}）",
            file=sys.stderr,
        )
        return 2
    try:
        me = asyncio.run(remote.login(args.server, open_browser=not args.no_browser))
    except (remote.RemoteError, httpx.HTTPError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    who = f"{me.get('display_name')}（{me.get('login_name')}）"
    print(f"已登录为 {who}，令牌保存在 {remote.config_path()}")
    return 0


def _logout(_args: argparse.Namespace) -> int:
    print("已退出登录" if asyncio.run(remote.logout()) else "本机没有登录过")
    return 0


async def whoami(c: AdminClient, args: argparse.Namespace) -> int:
    me = await c.get("/api/admin/auth/me")
    if args.json:
        output.dump(me)
    else:
        output.fields(me, [("登录名", "login_name"), ("姓名", "display_name"), ("角色", "role")])
    return 0


async def raw(c: AdminClient, args: argparse.Namespace) -> int:
    params: dict[str, Any] = {}
    for item in args.param or []:
        key, _, value = item.partition("=")
        params[key] = value
    body = None
    if args.data is not None:
        source = read_text(args.data[1:]) if args.data.startswith("@") else args.data
        try:
            body = json.loads(source)
        except json.JSONDecodeError as exc:
            raise CliError(f"--data 不是合法 JSON：{exc}") from None
    path = args.path if args.path.startswith("/") else f"/{args.path}"
    data = await c.request(
        args.operation.upper(), path, params=params, body=body, version=args.if_match
    )
    output.dump(data)
    return 0


async def _run(args: argparse.Namespace) -> int:
    async with connect(args.actor) as client:
        return await args.admin(client, args)  # type: ignore[no-any-return]


def run(args: argparse.Namespace) -> int:
    try:
        return asyncio.run(_run(args))
    except CliError as exc:
        print(str(exc), file=sys.stderr)
        return 1
