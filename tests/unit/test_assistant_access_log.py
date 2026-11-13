"""解密访问的记录：只有编号、动作与对象代号；不认识的动作直接报错。"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

from coreman.core.assistants import access_log

BOT, ACTOR = uuid.uuid4(), uuid.uuid4()


async def test_a_read_is_logged_with_ids_action_and_target_only() -> None:
    with capture_logs() as logs:
        await access_log.record(AsyncMock(), BOT, ACTOR, "followups.read", "open")
    assert logs == [
        {
            "event": "assistant_content_access",
            "log_level": "info",
            "bot_id": str(BOT),
            "actor_id": str(ACTOR),
            "action": "followups.read",
            "target": "open",
        }
    ]


async def test_the_target_is_cut_and_unknown_actions_are_rejected() -> None:
    with capture_logs() as logs:
        await access_log.record(AsyncMock(), BOT, ACTOR, "jobs.read", "x" * 500)
    assert len(logs[0]["target"]) == access_log.MAX_TARGET
    with pytest.raises(ValueError, match="unknown access action"):
        await access_log.record(AsyncMock(), BOT, ACTOR, "pages.dump", "")
