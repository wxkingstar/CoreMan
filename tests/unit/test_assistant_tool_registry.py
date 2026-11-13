"""助理自有工具的注册表：普通轮次三组、子任务轮次两组，按组列出、按组分派。"""

from __future__ import annotations

from typing import Any

import pytest

from coreman.core import personal_schedules as schedules
from coreman.core.assistants import tools
from coreman.core.assistants.approvals import catalog
from coreman.core.assistants.approvals.tools import STAGED_NOTE
from coreman.core.assistants.kinds import (
    APPROVAL_RESULT_KIND,
    HANDOVER_KIND,
    JOB_RESULT_KIND,
    SUBTASK_KIND,
)
from coreman.core.feishu_personal import endpoints
from coreman.core.feishu_personal import tools as personal_tools
from tests.integration.assistant_helpers import isolate_tool_registry

APPROVAL_TOOLS = [
    "approval_revise",
    "approval_withdraw",
    "approval_list",
    "assistant_mark_processing",
]
NOTES_TOOLS = ["notes_index", "notes_read", "notes_search", "notes_write", "notes_delete"]
SETTINGS_TOOLS = ["assistant_settings_get", "assistant_settings_update"]
FOLLOWUP_TOOLS = [
    "followup_add",
    "followup_update",
    "followup_snooze",
    "followup_close",
    "followup_list",
]
JOB_TOOLS = [
    "assistant_spawn",
    "assistant_job_message",
    "assistant_job_list",
    "assistant_job_cancel",
]
INVALID = {"error": "invalid_tool_or_arguments"}
CTX = tools.Context(None, None, None)  # type: ignore[arg-type]


def listing(*names: str) -> tools.Definitions:
    return lambda: [
        {"name": name, "description": "demo", "inputSchema": {"type": "object", "properties": {}}}
        for name in names
    ]


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(
        self, ctx: tools.Context, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        assert ctx is CTX
        self.calls.append((name, arguments))
        return {"ok": name}


@pytest.fixture
def demo(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """一个 knowledge 组与一个 jobs 组的演示工具，共用一个记录器。"""
    isolate_tool_registry(monkeypatch)
    recorder = Recorder()
    tools.register("knowledge", listing("notes_demo"), recorder)
    tools.register("jobs", listing("assistant_spawn_demo"), recorder)
    return recorder


def base() -> list[dict[str, Any]]:
    return personal_tools.definitions(auth=True, level="all", scopes=endpoints.manifest_scopes())


def test_groups_and_the_built_in_approval_group() -> None:
    assert tools.GROUPS == ("knowledge", "approval", "jobs")
    for name in APPROVAL_TOOLS:
        assert tools.group_of(name) == "approval"
    assert tools.group_of("feishu_chat_history") is None
    assert tools.group_of("notes_demo") is None
    assert tools.SUBTASK_GROUP == "subtask"
    assert tools.LOCAL_GROUPS == {"knowledge", "jobs", "subtask", "voice"}
    for name in JOB_TOOLS:
        assert tools.group_of(name) == "jobs"
    assert tools.group_of("assistant_job_blocked") == "subtask"


def test_only_subtasks_are_narrowed() -> None:
    assert tools.groups_for(SUBTASK_KIND) == ("knowledge", "subtask")
    for kind in ("chat", HANDOVER_KIND, JOB_RESULT_KIND, APPROVAL_RESULT_KIND):
        assert tools.groups_for(kind) == tools.GROUPS, kind


def test_register_refuses_unknown_groups_and_name_clashes(demo: Recorder) -> None:
    with pytest.raises(ValueError, match="group"):
        tools.register("misc", listing("x_demo"), demo)
    for taken in ("notes_demo", "approval_list", "feishu_chat_history"):
        with pytest.raises(ValueError, match="taken"):
            tools.register("knowledge", listing("fresh_demo", taken), demo)
    with pytest.raises(ValueError, match="taken"):
        tools.register("knowledge", listing("twice_demo", "twice_demo"), demo)
    # 被拒绝的那一批一个都没登记进去。
    assert tools.group_of("fresh_demo") is None and tools.group_of("twice_demo") is None


@pytest.mark.parametrize(
    "taken",
    [
        "feishu_authorize",
        "feishu_authorization_status",
        "feishu_revoke_authorization",
        *sorted(schedules.TOOLS),
    ],
)
def test_register_refuses_names_the_entry_handles_itself(demo: Recorder, taken: str) -> None:
    """授权工具与本人定时任务工具由 MCP 入口另行分派：重名的助理工具永远轮不到。"""
    with pytest.raises(ValueError, match="taken"):
        tools.register("knowledge", listing("fresh_demo", taken), demo)
    assert tools.group_of("fresh_demo") is None


def test_a_full_turn_lists_staged_writes_and_every_group(demo: Recorder) -> None:
    source = base()
    out = tools.definitions(source, groups=tools.GROUPS)
    names = [item["name"] for item in out]
    assert names[: len(source)] == [item["name"] for item in source]
    assert names[len(source) :] == [
        *NOTES_TOOLS,
        *FOLLOWUP_TOOLS,
        *SETTINGS_TOOLS,
        "notes_demo",
        *APPROVAL_TOOLS,
        *JOB_TOOLS,
        "assistant_spawn_demo",
    ]
    assert "assistant_job_blocked" not in names
    writes = [item for item in out if catalog.is_write(item["name"])]
    assert writes and "feishu_reply_message" in {item["name"] for item in writes}
    for item in writes:
        assert "approval" in item["inputSchema"]["properties"], item["name"]
        assert item["description"].endswith(STAGED_NOTE), item["name"]
    # 原清单没有被就地改动。
    original = next(item for item in source if item["name"] == "feishu_reply_message")
    assert "approval" not in original["inputSchema"].get("properties", {})


def test_a_turn_without_the_approval_group_is_read_only(demo: Recorder) -> None:
    source = base()
    names = [item["name"] for item in tools.definitions(source, groups=("knowledge",))]
    reads = [item["name"] for item in source if not catalog.is_write(item["name"])]
    assert names == [*reads, *NOTES_TOOLS, *FOLLOWUP_TOOLS, *SETTINGS_TOOLS, "notes_demo"]
    assert "feishu_chat_history" in names and "feishu_reply_message" not in names


def test_a_subtask_turn_adds_only_the_blocked_tool(demo: Recorder) -> None:
    source = base()
    names = [
        item["name"] for item in tools.definitions(source, groups=tools.groups_for(SUBTASK_KIND))
    ]
    reads = [item["name"] for item in source if not catalog.is_write(item["name"])]
    assert names == [
        *reads,
        *NOTES_TOOLS,
        *FOLLOWUP_TOOLS,
        *SETTINGS_TOOLS,
        "notes_demo",
        "assistant_job_blocked",
    ]
    assert not set(names) & set(JOB_TOOLS)


async def test_dispatch_follows_the_groups(demo: Recorder) -> None:
    assert await tools.dispatch(CTX, "notes_demo", {"q": 1}, groups=("knowledge",)) == {
        "ok": "notes_demo"
    }
    assert await tools.dispatch(CTX, "assistant_spawn_demo", {}, groups=tools.GROUPS) == {
        "ok": "assistant_spawn_demo"
    }
    assert demo.calls == [("notes_demo", {"q": 1}), ("assistant_spawn_demo", {})]
    # 组不在这一轮里：不受理，登记的函数也没被调用。
    assert await tools.dispatch(CTX, "assistant_spawn_demo", {}, groups=("knowledge",)) == INVALID
    assert await tools.dispatch(CTX, "approval_list", {}, groups=("knowledge",)) == INVALID
    # 没有 approval 组的轮次调用写类个人工具：不暂存、不提交，直接拒绝。
    assert await tools.dispatch(CTX, "feishu_reply_message", {}, groups=("knowledge",)) == INVALID
    assert await tools.dispatch(CTX, "notes_demo", ["not", "a", "dict"], groups=tools.GROUPS) == (
        INVALID
    )
    assert len(demo.calls) == 2
