"""Exercise install package delivery and revocation against isolated fixtures."""

import hashlib
import json
from pathlib import Path

from coreman.core.runtime_nodes import bundle as bundles
from tests.api.conftest import login_as

REPO = Path(__file__).resolve().parents[2]


async def test_bundle_bytes_checksum_no_store_and_revocation(client, db_session, app, tmp_path):
    await login_as(client, db_session, role="platform_admin")
    app.state.settings.runtime_bundle_dir = str(tmp_path)
    created = await client.post(
        "/api/admin/runtime-nodes/install-links", json={"workspace_root": "/tmp/qa-runtime"}
    )
    assert created.status_code == 200
    link = created.json()["data"]
    token = link["url"].split("/install/")[1].split("/")[0]
    url = f"/api/runtime/install/{token}/darwin/arm64/bundle"
    listed = await client.get("/api/admin/runtime-nodes/install-links")
    assert listed.status_code == 200
    assert listed.json()["data"][0]["id"] == link["id"]
    assert token not in listed.text
    assert (await client.get(url)).status_code == 503
    payload = b"qa-package-bytes"
    checksum = hashlib.sha256(payload).hexdigest()
    bundle = tmp_path / "coreman-runtime-darwin-arm64.tar.gz"
    bundle.write_bytes(payload)
    bundle.with_suffix(".gz.sha256").write_text(checksum + "  " + bundle.name)
    # 从源码运行时：没有源码摘要清单或摘要过期的发布包不下发，并提示重新构建。
    missing = await client.get(url)
    assert missing.status_code == 503 and "build.py" in missing.json()["message"]
    manifest = bundles.manifest_path(bundle)
    manifest.write_text(json.dumps({"source_digest": "0" * 64}))
    stale = await client.get(url)
    assert stale.status_code == 503 and "摘要不一致" in stale.json()["message"]
    digest = bundles.source_digest(bundles.source_manifest(REPO))
    manifest.write_text(json.dumps({"source_digest": digest}))
    response = await client.get(url)
    assert response.status_code == 200
    assert response.content == payload and response.headers["x-sha256"] == checksum
    assert "no-store" in response.headers["cache-control"]
    assert (await client.get(url.replace("darwin", "unsupported"))).status_code == 422
    assert (
        await client.delete(f"/api/admin/runtime-nodes/install-links/{link['id']}")
    ).status_code == 200
    assert (await client.get(url)).status_code == 401
    await login_as(client, db_session, role="member")
    assert (await client.get("/api/admin/runtime-nodes/install-links")).status_code == 403
