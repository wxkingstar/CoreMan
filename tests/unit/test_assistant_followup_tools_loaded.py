"""待跟进工具在 knowledge 组里，MCP 路由一导入就注册好。"""

from __future__ import annotations

import subprocess
import sys

from coreman.core.assistants import followup_tools, tools

NAMES = ["followup_add", "followup_close", "followup_list", "followup_snooze", "followup_update"]


def _names(group: str) -> set[str]:
    return {str(item["name"]) for item in tools.definitions([], groups={group})}


def test_tools_are_registered_in_the_knowledge_group() -> None:
    assert sorted(followup_tools.OWN) == NAMES
    assert set(NAMES) <= _names("knowledge")
    assert not set(NAMES) & _names("jobs")


def test_definitions_describe_ids_dates_and_scopes() -> None:
    by_name = {str(item["name"]): item for item in followup_tools.definitions()}
    add = by_name["followup_add"]["inputSchema"]
    assert add["required"] == ["category", "title"]
    assert add["properties"]["category"]["enum"] == ["decide", "promised", "waiting", "remind"]
    assert set(add["properties"]) == {
        "category",
        "title",
        "notes",
        "review_on",
        "due_on",
        "origin_message_id",
    }
    snooze = by_name["followup_snooze"]["inputSchema"]
    assert snooze["properties"]["until"]["description"] == "YYYY-MM-DD"
    listing = by_name["followup_list"]
    assert "F012" in listing["inputSchema"]["properties"]["scope"]["description"]
    for word in ("decide", "promised", "waiting", "remind"):
        assert word in by_name["followup_add"]["description"]


def test_the_mcp_router_loads_the_followup_tools() -> None:
    # 另起一个解释器：本进程里这个测试文件已经导入过 followup_tools，证明不了路由会导入它。
    code = (
        "import coreman.api.routers.feishu_personal\n"
        "from coreman.core.assistants import tools\n"
        "names = [d['name'] for d in tools.definitions([], groups={'knowledge'})]\n"
        "print(sorted(n for n in names if n.startswith('followup_')))\n"
    )
    done = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)
    assert done.stdout.strip() == str(NAMES)
