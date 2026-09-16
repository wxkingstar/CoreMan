from coreman.core.wecom.thinking import ThinkingCollector
from coreman.runtime.gateway_feishu.cards import visible_parts


def test_open_think_tag_never_leaks_into_first_answer():
    assert visible_parts("", "<think>still working")[1] == ""
    assert visible_parts("", "<thi")[1] == ""
    assert visible_parts("", "<think>done</think>actual answer")[1] == "actual answer"


def test_full_thinking_is_preserved_across_tool_boundaries():
    collector = ThinkingCollector()
    content = "开始分析。" + "详细内容" * 100 + "结束分析。"
    collector.add_generating(content[:150])
    collector.add_generating(content[150:])
    collector.add_tool_call("Read")
    assert content in collector.to_markdown()


def test_card_shows_recent_process_with_full_view_link():
    from coreman.runtime.gateway_feishu.cards import stream_card

    card = stream_card(
        "\n".join(f"步骤{i}" for i in range(20)), "", session_url="https://coreman.test/session/abc"
    )
    panel = card["body"]["elements"][0]
    assert panel["header"]["title"]["content"] == "🤔 思考过程"
    preview = panel["elements"][0]["content"]
    assert "步骤19" in preview and "步骤0\n" not in preview
    assert len(preview.splitlines()) <= 5
    assert "https://coreman.test/session/abc" in str(panel)
