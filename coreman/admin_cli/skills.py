"""技能管理：技能目录、来源同步、批量升级与环境组预设。"""

from __future__ import annotations

import argparse
from typing import Any

from coreman.admin_cli import output
from coreman.admin_cli.base import add_yes, changed, leaf, pairs, read_text, require_yes
from coreman.admin_cli.client import AdminClient, CliError

SKILLS = "/api/admin/skills"
SOURCES = "/api/admin/skill-sources"
PRESETS = "/api/admin/env-presets"
# PUT /skills/{id} 是整份替换：取回现值、改动给出的字段、原样回传其余字段。
# mcp_config 不回传：缺省时服务端保留原有的加密配置。
SKILL_FIELDS = (
    "name",
    "source_id",
    "description",
    "category",
    "security_level",
    "version",
    "env_groups",
    "selectable_env_groups",
    "data_sources",
    "default_data_source",
    "doris_enabled_groups",
    "user_env_vars",
    "install_type",
    "external_repo_url",
    "security_prompt_template",
    "enabled",
)


def register(ops: Any) -> None:
    """挂到已有的 `skills` 命令下（与 `skills sync` 并列）。"""
    p = leaf(ops, "list", "列出技能目录", list_skills)
    p.add_argument("--status", choices=("enabled", "disabled"))
    leaf(ops, "show", "查看技能详情", show).add_argument("skill", help="技能名称或 id")
    leaf(ops, "enable", "在目录中启用技能", enable).add_argument("skill")
    leaf(ops, "disable", "在目录中停用技能", disable).add_argument("skill")
    p = leaf(ops, "set", "修改技能目录信息（只改给出的字段）", set_skill)
    p.add_argument("skill")
    p.add_argument("--description")
    p.add_argument("--category")
    p.add_argument("--security-level", choices=("public", "internal"), help="internal 安装需审批")
    p.add_argument("--version", dest="skill_version")
    p.add_argument("--repo", help="技能自己的 Git 仓库地址（覆盖来源仓库）")
    p.add_argument("--security-prompt-file", metavar="文件", help="内部技能的安全约束模板")
    leaf(ops, "upgrade", "为已安装该技能的所有 AI 员工重装技能", upgrade).add_argument("skill")
    p = leaf(ops, "delete", "从目录删除技能", delete)
    p.add_argument("skill")
    add_yes(p)
    leaf(ops, "sources", "列出技能来源", sources)
    leaf(ops, "source-sync", "从来源仓库同步技能目录", source_sync).add_argument(
        "source", help="来源标识"
    )
    leaf(ops, "presets", "列出环境组预设（值已脱敏）", presets)
    p = leaf(ops, "preset-set", "新建或修改环境组预设", preset_set)
    p.add_argument("group", help="环境组标识")
    p.add_argument("--label", help="显示名称（新建时必填）")
    p.add_argument("--var", action="append", metavar="KEY=VALUE", help="新增或修改变量，可重复")
    p.add_argument("--unset", action="append", metavar="KEY", help="删除变量，可重复")
    p.add_argument("--tag", action="append", metavar="标签", help="替换全部标签，可重复")


async def source_keys(c: AdminClient) -> dict[str, str]:
    return {str(s["id"]): s["key"] for s in await c.get(SOURCES)}


async def list_skills(c: AdminClient, args: argparse.Namespace) -> int:
    rows = await c.collect(SKILLS)
    if args.status:
        rows = [r for r in rows if r["enabled"] == (args.status == "enabled")]
    if args.json:
        output.dump(rows)
        return 0
    keys = await source_keys(c)
    output.table(
        rows,
        [
            ("名称", "name"),
            ("状态", output.enabled),
            ("安全级别", "security_level"),
            ("安装方式", "install_type"),
            ("分类", "category"),
            ("版本", "version"),
            ("来源", lambda r: keys.get(str(r["source_id"]))),
        ],
    )
    return 0


async def show(c: AdminClient, args: argparse.Namespace) -> int:
    skill = await c.skill(args.skill)
    if args.json:
        output.dump(skill)
        return 0
    keys = await source_keys(c)
    output.fields(
        skill,
        [
            ("名称", "name"),
            ("id", "id"),
            ("状态", output.enabled),
            ("来源", lambda s: keys.get(str(s["source_id"]))),
            ("安全级别", "security_level"),
            ("安装方式", "install_type"),
            ("分类", "category"),
            ("版本", "version"),
            ("外部仓库", "external_repo_url"),
            ("固定环境组", "env_groups"),
            ("可选数据库", "selectable_env_groups"),
            ("数据源", "data_sources"),
            ("默认数据源", "default_data_source"),
            ("需填写的变量", lambda s: sorted(s["user_env_vars"] or {})),
            ("安全约束模板", "security_prompt_template"),
            ("描述", "description"),
        ],
    )
    return 0


async def set_status(c: AdminClient, args: argparse.Namespace, want: bool) -> int:
    skill = await c.skill(args.skill)
    word = "启用" if want else "停用"
    if skill["enabled"] == want:
        print(f"{skill['name']} 已经是{word}状态")
        return 0
    data = await c.request(
        "PATCH", f"{SKILLS}/{skill['id']}", body={"enabled": want}, version=skill["revision"]
    )
    if args.json:
        output.dump(data)
    else:
        print(f"已{word}技能 {skill['name']}（待审申请与排队中的安装须重新申请）")
    return 0


async def enable(c: AdminClient, args: argparse.Namespace) -> int:
    return await set_status(c, args, True)


async def disable(c: AdminClient, args: argparse.Namespace) -> int:
    return await set_status(c, args, False)


async def set_skill(c: AdminClient, args: argparse.Namespace) -> int:
    updates = changed(
        args,
        {
            "description": "description",
            "category": "category",
            "security_level": "security_level",
            "skill_version": "version",
            "repo": "external_repo_url",
        },
    )
    if args.security_prompt_file is not None:
        updates["security_prompt_template"] = read_text(args.security_prompt_file)
    if not updates:
        raise CliError("没有要修改的字段，见 --help")
    skill = await c.skill(args.skill)
    body = {key: skill[key] for key in SKILL_FIELDS} | updates
    data = await c.request("PUT", f"{SKILLS}/{skill['id']}", body=body, version=skill["revision"])
    if args.json:
        output.dump(data)
    else:
        print(f"已更新技能 {skill['name']}：{'、'.join(updates)}")
    return 0


async def upgrade(c: AdminClient, args: argparse.Namespace) -> int:
    skill = await c.skill(args.skill)
    data = await c.request("POST", f"{SKILLS}/{skill['id']}/upgrade")
    if args.json:
        output.dump(data)
        return 0
    print(f"{skill['name']}：已排队 {len(data['queued'])} 个，跳过 {len(data['skipped'])} 个")
    output.table(data["queued"], [("AI 员工", "bot_name"), ("任务", "task_id")], empty="")
    if data["skipped"]:
        print("\n跳过：")
        output.table(data["skipped"], [("AI 员工", "bot_name"), ("原因", "reason")])
    return 0


async def delete(c: AdminClient, args: argparse.Namespace) -> int:
    skill = await c.skill(args.skill)
    require_yes(args, f"从目录删除技能 {skill['name']}")
    await c.request("DELETE", f"{SKILLS}/{skill['id']}", version=skill["revision"])
    print(f"已从目录删除技能 {skill['name']}")
    return 0


async def sources(c: AdminClient, args: argparse.Namespace) -> int:
    rows = await c.get(SOURCES)
    if args.json:
        output.dump(rows)
    else:
        output.table(
            rows,
            [
                ("标识", "key"),
                ("名称", "label"),
                ("仓库", "git_url"),
                ("访问令牌", "has_access_token"),
            ],
        )
    return 0


async def source_sync(c: AdminClient, args: argparse.Namespace) -> int:
    rows = [s for s in await c.get(SOURCES) if s["key"] == args.source]
    if not rows:
        raise CliError(f"找不到技能来源：{args.source}")
    source = rows[0]
    data = await c.request("POST", f"{SOURCES}/{source['id']}/sync", version=source["version"])
    if args.json:
        output.dump(data)
    else:
        print(f"已同步来源 {source['key']}：")
        output.fields(data, [(key, key) for key in data])
    return 0


async def presets(c: AdminClient, args: argparse.Namespace) -> int:
    rows = await c.get(PRESETS)
    if args.json:
        output.dump(rows)
    else:
        output.table(
            rows,
            [
                ("环境组", "group_key"),
                ("名称", "label"),
                ("标签", "tags"),
                ("变量", lambda r: sorted(r["vars"])),
            ],
        )
    return 0


async def preset_set(c: AdminClient, args: argparse.Namespace) -> int:
    current = next((p for p in await c.get(PRESETS) if p["group_key"] == args.group), None)
    if current is None and not args.label:
        raise CliError(f"新建环境组 {args.group} 需要 --label")
    # 其余变量回传脱敏值，服务端按「脱敏 = 不变」保留原值。
    values = dict(current["vars"]) if current else {}
    missing = [k for k in args.unset or [] if k not in values]
    if missing:
        raise CliError(f"没有这些变量：{'、'.join(missing)}")
    for key in args.unset or []:
        del values[key]
    values.update(pairs(args.var))
    body = {
        "group_key": args.group,
        "label": args.label or current["label"],  # type: ignore[index]
        "vars": values,
        "tags": args.tag if args.tag is not None else (current["tags"] if current else []),
    }
    data = await c.request(
        "PUT", f"{PRESETS}/{args.group}", body=body, version=current["version"] if current else 0
    )
    if args.json:
        output.dump(data)
    else:
        print(f"已保存环境组 {args.group}：{'、'.join(sorted(data['vars'])) or '无变量'}")
    return 0
