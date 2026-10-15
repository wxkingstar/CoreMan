"""把目录里的 Git 技能装到用户自己的电脑：带时效的安装命令、技能打包与环境变量导出。

用户没有技能仓库的权限，由服务端代为克隆，只把这个技能的目录打包下发。
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import os
import shlex
import tarfile
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from coreman.core.crypto import Cipher
from coreman.core.knowledge.catalog_sync import clone_repository

TOKEN_AAD = "skill_local_install"
TOKEN_TTL = timedelta(minutes=30)
SCRIPT_PATH = "/api/skill-install/"
# 打包前统计的文件总大小上限；技能目录一般只有几十 KB。
ARCHIVE_LIMIT = 50 * 1024 * 1024
SKIP_DIRS = {".git", "node_modules"}
MAX_DEPTH = 8


def seal_token(cipher: Cipher, user_id: uuid.UUID, skill_id: uuid.UUID, now: datetime) -> str:
    claims = {"u": str(user_id), "s": str(skill_id), "x": int((now + TOKEN_TTL).timestamp())}
    raw = cipher.seal(json.dumps(claims, separators=(",", ":")), TOKEN_AAD)
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def open_token(cipher: Cipher, token: str, now: datetime) -> tuple[uuid.UUID, uuid.UUID]:
    """(用户, 技能)；伪造、损坏抛 ValueError("invalid")，过期抛 ValueError("expired")。"""
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        claims = json.loads(cipher.open(raw, TOKEN_AAD))
        user_id, skill_id, expires = uuid.UUID(claims["u"]), uuid.UUID(claims["s"]), claims["x"]
    except (binascii.Error, ValueError, KeyError, TypeError):
        raise ValueError("invalid") from None
    if expires <= now.timestamp():
        raise ValueError("expired")
    return user_id, skill_id


def install_command(base_url: str, token: str) -> str:
    return f"curl -fsSL {shlex.quote(base_url.rstrip('/') + SCRIPT_PATH + token)} | sh"


def skill_name_in(manifest: Path) -> str | None:
    """SKILL.md frontmatter 里的 name；没有或解析不了返回 None。"""
    try:
        text = manifest.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if not text.startswith("---"):
        return None
    head, sep, _ = text[3:].partition("\n---")
    if not sep:
        return None
    try:
        data = yaml.safe_load(head)
    except yaml.YAMLError:
        return None
    value = data.get("name") if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


def find_skill_dir(root: Path, name: str) -> Path:
    """仓库里 SKILL.md 的 name 与技能名一致的目录（同 npx skills，不分大小写），同名取最浅的。"""
    found: list[tuple[int, str, Path]] = []
    for current, dirs, files in os.walk(root):
        path = Path(current)
        depth = len(path.relative_to(root).parts)
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and depth < MAX_DEPTH]
        manifest = path / "SKILL.md"
        if "SKILL.md" in files and not manifest.is_symlink():
            declared = skill_name_in(manifest)
            if declared is not None and declared.lower() == name.lower():
                found.append((depth, str(path), path))
    if not found:
        raise LookupError(name)
    return min(found)[2]


def pack(directory: Path) -> bytes:
    """技能目录打成 tar.gz，成员位于包根；不含 .git，符号链接原样保留、不跟随。"""
    total = 0
    for current, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d != ".git"]
        for file in files:
            path = Path(current) / file
            if not path.is_symlink():
                total += path.stat().st_size
    if total > ARCHIVE_LIMIT:
        raise OverflowError(total)

    def clean(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if ".git" in Path(info.name).parts:
            return None
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        return info

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for child in sorted(directory.iterdir()):
            archive.add(child, arcname=child.name, filter=clean)
    return buffer.getvalue()


def fetch_skill(
    url: str, name: str, access_token: str | None = None, username: str | None = None
) -> bytes:
    """克隆技能所在仓库，打包其中名为 name 的技能目录。"""
    with tempfile.TemporaryDirectory(prefix="coreman-skill-") as directory:
        root = Path(directory) / "repo"
        clone_repository(url, root, access_token, username)
        return pack(find_skill_dir(root, name))


def repository(url: str) -> str:
    """去掉 Runtime 认的 `<仓库>.git@<技能名>` 后缀，只留仓库地址。"""
    head, sep, _ = url.partition(".git@")
    return head + ".git" if sep else url


def install_script(name: str, archive_url: str) -> str:
    """安装到当前目录的 .agents/skills/<name>，再在 .claude/skills/<name> 建相对软链。

    整段包在函数里，下载中断时 sh 不会执行半截脚本。重复执行即更新：先解压到同级临时目录，
    确认有 SKILL.md 后再替换旧目录。变量后紧跟中文时要写成 ${...}：有的 sh 会把多字节字符
    当成变量名的一部分。
    """
    return f"""#!/bin/sh
# CoreMan 技能本地安装：{name}
set -eu

main() {{
  name={shlex.quote(name)}
  url={shlex.quote(archive_url)}
  target=".agents/skills/$name"
  link=".claude/skills/$name"
  relative="../../.agents/skills/$name"
  for tool in curl tar; do
    command -v "$tool" >/dev/null 2>&1 || {{ echo "缺少命令：$tool" >&2; exit 1; }}
  done
  if [ -L "$link" ]; then
    if [ "$(readlink "$link")" != "$relative" ]; then
      echo "$link 已是指向其他位置的软链，请先移走后重试" >&2; exit 1
    fi
  elif [ -e "$link" ]; then
    echo "$link 已存在且不是软链，请先移走后重试" >&2; exit 1
  fi
  if [ -L "$target" ]; then
    echo "$target 是软链，请先移走后重试" >&2; exit 1
  fi
  mkdir -p .agents/skills .claude/skills
  staging=$(mktemp -d ".agents/skills/.$name.XXXXXX")
  trap 'rm -rf "$staging"' EXIT
  status=$(curl -sSL -o "$staging/skill.tar.gz" -w '%{{http_code}}' "$url") || {{
    echo "下载失败，请检查网络后重试" >&2; exit 1
  }}
  if [ "$status" != 200 ]; then
    echo "下载失败（HTTP ${{status}}）：$(head -c 500 "$staging/skill.tar.gz")" >&2; exit 1
  fi
  mkdir "$staging/skill"
  tar -xzf "$staging/skill.tar.gz" -C "$staging/skill"
  if [ ! -f "$staging/skill/SKILL.md" ]; then
    echo "下载内容缺少 SKILL.md" >&2; exit 1
  fi
  action=安装
  if [ -e "$target" ]; then
    action=更新
    rm -rf "$target"
  fi
  mv "$staging/skill" "$target"
  [ -L "$link" ] || ln -s "$relative" "$link"
  echo "已${{action}}技能 $name"
  echo "  $(pwd)/$target"
  echo "  $(pwd)/$link -> $relative"
}}

main
"""


def export_lines(
    skill_name: str,
    groups: list[tuple[str, str, dict[str, str]]],
    user_fields: dict[str, Any],
) -> str:
    """各环境组的变量拼成 export 语句（明文）；同名变量只写第一次。

    用户自填的配置没有值，写成注释提示自行填写，免得粘贴时把已有值清空。
    """
    lines = [f"# CoreMan 技能 {skill_name} 的环境变量（明文，请勿外传）"]
    seen: set[str] = set()
    for key, label, values in groups:
        fresh = {name: value for name, value in values.items() if name not in seen}
        if not fresh:
            continue
        lines.append(f"# {key}（{one_line(label)}）" if label and label != key else f"# {key}")
        for name, value in fresh.items():
            lines.append(f"export {name}={shlex.quote(value)}")
            seen.add(name)
    pending = [name for name in user_fields if name not in seen]
    if pending:
        lines.append("# 以下由使用者自行填写")
        for name in pending:
            field = user_fields[name]
            label = one_line(str(field.get("label") or ""))
            suffix = "（必填）" if field.get("required") else ""
            lines.append(f"# export {name}=''" + (f"  # {label}{suffix}" if label else suffix))
    return "\n".join(lines) + "\n"


def one_line(text: str) -> str:
    return " ".join(text.split())
