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


def test_cancel_words() -> None:
    from coreman.core.chat.commands import is_cancel_word

    assert is_cancel_word(" 取消 ") and is_cancel_word("Cancel")
    assert not is_cancel_word("取消订单") and not is_cancel_word("stop")
