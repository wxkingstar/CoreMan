"""回复里的本地文件：认链接、换算路径、经节点读回、上传飞书、排出站文件消息。"""

import base64
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from coreman.core.platforms.feishu import FeishuClient
from coreman.runtime.worker.chat import reply_files
from coreman.runtime.worker.chat.reply_files import ReplyFiles, file_links, relative_path


def test_file_links_skip_images_urls_anchors_and_duplicates() -> None:
    text = (
        "报表 [销售日报.xlsx](out/report.xlsx)，图 ![趋势](out/a.png)，"
        "[官网](https://example.com/a.pdf)、[锚点](#a.b)、[再一次](out/report.xlsx)、"
        "[目录](out/data)、[绝对](file:///srv/ws/bot/x%20y.csv)"
    )
    links = file_links(text)
    assert [(link.label, link.path) for link in links] == [
        ("销售日报.xlsx", "out/report.xlsx"),
        ("绝对", "/srv/ws/bot/x y.csv"),
    ]
    assert text[links[0].start : links[0].end] == "[销售日报.xlsx](out/report.xlsx)"
    many = " ".join(f"[f{i}](f{i}.txt)" for i in range(9))
    assert len(file_links(many)) == reply_files.MAX_FILES


@pytest.mark.parametrize(
    ("target", "working_dir", "expected"),
    [
        ("out/a.csv", "bot1", "out/a.csv"),
        ("./out/a.csv", "/srv/ws/bot1", "out/a.csv"),
        ("/srv/ws/bot1/out/a.csv", "/srv/ws/bot1", "out/a.csv"),
        ("/srv/ws/bot10/a.csv", "/srv/ws/bot1", None),
        ("/home/x/coreman/bot1/.worktrees/s1/a.csv", "bot1", ".worktrees/s1/a.csv"),
        ("/etc/passwd.txt", "bot1", None),
        ("../a.csv", "bot1", None),
        ("out/../../a.csv", "bot1", None),
    ],
)
def test_relative_path(target: str, working_dir: str, expected: str | None) -> None:
    assert relative_path(target, working_dir) == expected


def _feishu(handler) -> FeishuClient:
    def route(request: httpx.Request) -> httpx.Response:
        if "access_token" in request.url.path:
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "synthetic", "expire": 7200}
            )
        return handler(request)

    http = httpx.AsyncClient(
        base_url="https://open.feishu.cn", transport=httpx.MockTransport(route)
    )
    return FeishuClient("cli", "secret", http=http)


async def test_attach_reads_uploads_queues_and_rewrites(monkeypatch) -> None:
    content = b"a,b\n" * 100_000  # 400KB：按节点报的块大小分几次读
    agent_calls: list[tuple[str, dict[str, Any]]] = []

    async def call_agent(relay, cipher, op, payload=None):
        agent_calls.append((op, payload or {}))
        if op == "ping":
            return {"workspace_protocol": 1, "workspace_chunk": 150_000}
        path = payload["path"]
        if path == "big.zip":
            return {"data": "", "size": reply_files.MAX_BYTES + 1, "eof": False}
        if path == "missing.txt":
            raise reply_files.AgentError("文件类型或大小不支持")
        part = content[payload["offset"] : payload["offset"] + payload["length"]]
        return {
            "data": base64.b64encode(part).decode(),
            "size": len(content),
            "eof": payload["offset"] + len(part) >= len(content),
        }

    queued: list[dict[str, Any]] = []

    async def add(session, **kw):
        queued.append(kw)

    uploads: list[bytes] = []

    def upload(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/open-apis/im/v1/files"
        uploads.append(request.content)
        return httpx.Response(200, json={"code": 0, "data": {"file_key": "file_v3_abc"}})

    monkeypatch.setattr(reply_files, "call_agent", call_agent)
    monkeypatch.setattr(reply_files.outbox, "add", add)
    bot: Any = SimpleNamespace(
        id=uuid.uuid4(), platform="feishu", working_dir="/srv/ws/bot1", credentials_enc=""
    )
    files = ReplyFiles(bot, SimpleNamespace(), None)  # type: ignore[arg-type]
    files._client = _feishu(upload)
    text = "做好了：[日报](out/report.csv)\n[包](big.zip) [缺](missing.txt) [外面](/etc/x.txt)"
    out = await files.attach(None, text, task_id=7, chat_id="oc1", reply_to="om_q")  # type: ignore[arg-type]
    assert out == (
        "做好了：📎 **report.csv**（已作为文件发送，见下方）\n"
        "📎 big.zip（超过 30MB，飞书发不了，可以让我压缩或拆分） "
        "📎 missing.txt（文件发送失败） 📎 x.txt（不在工作目录里，发不出来）"
    )
    reads = [p for op, p in agent_calls if op == "workspace-read" and p["path"] == "out/report.csv"]
    assert [p["offset"] for p in reads] == [0, 150_000, 300_000]
    assert all(p["working_dir"] == "/srv/ws/bot1" and p["bot_id"] == str(bot.id) for p in reads)
    assert len(uploads) == 1 and content in uploads[0] and b'name="file_type"' in uploads[0]
    assert b"\r\n\r\nxls\r\n" in uploads[0]  # csv 按表格类型上传
    [item] = queued
    assert item["kind"] == "send" and item["dedupe_key"] == "7:file:0"
    assert item["target"] == {"chat_id": "oc1", "message_id": "om_q"}
    assert item["payload"]["_file"] == {
        "file_key": "file_v3_abc",
        "name": "report.csv",
        "size": len(content),
        "media": None,
    }


def test_only_ordinary_feishu_bots() -> None:
    feishu: Any = SimpleNamespace(platform="feishu", kind="bot")
    wecom: Any = SimpleNamespace(platform="wecom", kind="bot")
    assistant: Any = SimpleNamespace(platform="feishu", kind="assistant")
    assert ReplyFiles.applies(feishu)
    assert not ReplyFiles.applies(wecom) and not ReplyFiles.applies(assistant)


def _box(kind: bytes, body: bytes) -> bytes:
    return (8 + len(body)).to_bytes(4, "big") + kind + body


def test_media_durations() -> None:
    from coreman.runtime.worker.chat.reply_files import mp4_duration_ms, opus_duration_ms

    mvhd = bytes(12) + (1000).to_bytes(4, "big") + (12_345).to_bytes(4, "big") + bytes(80)
    mp4 = _box(b"ftyp", b"isom" + bytes(4)) + _box(b"moov", _box(b"mvhd", mvhd))
    assert mp4_duration_ms(mp4) == 12_345
    assert mp4_duration_ms(b"not an mp4") is None
    head = b"OggS" + bytes(24) + b"OpusHead" + b"\x01\x02" + (312).to_bytes(2, "little") + bytes(8)
    tail = b"OggS" + b"\x00\x04" + (48_000 * 3 + 312).to_bytes(8, "little") + bytes(16)
    assert opus_duration_ms(head + tail) == 3000
    assert opus_duration_ms(b"OggS" + bytes(20)) is None


def test_media_card_is_not_forwardable() -> None:
    from coreman.runtime.gateway_feishu.cards import media_card

    video = media_card("video", "file_v3_x", "demo.mp4")
    assert video["config"]["enable_forward"] is False
    assert video["body"]["elements"][1]["tag"] == "video"
    audio = media_card("audio", "file_v3_y", "a.opus")
    assert audio["body"]["elements"][1]["tag"] == "audio"
