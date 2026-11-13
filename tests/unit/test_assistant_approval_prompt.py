"""审批规则只进个人助理的提示词。"""

from __future__ import annotations

from coreman.core.assistants.approvals import prompt, service
from coreman.runtime.worker.chat import personal


def test_rules_name_the_tools_and_limits_the_platform_enforces() -> None:
    text = prompt.RULES
    for word in (
        "pending_approval",
        "chat_id",
        "anchor_message_id",
        "approval_revise",
        "approval_withdraw",
        "approval_list",
        "assistant_mark_processing",
        "more_recipients",
        "feishu_prepare_upload",
        "upload_id",
    ):
        assert word in text
    assert f"最多 {service.MAX_PENDING} 张" in text


def test_standard_bot_guidance_does_not_mention_approvals() -> None:
    rules, _ = personal.feishu_guidance(None, "https://coreman.example.com", scheduled=False)
    assert "审批单" not in rules and "pending_approval" not in rules
