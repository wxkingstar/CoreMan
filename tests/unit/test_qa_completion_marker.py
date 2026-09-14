from coreman.core.i18n.messages import msg
from tests.unit.test_background_pusher import pusher


def test_proactive_completion_has_one_marker_and_keeps_content():
    result = pusher().finish("NEPTUNE" + msg("done_suffix"))
    assert result == [msg("bg_done_prefix") + "NEPTUNE"]
    assert pusher().finish(msg("done_suffix")) == [msg("bg_done_plain")]
    assert pusher(verbosity=3).finish("NEPTUNE" + msg("done_suffix")) == [
        "NEPTUNE" + msg("done_suffix")
    ]


def test_already_delivered_body_still_gets_one_completion_notice():
    p = pusher()
    p.offset = len("NEPTUNE")
    p.pushed_count = 1
    assert p.finish("NEPTUNE" + msg("done_suffix")) == [msg("bg_done_plain")]
