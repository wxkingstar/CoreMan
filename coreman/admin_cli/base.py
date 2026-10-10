"""各模块命令共用的参数与小工具。"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from coreman.admin_cli.client import ACTOR_ENV, AdminClient, CliError

Handler = Callable[[AdminClient, argparse.Namespace], Awaitable[int]]

COMMON = argparse.ArgumentParser(add_help=False)
COMMON.add_argument(
    "--as", dest="actor", metavar="登录名", help=f"以谁的身份操作（默认取环境变量 {ACTOR_ENV}）"
)
COMMON.add_argument("--json", action="store_true", help="输出接口返回的原始 JSON")


def leaf(ops: Any, name: str, help_text: str, handler: Handler) -> argparse.ArgumentParser:
    parser: argparse.ArgumentParser = ops.add_parser(
        name, help=help_text, description=help_text, parents=[COMMON]
    )
    parser.set_defaults(admin=handler)
    return parser


def group(sub: Any, name: str, help_text: str) -> Any:
    parser = sub.add_parser(name, help=help_text, description=help_text)
    return parser.add_subparsers(dest="operation", required=True, metavar="操作")


def add_yes(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--yes", action="store_true", help="确认执行（删除类操作必填）")


def require_yes(args: argparse.Namespace, what: str) -> None:
    if not args.yes:
        raise CliError(f"{what}不可撤销，确认无误后加 --yes 再执行")


def on_off(value: str) -> bool:
    lowered = value.lower()
    if lowered in ("on", "true", "yes", "1"):
        return True
    if lowered in ("off", "false", "no", "0"):
        return False
    raise argparse.ArgumentTypeError("取值为 on 或 off")


def pairs(values: list[str] | None) -> dict[str, str]:
    """把若干 KEY=VALUE 解析成字典；VALUE 可以含等号。"""
    out: dict[str, str] = {}
    for item in values or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise CliError(f"参数应为 KEY=VALUE：{item}")
        out[key] = value
    return out


def read_text(path: str) -> str:
    """读文本参数；- 表示从标准输入读。"""
    if path == "-":
        return sys.stdin.read()
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise CliError(f"读不了文件 {path}：{exc.strerror}") from None


def changed(args: argparse.Namespace, mapping: dict[str, str]) -> dict[str, Any]:
    """命令行上给了的参数 → 接口字段（没给的是 None，不出现在结果里）。"""
    return {
        field: getattr(args, dest)
        for dest, field in mapping.items()
        if getattr(args, dest) is not None
    }
