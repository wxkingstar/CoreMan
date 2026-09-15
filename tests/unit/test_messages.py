import pytest

from coreman.core.i18n.messages import MESSAGES, msg


def test_locales_isomorphic() -> None:
    assert set(MESSAGES) == {"zh", "ja"}
    assert set(MESSAGES["zh"]) == set(MESSAGES["ja"])
    assert all(v for v in MESSAGES["ja"].values())


def test_msg_format_and_fallback() -> None:
    assert (
        msg("no_text_with_tools", n=3, url="http://x")
        == "任务已执行（调用了 3 次工具），但 AI 未返回文字总结，产物可查看 [会话记录](http://x)。"
    )
    assert msg("stopped", locale="ja") == MESSAGES["ja"]["stopped"]
    assert msg("stopped", locale="en") == MESSAGES["zh"]["stopped"]
    with pytest.raises(KeyError):
        msg("nope")
    assert msg("help").startswith("📖 **命令列表**")


# 媒体 / 引用消息文案：zh 默认文案，两套 locale 都必须齐。
MEDIA_KEYS = {
    "media_prompt_image",
    "media_prompt_images",
    "media_prompt_file",
    "media_image_missing",
    "media_file_missing",
    "media_mixed_empty",
    "media_image_failed_item",
    "media_image_placeholder",
    "media_image_failed",
    "media_file_failed",
    "voice_empty",
    "quote_image_prefix",
    "quote_file_prefix",
    "quote_text_prefix",
    "quote_voice_prefix",
    "downloading_image",
    "downloading_file",
    "processing_mixed",
    "downloading_quote_image",
    "downloading_quote_file",
    # 五个 reason 与 MediaError.reason 一一对应，缺一个 ContentBuilder 就会 KeyError
    "media_reason_timeout",
    "media_reason_too_large",
    "media_reason_download_failed",
    "media_reason_decrypt_failed",
    "media_reason_invalid_key",
    "unknown_filename",
}


def test_media_keys_present_in_both_locales() -> None:
    assert MEDIA_KEYS <= set(MESSAGES["zh"])
    assert MEDIA_KEYS <= set(MESSAGES["ja"])


def test_media_wording_is_verbatim() -> None:
    assert msg("media_prompt_image") == "请描述这张图片的内容。"
    assert msg("media_prompt_images") == "请描述这些图片的内容。"
    assert (
        msg("media_prompt_file", name="a.pdf") == "[用户发送了文件: a.pdf] 请分析这个文件的内容。"
    )
    assert msg("media_image_missing") == "[用户发送了一张图片] 请描述这张图片的内容。"
    assert msg("media_file_missing", name="a.pdf") == "[用户发送了文件: a.pdf] 请分析这个文件。"
    assert msg("media_mixed_empty") == "[用户发送了图文混合消息]"
    assert msg("media_image_failed_item") == "[图片加载失败]"
    assert msg("media_image_placeholder") == "[图片]"
    assert msg("media_image_failed", error="下载超时") == "图片处理失败: 下载超时"
    assert msg("media_file_failed", error="下载失败") == "文件处理失败: 下载失败"
    assert msg("voice_empty") == "未能识别语音内容，请重试。"
    assert msg("quote_image_prefix", text="这是啥") == "[引用了一张图片]\n\n这是啥"
    assert msg("quote_file_prefix", name="a.pdf", text="总结") == "[引用了文件: a.pdf]\n\n总结"
    assert msg("quote_text_prefix", quoted="原话", text="回复") == "[引用消息: 原话]\n\n回复"
    assert msg("quote_voice_prefix", quoted="原音", text="嗯") == "[引用语音: 原音]\n\n嗯"
    assert msg("unknown_filename") == "未知"


def test_model_facing_media_keys_keep_chinese_in_ja() -> None:
    # 这些文案是塞进 content parts 发给模型的，不是给用户看的，ja 必须与 zh 同文。
    for key in ("media_prompt_image", "media_prompt_file", "media_image_missing"):
        assert MESSAGES["ja"][key] == MESSAGES["zh"][key]
    for key in ("quote_text_prefix", "quote_image_prefix", "quote_file_prefix"):
        assert MESSAGES["ja"][key] == MESSAGES["zh"][key]
