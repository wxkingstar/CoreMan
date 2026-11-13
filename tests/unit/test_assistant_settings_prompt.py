"""设置规则：点到两个工具与三条私聊命令；只进个人助理的提示词。"""

from __future__ import annotations

from coreman.core.assistants import settings, settings_prompt, settings_tools
from coreman.runtime.worker.chat import personal


def test_rules_name_the_tools_commands_and_limits() -> None:
    text = settings_prompt.SETTINGS_RULES
    assert text.startswith("\n\n## 工作时段与关注的会话\n")
    for name in settings_tools.OWN:
        assert name in text
    for word in ("上班", "下班", "focus_chats", "ignored_chats", "IANA", "owner_turn_required"):
        assert word in text
    assert f"最多 {settings.MAX_FOCUS} 个" in text


def test_rules_are_stable_text() -> None:
    assert "2026" not in settings_prompt.SETTINGS_RULES


def test_standard_bot_guidance_does_not_mention_settings() -> None:
    rules, _ = personal.feishu_guidance(None, "https://coreman.example.com", scheduled=False)
    assert "assistant_settings_" not in rules and "工作时段与关注的会话" not in rules
