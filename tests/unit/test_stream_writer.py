from types import SimpleNamespace

from coreman.core.chat import redaction
from coreman.runtime.worker.stream_writer import StreamWriter


def writer() -> StreamWriter:
    ctx = SimpleNamespace(
        clock=lambda: 0.0,
        redact=lambda text: text,
        redact_text=lambda text: redaction.redact_text(text, frozenset()),
    )
    return StreamWriter(ctx)  # type: ignore[arg-type]


def test_text_after_tool_starts_a_new_paragraph() -> None:
    w = writer()
    w.add_tool("Bash")
    w.add_text("先拉")
    w.add_text("仓库。")
    w.add_tool("Bash")
    w.add_tool("Read")
    w.add_text("已拉完")
    w.add_text("。")
    assert w.pending_text == "先拉仓库。\n\n已拉完。"
    # 切分点落在空行之后：分段推送的每一段都从正文开始。
    assert w.boundaries == [0, 7, 7]
    assert w.pending_text[w.boundaries[-1] :] == "已拉完。"


def test_existing_newlines_count_toward_the_gap() -> None:
    w = writer()
    w.add_text("第一段\n")
    w.add_tool("Bash")
    w.add_text("第二段")
    assert w.pending_text == "第一段\n\n第二段"
    w.add_tool("Bash")
    w.add_text("\n\n第三段")
    assert w.pending_text == "第一段\n\n第二段\n\n第三段"
