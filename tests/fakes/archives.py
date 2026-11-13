"""造压缩包的测试公共件：普通的包、带文件类型位的条目、标了加密位的条目。"""

from __future__ import annotations

import io
import zipfile


def pack(files: dict[str, str | bytes]) -> bytes:
    """按给的顺序把（文件名 → 内容）打成 zip；名字以 `/` 结尾的是目录条目。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buffer.getvalue()


def special(name: str, kind: int) -> bytes:
    """只有一个条目的 zip，条目带着文件类型位（`stat.S_IFLNK`、`stat.S_IFIFO`……）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        info = zipfile.ZipInfo(name)
        info.external_attr = (kind | 0o777) << 16
        zf.writestr(info, "target")
    return buffer.getvalue()


def encrypted(name: str) -> bytes:
    """只有一个条目的 zip，中央目录里它的通用标志位第 0 位（加密）被置上。"""
    data = bytearray(pack({name: "x"}))
    at = data.rfind(b"PK\x01\x02")
    data[at + 8] |= 0x1
    return bytes(data)
