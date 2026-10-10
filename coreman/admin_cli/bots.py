"""AI 员工：查看、启停、改配置、环境变量、协作者与白名单、技能安装、业务系统授权。"""

from __future__ import annotations

import argparse
from typing import Any

from coreman.admin_cli import output
from coreman.admin_cli.base import (
    add_yes,
    changed,
    group,
    leaf,
    on_off,
    pairs,
    read_text,
    require_yes,
)
from coreman.admin_cli.client import AdminClient, CliError

BOTS = "/api/admin/bots"
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def register(sub: Any) -> None:
    ops = group(sub, "bots", "AI 员工")
    p = leaf(ops, "list", "列出 AI 员工", list_bots)
    p.add_argument("--keyword", help="按名称、标识、描述模糊搜索")
    p.add_argument("--platform", choices=("wecom", "feishu"))
    p.add_argument("--status", choices=("enabled", "disabled"))
    p.add_argument("--team", help="团队 slug 或名称")
    p.add_argument("--mine", action="store_true", help="只看自己创建或协作的")
    leaf(ops, "show", "查看 AI 员工详情", show).add_argument("bot", help="标识、名称或 id")
    leaf(ops, "prompt", "输出系统提示词全文", prompt).add_argument("bot")
    leaf(ops, "enable", "启用 AI 员工", enable).add_argument("bot")
    leaf(ops, "disable", "停用 AI 员工", disable).add_argument("bot")
    p = leaf(ops, "set", "修改 AI 员工配置（只改给出的字段）", set_bot)
    p.add_argument("bot")
    p.add_argument("--name")
    p.add_argument("--description")
    p.add_argument("--model", help="模型名，须是当前运行时支持的")
    p.add_argument("--effort", choices=(*EFFORTS, "default"), help="推理强度；default 为模型默认")
    p.add_argument("--verbosity", type=int, choices=(1, 2, 3, 4), help="详细度 1 极简…4 详细")
    p.add_argument("--team", help="团队 slug 或名称；none 为不属于任何团队")
    p.add_argument("--prompt-file", metavar="文件", help="用文件内容替换系统提示词；- 为标准输入")
    p.add_argument("--welcome", help="欢迎语；空字符串为清除")
    p.add_argument("--rich-cards", type=on_off, metavar="on|off", help="飞书富卡片回复")
    p.add_argument("--timeout", type=int, metavar="秒", help="单次任务超时（1800–43200）")
    p = leaf(ops, "delete", "删除 AI 员工", delete)
    p.add_argument("bot")
    add_yes(p)

    leaf(ops, "env", "列出环境变量（值已脱敏）", env).add_argument("bot")
    p = leaf(ops, "env-set", "新增或修改环境变量", env_set)
    p.add_argument("bot")
    p.add_argument("vars", nargs="+", metavar="KEY=VALUE")
    p = leaf(ops, "env-unset", "删除环境变量", env_unset)
    p.add_argument("bot")
    p.add_argument("keys", nargs="+", metavar="KEY")

    leaf(ops, "members", "列出协作者", members).add_argument("bot")
    for name, handler, text in (
        ("member-add", member_add, "添加协作者"),
        ("member-remove", member_remove, "移除协作者"),
        ("allowed-add", allowed_add, "把成员加入使用白名单"),
        ("allowed-remove", allowed_remove, "把成员移出使用白名单"),
    ):
        p = leaf(ops, name, text, handler)
        p.add_argument("bot")
        p.add_argument("users", nargs="+", metavar="成员", help="登录名、邮箱、姓名或 id")
    leaf(ops, "allowed", "列出使用白名单（空 = 不限制）", allowed).add_argument("bot")
    leaf(ops, "allowed-clear", "清空使用白名单（不再限制）", allowed_clear).add_argument("bot")

    leaf(ops, "skills", "列出已装技能与待审申请", skills).add_argument("bot")
    p = leaf(ops, "skill-install", "安装或重装技能（内部技能会提交审批）", skill_install)
    p.add_argument("bot")
    p.add_argument("skill", help="技能名称或 id")
    p.add_argument("--env-group", action="append", metavar="组", help="申请的数据库/环境组，可重复")
    p.add_argument("--data-source", help="数据源")
    p.add_argument("--var", action="append", metavar="KEY=VALUE", help="技能要求填写的变量，可重复")
    p.add_argument("--security-prompt-file", metavar="文件", help="内部技能的安全约束")
    p.add_argument("--keep-code", action="store_true", help="不重新拉取技能代码，只刷新配置")
    p = leaf(ops, "skill-uninstall", "停用技能", skill_uninstall)
    p.add_argument("bot")
    p.add_argument("skill")

    leaf(ops, "systems", "列出业务系统授权", systems).add_argument("bot")
    p = leaf(ops, "system-grant", "授权业务系统", system_grant)
    p.add_argument("bot")
    p.add_argument("systems", nargs="+", metavar="系统标识")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="同时允许经平台代理执行写操作")
    mode.add_argument("--read-only", action="store_true", help="取消这些系统的写操作权限")
    p.add_argument("--comment", default="", help="授权说明，记入审计日志")
    p = leaf(ops, "system-revoke", "收回业务系统授权", system_revoke)
    p.add_argument("bot")
    p.add_argument("systems", nargs="+", metavar="系统标识")
    p.add_argument("--comment", default="")


LIST_COLUMNS: list[output.Column] = [
    ("标识", "bot_key"),
    ("名称", "name"),
    ("平台", "platform"),
    ("状态", output.enabled),
    ("模型", "model"),
    ("运行时", "relay_name"),
    ("团队", "team_name"),
]


async def list_bots(c: AdminClient, args: argparse.Namespace) -> int:
    team = (await c.team(args.team))["id"] if args.team else None
    enabled = None if args.status is None else args.status == "enabled"
    rows = await c.collect(
        BOTS,
        scope="mine" if args.mine else "all",
        keyword=args.keyword,
        platform=args.platform,
        enabled=enabled,
        team_id=team,
    )
    if args.json:
        output.dump(rows)
    else:
        output.table(rows, LIST_COLUMNS)
    return 0


async def show(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    if args.json:
        output.dump(bot)
        return 0
    output.fields(
        bot,
        [
            ("标识", "bot_key"),
            ("名称", "name"),
            ("id", "id"),
            ("平台", "platform"),
            ("状态", output.enabled),
            ("团队", "team_name"),
            ("创建者", "created_by_name"),
            ("运行时", "relay_name"),
            ("模型", "model"),
            ("推理强度", lambda b: b["effort_level"] or "默认"),
            ("详细度", "verbosity_level"),
            ("工作目录", "working_dir"),
            ("工作目录状态", "workspace_state"),
            ("富卡片回复", "rich_cards"),
            ("任务超时（秒）", "sse_timeout_seconds"),
            ("协作者", "member_count"),
            (
                "使用白名单",
                lambda b: f"{b['allowed_user_count']} 人" if b["allowed_user_count"] else "不限制",
            ),
            ("环境变量", lambda b: sorted(b.get("env_vars") or {})),
            (
                "系统提示词",
                lambda b: f"{len(b['system_prompt'])} 字" if "system_prompt" in b else "无权查看",
            ),
            ("描述", "description"),
            ("更新时间", "updated_at"),
        ],
    )
    return 0


async def prompt(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    if "system_prompt" not in bot:
        raise CliError("没有查看这个 AI 员工提示词的权限")
    print(bot["system_prompt"])
    return 0


async def toggle(c: AdminClient, args: argparse.Namespace, want: bool) -> int:
    bot = await c.bot(args.bot)
    word = "启用" if want else "停用"
    if bot["enabled"] == want:
        print(f"{bot['name']} 已经是{word}状态")
        return 0
    data = await c.request("POST", f"{BOTS}/{bot['id']}/toggle")
    if args.json:
        output.dump(data)
    else:
        print(f"已{word} {data['name']}（{data['bot_key']}）")
    return 0


async def enable(c: AdminClient, args: argparse.Namespace) -> int:
    return await toggle(c, args, True)


async def disable(c: AdminClient, args: argparse.Namespace) -> int:
    return await toggle(c, args, False)


async def patch(c: AdminClient, bot: dict[str, Any], body: dict[str, Any], json_out: bool) -> int:
    data = await c.request("PATCH", f"{BOTS}/{bot['id']}", body=body, version=bot["version"])
    if json_out:
        output.dump(data)
    else:
        print(f"已更新 {data['name']}（{data['bot_key']}）：{'、'.join(body)}")
    return 0


async def set_bot(c: AdminClient, args: argparse.Namespace) -> int:
    body = changed(
        args,
        {
            "name": "name",
            "description": "description",
            "model": "model",
            "verbosity": "verbosity_level",
            "rich_cards": "rich_cards",
            "timeout": "sse_timeout_seconds",
        },
    )
    if args.effort is not None:
        body["effort_level"] = None if args.effort == "default" else args.effort
    if args.welcome is not None:
        body["welcome_message"] = args.welcome or None
    if args.prompt_file is not None:
        body["system_prompt"] = read_text(args.prompt_file)
    if args.team is not None:
        body["team_id"] = None if args.team == "none" else (await c.team(args.team))["id"]
    if not body:
        raise CliError("没有要修改的字段，见 --help")
    return await patch(c, await c.bot(args.bot), body, args.json)


async def delete(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    require_yes(args, f"删除 AI 员工 {bot['name']}（{bot['bot_key']}）")
    await c.request("DELETE", f"{BOTS}/{bot['id']}")
    print(f"已删除 {bot['name']}（{bot['bot_key']}）")
    return 0


def env_of(bot: dict[str, Any]) -> dict[str, str]:
    if "env_vars" not in bot:
        raise CliError("没有查看这个 AI 员工环境变量的权限")
    return dict(bot["env_vars"])


async def env(c: AdminClient, args: argparse.Namespace) -> int:
    values = env_of(await c.bot(args.bot))
    if args.json:
        output.dump(values)
    else:
        output.table(
            [{"k": k, "v": v} for k, v in sorted(values.items())],
            [("变量", "k"), ("值（脱敏）", "v")],
        )
    return 0


async def env_set(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    # 其余变量回传脱敏值，服务端按「脱敏 = 不变」保留原值。
    values = {**env_of(bot), **pairs(args.vars)}
    return await patch(c, bot, {"env_vars": values}, args.json)


async def env_unset(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    values = env_of(bot)
    missing = [k for k in args.keys if k not in values]
    if missing:
        raise CliError(f"没有这些环境变量：{'、'.join(missing)}")
    return await patch(
        c, bot, {"env_vars": {k: v for k, v in values.items() if k not in args.keys}}, args.json
    )


USER_COLUMNS: list[output.Column] = [("登录名", "login_name"), ("姓名", "display_name")]


async def members(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    rows = await c.get(f"{BOTS}/{bot['id']}/members")
    if args.json:
        output.dump(rows)
    else:
        print(f"创建者：{bot['created_by_name']}")
        output.table(rows, [*USER_COLUMNS, ("加入时间", "added_at")], empty="（没有协作者）")
    return 0


async def member_add(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    for ref in args.users:
        user = await c.user(ref)
        await c.request("POST", f"{BOTS}/{bot['id']}/members", body={"user_id": user["id"]})
        print(f"已把 {user['display_name']}（{user['login_name']}）加为 {bot['name']} 的协作者")
    return 0


async def member_remove(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    for ref in args.users:
        user = await c.user(ref)
        await c.request("DELETE", f"{BOTS}/{bot['id']}/members/{user['id']}")
        print(f"已把 {user['display_name']}（{user['login_name']}）移出 {bot['name']} 的协作者")
    return 0


async def allowed(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    rows = await c.get(f"{BOTS}/{bot['id']}/allowed-users")
    if args.json:
        output.dump(rows)
    else:
        output.table(rows, USER_COLUMNS, empty="（白名单为空：所有人都能使用）")
    return 0


async def replace_allowed(c: AdminClient, bot: dict[str, Any], ids: list[str]) -> None:
    rows = await c.request("PUT", f"{BOTS}/{bot['id']}/allowed-users", body={"user_ids": ids})
    names = "、".join(r["display_name"] for r in rows) if rows else "空（所有人都能使用）"
    print(f"{bot['name']} 的使用白名单现为：{names}")


async def allowed_add(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    current = [r["user_id"] for r in await c.get(f"{BOTS}/{bot['id']}/allowed-users")]
    added = [(await c.user(ref))["id"] for ref in args.users]
    await replace_allowed(c, bot, list(dict.fromkeys([*current, *added])))
    return 0


async def allowed_remove(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    current = [r["user_id"] for r in await c.get(f"{BOTS}/{bot['id']}/allowed-users")]
    removed = {(await c.user(ref))["id"] for ref in args.users}
    if not removed & set(current):
        raise CliError("这些成员都不在白名单里")
    remaining = [uid for uid in current if uid not in removed]
    if not remaining:
        # 移空 = 不再限制，等于对所有人开放，这种放宽要明说。
        raise CliError("移除后白名单为空，会变成所有人都能使用；确实要这样请用 allowed-clear")
    await replace_allowed(c, bot, remaining)
    return 0


async def allowed_clear(c: AdminClient, args: argparse.Namespace) -> int:
    await replace_allowed(c, await c.bot(args.bot), [])
    return 0


async def bot_skills(c: AdminClient, bot: dict[str, Any]) -> dict[str, Any]:
    return await c.get(f"/api/admin/bots/{bot['id']}/skills")  # type: ignore[no-any-return]


async def skills(c: AdminClient, args: argparse.Namespace) -> int:
    data = await bot_skills(c, await c.bot(args.bot))
    if args.json:
        output.dump(data)
        return 0
    output.table(
        data["items"],
        [
            ("技能", "name"),
            ("状态", "status"),
            ("版本", "version"),
            ("数据库", lambda r: r["approved_databases"] or r["selected_env_groups"]),
            ("安装时间", "installed_at"),
            ("错误", "error_message"),
        ],
        empty="（没有安装技能）",
    )
    if data["pending_approvals"]:
        print("\n待审申请：")
        output.table(
            data["pending_approvals"],
            [("审批 id", "id"), ("申请库", "requested_databases"), ("申请时间", "requested_at")],
        )
    return 0


async def skill_install(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    skill = await c.skill(args.skill)
    body: dict[str, Any] = {
        "selected_env_groups": args.env_group or [],
        "data_source": args.data_source,
        "reinstall_code": not args.keep_code,
    }
    if args.var:
        body["user_env_vars"] = pairs(args.var)
    if args.security_prompt_file:
        body["requested_security_prompt"] = read_text(args.security_prompt_file)
    data = await c.request(
        "POST", f"/api/admin/bots/{bot['id']}/skills/{skill['id']}/install", body=body
    )
    if args.json:
        output.dump(data)
    elif data["status"] == "pending_approval":
        print(f"{skill['name']} 是内部技能，已提交审批（审批 id {data['approval']['id']}）")
    else:
        print(f"已为 {bot['name']} 排队安装 {skill['name']}（任务 {data['task_id']}）")
    return 0


async def skill_uninstall(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    skill = await c.skill(args.skill)
    rows = [
        r for r in (await bot_skills(c, bot))["items"] if str(r["skill_id"]) == str(skill["id"])
    ]
    if not rows:
        raise CliError(f"{bot['name']} 没有安装 {skill['name']}")
    await c.request(
        "DELETE", f"/api/admin/bots/{bot['id']}/skills/{skill['id']}", version=rows[0]["revision"]
    )
    print(f"已停用 {bot['name']} 的技能 {skill['name']}")
    return 0


async def grants(c: AdminClient, bot: dict[str, Any]) -> dict[str, Any]:
    return await c.get(f"/api/admin/bots/{bot['id']}/system-grants")  # type: ignore[no-any-return]


async def systems(c: AdminClient, args: argparse.Namespace) -> int:
    data = await grants(c, await c.bot(args.bot))
    if args.json:
        output.dump(data)
    else:
        rows = [{"key": k, "write": k in data["write_keys"]} for k in data["system_keys"]]
        output.table(
            rows, [("系统", "key"), ("允许写入", "write")], empty="（没有授权任何业务系统）"
        )
    return 0


async def put_grants(
    c: AdminClient,
    bot: dict[str, Any],
    current: dict[str, Any],
    keys: list[str],
    writes: list[str] | None,
    comment: str,
) -> None:
    body = {"system_keys": keys, "write_keys": writes, "comment": comment}
    data = await c.request(
        "PUT", f"/api/admin/bots/{bot['id']}/system-grants", body=body, version=current["version"]
    )
    shown = [f"{k}（可写）" if k in data["write_keys"] else k for k in data["system_keys"]]
    print(f"{bot['name']} 的业务系统授权现为：{'、'.join(shown) or '无'}")


async def system_grant(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    current = await grants(c, bot)
    keys = sorted({*current["system_keys"], *args.systems})
    writes = None  # 省略时服务端保留原有的写权限
    if args.write:
        writes = sorted({*current["write_keys"], *args.systems})
    elif args.read_only:
        writes = sorted(set(current["write_keys"]) - set(args.systems))
    await put_grants(c, bot, current, keys, writes, args.comment)
    return 0


async def system_revoke(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    current = await grants(c, bot)
    missing = sorted(set(args.systems) - set(current["system_keys"]))
    if missing:
        raise CliError(f"没有授权过这些系统：{'、'.join(missing)}")
    keys = [k for k in current["system_keys"] if k not in args.systems]
    writes = [k for k in current["write_keys"] if k in keys]
    await put_grants(c, bot, current, keys, writes, args.comment)
    return 0
