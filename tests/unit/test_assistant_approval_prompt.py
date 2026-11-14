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


def test_rules_say_how_writes_are_confirmed_present_and_away() -> None:
    """在场：本轮结束时出确认卡，不要重试、不要让所有者去审批系统；不在场：一轮合成一张单。"""
    from coreman.core.assistants.approvals import batch
    from coreman.core.systems_catalog import confirm

    text = prompt.RULES
    assert "pending_confirmation" in text and "本轮结束时平台给他发一张确认卡" in text
    assert "不要重试" in text and "不要让他去审批系统" in text
    assert "合成一张审批单" in text and "整单都不执行" in text
    assert f"一轮最多暂存 {batch.MAX_ITEMS} 个写操作" in text
    assert f"卡片 {int(confirm.TTL.total_seconds() // 60)} 分钟内有效" in text


def test_rules_say_how_to_handle_being_sent_back() -> None:
    """打回：照意见重写、只改被点到的、已处理的不重提、同类第二次提议记成规则。"""
    from coreman.core.assistants import feedback

    text = prompt.RULES
    assert "owner_feedback" in text and feedback.PATH in text
    assert "inbox/feedback.md" in text
    assert "只改意见点到的项" in text
    assert "已亲自处理" in text and "不要重提" in text
    assert "同类意见出现第二次" in text and "needs_owner=true" in text
    assert "起草之前" in text


def test_daily_compile_does_not_repeat_recorded_feedback() -> None:
    from coreman.core.assistants import compiling, feedback

    assert feedback.PATH in compiling.INSTRUCTIONS
