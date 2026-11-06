"""管理命令：定时任务、公告、我的提醒与「我的」个人设置。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from coreman.core.db.models import Announcement, CronJob, UserIdentity
from coreman.core.personal_credentials import store
from coreman.core.personal_schedules import MODE as PERSONAL_MODE
from coreman.core.reminders import MODE as REMINDER_MODE
from tests.api.test_admin_cli import add_user, cli  # noqa: F401
from tests.integration.worker_helpers import seed_bot


async def test_cron_commands_follow_creator_rules(cli, db_session, tmp_path):  # noqa: F811
    bot, _, _ = await seed_bot(db_session)
    await add_user(db_session, "bob")
    # 定时任务只有绑定了平台身份的员工管理员能操作
    db_session.add(UserIdentity(user_id=bot.created_by, platform="wecom", platform_user_id="c1"))
    await db_session.commit()

    code, _, err = await cli(
        "cron",
        "create",
        "--bot",
        "sales_bot",
        "--name",
        "日报",
        "--cron",
        "0 9 * * *",
        "--prompt",
        "汇总昨天的数据",
        "--email",
        "alice@example.test",
        "--as",
        "creator",
    )
    assert code == 0, err
    job = await db_session.scalar(select(CronJob))
    assert (job.name, job.notify_emails, job.enabled) == ("日报", ["alice@example.test"], True)

    # 请求体也可以整份给 JSON，命令行上的字段覆盖它
    body = {"bot_id": str(bot.id), "name": "x", "cron_expression": "0 8 * * *", "prompt": "p"}
    code, out, err = await cli(
        "cron",
        "create",
        "--data",
        json.dumps(body),
        "--name",
        "早报",
        "--disabled",
        "--json",
        "--as",
        "creator",
    )
    assert code == 0, err
    assert json.loads(out)["name"] == "早报"

    code, out, _ = await cli("cron", "list", "--bot", "sales_bot", "--as", "creator")
    assert code == 0 and "日报" in out and "早报" in out and "sales_bot" in out
    code, out, _ = await cli("cron", "show", "日报", "--as", "creator")
    assert code == 0 and "汇总昨天的数据" in out and "alice@example.test" in out

    # 看不到这个 AI 员工的成员也找不到它的任务
    code, _, err = await cli("cron", "set", "日报", "--name", "x", "--as", "bob")
    assert code == 1 and "找不到定时任务" in err

    code, _, err = await cli(
        "cron", "set", job.id.hex[:8], "--name", "周报", "--cron", "0 9 * * 1", "--as", "creator"
    )
    assert code == 0, err
    await db_session.refresh(job)
    assert (job.name, job.cron_expression, job.prompt) == ("周报", "0 9 * * 1", "汇总昨天的数据")

    assert (await cli("cron", "disable", "周报", "--as", "creator"))[0] == 0
    await db_session.refresh(job)
    assert job.enabled is False
    code, out, _ = await cli("cron", "enable", "周报", "--as", "creator")
    await db_session.refresh(job)
    assert code == 0 and job.enabled is True and "已启用" in out

    assert (await cli("cron", "run", "周报", "--as", "creator"))[0] == 0
    await db_session.refresh(job)
    assert job.force_run_at is not None
    code, _, err = await cli("cron", "run", "周报", "--as", "creator")
    assert code == 1 and "等待触发" in err
    assert (await cli("cron", "cancel-run", "周报", "--as", "creator"))[0] == 0
    await db_session.refresh(job)
    assert job.force_run_at is None

    code, out, _ = await cli("cron", "runs", "周报", "--as", "creator")
    assert code == 0 and "还没有执行记录" in out
    code, out, _ = await cli("cron", "notification-tests", "周报", "--as", "creator")
    assert code == 0 and "还没有发过测试通知" in out
    code, out, _ = await cli("cron", "notification-chats", "sales_bot", "--as", "creator")
    assert code == 0 and "还没有在群里收到过消息" in out

    script = tmp_path / "check.py"
    script.write_text(
        'def should_trigger(ctx):\n    return {"trigger": ctx["n"] > 2, "reason": "threshold"}\n',
        encoding="utf-8",
    )
    code, out, _ = await cli(
        "cron", "precheck-test", str(script), "--ctx", '{"n": 3}', "--as", "creator"
    )
    assert code == 0 and "threshold" in out
    script.write_text("import os\n", encoding="utf-8")
    code, out, _ = await cli("cron", "precheck-test", str(script), "--as", "creator")
    assert code == 1 and "预检失败" in out

    code, _, err = await cli("cron", "delete", "周报", "--as", "creator")
    assert code == 1 and "--yes" in err
    assert (await cli("cron", "delete", "周报", "--yes", "--as", "creator"))[0] == 0
    assert [j.name for j in await db_session.scalars(select(CronJob))] == ["早报"]


async def test_announcement_commands_need_manager_role(cli, db_session):  # noqa: F811
    await seed_bot(db_session)
    await add_user(db_session, "carol", "ai_committee")
    await add_user(db_session, "bob")

    code, _, err = await cli("announcements", "create", "--content", "维护中", "--as", "bob")
    assert code == 1 and err.strip()
    code, out, err = await cli("announcements", "create", "--content", "维护中", "--as", "carol")
    assert code == 0, err
    assert "全局" in out
    row = await db_session.scalar(select(Announcement))
    ref = str(row.id)[:8]

    code, _, err = await cli(
        "announcements",
        "set",
        ref,
        "--bot",
        "sales_bot",
        "--end",
        "2099-01-01T00:00:00+00:00",
        "--as",
        "carol",
    )
    assert code == 0, err
    await db_session.refresh(row)
    assert (row.scope, row.content, row.end_at.year) == ("bot", "维护中", 2099)
    code, out, _ = await cli("announcements", "list", "--as", "carol")
    assert code == 0 and "sales_bot" in out and "生效中" in out

    assert (await cli("announcements", "disable", ref, "--as", "carol"))[0] == 0
    await db_session.refresh(row)
    assert row.is_active is False
    code, out, _ = await cli("announcements", "disable", ref, "--as", "carol")
    assert code == 0 and "已经是停用状态" in out
    assert (await cli("announcements", "enable", ref, "--as", "carol"))[0] == 0

    code, _, err = await cli("announcements", "delete", ref, "--as", "carol")
    assert code == 1 and "--yes" in err
    assert (await cli("announcements", "delete", ref, "--yes", "--as", "carol"))[0] == 0
    assert await db_session.scalar(select(Announcement)) is None


async def test_reminder_commands_only_touch_own_rows(cli, db_session):  # noqa: F811
    bot, _, _ = await seed_bot(db_session)
    alice = await add_user(db_session, "alice")
    await add_user(db_session, "bob")
    later = datetime.now(UTC) + timedelta(hours=1)
    reminder = CronJob(
        bot_id=bot.id,
        created_by=alice.id,
        name="喝水",
        execution_mode=REMINDER_MODE,
        cron_expression="",
        schedule_kind="once",
        run_at=later,
        prompt="喝水",
        target_users=[alice.id],
        next_run_at=later,
    )
    schedule = CronJob(
        bot_id=bot.id,
        created_by=alice.id,
        name="周报提醒",
        execution_mode=PERSONAL_MODE,
        cron_expression="0 9 * * 1",
        prompt="整理周报",
        target_users=[alice.id],
        next_run_at=later,
    )
    db_session.add_all([reminder, schedule])
    await db_session.commit()

    code, out, _ = await cli("reminders", "list", "--as", "alice")
    assert code == 0 and "喝水" in out and "周报提醒" in out
    code, out, _ = await cli("reminders", "list", "--as", "bob")
    assert code == 0 and "（无）" in out
    code, _, err = await cli("reminders", "pause", str(schedule.id), "--as", "bob")
    assert code == 1 and "找不到提醒" in err

    assert (await cli("reminders", "pause", "周报提醒", "--as", "alice"))[0] == 0
    await db_session.refresh(schedule)
    assert schedule.enabled is False
    assert (await cli("reminders", "cancel", str(reminder.id)[:8], "--as", "alice"))[0] == 0
    await db_session.refresh(reminder)
    assert reminder.enabled is False

    code, _, err = await cli("reminders", "delete", "周报提醒", "--as", "alice")
    assert code == 1 and "--yes" in err
    assert (await cli("reminders", "delete", "周报提醒", "--yes", "--as", "alice"))[0] == 0
    assert [j.name for j in await db_session.scalars(select(CronJob))] == ["喝水"]


async def test_me_commands_keep_values_off_the_command_line(cli, db_session, tmp_path):  # noqa: F811
    bot, _, cipher = await seed_bot(db_session)
    alice = await add_user(db_session, "alice")
    await store.save(
        db_session,
        cipher,
        bot_id=bot.id,
        user_id=alice.id,
        fields=[
            {"key": "DEMO_USERNAME", "label": "账号", "secret": False},
            {"key": "DEMO_PIN", "label": "PIN", "secret": True},
        ],
        values={"DEMO_USERNAME": "alice", "DEMO_PIN": "pin-778899"},
    )
    await db_session.commit()

    code, out, _ = await cli("me", "credentials", "--as", "alice")
    assert code == 0 and "DEMO_USERNAME" in out and "（保密）" in out
    assert "pin-778899" not in out
    code, out, _ = await cli("me", "credentials", "--as", "creator")
    assert code == 0 and "还没有个人凭证" in out

    value = tmp_path / "pin.txt"
    value.write_text("pin-222333\n", encoding="utf-8")
    code, out, err = await cli(
        "me", "credential-set", "销售", "DEMO_PIN", "--value-file", str(value), "--as", "alice"
    )
    assert code == 0, err
    assert "pin-222333" not in out
    injected = await store.injected(db_session, cipher, bot_id=bot.id, user_id=alice.id)
    assert injected.env["DEMO_PIN"] == "pin-222333"
    await db_session.commit()  # 读取会记下使用时间并锁住这两行
    code, _, err = await cli(
        "me", "credential-set", "销售", "MISSING", "--value-file", str(value), "--as", "alice"
    )
    assert code == 1 and "找不到个人凭证" in err

    code, _, err = await cli("me", "credential-delete", "销售", "DEMO_PIN", "--as", "alice")
    assert code == 1 and "--yes" in err
    code, _, _ = await cli("me", "credential-delete", "销售", "DEMO_PIN", "--yes", "--as", "alice")
    injected = await store.injected(db_session, cipher, bot_id=bot.id, user_id=alice.id)
    assert code == 0 and set(injected.env) == {"DEMO_USERNAME"}

    code, out, _ = await cli("me", "feishu-auth", "--as", "alice")
    assert code == 0 and "没有飞书个人授权" in out
    code, out, _ = await cli("me", "wecom", "--as", "alice")
    assert code == 0 and "unbound" in out
    code, _, err = await cli("me", "wecom-check", "--as", "alice")
    assert code == 1 and "还没有绑定企业微信" in err
    code, _, err = await cli("me", "wecom-unbind", "--as", "alice")
    assert code == 1 and "--yes" in err
    assert (await cli("me", "wecom-unbind", "--yes", "--as", "alice"))[0] == 0


async def test_api_token_and_api_access_commands(cli, db_session):  # noqa: F811
    from coreman.core.db.models import BotConversationAlias, PersonalAccessToken

    bot, _, _ = await seed_bot(db_session)
    bot.platform = "feishu"
    await add_user(db_session, "alice")
    db_session.add(BotConversationAlias(bot_id=bot.id, alias="订单异常", chat_id="oc_g"))
    await db_session.commit()

    code, out, err = await cli(
        "me", "api-token-create", "脚本", "--bot", "sales_bot", "--days", "30", "--as", "alice"
    )
    assert code == 0, err
    plaintext = out.strip().splitlines()[-1]
    assert plaintext.startswith("cmt_")
    code, out, _ = await cli("me", "api-tokens", "--as", "alice")
    assert code == 0 and "脚本" in out and "有效" in out and plaintext not in out
    code, _, err = await cli("me", "api-token-revoke", "脚本", "--as", "alice")
    assert code == 1 and "--yes" in err
    code, out, _ = await cli("me", "api-token-revoke", "脚本", "--yes", "--as", "alice")
    assert code == 0 and "已吊销" in out
    token = await db_session.scalar(select(PersonalAccessToken))
    await db_session.refresh(token)
    assert token.revoked_at is not None

    code, _, _ = await cli("bots", "api-access-set", "sales_bot", "on", "--as", "alice")
    assert code == 1
    code, out, err = await cli("bots", "api-access-set", "sales_bot", "on", "--as", "creator")
    assert code == 0, err
    await db_session.refresh(bot)
    assert bot.api_access is True
    code, out, _ = await cli("bots", "api-access", "sales_bot", "--as", "creator")
    assert code == 0 and "订单异常" in out
    code, out, err = await cli(
        "bots", "api-alias-remove", "sales_bot", "订单异常", "--yes", "--as", "creator"
    )
    assert code == 0, err
    assert await db_session.scalar(select(BotConversationAlias)) is None
