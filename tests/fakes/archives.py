"""造压缩包的测试公共件：普通的包、带文件类型位的条目、标了加密位的条目。"""

from __future__ import annotations

import io
import zipfile

FIXED_TIME = (2026, 1, 1, 0, 0, 0)


def pack(files: dict[str, str | bytes]) -> bytes:
    """按给的顺序把（文件名 → 内容）打成 zip；名字以 `/` 结尾的是目录条目。

    条目时间固定：包的字节进了参数化用例的 ID，带当前时间的话 xdist 各 worker
    收集到的用例会因为跨秒而对不上。
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            # 与 writestr(文件名) 一致的权限位：目录 0o40775 加 DOS 目录位，文件 0o600。
            info.external_attr = (0o40775 << 16 | 0x10) if name.endswith("/") else 0o600 << 16
            zf.writestr(info, content)
    return buffer.getvalue()


def special(name: str, kind: int) -> bytes:
    """只有一个条目的 zip，条目带着文件类型位（`stat.S_IFLNK`、`stat.S_IFIFO`……）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
        info.external_attr = (kind | 0o777) << 16
        zf.writestr(info, "target")
    return buffer.getvalue()


def encrypted(name: str) -> bytes:
    """只有一个条目的 zip，中央目录里它的通用标志位第 0 位（加密）被置上。"""
    data = bytearray(pack({name: "x"}))
    at = data.rfind(b"PK\x01\x02")
    data[at + 8] |= 0x1
    return bytes(data)
