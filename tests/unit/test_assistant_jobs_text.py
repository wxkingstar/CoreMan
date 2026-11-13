"""子任务每一轮交给模型的文本：首轮是交代，之后是追问。"""

from __future__ import annotations

import uuid

from coreman.core.assistants import jobs
from coreman.core.db.models import AssistantJob

JOB = uuid.UUID("0b0f6f0c-9f43-4c50-8f0e-3b2b3a8f7a11")


def _row(**kw: object) -> AssistantJob:
    values: dict[str, object] = {
        "id": JOB,
        "title_enc": "查李四的报价\n第二行",
        "brief_enc": "翻一下上周的聊天记录，找出报价。",
        "rounds": 1,
    }
    return AssistantJob(**{**values, **kw})


def test_the_first_round_is_the_brief() -> None:
    text = jobs.round_text(_row())
    assert text.startswith("这是主会话派给你的子任务")
    assert f"编号 {JOB}" in text
    # 标题压成一行。
    assert "标题：查李四的报价 第二行" in text
    assert "翻一下上周的聊天记录，找出报价。" in text
    assert "不会发给任何人" in text and "「你现在是子任务」" in text


def test_later_rounds_are_the_follow_up_only() -> None:
    text = jobs.round_text(_row(rounds=3, message_enc="合同里怎么写的？"))
    assert text.startswith(f"主会话就子任务 {JOB} 追问（第 3 轮，最多 {jobs.MAX_ROUNDS} 轮）")
    assert "合同里怎么写的？" in text
    assert "翻一下上周的聊天记录" not in text
