"""worker 集成测试公共件：造 bot + relay，构造 TaskContext。"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.bots.secrets import CREDENTIALS_AAD, ENV_AAD
from coreman.core.chat.chat_logs import ChatLogWriter
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, RelayServer, Task, User
from coreman.core.db.session import make_session_factory
from coreman.core.relay.client import RelayClient
from coreman.core.settings_store import SettingsStore
from coreman.runtime.worker.context import TaskContext

MASTER = b"\x07" * 32


async def seed_bot(
    session: AsyncSession,
    *,
    model: str = "vllm/claude-sonnet-4-6",
    env: dict[str, str] | None = None,
    allowed_user_ids: list | None = None,
    verbosity: int = 1,
    effort: str | None = None,
) -> tuple[Bot, RelayServer, Cipher]:
    cipher = Cipher(MASTER)
    creator = User(login_name="creator", display_name="创建者")
    session.add(creator)
    await session.flush()
    relay = RelayServer(
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
        openuserid=openuserid,
        media_fetcher=media_fetcher,
        **({"clock": clock} if clock else {}),
    )


assert base64.b64encode(MASTER)  # 保持 MASTER_KEY 形态一致（api 测试用 base64 串）
