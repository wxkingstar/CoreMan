"""「要不要回」的规则只进个人助理主会话的提示词。"""

from __future__ import annotations

from coreman.core.assistants import reply_prompt
from coreman.runtime.worker.chat import personal


def test_rules_cover_the_judgements_that_get_drafts_sent_back() -> None:
    text = reply_prompt.RULES
    assert text.startswith("\n\n## 要不要回\n")
    for phrase in (
        "整段上下文",
        "已经在这个话题里表过态",
        "收口",
        "没 @ 他",
        "不替他做新的承诺",
        "承诺性的决定",
        "根因还没查实",
        "不是所有者的要求",
        "拿不准",
        "followup_add",
        "assistant_report",
    ):
        assert phrase in text


def test_rules_carry_no_personal_content() -> None:
    text = reply_prompt.RULES
    for word in ("example.com", "http://", "https://", "ou_", "oc_"):
        assert word not in text


def test_standard_bot_guidance_does_not_get_these_rules() -> None:
    rules, _ = personal.feishu_guidance(None, "https://coreman.example.com", scheduled=False)
    assert "## 要不要回" not in rules
