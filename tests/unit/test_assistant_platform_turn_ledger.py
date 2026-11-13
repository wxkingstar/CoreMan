"""平台来源的任务只能由平台代码经 `platform_turns.enqueue` 创建。

这些类型的任务不带入站事件也能拿到所有者的个人工具（`platform_turns.scope_for`），靠的就是
「任务只能由平台创建」。这里按语法树扫 `coreman/` 下的全部代码：
- 任务行只在 `coreman/core/bus/tasks.py` 插入；
- 每一处 `NewTask(...)` 的 `kind` 都是看得出取值的写法，且不是平台来源的类型；
- 平台来源类型的字面量只出现在定义处、任务类型元组与 worker 的处理器注册表里。
新增一处时这里会失败：改成调 `platform_turns.enqueue`，或确认它与平台来源的类型无关后更新台账。
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

from coreman.core.assistants import kinds

ROOT = Path(__file__).resolve().parents[2]
PLATFORM_VALUES = frozenset(kinds.PLATFORM_TURN_KINDS)
ENTRY = "coreman/core/assistants/platform_turns.py"
BUS = "coreman/core/bus/tasks.py"
# 可以出现类型字面量的文件：常量定义、任务类型元组、worker 入口按类型登记处理器。
LITERAL_FILES = frozenset(
    {
        "coreman/core/assistants/kinds.py",
        "coreman/core/db/models/bus.py",
        "coreman/runtime/worker/__main__.py",
    }
)
# `NewTask(kind=<名字>)` 里允许出现的常量名：取值都不是平台来源的类型。
KNOWN_KIND_NAMES = frozenset({"RESUME_KIND", "APPROVAL_RESULT_KIND", "APPROVAL_EXECUTE_KIND"})


def _sources() -> Iterator[tuple[str, ast.AST]]:
    for path in sorted((ROOT / "coreman").rglob("*.py")):
        yield path.relative_to(ROOT).as_posix(), ast.parse(path.read_text(encoding="utf-8"))


def _called(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else ""


def _named(node: ast.expr, name: str) -> bool:
    return (isinstance(node, ast.Name) and node.id == name) or (
        isinstance(node, ast.Attribute) and node.attr == name
    )


def _new_tasks() -> Iterator[tuple[str, ast.Call]]:
    for relative, tree in _sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _called(node) == "NewTask":
                yield relative, node


def test_task_rows_are_inserted_only_by_the_bus() -> None:
    found = set()
    for relative, tree in _sources():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            built = _called(node) == "Task"
            inserted = (
                _called(node).endswith("insert")
                and bool(node.args)
                and _named(node.args[0], "Task")
            )
            if built or inserted:
                found.add(relative)
    assert found == {BUS}


def test_every_new_task_kind_is_static_and_not_a_platform_kind() -> None:
    problems: list[str] = []
    for relative, node in _new_tasks():
        if relative == ENTRY:
            continue
        where = f"{relative}:{node.lineno}"
        kind = next((kw.value for kw in node.keywords if kw.arg == "kind"), None)
        if kind is None or len(node.args) > 1:
            problems.append(f"{where} kind 必须用关键字参数给出")
        elif isinstance(kind, ast.Constant) and isinstance(kind.value, str):
            if kind.value in PLATFORM_VALUES:
                problems.append(f"{where} 平台来源的类型只能经 platform_turns.enqueue 入队")
        elif not (isinstance(kind, ast.Name) and kind.id in KNOWN_KIND_NAMES):
            problems.append(f"{where} kind 的写法不在台账里")
    assert problems == []


def test_the_single_entry_passes_its_checked_kind() -> None:
    calls = [node for relative, node in _new_tasks() if relative == ENTRY]
    assert len(calls) == 1
    kind = next(kw.value for kw in calls[0].keywords if kw.arg == "kind")
    # 入口把校验过的参数原样传下去，不在这里另写类型。
    assert isinstance(kind, ast.Name) and kind.id == "kind"


def test_platform_kind_literals_stay_in_their_files() -> None:
    found = set()
    for relative, tree in _sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in PLATFORM_VALUES:
                found.add(relative)
    assert found <= LITERAL_FILES, sorted(found - LITERAL_FILES)


def test_known_names_are_not_platform_kinds() -> None:
    assert kinds.APPROVAL_RESULT_KIND not in PLATFORM_VALUES
    assert kinds.APPROVAL_EXECUTE_KIND not in PLATFORM_VALUES
    assert kinds.BOT_HELP_KIND not in PLATFORM_VALUES
    assert PLATFORM_VALUES == {
        "assistant_handover",
        "assistant_job_result",
        "assistant_compile",
        "assistant_subtask",
    }
