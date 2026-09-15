#!/usr/bin/env python3
"""Build all release bundles. No CLI accounts or local session files are packaged."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_NAME = "SOURCE-MANIFEST.json"


def source_files(root: Path) -> list[Path]:
    """Sources a bundle is built from: daemon Python, requirements and driver sources.

    Must match coreman/core/runtime_nodes/bundle.py, which refuses to serve a bundle whose
    recorded digest differs from the local source tree (a test keeps both in step).
    """
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime_daemon/dist")
    parser.add_argument(
        "--target",
        action="append",
        choices=["linux-amd64", "linux-arm64", "darwin-amd64", "darwin-arm64"],
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    files = source_manifest(ROOT)
    manifest = json.dumps({"source_digest": source_digest(files), "files": files}, indent=2)
    with tempfile.TemporaryDirectory(prefix="runtime-build-") as tmp:
        stage = Path(tmp)
        wheels = stage / "wheels"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--only-binary=:all:",
                "--platform",
                "any",
                "--python-version",
                "310",
                "--implementation",
                "py",
                "--abi",
                "none",
                "-r",
                str(ROOT / "runtime_daemon/requirements.txt"),
                "-d",
                str(wheels),
            ],
            check=True,
        )
        for target in args.target or ["linux-amd64", "linux-arm64", "darwin-amd64", "darwin-arm64"]:
            system, arch = target.split("-")
            bundle = stage / target
            (bundle / "runtime_daemon/bin").mkdir(parents=True)
            for src in (ROOT / "runtime_daemon").glob("*.py"):
                shutil.copy2(src, bundle / "runtime_daemon" / src.name)
            shutil.copy2(ROOT / "runtime_daemon/requirements.txt", bundle / "runtime_daemon")
            shutil.copy2(ROOT / "LICENSE", bundle / "LICENSE")
            shutil.copy2(ROOT / "THIRD_PARTY_NOTICES.md", bundle / "THIRD_PARTY_NOTICES.md")
            shutil.copy2(ROOT / "runtime_daemon/drivers/LICENSE", bundle / "LICENSE.clawrelay")
            shutil.copy2(ROOT / "runtime_daemon/drivers/SOURCE.json", bundle / "SOURCE.json")
            shutil.copytree(wheels, bundle / "wheels")
            (bundle / MANIFEST_NAME).write_text(manifest)
            for provider in ("claude", "codex"):
                subprocess.run(
                    [
                        "go",
                        "build",
                        "-trimpath",
                        "-o",
                        str(bundle / "runtime_daemon/bin" / ("runtime-" + provider)),
                        "./cmd/relay-" + provider,
                    ],
                    cwd=ROOT / "runtime_daemon/drivers",
                    env={**os.environ, "CGO_ENABLED": "0", "GOOS": system, "GOARCH": arch},
                    check=True,
                )
            path = args.output / f"coreman-runtime-{target}.tar.gz"
            with tarfile.open(path, "w:gz") as archive:
                for source in sorted(bundle.rglob("*")):
                    if source.is_file():
                        archive.add(
                            source, arcname=str(source.relative_to(bundle)), recursive=False
                        )
            checksum = hashlib.sha256(path.read_bytes()).hexdigest()
            path.with_suffix(path.suffix + ".sha256").write_text(checksum + "\n")
            # The API compares this with the local sources before serving the bundle.
            path.with_name(path.name + ".manifest.json").write_text(manifest)
            print(path)


if __name__ == "__main__":
    main()
