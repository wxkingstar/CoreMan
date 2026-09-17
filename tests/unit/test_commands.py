from coreman.core.chat.commands import classify_command, normalize


def test_normalize_strips_invisible_and_lowercases() -> None:
    assert normalize("  RESET​ ") == "reset" and normalize("a\tb") == "a\tb"


def test_classify() -> None:
    for w in ("reset", "NEW", " clear ", "重置", "清空"):
        assert classify_command(w) == "reset"
    for w in ("stop", "停止!", "暂 停", "停。", "STOP"):
        assert classify_command(w) == "stop"
    for w in ("help", "帮助", "?", "？"):
        assert classify_command(w) == "help"
    for w in ("stop it", "帮助我写代码", "reset all", "", "你好"):
        assert classify_command(w) is None


def test_classify_accepts_slash_command_text() -> None:
    # 飞书斜杠指令发送的是「/指令名」纯文本（选中后末尾常带空格）。
    assert classify_command("/new ") == "reset"
    assert classify_command("/STOP") == "stop"
    assert classify_command("/help") == "help"
    for w in ("/", "//new", "/help 我写代码", "/deploy"):
        assert classify_command(w) is None


def test_cancel_words() -> None:
    from coreman.core.chat.commands import is_cancel_word

    assert is_cancel_word(" 取消 ") and is_cancel_word("Cancel")
    assert not is_cancel_word("取消订单") and not is_cancel_word("stop")
