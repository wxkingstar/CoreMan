from coreman.core.wecom.thinking import ThinkingCollector


def test_steps_to_markdown() -> None:
    c = ThinkingCollector(clock=lambda: 100.0)
    c.add_start("正在思考中...")
    c.add_tool_call("Bash")
    c.add_tool_call("Read")
    c.add_generating("a" * 150)
    c.add_generating("b" * 100)
    c.add_end("回复生成完成", now=112.4)
    md = c.to_markdown()
    lines = md.split("\n")
    assert (
        lines[0] == "🤔 正在思考中..." and lines[1] == "🔧 **Bash**" and lines[2] == "🔧 **Read**"
    )
    assert lines[3].startswith("💭 ") and len(lines[3]) == 2 + 200 and lines[3].endswith("b" * 100)
    assert lines[4] == "✨ 回复生成完成（总耗时12s）" and c.step_count == 5


def test_empty_collector() -> None:
    assert ThinkingCollector().to_markdown() == ""
