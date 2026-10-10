"""管理后台的命令行版本：AI 员工、技能管理、技能审批、业务系统、团队与用户。

在 API 镜像里运行，以 --as 指定的成员身份操作，权限与审计和管理后台一致，见 client.py。
其余管理后台操作可以用 `api` 命令直接调接口。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from coreman.admin_cli import approvals, bots, org, output, skills, systems
from coreman.admin_cli.base import group, leaf, read_text
from coreman.admin_cli.client import AdminClient, CliError, connect

__all__ = ["register", "register_skills", "run"]


def register(sub: Any) -> None:
    """注册除 `skills` 以外的命令组；`skills` 已有 sync，见 register_skills。"""
    bots.register(sub)
    approvals.register(sub)
    systems.register(sub)
    org.register(sub)
    ops = group(sub, "api", "直接调用管理 API（覆盖其余管理后台操作）")
    for method in ("get", "post", "put", "patch", "delete"):
        p = leaf(ops, method, f"{method.upper()} 请求，输出返回的 data", raw)
        p.add_argument("path", help="接口路径，如 /api/admin/cron-jobs")
        p.add_argument("--param", action="append", metavar="KEY=VALUE", help="查询参数，可重复")
        p.add_argument("--data", help="JSON 请求体；@文件 从文件读，@- 从标准输入读")
        p.add_argument("--if-match", type=int, metavar="版本", help="乐观锁版本号")


def register_skills(ops: Any) -> None:
    skills.register(ops)


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
