"""一次性容器初始化：仅设置共享附件目录的所有者，不递归改历史文件。"""

import os
from pathlib import Path


def initialize(root: str = "/data/storage", uid: int = 10001, gid: int = 10001) -> None:
    base = Path(root)
    for path in (base, base / "objects"):
        if path.is_symlink():
            raise ValueError("storage directory must not be a symlink")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chown(path, uid, gid)
        path.chmod(0o700)


if __name__ == "__main__":
    initialize()
