import hashlib
import io
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from coreman.core.db.models import StoredObject
from coreman.core.s3_store import S3ObjectStore
from coreman.core.timeutils import utcnow


class FakeS3:
    def __init__(self):
        self.files = {}
        self.deleted = []
        self.closed = 0

    def put_object(self, **args):
        self.files[args["Key"]] = {"body": args["Body"].read(), "modified": utcnow()}
        return {"VersionId": "v1"}

    def get_object(self, **args):
        value = self.files[args["Key"]]["body"]
        return {"Body": io.BytesIO(value), "ContentLength": len(value)}

    def delete_objects(self, **args):
        self.deleted.extend(args["Delete"]["Objects"])
        for item in args["Delete"]["Objects"]:
            self.files.pop(item["Key"], None)
        return {"Deleted": args["Delete"]["Objects"]}

    def list_objects_v2(self, **args):
        return {
            "Contents": [
                {"Key": key, "LastModified": item["modified"]} for key, item in self.files.items()
            ]
        }

    def close(self):
        self.closed += 1


async def source():
    yield b"private-attachment"


def config(api_settings):
    from pydantic import SecretStr

    return api_settings.model_copy(
        update={
            "s3_bucket": "test-bucket",
            "s3_access_key": SecretStr("synthetic-access-key"),
            "s3_secret_key": SecretStr("synthetic-secret-key"),
        }
    )


async def test_s3_upload_signed_download_cleanup_and_store_binding(
    client, app, db_session, api_settings, monkeypatch
):
    cfg = config(api_settings)
    app.state.settings = cfg
    fake = FakeS3()
    monkeypatch.setattr(S3ObjectStore, "client", lambda self: fake)
    store = S3ObjectStore(cfg)
    now = utcnow()
    row = await store.put(
        db_session, source(), filename="../private\r\n.txt", content_type="text/html", now=now
    )
    await db_session.commit()
    assert (
        row.filename == "private.txt"
        and row.sha256 == hashlib.sha256(b"private-attachment").hexdigest()
    )
    assert row.storage_metadata["version_id"] == "v1"
    response = await client.get(store.url(row))
    assert response.status_code == 200, response.text
    assert response.content == b"private-attachment"
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"].startswith("attachment;")
    wrong = S3ObjectStore(cfg.model_copy(update={"s3_bucket": "different-bucket"}))
    with pytest.raises(ValueError):
        wrong.object_key(row)
    assert await store.cleanup(db_session, now + timedelta(hours=25)) == 1
    await db_session.commit()
    assert await db_session.scalar(select(StoredObject)) is None
    assert fake.deleted[0]["VersionId"] == "v1"


async def test_s3_orphans_are_limited_to_owned_keys_and_age(api_settings, db_session, monkeypatch):
    fake = FakeS3()
    store = S3ObjectStore(config(api_settings))
    monkeypatch.setattr(S3ObjectStore, "client", lambda self: fake)
    old, fresh = utcnow() - timedelta(hours=26), utcnow()
    old_key, new_key = store.prefix + uuid.uuid4().hex, store.prefix + uuid.uuid4().hex
    foreign = "other/" + uuid.uuid4().hex
    for key, modified in [
        (old_key, old),
        (new_key, fresh),
        (foreign, old),
        (store.prefix + "manual-document", old),
    ]:
        fake.files[key] = {"body": b"x", "modified": modified}
    assert await store.cleanup(db_session, utcnow()) == 1
    assert set(fake.files) == {new_key, foreign, store.prefix + "manual-document"}


def test_s3_requires_explicit_credentials_and_https(api_settings):
    with pytest.raises(ValueError):
        S3ObjectStore(api_settings)
    for endpoint in [
        "http://private.example",
        "https://user:secret@bucket.example",
        "https://169.254.169.254",
    ]:
        with pytest.raises(ValueError):
            S3ObjectStore(config(api_settings).model_copy(update={"s3_endpoint": endpoint}))
