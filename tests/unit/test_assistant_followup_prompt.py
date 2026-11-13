"""待跟进规则：点到五个工具、四个分类与平台执行的限制；只进个人助理的提示词。"""

from __future__ import annotations

from coreman.core.assistants import followup_prompt, followup_tools, followups
from coreman.runtime.worker.chat import personal


def test_rules_name_the_tools_categories_and_limits() -> None:
    text = followup_prompt.FOLLOWUP_RULES
    assert text.startswith("\n\n## 待跟进\n")
    for name in followup_tools.OWN:
        assert name in text
    for word in ("decide", "promised", "waiting", "remind", "F012", "origin_message_id"):
        assert word in text
    for word in ("review_on", "due_on", "today", "outcome=dropped"):
        assert word in text
    assert f"最多 {followups.MAX_OPEN} 条" in text


def test_rules_are_stable_text() -> None:
    # 进 system prompt 的稳定段：不能带日期这类每轮会变的东西。
    assert "2026" not in followup_prompt.FOLLOWUP_RULES


def test_standard_bot_guidance_does_not_mention_followups() -> None:
    rules, _ = personal.feishu_guidance(None, "https://coreman.example.com", scheduled=False)
    assert "待跟进" not in rules and "followup_" not in rules
