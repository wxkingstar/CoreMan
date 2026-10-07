"""worker 集成测试公共件：造 bot + relay，构造 TaskContext。"""

from __future__ import annotations

import base64
import json
import re
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.bots.secrets import CREDENTIALS_AAD, ENV_AAD
from coreman.core.chat.chat_logs import ChatLogWriter
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, RelayServer, RuntimeNode, Task, User
from coreman.core.db.session import make_session_factory
from coreman.core.relay.client import RelayClient
from coreman.core.settings_store import SettingsStore
from coreman.runtime.worker.context import TaskContext

MASTER = b"\x07" * 32
# 平台写在用户消息开头的本轮块（发言者与本轮状态），见 prompting.build_turn_context。
TURN_BLOCK = re.compile(r"\A\[SYS_TURN:([0-9a-f]{8})\]\n.*?\n\[/SYS_TURN:\1\](?:\n\n|\Z)", re.S)


def turn_block(body: dict[str, Any]) -> str:
    """请求体里用户消息开头的本轮块；没有就是空串。"""
    content = body["messages"][1]["content"]
    text = content if isinstance(content, str) else str(content[0].get("text", ""))
    match = TURN_BLOCK.match(text)
    return match.group(0).strip() if match else ""


def user_input(body: dict[str, Any]) -> Any:
    """请求体里的用户消息去掉开头的本轮块：纯文本是字符串，含媒体是 content parts。"""
    content = body["messages"][1]["content"]
    if isinstance(content, str):
        return TURN_BLOCK.sub("", content, count=1)
    first = content[0] if content else {}
    if first.get("type") == "text" and TURN_BLOCK.match(str(first.get("text", "")) + "\n\n"):
        return content[1:]
    return content


async def seed_bot(
    session: AsyncSession,
    *,
    model: str = "vllm/claude-sonnet-4-6",
    env: dict[str, str] | None = None,
    allowed_user_ids: list | None = None,
    verbosity: int = 4,
    effort: str | None = None,
) -> tuple[Bot, RelayServer, Cipher]:
    cipher = Cipher(MASTER)
    # 有邮箱才拿得到业务系统令牌（sub 只取邮箱前缀，见 auth/system_access）。
    creator = User(login_name="creator", display_name="创建者", email="creator@example.test")
    session.add(creator)
    await session.flush()
    # 运行时实例一律挂在节点上（relay.runtime_node_id 恒有值），测试夹具与生产保持一致。
    node = RuntimeNode(
        capabilities={p: {"installed": True, "login": "ready"} for p in ("claude", "codex")},
        id=uuid.uuid4(),
        name="node-1",
        token_hash="test-token-hash",
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
        name="r1",
        host="relay.test",
        clawrelay_port=80,
        model_provider="codex" if model.startswith("codex/") else "claude",
    )
    session.add(relay)
    await session.flush()
    bot = Bot(
        bot_key="sales_bot",
        platform="wecom",
        name="销售",
        created_by=creator.id,
        relay_server_id=relay.id,
        model=model,
        working_dir="/data/skills/sales_bot",
        system_prompt="你是销售",
        merged_system_prompt="你是销售",
        verbosity_level=verbosity,
        effort_level=effort,
        credentials_enc=cipher.encrypt(
            json.dumps({"bot_id": "wx", "secret": "s"}), CREDENTIALS_AAD
        ),
        env_vars_enc=cipher.encrypt(json.dumps(env or {}), ENV_AAD),
    )
    session.add(bot)
    await session.flush()
    from coreman.core.db.models import BotAllowedUser

    for uid in allowed_user_ids or []:
        session.add(BotAllowedUser(bot_id=bot.id, user_id=uid))
    await session.commit()
    return bot, relay, cipher


def build_ctx(
    engine: AsyncEngine,
    task: Task,
    *,
    relay_client_factory: Callable[[RelayServer], RelayClient] | None = None,
    clock=None,
    openuserid=None,
    media_fetcher=None,
    public_base_url: str = "http://localhost",
) -> TaskContext:  # type: ignore[no-untyped-def]
    factory = make_session_factory(engine)
    return TaskContext(
        task=task,
        instance_id="worker-test",
        session_factory=factory,
        settings_store=SettingsStore(factory),
        cipher=Cipher(MASTER),
        relay_client_factory=relay_client_factory or (lambda r: RelayClient(r.relay_url)),
        chat_logs=ChatLogWriter(factory),
        public_base_url=public_base_url,
        openuserid=openuserid,
        media_fetcher=media_fetcher,
        **({"clock": clock} if clock else {}),
    )


assert base64.b64encode(MASTER)  # 保持 MASTER_KEY 形态一致（api 测试用 base64 串）
