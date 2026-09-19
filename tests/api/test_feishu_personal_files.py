"""Files move between the owner's Feishu data and the agent through short-lived sealed links."""

import json

import httpx
import pytest
import respx

from coreman.core.db.models import FeishuPersonalGrant
from coreman.core.feishu_personal import files
from tests.api.test_feishu_personal import URL, grant, headers, rpc, setup, value

FEISHU = "https://open.feishu.cn/open-apis"
SCOPES = [
    "im:message:readonly",
    "im:message",
    "im:message.send_as_user",
    "im:resource",
    "drive:file:download",
    "drive:file:upload",
]


@pytest.fixture
async def owner(app, db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(app.state.settings, "object_storage_root", str(tmp_path))
    bot, user, task = await setup(db_session, app)
    await grant(
        db_session,
        app,
        bot,
        user,
        authorization_level="all",
        scopes=SCOPES,
        requested_scopes=SCOPES,
    )
    return bot, user, await headers(app, task, user)


def _path(url):
    assert url.startswith("http://testserver/api/runtime/feishu-personal/")
    return url.removeprefix("http://testserver")


async def test_chat_file_is_fetched_as_the_owner_and_served_without_credentials(
    client, db_session, owner
):
    bot, user, auth = owner
    with respx.mock as mock:
        route = mock.get(f"{FEISHU}/im/v1/messages/om_1/resources/fk_1").mock(
            return_value=httpx.Response(
                200,
                content=b"quarterly numbers",
                headers={
                    "content-type": "application/pdf",
                    "content-disposition": "attachment; filename*=UTF-8''%E6%8A%A5%E5%91%8A.pdf",
                },
            )
        )
        out = value(
            await client.post(
                URL,
                headers=auth,
                json=rpc(
                    "feishu_download_message_file", {"message_id": "om_1", "file_key": "fk_1"}
                ),
            )
        )
    assert route.calls[0].request.headers["authorization"] == "Bearer user-secret"
    assert route.calls[0].request.url.params["type"] == "file"
    assert out["filename"] == "报告.pdf" and out["size"] == 17
    assert "user-secret" not in json.dumps(out)
    # The agent's curl carries no credential; the sealed link is the only key.
    response = await client.get(_path(out["download_url"]))
    assert response.status_code == 200 and response.content == b"quarterly numbers"
    assert response.headers["content-disposition"].startswith("attachment")
    tampered = out["download_url"][:-4] + (
        "AAAA" if not out["download_url"].endswith("AAAA") else "BBBB"
    )
    assert (await client.get(_path(tampered))).status_code == 404
    # Reconnecting or revoking ends every link issued before.
    row = await db_session.get(FeishuPersonalGrant, (bot.id, user.id))
    row.status = "revoked"
    await db_session.commit()
    assert (await client.get(_path(out["download_url"]))).status_code == 404


async def test_uploaded_file_is_sent_in_chat_as_the_owner(client, owner):
    _, _, auth = owner
    prepared = value(
        await client.post(
            URL, headers=auth, json=rpc("feishu_prepare_upload", {"filename": "方案.pdf"})
        )
    )
    upload = _path(prepared["upload_url"])
    # A browser page cannot push files into someone's upload link.
    blocked = await client.put(upload, content=b"x", headers={"Origin": "https://example.com"})
    assert blocked.status_code == 403
    assert (await client.put(upload, content=b"")).status_code == 400
    stored = (await client.put(upload, content=b"%PDF-1.4 plan")).json()
    assert stored["filename"] == "方案.pdf" and stored["size"] == 13
    # A download link cannot be used as an upload link or the other way round.
    assert (await client.get(upload.replace("/uploads/", "/files/"))).status_code == 404
    with respx.mock as mock:
        uploaded = mock.post(f"{FEISHU}/im/v1/files").mock(
            return_value=httpx.Response(200, json={"code": 0, "data": {"file_key": "file_9"}})
        )
        sent = mock.post(f"{FEISHU}/im/v1/messages").mock(
            return_value=httpx.Response(200, json={"code": 0, "data": {"message_id": "om_9"}})
        )
        out = value(
            await client.post(
                URL,
                headers=auth,
                json=rpc(
                    "feishu_send_file",
                    {
                        "receive_id": "oc_team",
                        "receive_id_type": "chat_id",
                        "upload_id": stored["upload_id"],
                        "uuid": "file-1",
                    },
                ),
            )
        )
    assert out["message_id"] == "om_9"
    form = uploaded.calls[0].request.content
    assert b'name="file_type"\r\n\r\npdf' in form and b"%PDF-1.4 plan" in form
    body = json.loads(sent.calls[0].request.content)
    assert body == {
        "receive_id": "oc_team",
        "msg_type": "file",
        "content": json.dumps({"file_key": "file_9"}),
        "uuid": "file-1",
    }


async def test_upload_link_refuses_files_over_the_limit(client, owner, monkeypatch):
    _, _, auth = owner
    monkeypatch.setattr(files, "UPLOAD_LIMIT", 8)
    prepared = value(
        await client.post(
            URL, headers=auth, json=rpc("feishu_prepare_upload", {"filename": "a.bin"})
        )
    )
    response = await client.put(_path(prepared["upload_url"]), content=b"123456789")
    assert response.status_code == 413
    bad = value(
        await client.post(
            URL,
            headers=auth,
            json=rpc(
                "feishu_send_file",
                {
                    "receive_id": "oc_team",
                    "receive_id_type": "chat_id",
                    "upload_id": "not-a-real-upload-id",
                    "uuid": "file-2",
                },
            ),
        )
    )
    assert bad == {"error": "upload_not_found"}


async def _drive_download(client, auth):
    return value(
        await client.post(
            URL, headers=auth, json=rpc("feishu_download_drive_file", {"file_token": "box1"})
        )
    )


async def test_redirects_stay_on_feishu_and_drop_the_token(client, owner):
    _, _, auth = owner
    with respx.mock as mock:
        mock.get(f"{FEISHU}/drive/v1/files/box1/download").mock(
            return_value=httpx.Response(
                302, headers={"location": "https://cdn.feishucdn.com/obj/box1?sig=1"}
            )
        )
        cdn = mock.get("https://cdn.feishucdn.com/obj/box1").mock(
            return_value=httpx.Response(
                200, content=b"zip", headers={"content-type": "application/zip"}
            )
        )
        out = await _drive_download(client, auth)
        assert out["size"] == 3
        assert "authorization" not in cdn.calls[0].request.headers
        mock.get(f"{FEISHU}/drive/v1/files/box1/download").mock(
            return_value=httpx.Response(302, headers={"location": "https://evil.example.com/x"})
        )
        assert await _drive_download(client, auth) == {"error": "feishu_read_failed"}


async def test_json_files_download_and_feishu_errors_do_not(client, owner):
    _, _, auth = owner
    with respx.mock as mock:
        route = mock.get(f"{FEISHU}/drive/v1/files/box1/download")
        route.mock(return_value=httpx.Response(200, json={"code": 7, "rows": [1, 2]}))
        out = await _drive_download(client, auth)
        assert (await client.get(_path(out["download_url"]))).json() == {"code": 7, "rows": [1, 2]}
        route.mock(return_value=httpx.Response(400, json={"code": 1061004, "msg": "forbidden"}))
        failed = await _drive_download(client, auth)
    assert failed == {"error": "feishu_read_failed", "upstream_code": 1061004}
