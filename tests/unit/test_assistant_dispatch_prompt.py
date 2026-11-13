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
