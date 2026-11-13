"""封存形式：明文键留在外面，其余进密文；封装顶层的键优先；AAD 不对解不开。"""

from __future__ import annotations

import json
import uuid

import pytest

from coreman.core.assistants import sealed
from coreman.core.crypto import Cipher, DecryptError
from coreman.core.db.models import (
    AssistantApproval,
    AssistantFollowup,
    AssistantInbox,
    AssistantJob,
    AssistantPage,
    AssistantSettings,
    AssistantTranscript,
    AssistantWatch,
    ChatLog,
    CloudPendingApproval,
    FeishuDelivery,
    FeishuSentMessage,
    InboundEvent,
    InteractionState,
    OutboxItem,
    Task,
    TaskStream,
)

CIPHER = Cipher(b"\x09" * 32, key_id="d1")
AAD = "tasks.payload:bot-a"
SECRET = "机密-片段-7f3a"


def _text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def test_every_content_column_is_registered() -> None:
    assert set(sealed.COLUMNS) == {
        InboundEvent,
        Task,
        TaskStream,
        OutboxItem,
        ChatLog,
        FeishuSentMessage,
        FeishuDelivery,
        InteractionState,
        AssistantApproval,
        CloudPendingApproval,
        AssistantTranscript,
        AssistantSettings,
        AssistantPage,
        AssistantFollowup,
        AssistantJob,
        AssistantInbox,
        AssistantWatch,
    }
    assert set(sealed.COLUMNS[FeishuDelivery]) == {"layout"}
    assert set(sealed.COLUMNS[InteractionState]) == {"state"}
    assert set(sealed.COLUMNS[AssistantApproval]) == {"args_enc", "reason_enc", "result_enc"}
    assert set(sealed.COLUMNS[CloudPendingApproval]) == {"reason"}
    assert set(sealed.COLUMNS[AssistantTranscript]) == {"blob_enc"}
    assert set(sealed.COLUMNS[AssistantWatch]) == {"head_enc"}
    assert set(sealed.COLUMNS[AssistantInbox]) == {"summary_enc"}
    assert set(sealed.COLUMNS[AssistantFollowup]) == {"title_enc", "notes_enc"}
    assert set(sealed.COLUMNS[AssistantJob]) == {
        "title_enc",
        "brief_enc",
        "message_enc",
        "result_enc",
        "blocked_enc",
    }
    assert set(sealed.COLUMNS[AssistantSettings]) == {"focus_chats_enc", "ignored_chats_enc"}
    assert set(sealed.COLUMNS[AssistantPage]) == {
        "path_enc",
        "title_enc",
        "summary_enc",
        "body_enc",
    }
    assert set(sealed.COLUMNS[Task]) == {"payload", "result", "error_message"}
    assert set(sealed.COLUMNS[FeishuSentMessage]) == {"text"}
    assert set(sealed.COLUMNS[TaskStream]) == {
        "thinking_md",
        "pending_text",
        "final_text",
        "pending_card",
        "plan",
        "activity",
    }
    assert set(sealed.COLUMNS[ChatLog]) == {
        "message_content",
        "quoted_content",
        "response_content",
        "file_info",
        "error_message",
    }
    assert {"serialize_session", "stale_hold", "platform_user_id"} <= sealed.TASK_PLAIN


def test_aad_names_table_column_and_bot() -> None:
    bot = uuid.UUID(int=7)
    assert sealed.aad("tasks", "payload", bot) == f"tasks.payload:{bot}"


def test_dict_keeps_plain_keys_and_hides_the_rest() -> None:
    spec = sealed.JsonSpec(plain=frozenset({"stale_hold", "platform_user_id"}))
    value = {"stale_hold": True, "platform_user_id": "ou_1", "message": {"text": SECRET}}
    out = sealed.seal_dict(CIPHER, AAD, value, spec)
    assert set(out) == {"_sealed", "stale_hold", "platform_user_id"}
    assert out["stale_hold"] is True and SECRET not in _text(out)
    assert sealed.is_sealed(out) and not sealed.is_sealed(value)
    assert sealed.open_dict(CIPHER, AAD, out, spec) == value
    # 没封存过的原样返回。
    assert sealed.open_dict(CIPHER, AAD, value, spec) == value


def test_envelope_top_level_keys_win_on_open() -> None:
    spec = sealed.JsonSpec(plain=frozenset({"stale_hold", "doc_links"}))
    out = sealed.seal_dict(CIPHER, AAD, {"stale_hold": True, "message": SECRET}, spec)
    # 不解密就能加键、删键（裸 SQL 的 payload || …、放行排队消息）。
    edited = {k: v for k, v in out.items() if k != "stale_hold"} | {"doc_links": ["doc_x"]}
    assert sealed.open_dict(CIPHER, AAD, edited, spec) == {
        "message": SECRET,
        "doc_links": ["doc_x"],
    }


def test_underscore_keys_stay_plain_and_card_task_id_is_mirrored() -> None:
    spec = sealed.JsonSpec(underscore=True, mirrors=(("card", "task_id"),))
    value = {
        "card": {"task_id": "choice:1:q0", "body": SECRET},
        "markdown": SECRET,
        "_feishu_message_id": "om_1",
    }
    out = sealed.seal_dict(CIPHER, AAD, value, spec)
    assert out["_feishu_message_id"] == "om_1" and out["card"] == {"task_id": "choice:1:q0"}
    assert SECRET not in _text(out)
    # 还原以密文里的整张卡为准，镜像不覆盖它。
    assert sealed.open_dict(CIPHER, AAD, out, spec) == value


def test_resealing_an_envelope_absorbs_stray_keys() -> None:
    spec = sealed.JsonSpec(plain=frozenset({"stale_hold"}))
    out = sealed.seal_dict(CIPHER, AAD, {"message": "a"}, spec)
    edited = {**out, "note": SECRET, "stale_hold": True}
    again = sealed.seal_dict(CIPHER, AAD, edited, spec, envelope=True)
    assert set(again) == {"_sealed", "stale_hold"} and SECRET not in _text(again)
    assert sealed.open_dict(CIPHER, AAD, again, spec) == {
        "message": "a",
        "note": SECRET,
        "stale_hold": True,
    }


def test_ciphertext_cannot_move_to_another_bot_column_or_table() -> None:
    spec = sealed.JsonSpec()
    out = sealed.seal_dict(CIPHER, AAD, {"message": SECRET}, spec)
    for other in ("tasks.payload:bot-b", "tasks.result:bot-a", "outbox.payload:bot-a"):
        with pytest.raises(DecryptError):
            sealed.open_dict(CIPHER, other, out, spec)
    with pytest.raises(DecryptError):
        sealed.open_dict(Cipher(b"\x0a" * 32, key_id="d1"), AAD, out, spec)


def test_another_generation_is_locked_not_an_error() -> None:
    # 重新签发之后：旧一代（d1）的密文遇到新一代（d2）的密钥，按已锁定处理，不算篡改。
    reissued = Cipher(b"\x0b" * 32, key_id="d2")
    old_dict = sealed.seal_dict(CIPHER, AAD, {"message": SECRET}, sealed.JsonSpec())
    old_whole = sealed.seal_whole(CIPHER, AAD, [SECRET])
    old_text = sealed.seal_text(CIPHER, AAD, SECRET)
    with pytest.raises(sealed.ContentLocked):
        sealed.open_dict(reissued, AAD, old_dict, sealed.JsonSpec())
    with pytest.raises(sealed.ContentLocked):
        sealed.open_whole(reissued, AAD, old_whole)
    with pytest.raises(sealed.ContentLocked):
        sealed.open_text(reissued, AAD, old_text)
    # 新一代自己写的照常读得出。
    fresh = sealed.seal_text(reissued, AAD, SECRET)
    assert fresh is not None and fresh.startswith("enc:v2:d2:")
    assert sealed.open_text(reissued, AAD, fresh) == SECRET


def test_tombstone_is_reported_as_locked() -> None:
    with pytest.raises(sealed.ContentLocked):
        sealed.open_dict(CIPHER, AAD, {"_sealed": "", "stale_hold": True}, sealed.JsonSpec())
    with pytest.raises(sealed.ContentLocked):
        sealed.open_whole(CIPHER, AAD, {"_sealed": ""})


def test_whole_value_and_text() -> None:
    plan = [{"text": SECRET, "status": "pending"}]
    stored = sealed.seal_whole(CIPHER, AAD, plan)
    assert set(stored) == {"_sealed"} and SECRET not in _text(stored)
    assert sealed.open_whole(CIPHER, AAD, stored) == plan
    assert sealed.open_whole(CIPHER, AAD, plan) == plan
    token = sealed.seal_text(CIPHER, AAD, SECRET)
    assert token is not None and token.startswith("enc:v2:d1:") and SECRET not in token
    assert sealed.open_text(CIPHER, AAD, token) == SECRET
    assert sealed.open_text(CIPHER, AAD, "明文") == "明文"
    assert sealed.seal_text(CIPHER, AAD, "") == "" and sealed.seal_text(CIPHER, AAD, None) is None


def test_blob_round_trip_generations_and_tampering() -> None:
    data = b"\x1f\x8b" + SECRET.encode() + bytes(range(256))
    token = sealed.seal_blob(CIPHER, AAD, data)
    assert token is not None and token.startswith(b"enc:v2:d1:")
    assert SECRET.encode() not in token
    assert sealed.is_blob_token(token) and not sealed.is_blob_token(data)
    assert sealed.open_blob(CIPHER, AAD, token) == data
    # 不是密文的原样返回；空值不加密。
    assert sealed.open_blob(CIPHER, AAD, b"plain") == b"plain"
    assert sealed.seal_blob(CIPHER, AAD, b"") == b"" and sealed.seal_blob(CIPHER, AAD, None) is None
    with pytest.raises(sealed.ContentLocked):
        sealed.open_blob(Cipher(b"\x0b" * 32, key_id="d2"), AAD, token)
    with pytest.raises(DecryptError):
        sealed.open_blob(CIPHER, "assistant_transcripts.blob_enc:bot-b", token)
    tampered = token[:-1] + bytes([token[-1] ^ 1])
    with pytest.raises(DecryptError):
        sealed.open_blob(CIPHER, AAD, tampered)


def test_master_cipher_bytes_use_the_key_id() -> None:
    master = Cipher(b"\x01" * 32, key_id="k9", previous={"k1": b"\x02" * 32})
    token = master.encrypt_bytes(b"payload", "t.c:x")
    assert token.startswith(b"enc:v2:k9:")
    assert master.decrypt_bytes(token, "t.c:x") == b"payload"
    old = Cipher(b"\x02" * 32, key_id="k1").encrypt_bytes(b"old", "t.c:x")
    assert master.decrypt_bytes(old, "t.c:x") == b"old"
    for bad in (b"enc:v1:abc", b"enc:v2:nokid", b"enc:v2:k9:short"):
        with pytest.raises(DecryptError):
            master.decrypt_bytes(bad, "t.c:x")


def _peak(call) -> int:  # type: ignore[no-untyped-def]
    import tracemalloc

    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        base = tracemalloc.get_traced_memory()[0]
        result = call()
        peak = tracemalloc.get_traced_memory()[1] - base
    finally:
        tracemalloc.stop()
    del result
    return peak


def test_large_blobs_are_sealed_and_opened_without_extra_full_copies() -> None:
    size = 8 * 1024 * 1024
    data = bytes(size)
    token = sealed.seal_blob(CIPHER, AAD, data)
    assert token is not None
    # 封存：密文一份、输出一份；打开：只有明文一份（不再先切出整段再切一次）。
    assert _peak(lambda: CIPHER.encrypt_bytes(data, AAD)) < 2.5 * size
    assert _peak(lambda: CIPHER.decrypt_bytes(token, AAD)) < 1.5 * size
    assert _peak(lambda: sealed.open_blob(CIPHER, AAD, token)) < 1.5 * size
    assert sealed.open_blob(CIPHER, AAD, token) == data


def test_malformed_blobs_still_fail_the_same_way() -> None:
    token = CIPHER.encrypt_bytes(b"payload", AAD)
    for bad in (
        b"enc:v2:d1",
        b"enc:v2:d1:",
        b"enc:v2:d1:" + b"x" * 27,
        token[:-1] + bytes([token[-1] ^ 1]),
        b"enc:v2:\xff\xfe:" + b"x" * 40,
        b"enc:v2:zz:" + b"x" * 40,
    ):
        with pytest.raises(DecryptError):
            CIPHER.decrypt_bytes(bad, AAD)


def test_transcript_transfer_slots() -> None:
    from coreman.api.routers import runtime_protocol

    assert runtime_protocol.TRANSCRIPT_TRANSFERS == 2
