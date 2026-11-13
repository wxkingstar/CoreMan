"""平台来源的任务只能由平台代码经 `platform_turns.enqueue` 创建。

这些类型的任务不带入站事件也能拿到所有者的个人工具（`platform_turns.scope_for`），靠的就是
「任务只能由平台创建」。运行时有一道闸（`bus.tasks.enqueue` 不收别处来的、没有入站事件的这些
类型）；这里再按语法树扫 `coreman/` 下的全部代码：
- 任务行只在 `coreman/core/bus/tasks.py` 插入；
- 每一处 `NewTask(...)` 的 `kind` 都能在导入模块后按取值判断（换个名字导入也一样），且不是平台
  来源的类型；取值是审批结果回合的，必须带入站事件；
- 放行入队的标记只有总线与唯一入口两处用到；
- 平台来源类型的字面量只出现在定义处、任务类型元组与 worker 的处理器注册表里。
新增一处时这里会失败：改成调 `platform_turns.enqueue`，或确认它与平台来源的类型无关。
"""

from __future__ import annotations

import ast
import importlib
from collections.abc import Iterator
from pathlib import Path

from coreman.core.assistants import kinds, platform_turns

ROOT = Path(__file__).resolve().parents[2]
PLATFORM_VALUES = frozenset(kinds.PLATFORM_TURN_KINDS)
ENTRY = "coreman/core/assistants/platform_turns.py"
BUS = "coreman/core/bus/tasks.py"
DELIVERY = "coreman/core/assistants/approvals/service.py"
# 可以出现类型字面量的文件：常量定义、任务类型元组、worker 入口按类型登记处理器。
LITERAL_FILES = frozenset(
    {
        "coreman/core/assistants/kinds.py",
        "coreman/core/db/models/bus.py",
        "coreman/runtime/worker/__main__.py",
    }
)
Functions = tuple[ast.AST, ...]
_UNKNOWN = object()


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


def _calls(node: ast.AST, inside: Functions) -> Iterator[tuple[ast.Call, Functions]]:
    if isinstance(node, ast.Call) and _called(node) == "NewTask":
        yield node, inside
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
        inside = (*inside, node)
    for child in ast.iter_child_nodes(node):
        yield from _calls(child, inside)


def _new_tasks() -> Iterator[tuple[str, ast.Call, Functions]]:
    """每一处 `NewTask(...)`：所在文件、调用节点、包着它的函数（由外到内）。"""
    for relative, tree in _sources():
        for node, inside in _calls(tree, ()):
            yield relative, node, inside


def _local_names(functions: Functions) -> set[str]:
    """这些函数里绑定过的名字（参数、赋值、导入）：它们的取值静态看不出来。"""
    found: set[str] = set()
    for function in functions:
        for node in ast.walk(function):
            if isinstance(node, ast.arg):
                found.add(node.arg)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                found.add(node.id)
            elif isinstance(node, ast.alias):
                found.add((node.asname or node.name).split(".")[0])
    return found


def _value(module: str, node: ast.expr | None, inside: Functions) -> object:
    """`kind` 这个表达式的取值：字面量，或模块级的名字（导入所在模块后取，不看它叫什么）。"""
    if isinstance(node, ast.Constant):
        return node.value
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name) or node.id in _local_names(inside):
        return _UNKNOWN
    found: object = importlib.import_module(module)
    for name in (node.id, *reversed(parts)):
        found = getattr(found, name, _UNKNOWN)
    return found


def _module(relative: str) -> str:
    return relative.removesuffix(".py").replace("/", ".")


def _keyword(node: ast.Call, name: str) -> ast.expr | None:
    return next((kw.value for kw in node.keywords if kw.arg == name), None)


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


def test_every_new_task_kind_resolves_to_a_value_that_is_not_a_platform_kind() -> None:
    problems: list[str] = []
    for relative, node, inside in _new_tasks():
        if relative == ENTRY:
            continue
        where = f"{relative}:{node.lineno}"
        kind = _keyword(node, "kind")
        if kind is None or len(node.args) > 1 or any(kw.arg is None for kw in node.keywords):
            problems.append(f"{where} kind 必须用关键字参数给出")
            continue
        value = _value(_module(relative), kind, inside)
        if not isinstance(value, str):
            problems.append(f"{where} kind 的取值看不出来")
        elif value in PLATFORM_VALUES:
            problems.append(f"{where} 平台来源的类型只能经 platform_turns.enqueue 入队")
        elif value in platform_turns.ORIGIN_KINDS:
            event = _keyword(node, "inbound_event_id")
            if event is None or (isinstance(event, ast.Constant) and event.value is None):
                problems.append(f"{where} 不经入口的审批结果回合必须带入站事件")
    assert problems == []


def test_resolving_sees_through_an_alias() -> None:
    """换个名字导入、经模块取属性，取到的都是同一个值；函数里绑定的名字看不出取值。"""
    tree = ast.parse(
        "def build(kind):\n"
        "    NewTask(kind=RENAMED)\n"
        "    NewTask(kind=kinds.HANDOVER_KIND)\n"
        "    NewTask(kind=kind)\n"
    )
    found = [
        _value("tests.unit.platform_kind_alias_sample", _keyword(call, "kind"), inside)
        for call, inside in _calls(tree, ())
    ]
    assert found == [kinds.HANDOVER_KIND, kinds.HANDOVER_KIND, _UNKNOWN]


def test_the_delivery_outside_the_entry_carries_its_inbound_event() -> None:
    """`approvals.service.deliver` 直接入队的那一处：类型是审批结果回合，带着提交那一轮的事件。"""
    found = [
        node
        for relative, node, inside in _new_tasks()
        if relative == DELIVERY
        and _value(_module(relative), _keyword(node, "kind"), inside) == kinds.APPROVAL_RESULT_KIND
    ]
    assert len(found) == 1
    event = _keyword(found[0], "inbound_event_id")
    assert event is not None and not isinstance(event, ast.Constant)


def test_the_single_entry_passes_its_checked_kind() -> None:
    calls = [node for relative, node, _ in _new_tasks() if relative == ENTRY]
    assert len(calls) == 1
    kind = _keyword(calls[0], "kind")
    # 入口把校验过的参数原样传下去，不在这里另写类型。
    assert isinstance(kind, ast.Name) and kind.id == "kind"


def test_only_the_entry_admits_a_task_past_the_gate() -> None:
    """放行标记是总线模块私有的：除了总线自己，只有唯一入口碰它。"""
    found = set()
    for relative, tree in _sources():
        for node in ast.walk(tree):
            name = node.id if isinstance(node, ast.Name) else getattr(node, "attr", "")
            if name == "_ADMITTED":
                found.add(relative)
    assert found == {BUS, ENTRY}


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
    assert platform_turns.ORIGIN_KINDS == (*kinds.PLATFORM_TURN_KINDS, kinds.APPROVAL_RESULT_KIND)
    assert PLATFORM_VALUES == {
        "assistant_handover",
        "assistant_job_result",
        "assistant_compile",
        "assistant_subtask",
    }
