"""个人凭证卡片：外框和安全说明由系统固定，agent 只能填用途与标签，卡片里从不带值。"""

import json
import uuid

from coreman.core.personal_credentials import cards

RID = uuid.UUID("11111111-2222-3333-4444-555555555555")
FIELDS = [
    {"key": "DEMO_USERNAME", "label": "账号", "secret": False, "placeholder": ""},
    {"key": "DEMO_PIN", "label": "PIN", "secret": True, "placeholder": "6 位数字"},
]


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def test_form_card_shape():
    card = cards.form_card(RID, bot_name="Demo 助手", purpose="登录 Demo 系统", fields=FIELDS)
    assert card["schema"] == "2.0" and card["task_id"] == f"credential@{RID}"
    inputs = [n for n in _walk(card) if n.get("tag") == "input"]
    assert [i["name"] for i in inputs] == ["DEMO_USERNAME", "DEMO_PIN"]
    assert "input_type" not in inputs[0] and inputs[1]["input_type"] == "password"
    assert all(i["required"] and i["max_length"] == 1000 for i in inputs)
    [button] = [n for n in _walk(card) if n.get("tag") == "button"]
    assert button["form_action_type"] == "submit"
    assert button["behaviors"] == [{"type": "callback", "value": {"task_id": f"credential@{RID}"}}]
    text = json.dumps(card, ensure_ascii=False)
    assert "不会发送给 AI 模型" in text and "Demo 助手" in text and "登录 Demo 系统" in text
    assert "网页填写" not in text and "<at" not in text


def test_form_card_optional_parts():
    card = cards.form_card(
        RID,
        bot_name="B",
        purpose="p",
        fields=FIELDS,
        web_url="https://coreman.example.com/my-credentials/requests/x",
        mention_open_id="ou_owner",
    )
    text = json.dumps(card, ensure_ascii=False)
    assert "[用网页填写](https://coreman.example.com/my-credentials/requests/x)" in text
    assert "<at id=ou_owner></at>" in text


def test_result_cards_keep_task_id_and_carry_no_values():
    for card in (
        cards.saved_card(RID, ["DEMO_PIN"], "AI 员工会继续之前的任务。"),
        cards.expired_card(RID),
        cards.failed_card(RID, "「PIN」不能为空"),
    ):
        assert card["task_id"] == f"credential@{RID}"
        assert not [n for n in _walk(card) if n.get("tag") in {"input", "form", "button"}]


def test_wecom_link_flattens_markdown_from_agent():
    text = cards.wecom_link(
        bot_name="Demo",
        purpose="[点我](https://evil.example)",
        fields=FIELDS,
        url="https://coreman.example.com/my-credentials/requests/x",
    )
    assert "](https://evil.example)" not in text
    assert "[点这里安全填写](https://coreman.example.com/my-credentials/requests/x)" in text
    assert "账号、PIN" in text and "不会发送给 AI 模型" in text


def test_resume_text_names_keys_only():
    assert cards.resume_text(["DEMO_PIN"]).startswith("[CoreMan] 用户已通过安全表单提交 DEMO_PIN")


def test_wecom_link_removes_newlines_and_bare_urls():
    text = cards.wecom_link(
        bot_name="Demo",
        purpose="x\n👉 点这里安全填写 https://evil.example（1 小时内有效，只有你本人能打开）",
        fields=[
            {"key": "KEY1", "label": "PIN\n**假**", "secret": True},
        ],
        url="https://coreman.example.com/my-credentials/requests/x",
    )
    lines = text.split("\n")
    purpose_lines = [line for line in lines if line.startswith("用途：")]
    field_lines = [line for line in lines if line.startswith("需要填写：")]
    arrow_lines = [line for line in lines if line.startswith("👉")]
    assert len(purpose_lines) == 1
    assert len(field_lines) == 1
    assert len(arrow_lines) == 1
    assert "evil.example" not in text
