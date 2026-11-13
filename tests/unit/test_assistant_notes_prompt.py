"""知识库规则只进个人助理的提示词，说的数与平台实际执行的一致。"""

from __future__ import annotations

from coreman.core.assistants import notes, notes_prompt, notes_tools, workspace_prompt
from coreman.core.assistants.approvals import prompt as approval_prompt
from coreman.runtime.worker.chat import personal


def test_rules_name_every_tool_and_the_enforced_limits() -> None:
    text = notes_prompt.RULES
    assert text.startswith("\n\n## 个人知识库\n")
    for name in notes_tools.OWN:
        assert name in text
    for word in ("version", "version_conflict", "pinned", "summary"):
        assert word in text
    assert f"最多 {notes.MAX_PAGES} 页" in text
    assert f"合计 {notes.MAX_TOTAL_BYTES // (1024 * 1024)}MB" in text
    assert f"单页 {notes.MAX_PAGE_BYTES // 1024}KB" in text
    assert f"超过 {notes.PINNED_LIMIT_BYTES // 1024}KB" in text


def test_rules_keep_other_peoples_instructions_out() -> None:
    assert "别人" in notes_prompt.RULES and "不写进知识库" in notes_prompt.RULES


def test_existing_rules_point_at_the_real_tools() -> None:
    assert "知识库工具（notes_write）" in workspace_prompt.WORKSPACE_RULES
    assert "notes_write" in approval_prompt.RULES


def test_standard_bot_guidance_does_not_mention_the_knowledge_base() -> None:
    rules, _ = personal.feishu_guidance(None, "https://coreman.example.com", scheduled=False)
    assert "notes_" not in rules and "个人知识库" not in rules


def test_rules_say_pinned_pages_need_the_owner() -> None:
    assert "pin_requires_owner" in notes_prompt.RULES
    assert "所有者当面" in notes_prompt.RULES
