#!/usr/bin/env python3
"""Build all release bundles. No CLI accounts or local session files are packaged."""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
            print(path)


if __name__ == "__main__":
    main()
