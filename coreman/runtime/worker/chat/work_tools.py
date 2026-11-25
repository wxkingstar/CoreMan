"""把任务工具挂进这一轮：借业务系统 MCP 的通用挂载，服务名 `system_coreman_tasks`。

只挂给能挂载 MCP 系统的运行时（节点声明了 `system_mcp_v1`）上的飞书员工；个人助理不接任务。
"""

from __future__ import annotations

import json
import uuid

from coreman.core.assistants.records import is_assistant
from coreman.core.auth.system_access import MCP_MOUNTS_ENV, token_env_var
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot
from coreman.core.work_tasks import tools


def mount(
    env: dict[str, str],
    *,
    cipher: Cipher,
    bot: Bot,
    task_id: int,
    actor_id: uuid.UUID | None,
    kind: str,
    public_base_url: str,
    mcp_supported: bool,
) -> bool:
    """在 env 里加上任务工具的挂载；挂不了时返回 False（不改 env）。"""
    if not mcp_supported or bot.platform != "feishu" or is_assistant(bot):
        return False
    mounts = [
        m
        for m in json.loads(env.get(MCP_MOUNTS_ENV) or "[]")
        if isinstance(m, dict) and m.get("key") != tools.MOUNT_KEY
    ]
    mounts.append({"key": tools.MOUNT_KEY, "url": public_base_url.rstrip("/") + tools.API_PATH})
    env[MCP_MOUNTS_ENV] = json.dumps(mounts)
    env[token_env_var(tools.MOUNT_KEY)] = tools.issue_capability(
        cipher, task_id=task_id, bot_id=bot.id, actor_id=actor_id, kind=kind
    )
    return True
