"""技能审批：查看与处理内部技能的安装申请。"""

from __future__ import annotations

import argparse
from typing import Any

from coreman.admin_cli import output
from coreman.admin_cli.base import group, leaf, read_text
from coreman.admin_cli.client import AdminClient, CliError

APPROVALS = "/api/admin/skill-approvals"


def register(sub: Any) -> None:
    ops = group(sub, "approvals", "技能审批")
    p = leaf(ops, "list", "列出技能审批（默认只看待审）", list_approvals)
    p.add_argument("--all", action="store_true", help="包括已处理的")
    leaf(ops, "show", "查看申请详情", show).add_argument("id", help="审批 id（可只写开头几位）")
    p = leaf(ops, "approve", "批准申请并排队安装", approve)
    p.add_argument("id")
    p.add_argument(
        "--db",
        action="append",
        metavar="组",
        help="批准的数据库范围，可重复；默认批准申请的全部范围",
    )
    p.add_argument(
        "--security-prompt-file", metavar="文件", help="改用这份安全约束；默认沿用申请的"
    )
    p.add_argument("--comment", default="", help="审批意见")
    p = leaf(ops, "reject", "驳回申请", reject)
    p.add_argument("id")
    p.add_argument("--comment", default="", help="驳回原因")


async def find(c: AdminClient, ref: str) -> dict[str, Any]:
    rows = await c.collect(APPROVALS)
    found = [r for r in rows if r["id"] == ref] or [r for r in rows if r["id"].startswith(ref)]
    if not found:
        raise CliError(f"找不到审批：{ref}")
    if len(found) > 1:
        raise CliError(f"审批 id 开头 {ref} 匹配到 {len(found)} 条，请多写几位")
    return found[0]


async def user_names(c: AdminClient, rows: list[dict[str, Any]]) -> dict[str, str]:
    ids = {r[key] for r in rows for key in ("requested_by", "reviewed_by") if r.get(key)}
    names = {}
    for uid in ids:
        try:
            names[uid] = (await c.user(uid))["display_name"]
        except CliError:
            names[uid] = uid
    return names


async def list_approvals(c: AdminClient, args: argparse.Namespace) -> int:
    rows = await c.collect(APPROVALS)
    if not args.all:
        rows = [r for r in rows if r["status"] == "pending"]
    if args.json:
        output.dump(rows)
        return 0
    names = await user_names(c, rows)
    output.table(
        rows,
        [
            ("审批 id", lambda r: r["id"][:8]),
            ("状态", "status"),
            ("AI 员工", "bot_name"),
            ("技能", "skill_name"),
            ("申请库", "requested_databases"),
            ("申请人", lambda r: names.get(r["requested_by"])),
            ("申请时间", "requested_at"),
        ],
        empty="（没有待审申请）" if not args.all else "（没有审批记录）",
    )
    return 0


async def show(c: AdminClient, args: argparse.Namespace) -> int:
    row = await find(c, args.id)
    if args.json:
        output.dump(row)
        return 0
    names = await user_names(c, [row])
    output.fields(
        row,
        [
            ("审批 id", "id"),
            ("状态", "status"),
            ("AI 员工", "bot_name"),
            ("技能", "skill_name"),
            ("申请人", lambda r: names.get(r["requested_by"])),
            ("申请时间", "requested_at"),
            ("申请库", "requested_databases"),
            ("重新拉取代码", "reinstall_code"),
            ("申请的安全约束", "requested_security_prompt"),
            ("批准库", "approved_databases"),
            ("批准的安全约束", "approved_security_prompt"),
            ("审批人", lambda r: names.get(r["reviewed_by"]) if r["reviewed_by"] else None),
            ("审批时间", "reviewed_at"),
            ("审批意见", "review_comment"),
        ],
    )
    return 0


async def review(c: AdminClient, row: dict[str, Any], body: dict[str, Any], json_out: bool) -> None:
    if row["status"] != "pending":
        raise CliError(f"这条申请已是 {row['status']} 状态")
    data = await c.request(
        "POST", f"{APPROVALS}/{row['id']}/review", body=body, version=row["version"]
    )
    if json_out:
        output.dump(data)
        return
    target = f"{row['bot_name']} 的 {row['skill_name']}"
    if body["decision"] == "approve":
        print(f"已批准 {target}，安装任务 {data['task_id']}")
    else:
        print(f"已驳回 {target}")


async def approve(c: AdminClient, args: argparse.Namespace) -> int:
    row = await find(c, args.id)
    databases = row["requested_databases"] if args.db is None else args.db
    body: dict[str, Any] = {
        "decision": "approve",
        "approved_databases": databases,
        "comment": args.comment,
    }
    if args.security_prompt_file:
        body["approved_security_prompt"] = read_text(args.security_prompt_file)
    await review(c, row, body, args.json)
    return 0


async def reject(c: AdminClient, args: argparse.Namespace) -> int:
    row = await find(c, args.id)
    await review(c, row, {"decision": "reject", "comment": args.comment}, args.json)
    return 0
