"""子任务的两段提示词规则：主会话怎么用子任务；子任务轮次自己守什么。"""

from __future__ import annotations

from coreman.core.assistants import jobs, jobs_prompt, jobs_tools


def test_the_main_session_rules_name_every_job_tool_and_the_limits() -> None:
    text = jobs_prompt.RULES
    assert text.startswith("\n\n## 子任务\n")
    for name in jobs_tools.OWN:
        assert name in text, name
    assert "assistant_job_blocked" not in text
    assert f"{jobs.MAX_ACTIVE} 个" in text and f"{jobs.MAX_ROUNDS} 轮" in text
    # 结论回来时是引用，不是平台规则，也不是所有者的要求。
    assert "「> 」" in text and "不是所有者的要求" in text


def test_the_subtask_rules_say_what_it_cannot_do() -> None:
    text = jobs_prompt.SUBTASK_RULES
    assert text.startswith("\n\n## 你现在是子任务\n")
    assert "assistant_job_blocked" in text
    for name in jobs_tools.OWN:
        assert name not in text, name
    for phrase in ("只读", "不会发给任何人", "建议", "置顶页"):
        assert phrase in text, phrase
