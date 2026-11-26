"""管理命令补齐的部分：后台换运行时、批量写操作明细、助理池、个人助理、权限申请、本机应用授权。"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from coreman.cli import main
from coreman.core.db.models import (
    AuditLog,
    Bot,
    BotSystemGrant,
    InteractionState,
    OAuthClient,
    OAuthGrant,
    RelayServer,
    SystemBatchItem,
    Task,
    User,
)
from coreman.core.systems_catalog import batch
from coreman.core.timeutils import utcnow
from tests.api.test_admin_cli import add_user
from tests.api.test_assistant_admin import _assistant
from tests.api.test_system_access_admin import _requested
from tests.fakes.runtime_node import add_node_relay
from tests.integration.worker_helpers import seed_bot


@pytest.fixture
def cli(api_settings, db_engine, capsys):  # type: ignore[no-untyped-def]
    """同 test_admin_cli：在另一个线程里跑 `python -m coreman.cli …`，返回退出码与输出。"""

    async def run(*argv: str) -> tuple[int, str, str]:
        capsys.readouterr()
        code = await asyncio.to_thread(main, list(argv))
        out, err = capsys.readouterr()
        return code, out, err

    return run


async def test_background_switch_job_status_and_cancel(cli, db_session):
    bot, _, _ = await seed_bot(db_session, model="claude-sonnet-5-5")
    await add_node_relay(db_session, name="target")
    await db_session.commit()

    code, out, err = await cli(
        "bots", "switch-relay", "sales_bot", "target", "--background", "--yes", "--as", "creator"
    )
    assert code == 0 and "已排队切换作业" in out, err
    job = await db_session.scalar(select(Task).where(Task.kind == "relay_switch"))
    assert job is not None and job.payload["origin"] == "admin"

    code, out, err = await cli("bots", "switch-job", "sales_bot", str(job.id), "--as", "creator")
    assert code == 0 and "queued" in out, err
    code, out, err = await cli("bots", "switch-cancel", "sales_bot", str(job.id), "--as", "creator")
    assert code == 0, err
    code, out, _ = await cli(
        "bots", "switch-job", "sales_bot", str(job.id), "--json", "--as", "creator"
    )
    assert json.loads(out)["status"] == "cancelled"
    # 结束了但没切成：退出码 1，脚本能据此判断。
    code, _, _ = await cli("bots", "switch-job", "sales_bot", str(job.id), "--as", "creator")
    assert code == 1
    await db_session.refresh(bot)
    assert bot.workspace_state == "ready"


async def test_batch_items_show_requests_and_results(cli, db_session):
    bot, _, cipher = await seed_bot(db_session)
    await add_user(db_session, "ops", "platform_admin")
    creator = await db_session.scalar(select(User).where(User.login_name == "creator"))
    state = InteractionState(
        bot_id=bot.id,
        kind=batch.KIND,
        scope_key="cli-items",
        status="submitted",
        state={
            "user_id": str(creator.id),
            "system": "stock",
            "system_name": "Stock",
            "operation_id": "createDraft",
            "summary": "新建草稿",
            "risk": "write",
            "count": 2,
            "chunk": 0,
        },
    )
    db_session.add(state)
    await db_session.flush()
    for seq, (status, sku) in enumerate([("ok", "A-1"), ("http_error", "B-2")]):
        aad = batch.item_aad(state.id, seq)
        db_session.add(
            SystemBatchItem(
                batch_id=state.id,
                seq=seq,
                request_enc=cipher.encrypt(json.dumps({"body": {"sku": sku}}), aad),
                brief_enc=cipher.encrypt("库存不足" if seq else "已创建", aad),
                idempotency_key=f"k{seq}",
                status=status,
                status_code=200 if seq == 0 else 409,
            )
        )
    await db_session.commit()
    short = str(state.id)[:8]

    code, out, err = await cli("systems", "batch-items", short, "--as", "ops")
    assert code == 0 and "A-1" in out and "B-2" in out and "库存不足" in out, err
    assert "共 2 项" in out
    code, out, _ = await cli(
        "systems", "batch-items", short, "--status", "failed", "--json", "--as", "ops"
    )
    data = json.loads(out)
    assert [i["seq"] for i in data["items"]] == [1] and data["items"][0]["status_code"] == 409
    # 看别人提交的业务数据要记审计：和管理台同一条接口。
    assert await db_session.scalar(
        select(AuditLog).where(AuditLog.action == "system_batch.view_items")
    )


async def test_assistant_pool_toggle(cli, db_session):
    await seed_bot(db_session)
    await add_user(db_session, "ops", "platform_admin")
    await add_user(db_session, "bob")

    code, _, err = await cli("runtimes", "assistant-pool", "node-1", "claude", "off", "--as", "bob")
    assert code == 1 and err.strip()
    code, out, err = await cli(
        "runtimes", "assistant-pool", "node-1", "claude", "off", "--as", "ops"
    )
    assert code == 0 and "已移出个人助理池" in out, err
    # 节点上有普通员工：加不进去，原因原样给出。
    code, _, err = await cli("runtimes", "assistant-pool", "node-1", "claude", "on", "--as", "ops")
    assert code == 1 and err.strip()
    relay = await db_session.scalar(
        select(RelayServer).where(RelayServer.model_provider == "claude")
    )
    assert relay is not None and relay.assistant_pool is False


async def test_admin_lists_and_deprovisions_assistants(cli, db_session):
    owner = await add_user(db_session, "zhang", display_name="张三")
    await add_user(db_session, "ops", "platform_admin")
    bot = await _assistant(db_session, owner)

    code, _, err = await cli("assistants", "list", "--as", "zhang")
    assert code == 1 and err.strip()
    code, out, err = await cli("assistants", "list", "--as", "ops")
    assert code == 0 and "张三（zhang）" in out and "active" in out, err
    code, _, err = await cli("assistants", "deprovision", "zhang", "--as", "ops")
    assert code == 1 and "--yes" in err
    code, out, err = await cli("assistants", "deprovision", "zhang", "--yes", "--as", "ops")
    assert code == 0 and "已开始停用" in out, err
    await db_session.refresh(bot)
    assert bot.assistant_state == "deprovisioning"


@pytest.mark.usefixtures("assistant_master")
async def test_access_requests_list_and_decide(cli, db_session):
    bot_id, row_id = await _requested(db_session)
    await add_user(db_session, "ops", "platform_admin")
    short = str(row_id)[:8]

    code, out, err = await cli("systems", "access-requests", "--as", "ops")
    assert code == 0 and short in out and "查客户订单状态" in out, err
    code, _, err = await cli("systems", "access-decide", short, "reject", "--as", "ops")
    assert code == 1 and "--note" in err
    code, out, err = await cli("systems", "access-decide", short, "approve", "--as", "ops")
    assert code == 0 and "已批准" in out, err
    db_session.expire_all()
    grant = await db_session.scalar(select(BotSystemGrant).where(BotSystemGrant.bot_id == bot_id))
    assert grant is not None and grant.system_key == "orders"
    code, out, _ = await cli("systems", "access-requests", "--as", "ops")
    assert short not in out
    code, out, _ = await cli("systems", "access-requests", "--all", "--as", "ops")
    assert short in out and "approved" in out
    assert await db_session.get(Bot, bot_id) is not None


async def test_member_lists_and_revokes_oauth_grants(cli, db_session):
    alice = await add_user(db_session, "alice")
    bob = await add_user(db_session, "bob")
    db_session.add(OAuthClient(client_id="c1", client_name="本机 Agent", redirect_uris=[]))
    await db_session.flush()
    grants = [
        OAuthGrant(
            client_id="c1",
            user_id=user.id,
            scope="mcp",
            refresh_hash=f"h-{user.login_name}",
            refresh_expires_at=utcnow() + timedelta(days=30),
        )
        for user in (alice, bob)
    ]
    db_session.add_all(grants)
    await db_session.commit()
    short = str(grants[0].id)[:8]

    code, out, err = await cli("me", "oauth-grants", "--as", "alice")
    assert code == 0 and short in out and "本机 Agent" in out and str(grants[1].id)[:8] not in out
    # 别人的授权：找不到。
    code, _, err = await cli("me", "oauth-revoke", short, "--yes", "--as", "bob")
    assert code == 1 and "找不到" in err
    code, _, err = await cli("me", "oauth-revoke", short, "--as", "alice")
    assert code == 1 and "--yes" in err
    code, out, err = await cli("me", "oauth-revoke", short, "--yes", "--as", "alice")
    assert code == 0 and "已撤销" in out, err
    await db_session.refresh(grants[0])
    assert grants[0].revoked_at is not None
    code, out, _ = await cli("me", "oauth-grants", "--as", "alice")
    assert "没有本机应用授权" in out
