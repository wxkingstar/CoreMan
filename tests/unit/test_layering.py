"""分层门禁：core 是最底层，不得依赖 api / runtime；api 只有 dev 路由可以引用 runtime。

CI 里另有同义的 grep 门禁；这里用 AST 连函数体内的延迟 import 一起检查，本地跑 pytest 就能发现。
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "coreman"

# api 进程里只有开发环境注入路由需要直接调用网关的入站函数（仅 COREMAN_ENV=dev 时注册）。
API_RUNTIME_ALLOWED = {"api/routers/dev.py"}


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def _violations(layer: str, forbidden: tuple[str, ...], allowed: set[str]) -> list[str]:
    found = []
    for path in sorted((PACKAGE / layer).rglob("*.py")):
        rel = path.relative_to(PACKAGE).as_posix()
        if rel in allowed:
            continue
        for module in sorted(_imported_modules(path)):
            if any(module == f or module.startswith(f + ".") for f in forbidden):
                found.append(f"coreman/{rel} -> {module}")
    return found


def test_core_does_not_depend_on_api_or_runtime() -> None:
    assert _violations("core", ("coreman.api", "coreman.runtime"), set()) == []


def test_api_does_not_depend_on_runtime() -> None:
    assert _violations("api", ("coreman.runtime",), API_RUNTIME_ALLOWED) == []


def test_runtime_does_not_depend_on_api() -> None:
    assert _violations("runtime", ("coreman.api",), set()) == []
