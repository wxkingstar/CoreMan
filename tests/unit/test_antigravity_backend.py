"""Antigravity 后端在平台侧的各处约定。"""

from coreman.core.prompting.rich_cards import GENERATED_LINKED_IMAGES_PROMPT, with_reply_images
from coreman.core.relay.models import BACKENDS, backend_of, known_effort_flags
from coreman.core.relay.sse import ERROR_MARKERS


def test_backend_is_listed_and_recognised_by_prefix_or_provider():
    assert "antigravity" in BACKENDS
    assert backend_of("antigravity/gemini-4-neon") == "antigravity"
    assert backend_of("gemini-4-neon", "antigravity") == "antigravity"
    # 同名 Claude 模型挂在 Antigravity 下时仍走 Antigravity。
    assert backend_of("antigravity/claude-opus-5-5") == "antigravity"
    assert backend_of("claude-opus-5-5") == "claude"


def test_effort_levels_follow_antigravity_not_the_vendor_table():
    assert known_effort_flags("antigravity/gemini-4-neon") == {
        "supports_xhigh": True,
        "supports_max": False,
    }
    assert known_effort_flags("antigravity/gemini-4-carbon") == {
        "supports_xhigh": False,
        "supports_max": False,
    }
    # Claude Opus 5.5 原生支持 xhigh / max，在 Antigravity 里只到 high。
    assert known_effort_flags("claude-opus-5-5")["supports_max"] is True
    assert known_effort_flags("antigravity/claude-opus-5-5") == {
        "supports_xhigh": False,
        "supports_max": False,
    }


def test_driver_errors_are_recognised_in_the_reply_text():
    assert ERROR_MARKERS["antigravity"] == ("\n\n[antigravity error] ",)


def test_feishu_replies_explain_how_to_show_images():
    assert with_reply_images("", "feishu", "antigravity") == GENERATED_LINKED_IMAGES_PROMPT
    assert with_reply_images("extra", "wecom", "antigravity") == "extra"
