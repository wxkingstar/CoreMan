"""调度公共员工的提示词：目标侧的本轮说明。"""

from __future__ import annotations

from coreman.core.assistants import dispatch_prompt


def test_the_target_note_asks_for_json_and_forbids_asking_the_user() -> None:
    read = dispatch_prompt.target_note(allow_write=False)
    write = dispatch_prompt.target_note(allow_write=True)
    for note in (read, write):
        assert note.startswith("## 个人助理代问")
        assert '"status":"completed"' in note and '"status":"blocked"' in note
        assert '"needs"' in note and "不要向用户提问" in note and "纯 JSON" in note
    assert "只查不改" in read and "只查不改" not in write
    assert "已经批准" in write and "已经批准" not in read


def test_the_assistant_rules_keep_the_dispatch_narrow() -> None:
    rules = dispatch_prompt.RULES
    assert rules.startswith("\n\n## 调度公共员工\n")
    # 裁定 17 放在助理这一层的四条防范。
    assert "必需、合理" in rules
    assert "不带原始聊天记录" in rules and "隐私" in rules
    assert "不调度，记一条待跟进" in rules
    assert "只有所有者明确要求" in rules and "allow_write" in rules
    # 所有者在私聊里看到的：先是一行「已请某员工查某事」，之后是助理消化过的结果。
    assert "已请〈员工名〉查〈某事〉" in rules and "用你自己的话" in rules
    assert "不要原样转贴" in rules
    assert "4000" in rules and "> " in rules
