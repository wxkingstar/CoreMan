"""工作目录的安全规则只进个人助理的提示词。"""

from __future__ import annotations

from coreman.core.assistants import workspace_prompt
from coreman.runtime.worker.chat import personal


def test_workspace_rules_are_for_assistants_only() -> None:
    text = workspace_prompt.WORKSPACE_RULES
    assert "不要往里写人名、聊天内容、审批内容或任何个人信息" in text
    assert "知识库" in text and "$TMPDIR" in text
    rules, _ = personal.feishu_guidance(None, "https://coreman.example.com", scheduled=False)
    assert "工作目录只放规则" not in rules
