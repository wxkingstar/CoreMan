"""发布包源码摘要：构建脚本与下发接口必须算出同一个摘要，过期的发布包不能下发。"""

import json
from pathlib import Path

from coreman.core.runtime_nodes import bundle
from runtime_daemon import build

REPO = Path(__file__).resolve().parents[2]


def test_build_script_and_api_agree_on_the_repository_digest():
    files = build.source_manifest(REPO)
    assert files == bundle.source_manifest(REPO)
    assert build.source_digest(files) == bundle.source_digest(files)
    assert "runtime_daemon/daemon.py" in files and "runtime_daemon/drivers/go.mod" in files
    assert not any(name.endswith("_test.go") for name in files)


def fake_tree(root: Path) -> None:
    daemon = root / "runtime_daemon"
    (daemon / "drivers/cmd/relay-x/testdata").mkdir(parents=True)
    (daemon / "drivers/.cache").mkdir()
    (daemon / "daemon.py").write_text("daemon")
    (daemon / "requirements.txt").write_text("httpx")
    (daemon / "drivers/go.mod").write_text("module x")
    (daemon / "drivers/cmd/relay-x/main.go").write_text("package main")
    (daemon / "drivers/cmd/relay-x/main_test.go").write_text("package main")
    (daemon / "drivers/cmd/relay-x/testdata/case.json").write_text("{}")
    (daemon / "drivers/.cache/state").write_text("x")


def test_digest_follows_shipped_sources_only(tmp_path):
    fake_tree(tmp_path)
    files = bundle.source_manifest(tmp_path)
    assert sorted(files) == [
        "runtime_daemon/daemon.py",
        "runtime_daemon/drivers/cmd/relay-x/main.go",
        "runtime_daemon/drivers/go.mod",
        "runtime_daemon/requirements.txt",
    ]
    before = bundle.source_digest(files)
    (tmp_path / "runtime_daemon/drivers/cmd/relay-x/main_test.go").write_text("changed")
    assert bundle.source_digest(bundle.source_manifest(tmp_path)) == before
    (tmp_path / "runtime_daemon/drivers/cmd/relay-x/main.go").write_text("changed")
    assert bundle.source_digest(bundle.source_manifest(tmp_path)) != before


def test_stale_bundle_detection(tmp_path):
    archive = tmp_path / "dist/coreman-runtime-linux-amd64.tar.gz"
    archive.parent.mkdir()
    archive.write_bytes(b"bundle")
    # 镜像内：没有源码树，不校验。
    assert bundle.stale_bundle_reason(tmp_path, archive) is None
    fake_tree(tmp_path)
    missing = bundle.stale_bundle_reason(tmp_path, archive)
    assert missing and "build.py" in missing
    digest = bundle.source_digest(bundle.source_manifest(tmp_path))
    manifest = bundle.manifest_path(archive)
    manifest.write_text(json.dumps({"source_digest": digest}))
    assert bundle.stale_bundle_reason(tmp_path, archive) is None
    (tmp_path / "runtime_daemon/daemon.py").write_text("newer")
    stale = bundle.stale_bundle_reason(tmp_path, archive)
    assert stale and "摘要不一致" in stale and "build.py" in stale
