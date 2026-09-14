"""入站 parts → relay content parts（spec §8.2 步骤 6-7，使用平台默认文案）。

规则矩阵见计划裁决第 4 条。`failed` 非空 = 这一轮不该进 AI：调用方把它回给用户并结束。
下载过程中的提示通过 `on_hint` 交给调用方写进思考区，本模块不碰流。

这里只做「组装」：下载解密在 `wecom.media`，消毒在 `prompting.sanitize`，谁也不越界。
媒体的 url / aeskey / data URI 一律不进日志——本模块连 logger 都不持有。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from coreman.core.i18n.messages import msg
from coreman.core.wecom.media import Media, MediaError, MediaFetcher, data_uri

# 能组装进 content parts 的 part 类型；其余（video 等）本期直接回「暂不支持」。
_SUPPORTED = ("text", "audio", "voice", "image", "file")
_AUDIO = ("audio", "voice")


@dataclass(frozen=True)
class BuiltContent:
    """一条入站消息组装出来的 relay 请求内容。

    Attributes:
        parts: 发给 relay 的 content parts（text / image_url / file_url）。
        text: parts 里文本部分的拼接，落 chat_logs.user_message 用。
        message_type: chat_logs.message_type（text/voice/image/file/mixed/quote_*）。
        quoted_content: 被引用消息的文字，没有引用则 None。
        file_info: 附件元信息（filename/size/mime），没有附件则 None。
        failed: 非空表示这一轮不进 AI，调用方把这句话回给用户并结束。
        failed_code: 与 `failed` 同生同灭的机器可读原因，调用方据此分任务终态：
            `media_failed` 记失败，`unsupported` / `voice_empty` 照旧算这轮答完了。
    """

    parts: list[dict[str, Any]]
    text: str
    message_type: str
    quoted_content: str | None
    file_info: dict[str, Any] | None
    failed: str | None
    failed_code: str | None = None


def _text_part(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _image_part(media: Media) -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": data_uri(media)}}


def _file_part(media: Media) -> dict[str, Any]:
    return {"type": "file_url", "file_url": {"url": data_uri(media), "filename": media.filename}}


def _file_info(media: Media) -> dict[str, Any]:
    return {"filename": media.filename, "size": len(media.data), "mime": media.mime}


def _has_ref(ref: dict[str, Any]) -> bool:
    """企微偶尔会给出缺 url 或缺 aeskey 的残缺引用；缺哪个都下不动，提前降级。"""
    return bool(
        (ref.get("url") and ref.get("aeskey")) or (ref.get("message_id") and ref.get("file_key"))
    )


def _ref_of(part: dict[str, Any]) -> dict[str, Any]:
    ref = part.get("ref")
    return dict(ref) if isinstance(ref, dict) else {}


def _joined_text(parts: list[dict[str, Any]]) -> str:
    return "\n".join(str(p["text"]) for p in parts if p.get("type") == "text" and p.get("text"))


def _own_text(own: list[dict[str, Any]]) -> str:
    """入站 part 自带的文字（文本原文 + 语音转写），用来判断「这条消息有没有话」。"""
    pieces = [str(p.get("text") or p.get("transcript") or "") for p in own]
    return "".join(p for p in pieces if p).strip()


class ContentBuilder:
    """把一条入站消息的 parts 组装成 relay 的 content parts。

    `on_hint` 可以是同步回调，也可以是协程函数：Task 7 传的是「把思考区刷给用户」的
    异步回调，下载几十兆的附件时用户才看得见「正在下载…」，所以这里 await 它。
    """

    def __init__(
        self,
        fetcher: MediaFetcher,
        *,
        locale: str = "zh",
        platform: str = "wecom",
        on_hint: Callable[[str], object] | None = None,
    ) -> None:
        self.fetcher = fetcher
        self.locale = locale
        self.platform = platform
        self._on_hint = on_hint

    async def _hint(self, key: str) -> None:
        if self._on_hint is None:
            return
        result = self._on_hint(msg(key, self.locale))
        if inspect.isawaitable(result):
            await result

    def _reason(self, exc: MediaError) -> str:
        """`MediaError.reason` 与 `media_reason_*` 文案键同名，缺键由 test_messages 兜住。"""
        return msg(f"media_reason_{exc.reason}", self.locale)

    async def build(self, parts: list[dict[str, Any]], *, text: str) -> BuiltContent:
        """组装一条消息；`parts` 是 `InboundMessage.parts` 的 dict 形式，`text` 已去 @。"""
        quote = next((p for p in parts if p.get("type") == "quote"), None)
        own = [p for p in parts if p.get("type") != "quote"]
        kinds = [str(p.get("type") or "") for p in own]
        if any(k not in _SUPPORTED for k in kinds):
            mtype = kinds[0] if len(set(kinds)) == 1 else "mixed"
            return BuiltContent(
                [], text, mtype, None, None, msg("unsupported_message", self.locale), "unsupported"
            )
        audio = [p for p in own if p.get("type") in _AUDIO]
        if audio and self.platform == "feishu":
            return BuiltContent(
                [], "", "voice", None, None, msg("voice_unsupported", self.locale), "unsupported"
            )
        if audio and not any(p.get("transcript") for p in audio):
            # 企微转写失败时 transcript 为空，此时没有任何可发的内容。
            return BuiltContent(
                [], "", "voice", None, None, msg("voice_empty", self.locale), "voice_empty"
            )
        if quote is not None:
            return await self._with_quote(quote, text)
        if not own:
            # 一个 part 都没有又没有引用：有话就当纯文本照发，一个字都没有才给模型一句
            # 占位（绝不能发空字符串过去）。
            if text.strip():
                return BuiltContent([_text_part(text)], text, "text", None, None, None)
            empty = msg("media_mixed_empty", self.locale)
            return BuiltContent([_text_part(empty)], empty, "mixed", None, None, None)
        media = [p for p in own if p.get("type") in ("image", "file")]
        if not media:
            mtype = "voice" if audio else "text"
            return BuiltContent([_text_part(text)], text, mtype, None, None, None)
        if len(media) == 1 and not text.strip() and not _own_text(own):
            return await self._single(media[0])
        # 图文混排（或多张图）：逐项组装，失败项降级为占位文本，不拖累整条消息。
        return await self._mixed(own, text)

    async def _single(self, part: dict[str, Any]) -> BuiltContent:
        """只有一张图 / 一个文件、没有配文：使用默认媒体说明交给模型。"""
        ref = _ref_of(part)
        if part["type"] == "image":
            if not _has_ref(ref):
                missing = msg("media_image_missing", self.locale)
                return BuiltContent([_text_part(missing)], missing, "image", None, None, None)
            await self._hint("downloading_image")
            try:
                media = await self.fetcher.fetch_image(ref)
            except MediaError as exc:
                failed = msg("media_image_failed", self.locale, error=self._reason(exc))
                return BuiltContent([], "", "image", None, None, failed, "media_failed")
            prompt = msg("media_prompt_image", self.locale)
            return BuiltContent(
                [_text_part(prompt), _image_part(media)], prompt, "image", None, None, None
            )
        given = part.get("filename") or None
        if not _has_ref(ref):
            name = given or msg("unknown_filename", self.locale)
            missing = msg("media_file_missing", self.locale, name=name)
            return BuiltContent([_text_part(missing)], missing, "file", None, None, None)
        await self._hint("downloading_file")
        try:
            media = await self.fetcher.fetch_file(ref, filename=given)
        except MediaError as exc:
            failed = msg("media_file_failed", self.locale, error=self._reason(exc))
            return BuiltContent([], "", "file", None, None, failed, "media_failed")
        prompt = msg("media_prompt_file", self.locale, name=media.filename or "")
        return BuiltContent(
            [_text_part(prompt), _file_part(media)], prompt, "file", None, _file_info(media), None
        )

    async def _mixed(self, own: list[dict[str, Any]], text: str) -> BuiltContent:
        """图文混排：按原顺序逐项组装。单张图失败只换成一行占位，其余内容照样进模型。"""
        await self._hint("processing_mixed")
        out: list[dict[str, Any]] = []
        files: list[dict[str, Any]] = []
        for part in own:
            kind = part.get("type")
            if kind == "text" or kind in _AUDIO:
                value = str(part.get("text") or part.get("transcript") or "")
                if value:
                    out.append(_text_part(value))
            elif kind == "image":
                ref = _ref_of(part)
                if not _has_ref(ref):
                    out.append(_text_part(msg("media_image_placeholder", self.locale)))
                    continue
                try:
                    out.append(_image_part(await self.fetcher.fetch_image(ref)))
                except MediaError:
                    out.append(_text_part(msg("media_image_failed_item", self.locale)))
            elif kind == "file":
                ref = _ref_of(part)
                if not _has_ref(ref):
                    name = part.get("filename") or msg("unknown_filename", self.locale)
                    out.append(_text_part(msg("media_file_missing", self.locale, name=name)))
                    continue
                try:
                    media = await self.fetcher.fetch_file(
                        ref, filename=part.get("filename") or None
                    )
                except MediaError as exc:
                    # 附件是用户这轮的主诉求，下不下来就别硬着头皮问模型。
                    failed = msg("media_file_failed", self.locale, error=self._reason(exc))
                    return BuiltContent([], "", "mixed", None, None, failed, "media_failed")
                out.append(
                    _text_part(msg("media_prompt_file", self.locale, name=media.filename or ""))
                )
                out.append(_file_part(media))
                files.append(_file_info(media))
        if not out:
            empty = msg("media_mixed_empty", self.locale)
            return BuiltContent([_text_part(empty)], empty, "mixed", None, None, None)
        if not any(p["type"] == "text" for p in out):
            out.insert(0, _text_part(msg("media_prompt_images", self.locale)))
        file_info = files[0] if len(files) == 1 else ({"files": files} if files else None)
        return BuiltContent(out, _joined_text(out), "mixed", None, file_info, None)

    async def _with_quote(self, quote: dict[str, Any], text: str) -> BuiltContent:
        """带引用的消息：引用内容拼进用户这句话的前缀，图片另外挂成 image part。"""
        kind = str(quote.get("kind") or "text")
        refs = [r for r in (quote.get("refs") or []) if isinstance(r, dict)]
        quoted = quote.get("text")
        if kind in ("text", "voice"):
            key = "quote_text_prefix" if kind == "text" else "quote_voice_prefix"
            body = msg(key, self.locale, quoted=quoted or "", text=text)
            return BuiltContent([_text_part(body)], body, "text", quoted or "", None, None)
        if kind == "image":
            ref = dict(refs[0]) if refs else {}
            body = msg("quote_image_prefix", self.locale, text=text)
            if not _has_ref(ref):
                return BuiltContent([_text_part(body)], body, "quote_image", None, None, None)
            await self._hint("downloading_quote_image")
            try:
                media = await self.fetcher.fetch_image(ref)
            except MediaError:
                # 引用的图只是上下文，丢了也能继续聊，不像主图那样必须报错。
                return BuiltContent([_text_part(body)], body, "quote_image", None, None, None)
            return BuiltContent(
                [_text_part(body), _image_part(media)], body, "quote_image", None, None, None
            )
        if kind == "file":
            ref = dict(refs[0]) if refs else {}
            unknown = msg("unknown_filename", self.locale)
            fetched: Media | None = None
            if _has_ref(ref):
                await self._hint("downloading_quote_file")
                try:
                    fetched = await self.fetcher.fetch_file(ref, filename=None)
                except MediaError:
                    # 引用的附件同样只是上下文：拿不到就只留一句「引用了文件」。
                    fetched = None
            if fetched is None:
                body = msg("quote_file_prefix", self.locale, name=unknown, text=text)
                return BuiltContent([_text_part(body)], body, "quote_file", None, None, None)
            name = fetched.filename or unknown
            body = msg("quote_file_prefix", self.locale, name=name, text=text)
            return BuiltContent(
                [_text_part(body), _file_part(fetched)],
                body,
                "quote_file",
                None,
                _file_info(fetched),
                None,
            )
        if kind == "mixed":
            return await self._quote_mixed(refs, text)
        # 未知引用类型：降级为普通文本，总比整条消息丢掉强。
        return BuiltContent([_text_part(text)], text, "text", None, None, None)

    async def _quote_mixed(self, refs: list[dict[str, Any]], text: str) -> BuiltContent:
        """被引用的是图文混排：文字按顺序拼成一句，图片挂在后面。"""
        items = (refs[0].get("msg_item") if refs else None) or []
        pieces: list[str] = []
        images: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("msgtype") == "text":
                pieces.append(str((item.get("text") or {}).get("content") or ""))
            elif item.get("msgtype") == "image":
                ref = dict(item.get("image") or {})
                pieces.append(msg("media_image_placeholder", self.locale))
                if _has_ref(ref):
                    try:
                        images.append(_image_part(await self.fetcher.fetch_image(ref)))
                    except MediaError:
                        pieces[-1] = msg("media_image_failed_item", self.locale)
        quoted_text = " ".join(p for p in pieces if p)
        body = msg("quote_text_prefix", self.locale, quoted=quoted_text, text=text)
        return BuiltContent(
            [_text_part(body), *images], body, "quote_mixed", quoted_text, None, None
        )
