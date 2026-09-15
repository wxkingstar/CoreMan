"""运行时发布包的源码摘要：发布包与本地源码树不一致时拒绝下发，避免新节点装上过期的守护进程与驱动。

摘要规则与 runtime_daemon/build.py 的 source_manifest 保持一致（有测试校验两边结果相同）。
镜像内只有构建好的发布包、没有 runtime_daemon 源码，此时不校验。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

MANIFEST_SUFFIX = ".manifest.json"


def source_files(root: Path) -> list[Path]:
    """参与摘要的源码：守护进程 Python 源、依赖清单与驱动源码（不含 Go 测试与测试数据）。"""
    daemon = root / "runtime_daemon"
    drivers = daemon / "drivers"
    files = [*sorted(daemon.glob("*.py")), daemon / "requirements.txt"]
    for path in sorted(drivers.rglob("*")):
        parts = path.relative_to(drivers).parts
        if (
            path.is_file()
            and not path.name.endswith("_test.go")
            and "testdata" not in parts
            and not any(part.startswith(".") for part in parts)
        ):
            files.append(path)
    return files


def source_manifest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in source_files(root)
    }


def source_digest(files: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(f"{name}\0{files[name]}\n".encode())
    return digest.hexdigest()


def manifest_path(bundle: Path) -> Path:
    return bundle.with_name(bundle.name + MANIFEST_SUFFIX)


def stale_bundle_reason(root: Path, bundle: Path) -> str | None:
    """本地存在源码树时校验发布包的源码摘要；不一致返回可操作的说明，一致或无源码返回 None。"""
    if not (root / "runtime_daemon/daemon.py").is_file():
        return None
    try:
        recorded = json.loads(manifest_path(bundle).read_text())["source_digest"]
    except (OSError, ValueError, KeyError, TypeError):
        return (
            "发布包缺少源码摘要清单，无法确认与当前源码一致；"
            "请运行 python runtime_daemon/build.py 重新构建发布包"
        )
    if recorded != source_digest(source_manifest(root)):
        return (
            "发布包早于当前源码构建（源码摘要不一致）；"
            "请运行 python runtime_daemon/build.py 重新构建发布包"
        )
    return None
