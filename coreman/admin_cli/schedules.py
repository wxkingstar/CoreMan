"""定时任务、公告、我的提醒与「我的」个人设置（个人凭证、飞书个人授权、企业微信绑定）。"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
import time
from typing import Any

from coreman.admin_cli import output
from coreman.admin_cli.base import add_yes, changed, group, leaf, on_off, read_text, require_yes
from coreman.admin_cli.client import AdminClient, CliError, one

CRON = "/api/admin/cron-jobs"
ANNOUNCEMENTS = "/api/admin/announcements"
REMINDERS = "/api/self-reminders"
ME = "/api/me"
# PUT /cron-jobs/{id} 是整份替换：取回现值、改动给出的字段、原样回传其余字段。
# Webhook 地址不在其中：不传表示保留已保存的地址。
CRON_FIELDS = (
    "bot_id",
    "name",
    "cron_expression",
    "schedule_kind",
    "run_at",
    "timezone",
    "prompt",
    "system_prompt",
    "precheck_script",
    "precheck_timeout_seconds",
    "enabled",
    "expires_at",
    "target_users",
    "target_chats",
    "notify_emails",
    "notify_webhook",
)
CRON_OPTIONS = {
    "name": "name",
    "timezone": "timezone",
    "precheck_timeout": "precheck_timeout_seconds",
    "webhook": "notify_webhook",
    "to_chat": "target_chats",
    "email": "notify_emails",
}
ANNOUNCEMENT_FIELDS = (
    "scope",
    "relay_server_id",
    "bot_id",
    "content",
    "is_active",
    "start_at",
    "end_at",
)
WECOM_LEVELS = ("readonly", "all_except_send", "all")


def register(sub: Any) -> None:
    register_cron(sub)
    register_announcements(sub)
    register_reminders(sub)
    register_me(sub)


# ---- 定时任务 ----


def cron_options(p: argparse.ArgumentParser, *, create: bool) -> None:
    if create:
        p.add_argument("--bot", help="AI 员工：标识、名称或 id")
        p.add_argument(
            "--data", help="JSON 请求体打底（字段同接口），命令行上给出的字段覆盖其中同名字段"
        )
        p.add_argument("--file", metavar="文件", help="从文件读 JSON 请求体打底；- 为标准输入")
        p.add_argument("--disabled", action="store_true", help="建成停用状态")
    p.add_argument("--name", help="任务名称")
    p.add_argument("--cron", metavar="表达式", help="周期执行的 cron 表达式，如 '0 9 * * 1-5'")
    p.add_argument(
        "--at", metavar="时间", help="改为一次性执行：带时区的时间，如 2026-12-01T09:00:00+08:00"
    )
    p.add_argument("--timezone", help="时区，默认 Asia/Shanghai")
    p.add_argument("--prompt", help="执行指令")
    p.add_argument("--prompt-file", metavar="文件", help="从文件读执行指令；- 为标准输入")
    p.add_argument("--system-prompt-file", metavar="文件", help="附加系统提示词；空文件为清除")
    p.add_argument("--precheck-file", metavar="文件", help="预检脚本；空文件为清除")
    p.add_argument("--precheck-timeout", type=int, metavar="秒", help="预检超时（5–120）")
    p.add_argument("--expires", metavar="时间", help="到期时间（带时区）；空字符串为清除")
    p.add_argument(
        "--to-user",
        action="append",
        metavar="成员",
        help="私聊通知的接收人，可重复；给出时整体替换原名单",
    )
    p.add_argument("--to-chat", action="append", metavar="群 id", help="通知的群，可重复；同上")
    p.add_argument("--email", action="append", metavar="邮箱", help="邮件通知地址，可重复；同上")
    p.add_argument(
        "--clear-targets", action="store_true", help="清空接收人、群和邮箱（默认只通知创建者）"
    )
    p.add_argument("--webhook", type=on_off, metavar="on|off", help="企微群机器人通知开关")
    p.add_argument(
        "--webhook-url",
        metavar="地址",
        help="企微群机器人 Webhook 地址（同时打开开关）；空字符串为清除",
    )


def register_cron(sub: Any) -> None:
    ops = group(sub, "cron", "定时任务")
    p = leaf(ops, "list", "列出自己创建或协作的 AI 员工的定时任务", list_jobs)
    p.add_argument("--bot", help="只看这个 AI 员工的")
    leaf(ops, "show", "查看定时任务详情", show_job).add_argument(
        "job", help="任务 id（可只写开头几位）或名称"
    )
    p = leaf(ops, "runs", "查看执行记录（新的在前）", runs)
    p.add_argument("job")
    p.add_argument("--limit", type=int, default=20, help="最多显示几条，默认 20")
    p = leaf(ops, "create", "新建定时任务", create_job)
    cron_options(p, create=True)
    p = leaf(ops, "set", "修改定时任务（只改给出的字段；只有创建者能改）", set_job)
    p.add_argument("job")
    cron_options(p, create=False)
    leaf(ops, "enable", "启用定时任务（只有创建者能启用）", enable_job).add_argument("job")
    leaf(
        ops, "disable", "停用定时任务（成员本人的私聊定时任务也能停用）", disable_job
    ).add_argument("job")
    leaf(ops, "run", "立即执行一次", run_job).add_argument("job")
    leaf(ops, "cancel-run", "取消正在执行或等待触发的这一轮", cancel_run).add_argument("job")
    p = leaf(ops, "delete", "删除定时任务（只有创建者能删）", delete_job)
    p.add_argument("job")
    add_yes(p)
    leaf(
        ops, "notification-chats", "列出 AI 员工待过的群（可作通知群）", notification_chats
    ).add_argument("bot", help="标识、名称或 id")
    leaf(
        ops, "test-notification", "按已保存的通知设置发一条测试通知", test_notification
    ).add_argument("job")
    leaf(ops, "notification-tests", "查看测试通知的投递结果", notification_tests).add_argument(
        "job"
    )
    p = leaf(ops, "precheck-test", "试跑一段预检脚本", precheck_test)
    p.add_argument("script", metavar="文件", help="预检脚本文件；- 为标准输入")
    p.add_argument("--ctx", help="传给脚本的 ctx，JSON 对象")


async def user_names(c: AdminClient, ids: set[str]) -> dict[str, str]:
    names = {}
    for uid in ids:
        try:
            user = await c.user(uid)
            names[uid] = f"{user['login_name']}（{user['display_name']}）"
        except CliError:
            names[uid] = uid
    return names


async def bot_names(c: AdminClient) -> dict[str, str]:
    rows = await c.collect("/api/admin/bots", scope="all")
    return {b["id"]: f"{b['bot_key']}（{b['name']}）" for b in rows}


def by_ref(rows: list[dict[str, Any]], ref: str, kind: str, label: Any) -> dict[str, Any]:
    """按完整 id、id 开头几位或名称找一行。"""
    found = (
        [r for r in rows if r["id"] == ref]
        or [r for r in rows if r["id"].startswith(ref)]
        or [r for r in rows if r.get("name") == ref]
    )
    return one(found, ref, kind, label)


async def job(c: AdminClient, ref: str) -> dict[str, Any]:
    rows = await c.collect(CRON)
    return by_ref(rows, ref, "定时任务", lambda r: f"{r['id'][:8]}（{r['name']}）")


def schedule_text(row: dict[str, Any]) -> str:
    if row["schedule_kind"] == "once":
        return f"一次 {row['run_at']}"
    return f"{row['cron_expression']}（{row['timezone']}）"


def job_status(row: dict[str, Any]) -> str:
    if row.get("running_task_id"):
        return "执行中"
    if row.get("force_run_at"):
        return "等待立即执行"
    return output.enabled(row)


async def list_jobs(c: AdminClient, args: argparse.Namespace) -> int:
    bot = (await c.bot(args.bot))["id"] if args.bot else None
    rows = await c.collect(CRON, bot_id=bot)
    if args.json:
        output.dump(rows)
        return 0
    bots = await bot_names(c)
    output.table(
        rows,
        [
            ("任务 id", lambda r: r["id"][:8]),
            ("名称", "name"),
            ("AI 员工", lambda r: bots.get(r["bot_id"], r["bot_id"])),
            ("计划", schedule_text),
            ("状态", job_status),
            ("下次执行", "next_run_at"),
            ("上次结果", "last_status"),
            ("投递", "delivery_status"),
            ("个人", "personal"),
        ],
    )
    return 0


async def show_job(c: AdminClient, args: argparse.Namespace) -> int:
    row = await job(c, args.job)
    if args.json:
        output.dump(row)
        return 0
    bots = await bot_names(c)
    names = await user_names(c, {row["created_by"], *row["target_users"]})
    output.fields(
        row,
        [
            ("任务 id", "id"),
            ("名称", "name"),
            ("AI 员工", lambda r: bots.get(r["bot_id"], r["bot_id"])),
            ("创建者", lambda r: names[r["created_by"]]),
            ("个人任务", "personal"),
            ("计划", schedule_text),
            ("状态", job_status),
            ("下次执行", "next_run_at"),
            ("到期时间", "expires_at"),
            ("上次执行", "last_run_at"),
            ("上次结果", "last_status"),
            ("最近投递", "delivery_status"),
            ("通知成员", lambda r: [names[u] for u in r["target_users"]]),
            ("通知群", "target_chats"),
            ("通知邮箱", "notify_emails"),
            ("企微群通知", lambda r: r["notify_webhook"] and r["has_webhook_url"]),
            ("预检超时（秒）", "precheck_timeout_seconds"),
            ("可修改", "can_edit"),
            ("版本", "version"),
        ],
    )
    for title, key in (
        ("执行指令", "prompt"),
        ("附加系统提示词", "system_prompt"),
        ("预检脚本", "precheck_script"),
    ):
        if row.get(key):
            print(f"\n{title}：\n{row[key]}")
    return 0


async def runs(c: AdminClient, args: argparse.Namespace) -> int:
    row = await job(c, args.job)
    data = await c.get(f"{CRON}/{row['id']}/runs", page=1, per_page=args.limit)
    if args.json:
        output.dump(data["items"])
        return 0
    output.table(
        data["items"],
        [
            ("记录", "id"),
            ("触发", "trigger_kind"),
            ("状态", "status"),
            ("开始", "started_at"),
            ("结束", "finished_at"),
            ("输入/输出 Token", lambda r: f"{r['input_tokens'] or 0}/{r['output_tokens'] or 0}"),
            ("投递", lambda r: [d["status"] for d in r["deliveries"]]),
            ("错误", "error_message"),
        ],
        empty="（还没有执行记录）",
    )
    if data["total"] > len(data["items"]):
        print(f"共 {data['total']} 条，只显示最新 {len(data['items'])} 条；--limit 可调")
    return 0


async def cron_changes(c: AdminClient, args: argparse.Namespace) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if args.clear_targets:
        updates = {"target_users": [], "target_chats": [], "notify_emails": []}
    updates |= changed(args, CRON_OPTIONS)
    if args.cron is not None:
        updates |= {"schedule_kind": "recurring", "cron_expression": args.cron, "run_at": None}
    if args.at is not None:
        updates |= {"schedule_kind": "once", "run_at": args.at}
    if args.prompt is not None:
        updates["prompt"] = args.prompt
    if args.prompt_file is not None:
        updates["prompt"] = read_text(args.prompt_file)
    if args.system_prompt_file is not None:
        updates["system_prompt"] = read_text(args.system_prompt_file) or None
    if args.precheck_file is not None:
        updates["precheck_script"] = read_text(args.precheck_file) or None
    if args.expires is not None:
        updates["expires_at"] = args.expires or None
    if args.to_user is not None:
        updates["target_users"] = [(await c.user(ref))["id"] for ref in args.to_user]
    if args.webhook_url is not None:
        updates["notify_webhook_url"] = args.webhook_url
        updates.setdefault("notify_webhook", bool(args.webhook_url))
    return updates


def json_body(args: argparse.Namespace) -> dict[str, Any]:
    if args.data is None and args.file is None:
        return {}
    source = args.data if args.data is not None else read_text(args.file)
    try:
        body = json.loads(source)
    except json.JSONDecodeError as exc:
        raise CliError(f"请求体不是合法 JSON：{exc}") from None
    if not isinstance(body, dict):
        raise CliError("请求体应为 JSON 对象")
    return body


async def create_job(c: AdminClient, args: argparse.Namespace) -> int:
    body = json_body(args) | await cron_changes(c, args)
    if args.bot:
        body["bot_id"] = (await c.bot(args.bot))["id"]
    if args.disabled:
        body["enabled"] = False
    data = await c.request("POST", CRON, body=body)
    if args.json:
        output.dump(data)
    else:
        print(f"已新建定时任务 {data['name']}（{data['id']}），下次执行：{data['next_run_at']}")
    return 0


async def put_job(c: AdminClient, row: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    body = {key: row[key] for key in CRON_FIELDS} | updates
    return await c.request(  # type: ignore[no-any-return]
        "PUT", f"{CRON}/{row['id']}", body=body, version=row["version"]
    )


async def set_job(c: AdminClient, args: argparse.Namespace) -> int:
    row = await job(c, args.job)
    updates = await cron_changes(c, args)
    if not updates:
        raise CliError("没有要修改的字段，见 --help")
    data = await put_job(c, row, updates)
    if args.json:
        output.dump(data)
    else:
        fields = [k for k in updates if k != "notify_webhook_url"] or ["notify_webhook_url"]
        print(f"已更新定时任务 {data['name']}：{'、'.join(fields)}")
    return 0


async def enable_job(c: AdminClient, args: argparse.Namespace) -> int:
    row = await job(c, args.job)
    if row["enabled"]:
        print(f"{row['name']} 已经是启用状态")
        return 0
    data = await put_job(c, row, {"enabled": True})
    if args.json:
        output.dump(data)
    else:
        print(f"已启用定时任务 {data['name']}，下次执行：{data['next_run_at']}")
    return 0


async def job_action(c: AdminClient, args: argparse.Namespace, action: str, done: str) -> int:
    row = await job(c, args.job)
    data = await c.request("POST", f"{CRON}/{row['id']}/{action}", version=row["version"])
    if args.json:
        output.dump(data)
    else:
        print(f"{done} {row['name']}")
    return 0


async def disable_job(c: AdminClient, args: argparse.Namespace) -> int:
    return await job_action(c, args, "disable", "已停用定时任务")


async def run_job(c: AdminClient, args: argparse.Namespace) -> int:
    return await job_action(c, args, "run", "已安排立即执行")


async def cancel_run(c: AdminClient, args: argparse.Namespace) -> int:
    return await job_action(c, args, "cancel-run", "已取消本轮执行")


async def delete_job(c: AdminClient, args: argparse.Namespace) -> int:
    row = await job(c, args.job)
    require_yes(args, f"删除定时任务 {row['name']}")
    await c.request("DELETE", f"{CRON}/{row['id']}", version=row["version"])
    print(f"已删除定时任务 {row['name']}")
    return 0


async def notification_chats(c: AdminClient, args: argparse.Namespace) -> int:
    bot = await c.bot(args.bot)
    rows = await c.get(f"{CRON}/notification-chats", bot_id=bot["id"], details=True)
    if args.json:
        output.dump(rows)
    else:
        output.table(rows, [("群 id", "id"), ("群名", "name")], empty="（还没有在群里收到过消息）")
    return 0


async def test_notification(c: AdminClient, args: argparse.Namespace) -> int:
    row = await job(c, args.job)
    data = await c.request("POST", f"{CRON}/{row['id']}/test-notification", version=row["version"])
    if args.json:
        output.dump(data)
        return 0
    print(f"已排队 {len(data['outbox_ids'])} 条测试通知；用 cron notification-tests 查看投递结果")
    for target, error in data["errors"].items():
        print(f"  - {target}：{error}")
    return 1 if data["errors"] else 0


async def notification_tests(c: AdminClient, args: argparse.Namespace) -> int:
    row = await job(c, args.job)
    rows = await c.get(f"{CRON}/{row['id']}/notification-tests")
    if args.json:
        output.dump(rows)
    else:
        output.table(
            rows,
            [
                ("投递 id", "id"),
                ("渠道", "channel"),
                ("状态", "status"),
                ("尝试次数", "attempts"),
                ("错误", "error"),
            ],
            empty="（还没有发过测试通知）",
        )
    return 0


async def precheck_test(c: AdminClient, args: argparse.Namespace) -> int:
    body: dict[str, Any] = {"script": read_text(args.script)}
    if args.ctx is not None:
        try:
            body["ctx"] = json.loads(args.ctx)
        except json.JSONDecodeError as exc:
            raise CliError(f"--ctx 不是合法 JSON：{exc}") from None
    data = await c.request("POST", f"{CRON}/precheck/test", body=body)
    if args.json:
        output.dump(data)
        return 0
    if data["status"] != "success":
        print(f"预检失败：{data['error']}")
        return 1
    output.fields(
        data,
        [("触发", "trigger"), ("原因", "reason"), ("追加到指令", "prompt_appendix")],
    )
    return 0


# ---- 公告 ----


def announcement_options(p: argparse.ArgumentParser, *, create: bool) -> None:
    target = p.add_mutually_exclusive_group()
    target.add_argument("--bot", help="只对这个 AI 员工生效：标识、名称或 id")
    target.add_argument("--relay", help="只对这个运行时上的员工生效：运行时名称或 id")
    if not create:
        target.add_argument("--global", dest="all_bots", action="store_true", help="改为全局公告")
    p.add_argument("--content", help="公告内容")
    p.add_argument("--content-file", metavar="文件", help="从文件读公告内容；- 为标准输入")
    p.add_argument("--start", metavar="时间", help="开始时间（带时区）；空字符串为不限")
    p.add_argument("--end", metavar="时间", help="结束时间（带时区）；空字符串为不限")


def register_announcements(sub: Any) -> None:
    ops = group(sub, "announcements", "公告（AI 委员会与平台管理员可用）")
    leaf(ops, "list", "列出公告", list_announcements)
    leaf(ops, "show", "查看公告", show_announcement).add_argument(
        "id", help="公告 id（可只写开头几位）"
    )
    p = leaf(ops, "create", "新建公告（默认全局、立即启用）", create_announcement)
    announcement_options(p, create=True)
    p.add_argument("--inactive", action="store_true", help="建成停用状态")
    p = leaf(ops, "set", "修改公告（只改给出的字段）", set_announcement)
    p.add_argument("id")
    announcement_options(p, create=False)
    leaf(ops, "enable", "启用公告", enable_announcement).add_argument("id")
    leaf(ops, "disable", "停用公告", disable_announcement).add_argument("id")
    p = leaf(ops, "delete", "删除公告", delete_announcement)
    p.add_argument("id")
    add_yes(p)


async def announcement(c: AdminClient, ref: str) -> dict[str, Any]:
    rows = await c.get(ANNOUNCEMENTS)
    return by_ref(rows, ref, "公告", lambda r: r["id"][:8])


def announcement_target(row: dict[str, Any]) -> str:
    if row["scope"] == "bot":
        return f"员工 {row['bot_key']}（{row['bot_name']}）"
    if row["scope"] == "relay":
        return f"运行时 {row['relay_name']}"
    return "全局"


def announcement_status(row: dict[str, Any]) -> str:
    window = {"pending": "未开始", "expired": "已过期", "active": "生效中"}[row["time_status"]]
    return window if row["is_active"] else f"停用（{window}）"


async def list_announcements(c: AdminClient, args: argparse.Namespace) -> int:
    rows = await c.get(ANNOUNCEMENTS)
    if args.json:
        output.dump(rows)
    else:
        output.table(
            rows,
            [
                ("公告 id", lambda r: r["id"][:8]),
                ("范围", announcement_target),
                ("状态", announcement_status),
                ("开始", "start_at"),
                ("结束", "end_at"),
                ("内容", lambda r: r["content"][:40]),
            ],
        )
    return 0


async def show_announcement(c: AdminClient, args: argparse.Namespace) -> int:
    row = await announcement(c, args.id)
    if args.json:
        output.dump(row)
        return 0
    output.fields(
        row,
        [
            ("公告 id", "id"),
            ("范围", announcement_target),
            ("状态", announcement_status),
            ("开始", "start_at"),
            ("结束", "end_at"),
            ("更新时间", "updated_at"),
        ],
    )
    print(f"\n{row['content']}")
    return 0


async def relay(c: AdminClient, ref: str) -> dict[str, Any]:
    rows = await c.collect("/api/admin/relay-servers")
    found = [r for r in rows if r["id"] == ref] or [r for r in rows if r["name"] == ref]
    return one(found, ref, "运行时", lambda r: f"{r['name']}（{r['id'][:8]}）")


async def announcement_changes(c: AdminClient, args: argparse.Namespace) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if args.bot:
        updates = {"scope": "bot", "bot_id": (await c.bot(args.bot))["id"], "relay_server_id": None}
    elif args.relay:
        updates = {"scope": "relay", "relay_server_id": (await relay(c, args.relay))["id"]}
        updates["bot_id"] = None
    elif getattr(args, "all_bots", False):
        updates = {"scope": "global", "bot_id": None, "relay_server_id": None}
    if args.content is not None:
        updates["content"] = args.content
    if args.content_file is not None:
        updates["content"] = read_text(args.content_file)
    if args.start is not None:
        updates["start_at"] = args.start or None
    if args.end is not None:
        updates["end_at"] = args.end or None
    return updates


async def create_announcement(c: AdminClient, args: argparse.Namespace) -> int:
    body = {"scope": "global", **await announcement_changes(c, args)}
    body["is_active"] = not args.inactive
    data = await c.request("POST", ANNOUNCEMENTS, body=body)
    if args.json:
        output.dump(data)
    else:
        print(
            f"已新建公告 {data['id'][:8]}：{announcement_target(data)}，{announcement_status(data)}"
        )
    return 0


async def set_announcement(c: AdminClient, args: argparse.Namespace) -> int:
    row = await announcement(c, args.id)
    updates = await announcement_changes(c, args)
    if not updates:
        raise CliError("没有要修改的字段，见 --help")
    body = {key: row[key] for key in ANNOUNCEMENT_FIELDS} | updates
    data = await c.request("PUT", f"{ANNOUNCEMENTS}/{row['id']}", body=body)
    if args.json:
        output.dump(data)
    else:
        print(f"已更新公告 {row['id'][:8]}：{'、'.join(updates)}")
    return 0


async def set_active(c: AdminClient, args: argparse.Namespace, want: bool) -> int:
    row = await announcement(c, args.id)
    word = "启用" if want else "停用"
    if row["is_active"] == want:
        print(f"公告 {row['id'][:8]} 已经是{word}状态")
        return 0
    data = await c.request("POST", f"{ANNOUNCEMENTS}/{row['id']}/toggle")
    if args.json:
        output.dump(data)
    else:
        print(f"已{word}公告 {row['id'][:8]}（{announcement_status(data)}）")
    return 0


async def enable_announcement(c: AdminClient, args: argparse.Namespace) -> int:
    return await set_active(c, args, True)


async def disable_announcement(c: AdminClient, args: argparse.Namespace) -> int:
    return await set_active(c, args, False)


async def delete_announcement(c: AdminClient, args: argparse.Namespace) -> int:
    row = await announcement(c, args.id)
    require_yes(args, f"删除公告 {row['id'][:8]}")
    await c.request("DELETE", f"{ANNOUNCEMENTS}/{row['id']}")
    print(f"已删除公告 {row['id'][:8]}")
    return 0


# ---- 我的提醒与私聊定时任务 ----


def register_reminders(sub: Any) -> None:
    ops = group(sub, "reminders", "我的提醒与私聊里创建的定时任务（只能管自己的）")
    leaf(ops, "list", "列出我的提醒与定时任务", list_reminders)
    for name, handler, text in (
        ("pause", pause_reminder, "暂停定时任务"),
        ("resume", resume_reminder, "恢复定时任务"),
        ("cancel", cancel_reminder, "取消固定提醒"),
    ):
        leaf(ops, name, text, handler).add_argument("id", help="id（可只写开头几位）")
    p = leaf(ops, "delete", "删除定时任务（运行中的一轮会先取消）", delete_reminder)
    p.add_argument("id")
    add_yes(p)


async def reminder(c: AdminClient, ref: str) -> dict[str, Any]:
    rows = await c.get(REMINDERS)
    return by_ref(rows, ref, "提醒", lambda r: f"{r['id'][:8]}（{r.get('name') or r['text']}）")


def reminder_plan(row: dict[str, Any]) -> str:
    if row["type"] == "schedule" and row["schedule_kind"] == "recurring":
        return f"{row['cron_expression']}（{row['timezone']}）"
    return f"一次 {row['run_at']}"


async def list_reminders(c: AdminClient, args: argparse.Namespace) -> int:
    rows = await c.get(REMINDERS)
    if args.json:
        output.dump(rows)
    else:
        output.table(
            rows,
            [
                ("id", lambda r: r["id"][:8]),
                ("类型", lambda r: "定时任务" if r["type"] == "schedule" else "提醒"),
                ("AI 员工", "bot_name"),
                ("内容", lambda r: (r.get("name") or r["text"])[:40]),
                ("计划", reminder_plan),
                ("状态", "status"),
                ("下次执行", lambda r: r.get("next_run_at")),
            ],
        )
    return 0


async def reminder_action(c: AdminClient, args: argparse.Namespace, action: str, done: str) -> int:
    row = await reminder(c, args.id)
    data = await c.request("POST", f"{REMINDERS}/{row['id']}/{action}")
    if args.json:
        output.dump(data)
    else:
        print(f"{done} {row['id'][:8]}（{data['status']}）")
    return 0


async def pause_reminder(c: AdminClient, args: argparse.Namespace) -> int:
    return await reminder_action(c, args, "pause", "已暂停")


async def resume_reminder(c: AdminClient, args: argparse.Namespace) -> int:
    return await reminder_action(c, args, "resume", "已恢复")


async def cancel_reminder(c: AdminClient, args: argparse.Namespace) -> int:
    return await reminder_action(c, args, "cancel", "已取消提醒")


async def delete_reminder(c: AdminClient, args: argparse.Namespace) -> int:
    row = await reminder(c, args.id)
    require_yes(args, f"删除定时任务 {row['id'][:8]}")
    await c.request("DELETE", f"{REMINDERS}/{row['id']}")
    print(f"已删除定时任务 {row['id'][:8]}")
    return 0


# ---- 我的：个人凭证、飞书个人授权、企业微信绑定 ----


def register_me(sub: Any) -> None:
    ops = group(sub, "me", "我的个人设置：个人凭证、飞书个人授权、企业微信绑定（只能管自己的）")
    leaf(ops, "credentials", "列出我交给 AI 员工的个人凭证（保密项不显示值）", credentials)
    p = leaf(ops, "credential-set", "更新一项个人凭证的值", credential_set)
    p.add_argument("bot", help="AI 员工名称或 id")
    p.add_argument("key", help="变量名")
    p.add_argument(
        "--value-file",
        default="-",
        metavar="文件",
        help="从文件读值；默认标准输入（终端里不回显）",
    )
    p = leaf(ops, "credential-delete", "删除一项个人凭证", credential_delete)
    p.add_argument("bot")
    p.add_argument("key")
    add_yes(p)
    leaf(
        ops, "credential-request", "查看 AI 员工发来的凭证索取表单", credential_request
    ).add_argument("id", help="表单 id（链接里的那一段）")
    p = leaf(ops, "credential-submit", "提交凭证索取表单", credential_submit)
    p.add_argument("id")
    p.add_argument(
        "--values-file",
        default="-",
        metavar="文件",
        help='JSON 对象 {"变量名": "值"}；默认从标准输入读',
    )

    leaf(ops, "feishu-auth", "列出我给各 AI 员工的飞书个人授权", feishu_auth)
    p = leaf(ops, "feishu-revoke", "撤销给某个 AI 员工的飞书个人授权", feishu_revoke)
    p.add_argument("bot", help="AI 员工名称或 id")
    add_yes(p)

    leaf(ops, "wecom", "查看我的企业微信绑定", wecom)
    p = leaf(ops, "wecom-set", "修改企业微信绑定的档位或开关", wecom_set)
    p.add_argument("--level", choices=WECOM_LEVELS, help="授权档位")
    p.add_argument("--enabled", type=on_off, metavar="on|off", help="是否启用")
    leaf(ops, "wecom-check", "逐项试一次只读调用，刷新各项能力状态", wecom_check)
    leaf(ops, "wecom-renewed", "已在企业微信里续期：重新检查并从现在起算有效期", wecom_renewed)
    p = leaf(ops, "wecom-scan", "发起扫码绑定：打印二维码链接并等待扫码完成", wecom_scan)
    p.add_argument("--timeout", type=int, default=300, metavar="秒", help="最多等多久，默认 300")
    p.add_argument("--no-wait", action="store_true", help="只打印链接，不等待")
    leaf(ops, "wecom-scan-cancel", "取消进行中的扫码绑定", wecom_scan_cancel)
    p = leaf(ops, "wecom-unbind", "解除企业微信绑定（删除托管的凭证）", wecom_unbind)
    add_yes(p)


def mine(rows: list[dict[str, Any]], ref: str) -> list[dict[str, Any]]:
    """按 AI 员工 id 或名称挑出我的那几行（这些接口只给 bot_id 与名称）。"""
    return [r for r in rows if ref in (r["bot_id"], r["bot_name"])]


async def own_credential(c: AdminClient, bot: str, key: str) -> dict[str, Any]:
    rows = [r for r in await c.get(f"{ME}/credentials") if r["env_key"] == key]
    return one(mine(rows, bot), f"{bot} 的 {key}", "个人凭证", lambda r: r["bot_id"])


async def credentials(c: AdminClient, args: argparse.Namespace) -> int:
    rows = await c.get(f"{ME}/credentials")
    if args.json:
        output.dump(rows)
    else:
        output.table(
            rows,
            [
                ("AI 员工", "bot_name"),
                ("变量名", "env_key"),
                ("说明", "label"),
                ("值", lambda r: "（保密）" if r["secret"] else r["value"]),
                ("更新时间", "updated_at"),
                ("上次使用", "last_used_at"),
            ],
            empty="（还没有个人凭证）",
        )
    return 0


def read_secret(path: str, prompt: str) -> str:
    if path == "-" and sys.stdin.isatty():
        return getpass.getpass(prompt)
    return read_text(path).rstrip("\r\n")


async def credential_set(c: AdminClient, args: argparse.Namespace) -> int:
    row = await own_credential(c, args.bot, args.key)
    value = read_secret(args.value_file, f"{args.key} 的新值：")
    await c.request("PUT", f"{ME}/credentials/{row['bot_id']}/{args.key}", body={"value": value})
    print(f"已更新 {row['bot_name']} 的 {args.key}")
    return 0


async def credential_delete(c: AdminClient, args: argparse.Namespace) -> int:
    row = await own_credential(c, args.bot, args.key)
    require_yes(args, f"删除 {row['bot_name']} 的个人凭证 {args.key}")
    await c.request("DELETE", f"{ME}/credentials/{row['bot_id']}/{args.key}")
    print(f"已删除 {row['bot_name']} 的 {args.key}")
    return 0


async def credential_request(c: AdminClient, args: argparse.Namespace) -> int:
    data = await c.get(f"{ME}/credential-requests/{args.id}")
    if args.json:
        output.dump(data)
        return 0
    output.fields(
        data,
        [
            ("表单 id", "id"),
            ("AI 员工", "bot_name"),
            ("用途", "purpose"),
            ("状态", "status"),
            ("保存供以后使用", "save"),
            ("过期时间", "expires_at"),
        ],
    )
    print("\n要填的项：")
    output.table(
        data["fields"],
        [("变量名", "key"), ("说明", "label"), ("保密", lambda f: f.get("secret", True))],
    )
    print(f"\n{data['security_note']}")
    return 0


async def credential_submit(c: AdminClient, args: argparse.Namespace) -> int:
    try:
        values = json.loads(read_text(args.values_file))
    except json.JSONDecodeError:
        # 不带异常详情：解析错误的上下文里可能有值。
        raise CliError("--values-file 不是合法 JSON") from None
    if not isinstance(values, dict):
        raise CliError('--values-file 应为 JSON 对象 {"变量名": "值"}')
    data = await c.request(
        "POST", f"{ME}/credential-requests/{args.id}/submit", body={"values": values}
    )
    if args.json:
        output.dump(data)
    else:
        print(f"已提交：{'、'.join(data['keys'])}")
    return 0


async def feishu_auth(c: AdminClient, args: argparse.Namespace) -> int:
    rows = (await c.get(f"{ME}/feishu-authorizations"))["items"]
    if args.json:
        output.dump(rows)
    else:
        output.table(
            rows,
            [
                ("AI 员工", "bot_name"),
                ("状态", "status"),
                ("档位", "authorization_level"),
                ("已授权范围", lambda r: len(r["scopes"])),
                ("缺少的范围", "missing_scopes"),
                ("到期", "expires_at"),
            ],
            empty="（没有飞书个人授权）",
        )
    return 0


async def feishu_revoke(c: AdminClient, args: argparse.Namespace) -> int:
    rows = (await c.get(f"{ME}/feishu-authorizations"))["items"]
    row = one(mine(rows, args.bot), args.bot, "飞书个人授权", lambda r: r["bot_id"])
    require_yes(args, f"撤销给 {row['bot_name']} 的飞书个人授权")
    data = await c.request("DELETE", f"{ME}/feishu-authorizations/{row['bot_id']}")
    if args.json:
        output.dump(data)
    else:
        remote = "" if data["remote_revoked"] else "（飞书侧未确认撤销，可在飞书里再检查一下）"
        print(f"已撤销给 {row['bot_name']} 的飞书个人授权{remote}")
    return 0


def capability(kind: str) -> Any:
    return lambda row: (row[kind] or {}).get("state")


def print_wecom(data: dict[str, Any]) -> None:
    output.fields(
        data,
        [
            ("状态", "status"),
            ("启用", "enabled"),
            ("档位", "authorization_level"),
            ("授权机器人", "bot_name"),
            ("授权人", "authorizer_name"),
            ("绑定时间", "bound_at"),
            ("上次检查", "verified_at"),
            ("最早到期", "next_expiry"),
            ("错误", "error"),
            ("企业微信账号已同步", "identity_linked"),
        ],
    )
    if data["capabilities"]:
        print()
        output.table(
            data["capabilities"],
            [
                ("能力", "service"),
                ("读", capability("read")),
                ("写", capability("write")),
                ("发送", capability("send")),
            ],
        )
    scan = data.get("scan")
    if scan:
        print(f"\n扫码：{scan['status']} {scan['error'] or ''}".rstrip())


def wecom_show(args: argparse.Namespace, data: dict[str, Any]) -> int:
    if args.json:
        output.dump(data)
    else:
        print_wecom(data)
    return 0


async def wecom(c: AdminClient, args: argparse.Namespace) -> int:
    return wecom_show(args, await c.get(f"{ME}/wecom-binding"))


async def wecom_set(c: AdminClient, args: argparse.Namespace) -> int:
    body = changed(args, {"level": "authorization_level", "enabled": "enabled"})
    if not body:
        raise CliError("没有要修改的字段，见 --help")
    return wecom_show(args, await c.request("PATCH", f"{ME}/wecom-binding", body=body))


async def wecom_check(c: AdminClient, args: argparse.Namespace) -> int:
    return wecom_show(args, await c.request("POST", f"{ME}/wecom-binding/check"))


async def wecom_renewed(c: AdminClient, args: argparse.Namespace) -> int:
    return wecom_show(args, await c.request("POST", f"{ME}/wecom-binding/renewed"))


async def wecom_scan(c: AdminClient, args: argparse.Namespace) -> int:
    data = await c.request("POST", f"{ME}/wecom-binding/scan")
    scan = data["scan"]
    print(
        f"用企业微信扫码或打开链接完成授权（{scan['expires_at']} 前有效）：\n{scan['url']}",
        file=sys.stderr if args.json else sys.stdout,
    )
    if args.no_wait:
        return 0
    deadline = time.monotonic() + args.timeout
    while scan["status"] == "pending":
        if time.monotonic() >= deadline:
            print("等待超时；扫码会话仍有效，可用 me wecom 查看结果，或 me wecom-scan-cancel 取消")
            return 1
        await asyncio.sleep(scan["retry_after"] or 3)
        data = await c.get(f"{ME}/wecom-binding/scan")
        scan = data["scan"]
    if not args.json:
        print()
    wecom_show(args, data)
    return 0 if data["status"] == "bound" else 1


async def wecom_scan_cancel(c: AdminClient, args: argparse.Namespace) -> int:
    return wecom_show(args, await c.request("DELETE", f"{ME}/wecom-binding/scan"))


async def wecom_unbind(c: AdminClient, args: argparse.Namespace) -> int:
    require_yes(args, "解除企业微信绑定")
    data = await c.request("DELETE", f"{ME}/wecom-binding")
    if args.json:
        output.dump(data)
    else:
        print("已解除企业微信绑定；企业微信里的授权机器人请自行删除")
    return 0
