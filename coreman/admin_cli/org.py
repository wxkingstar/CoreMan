"""团队与用户：团队增删改与部门归属规则，成员的团队、角色与启停。"""

from __future__ import annotations

import argparse
from typing import Any

from coreman.admin_cli import output
from coreman.admin_cli.base import add_yes, changed, group, leaf, on_off, require_yes
from coreman.admin_cli.client import AdminClient, CliError

TEAMS = "/api/admin/teams"
USERS = "/api/admin/users"
TEAM_FIELDS = ("slug", "name_zh", "name_ja", "name_en", "sort_order", "enabled")
TEAM_OPTIONS = {"name": "name_zh", "name_ja": "name_ja", "name_en": "name_en", "sort": "sort_order"}
ROLES = ("platform_admin", "ai_committee", "team_lead", "member")

USER_COLUMNS: list[output.Column] = [
    ("登录名", "login_name"),
    ("姓名", "display_name"),
    ("角色", "role"),
    ("团队", "team_name"),
    ("状态", "status"),
    ("邮箱", "email"),
]


def register(sub: Any) -> None:
    ops = group(sub, "teams", "团队")
    leaf(ops, "list", "列出团队", list_teams)
    leaf(ops, "show", "查看团队与归属规则", show_team).add_argument("team", help="slug、名称或 id")
    p = leaf(ops, "create", "新建团队", create_team)
    p.add_argument("slug", help="小写字母数字、- 或 _")
    p.add_argument("--name", required=True, help="中文名")
    p.add_argument("--name-ja")
    p.add_argument("--name-en")
    p.add_argument("--sort", type=int, default=0)
    p = leaf(ops, "set", "修改团队（只改给出的字段）", set_team)
    p.add_argument("team")
    p.add_argument("--slug")
    p.add_argument("--name", help="中文名")
    p.add_argument("--name-ja")
    p.add_argument("--name-en")
    p.add_argument("--sort", type=int)
    p.add_argument("--enabled", type=on_off, metavar="on|off")
    p = leaf(ops, "delete", "删除团队（须先移走成员、AI 员工与运行时）", delete_team)
    p.add_argument("team")
    add_yes(p)
    leaf(ops, "members", "列出团队成员", team_members).add_argument("team")
    p = leaf(
        ops, "rule-add", "加一条部门归属规则：部门路径包含该文本的成员同步时归入本团队", rule_add
    )
    p.add_argument("team")
    p.add_argument("pattern", help="部门路径包含的文本")
    p.add_argument("--platform", choices=("wecom", "feishu"), help="只匹配这个平台的部门")
    p.add_argument("--sort", type=int, default=0)
    p = leaf(ops, "rule-remove", "删除部门归属规则", rule_remove)
    p.add_argument("team")
    p.add_argument("pattern")

    ops = group(sub, "users", "用户")
    p = leaf(ops, "list", "列出用户", list_users)
    p.add_argument("--keyword", help="按姓名、登录名、邮箱模糊搜索")
    p.add_argument("--team", help="团队 slug 或名称")
    p.add_argument("--unassigned", action="store_true", help="只看未归属团队的")
    p.add_argument("--role", choices=ROLES)
    p.add_argument("--status", choices=("active", "disabled"))
    leaf(ops, "show", "查看用户详情", show_user).add_argument(
        "user", help="登录名、邮箱、姓名或 id"
    )
    p = leaf(ops, "set", "修改用户（只改给出的字段；改过的字段通讯录同步不再覆盖）", set_user)
    p.add_argument("user")
    p.add_argument("--team", help="团队 slug 或名称；none 为移出团队")
    p.add_argument("--role", choices=ROLES)
    p.add_argument("--locale", choices=("zh", "ja", "en"))
    p.add_argument("--bot-accessible", type=on_off, metavar="on|off", help="能否使用 AI 员工")
    p.add_argument("--position", help="职位；空字符串为清除")
    p.add_argument("--skills", help="擅长领域；空字符串为清除")
    leaf(ops, "enable", "启用用户（允许登录）", enable_user).add_argument("user")
    leaf(ops, "disable", "停用用户（禁止登录）", disable_user).add_argument("user")


def team_name(t: dict[str, Any]) -> str:
    return f"{t['slug']}（{t['name_zh']}）"


async def list_teams(c: AdminClient, args: argparse.Namespace) -> int:
    rows = await c.get(TEAMS)
    if args.json:
        output.dump(rows)
    else:
        output.table(
            rows,
            [
                ("slug", "slug"),
                ("名称", "name_zh"),
                ("状态", output.enabled),
                ("成员数", "member_count"),
                ("归属规则", lambda t: len(t["rules"])),
            ],
        )
    return 0


def print_rules(team: dict[str, Any]) -> None:
    output.table(
        team["rules"],
        [
            ("部门路径包含", "dept_path_contains"),
            ("平台", lambda r: r["platform"] or "全部"),
            ("排序", "sort_order"),
        ],
        empty="（没有归属规则）",
    )


async def show_team(c: AdminClient, args: argparse.Namespace) -> int:
    team = await c.team(args.team)
    if args.json:
        output.dump(team)
        return 0
    output.fields(
        team,
        [
            ("slug", "slug"),
            ("中文名", "name_zh"),
            ("日文名", "name_ja"),
            ("英文名", "name_en"),
            ("id", "id"),
            ("状态", output.enabled),
            ("排序", "sort_order"),
            ("成员数", "member_count"),
        ],
    )
    print("\n归属规则：")
    print_rules(team)
    return 0


async def create_team(c: AdminClient, args: argparse.Namespace) -> int:
    body = {"slug": args.slug, **changed(args, TEAM_OPTIONS), "enabled": True}
    data = await c.request("POST", TEAMS, body=body)
    if args.json:
        output.dump(data)
    else:
        print(f"已新建团队 {team_name(data)}")
    return 0


async def set_team(c: AdminClient, args: argparse.Namespace) -> int:
    updates = changed(args, {**TEAM_OPTIONS, "slug": "slug", "enabled": "enabled"})
    if not updates:
        raise CliError("没有要修改的字段，见 --help")
    team = await c.team(args.team)
    body = {key: team[key] for key in TEAM_FIELDS} | updates
    data = await c.request("PUT", f"{TEAMS}/{team['id']}", body=body)
    if args.json:
        output.dump(data)
    else:
        print(f"已更新团队 {team_name(data)}：{'、'.join(updates)}")
    return 0


async def delete_team(c: AdminClient, args: argparse.Namespace) -> int:
    team = await c.team(args.team)
    require_yes(args, f"删除团队 {team_name(team)}")
    await c.request("DELETE", f"{TEAMS}/{team['id']}")
    print(f"已删除团队 {team_name(team)}")
    return 0


async def team_members(c: AdminClient, args: argparse.Namespace) -> int:
    team = await c.team(args.team)
    rows = await c.collect(USERS, team_id=team["id"])
    if args.json:
        output.dump(rows)
    else:
        output.table(rows, USER_COLUMNS)
    return 0


def rule_body(rule: dict[str, Any]) -> dict[str, Any]:
    return {k: rule[k] for k in ("platform", "dept_path_contains", "sort_order")}


async def save_rules(
    c: AdminClient, args: argparse.Namespace, team: dict[str, Any], rules: list[dict[str, Any]]
) -> None:
    data = await c.request("PUT", f"{TEAMS}/{team['id']}/rules", body=rules)
    if args.json:
        output.dump(data)
    else:
        print(f"{team_name(data)} 的归属规则：")
        print_rules(data)


async def rule_add(c: AdminClient, args: argparse.Namespace) -> int:
    team = await c.team(args.team)
    rule = {"platform": args.platform, "dept_path_contains": args.pattern, "sort_order": args.sort}
    current = [rule_body(r) for r in team["rules"]]
    if any(
        r["dept_path_contains"] == args.pattern and r["platform"] == args.platform for r in current
    ):
        raise CliError("已有这条规则")
    await save_rules(c, args, team, [*current, rule])
    return 0


async def rule_remove(c: AdminClient, args: argparse.Namespace) -> int:
    team = await c.team(args.team)
    current = [rule_body(r) for r in team["rules"]]
    remaining = [r for r in current if r["dept_path_contains"] != args.pattern]
    if len(remaining) == len(current):
        raise CliError(f"没有部门路径包含「{args.pattern}」的规则")
    await save_rules(c, args, team, remaining)
    return 0


async def list_users(c: AdminClient, args: argparse.Namespace) -> int:
    team = (await c.team(args.team))["id"] if args.team else None
    rows = await c.collect(
        USERS,
        keyword=args.keyword,
        team_id=team,
        unassigned=args.unassigned or None,
        role=args.role,
        status=args.status,
    )
    if args.json:
        output.dump(rows)
    else:
        output.table(rows, USER_COLUMNS)
    return 0


async def show_user(c: AdminClient, args: argparse.Namespace) -> int:
    user = await c.user(args.user)
    if args.json:
        output.dump(user)
        return 0
    output.fields(
        user,
        [
            ("登录名", "login_name"),
            ("姓名", "display_name"),
            ("id", "id"),
            ("状态", "status"),
            ("角色", "role"),
            ("团队", "team_name"),
            ("邮箱", "email"),
            ("手机", "mobile"),
            ("职位", "position"),
            ("擅长领域", "skills"),
            ("能用 AI 员工", "bot_accessible"),
            ("语言", "locale"),
            ("来源", "source"),
            ("部门", "departments"),
            ("平台身份", lambda u: [i["platform"] for i in u.get("identities") or []]),
            ("手工维护字段", "manual_fields"),
            ("最近登录", "last_login_at"),
        ],
    )
    return 0


async def patch_user(c: AdminClient, args: argparse.Namespace, body: dict[str, Any]) -> int:
    user = await c.user(args.user)
    data = await c.request("PATCH", f"{USERS}/{user['id']}", body=body)
    if args.json:
        output.dump(data)
    else:
        print(f"已更新 {data['display_name']}（{data['login_name']}）：{'、'.join(body)}")
    return 0


async def set_user(c: AdminClient, args: argparse.Namespace) -> int:
    body = changed(args, {"role": "role", "locale": "locale", "bot_accessible": "bot_accessible"})
    for key in ("position", "skills"):
        value = getattr(args, key)
        if value is not None:
            body[key] = value or None
    if args.team is not None:
        body["team_id"] = None if args.team == "none" else (await c.team(args.team))["id"]
    if not body:
        raise CliError("没有要修改的字段，见 --help")
    return await patch_user(c, args, body)


async def enable_user(c: AdminClient, args: argparse.Namespace) -> int:
    return await patch_user(c, args, {"status": "active"})


async def disable_user(c: AdminClient, args: argparse.Namespace) -> int:
    return await patch_user(c, args, {"status": "disabled"})
