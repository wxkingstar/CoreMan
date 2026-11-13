"""调度公共员工用例的公共件：一位可被调度的普通员工、派出的一次求助、它在目标员工那边的一轮。"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.bots.secrets import CREDENTIALS_AAD, ENV_AAD
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    AssistantJob,
    Bot,
    BotAllowedUser,
    RelayServer,
    RuntimeNode,
    Task,
    User,
)
from tests.fakes import fake_relay
from tests.fakes.fake_relay import FakeRelay
from tests.integration.assistant_helpers import SECRET
from tests.integration.worker_helpers import MASTER, build_ctx

QUESTION = f"上周华东仓的库存周转是多少？{SECRET}"
ANSWER = f"周转 4.2 次，来源是周报第 3 页。{SECRET}"


async def target_bot(
    session: AsyncSession,
    *,
    key: str = "demo-bot",
    name: str = "demo-bot",
    accept: bool = True,
    enabled: bool = True,
    platform: str = "feishu",
    allowed: list[uuid.UUID] | None = None,
) -> Bot:
    """一位普通员工，绑在自己的节点与实例上（不与助理共用：助理所在的节点不承载普通员工）。"""
    cipher, tag = Cipher(MASTER), uuid.uuid4().hex[:8]
    creator = User(
        login_name=f"c{tag}", display_name="李四", email=f"c{tag}@example.test", source="sync"
    )
    session.add(creator)
    await session.flush()
    node = RuntimeNode(
        capabilities={p: {"installed": True, "login": "ready"} for p in ("claude", "codex")},
        id=uuid.uuid4(),
        name=f"node-target-{tag}",
        token_hash=f"hash-{tag}",
        workspace_root="/data/skills",
        hostname="relay.test",
        username="coreman",
        platform="linux",
        architecture="amd64",
        environment="host",
        version="test",
    )
    session.add(node)
    await session.flush()
    relay = RelayServer(
        runtime_node_id=node.id,
        name=f"r-target-{tag}",
        host="relay.test",
        clawrelay_port=80,
        model_provider="claude",
    )
    session.add(relay)
    await session.flush()
    bot = Bot(
        bot_key=key,
        platform=platform,
        name=name,
        description="查库存与订单",
        created_by=creator.id,
        relay_server_id=relay.id,
        model="vllm/claude-sonnet-4-6",
        working_dir=f"/data/skills/{key}",
        enabled=enabled,
        accept_bot_help=accept,
        credentials_enc=cipher.encrypt(
            json.dumps({"app_id": "cli_demo", "app_secret": "s"}), CREDENTIALS_AAD
        ),
        env_vars_enc=cipher.encrypt(json.dumps({}), ENV_AAD),
    )
    session.add(bot)
    await session.flush()
    for user_id in allowed or []:
        session.add(BotAllowedUser(bot_id=bot.id, user_id=user_id))
    await session.commit()
    return bot


async def asked(
    session: AsyncSession, bot: Bot, target: Bot, *, question: str = QUESTION, **kw: Any
) -> AssistantJob:
    """助理向 `target` 派出一次只读求助（已提交）。"""
    from coreman.core.assistants import dispatch

    options = {"allow_write": False, "attended": True, **kw}
    row = await dispatch.send(session, bot, target, question, **options)
    await session.commit()
    return row


async def help_task(session: AsyncSession, row: AssistantJob) -> Task:
    """认领这次求助在目标员工那边的那一轮。"""
    from tests.integration.job_helpers import reloaded, take

    return await take(session, (await reloaded(session, row)).last_task_id)


def reply(status: str, answer: str, needs: list[str] | None = None) -> str:
    """目标员工按约定写出的终稿。"""
    body: dict[str, Any] = {"status": status, "answer": answer}
    if needs is not None:
        body["needs"] = needs
    return json.dumps(body, ensure_ascii=False)


def scripted(monkeypatch: pytest.MonkeyPatch, text_: str, *, name: str = "help_answer") -> str:
    """给假 relay 加一个只回这段话的脚本，返回脚本名。"""
    monkeypatch.setitem(
        fake_relay.SCENARIOS,
        name,
        lambda: [
            fake_relay._text(text_),
            fake_relay.FINISH,
            fake_relay._chunk(None, usage=fake_relay.USAGE),
            fake_relay.DONE,
        ],
    )
    return name


async def run_help(
    engine: AsyncEngine,
    session: AsyncSession,
    task: Task,
    scenario: str = "normal",
    handler: Any = None,
) -> tuple[Task, FakeRelay]:
    """在目标员工那边跑这一轮，返回结束后的任务与假 relay。"""
    from coreman.runtime.worker.assistant_bot_help import AssistantBotHelpHandler

    relay = FakeRelay(scenario)
    ctx = build_ctx(engine, task, relay_client_factory=lambda _r: relay.client())
    ctx.cloud_proxy_ready = True
    await (handler or AssistantBotHelpHandler()).run(ctx)
    await ctx.chat_logs.drain(5)
    done = await session.get(Task, task.id, populate_existing=True)
    assert done is not None
    return done, relay


async def rows_in(session: AsyncSession, table: str) -> int:
    """一张表现在有多少行（表名只来自用例里的字面量）。"""
    return int((await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())
