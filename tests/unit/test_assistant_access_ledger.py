"""运行时路径之外的解密台账：每一处都要在同一个函数里记访问日志，或写明为什么不用记。

按语法树扫 `coreman/` 下的全部代码，认两类位置：

1. 任何地方带 `viewer=` 的 `open_row` / `open_rows` 调用——「只对查看者本人还原」只在把内容交给
   人看的时候才用。
2. 接口层（`coreman/api/`）与管理命令（`coreman/cli.py`）里对 `DECRYPTING` 中函数的调用——它们
   返回解密后的个人内容，或动了数据密钥。

所在的函数（最内层的 `def`）里必须有 `access_log.record(...)`、`record_opened(...)` 或
`record_refused(...)`；
确实不用记的登记在 `EXEMPT` 里并写明理由。新增一处解密而两样都没做，这里失败。

扫不到的：`session.get` / `session.scalar` 取整行时的自动还原（那一类由
`tests/unit/test_assistant_sealed_ledger.py` 的整行读取台账守着），以及经 `coreman/admin_cli/`
以某位成员的身份调接口——后者由接口那一侧处理（`deps.via_cli`：管理命令的会话一律不还原）。
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# 接口层与管理命令里，调用这些「模块.函数」就是在解密个人内容或动数据密钥。
DECRYPTING = frozenset(
    {
        ("notes", "index"),
        ("notes", "read"),
        ("notes", "search"),
        ("notes", "dump"),
        ("followups", "list_"),
        ("followups", "due"),
        ("followups", "get"),
        ("jobs", "list_"),
        ("owner_view", "approvals"),
        ("transfer", "plan"),
        ("transfer", "export"),
        ("sealed", "open_row"),
        ("sealed", "open_rows"),
        ("sealed", "open_value"),
        ("assistant_keys", "rewrap"),
    }
)
OUTER_LAYERS = ("coreman/api/", "coreman/cli.py")
RECORDERS = frozenset(
    {
        ("access_log", "record"),
        ("access_log", "record_opened"),
        # 管理命令的会话：内容没有还原，记的是「有人试过」
        ("access_log", "record_refused"),
    }
)
# （文件, 函数）→ 为什么这一处解密不用记访问日志。
EXEMPT = {
    # 幂等重放时比对已入队的明文载荷，只回编号与状态，不输出内容
    ("coreman/api/routers/infra_push.py", "push"): "compares, never outputs",
    # 核心函数：由调用它的路由记账（路由受上面第 2 类约束）
    ("coreman/core/assistants/owner_view.py", "approvals"): "recorded by the calling route",
}


def _dotted(node: ast.Call) -> tuple[str, str]:
    """`模块.函数(...)` 调用的（模块, 函数）；不是这种形状的返回空串。"""
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id, func.attr
    return "", ""


def _decrypts(node: ast.Call, relative: str) -> bool:
    attr = node.func.attr if isinstance(node.func, ast.Attribute) else ""
    if attr in ("open_row", "open_rows") and any(kw.arg == "viewer" for kw in node.keywords):
        return True
    return relative.startswith(OUTER_LAYERS) and _dotted(node) in DECRYPTING


def _own_calls(function: ast.AST) -> Iterator[ast.Call]:
    """这个函数自己的调用；嵌套定义的函数各算各的。"""
    stack = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call):
            yield node
        stack.extend(ast.iter_child_nodes(node))


def unrecorded(relative: str, tree: ast.AST) -> set[tuple[str, str]]:
    """这个文件里解密了个人内容、却没有在同一个函数里记访问日志的（文件, 函数）。"""
    found: set[tuple[str, str]] = set()
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls = list(_own_calls(function))
        if not any(_decrypts(call, relative) for call in calls):
            continue
        if not any(_dotted(call) in RECORDERS for call in calls):
            found.add((relative, function.name))
    return found


def _scan() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted((ROOT / "coreman").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        found |= unrecorded(relative, ast.parse(path.read_text(encoding="utf-8")))
    return found


def test_every_decryption_outside_the_runtime_is_recorded_or_exempt() -> None:
    # 两头都查：漏记的会多出来；豁免清单里已经不存在的条目也要删掉。
    assert _scan() == set(EXEMPT)


def test_the_ledger_catches_a_route_that_forgets_to_record() -> None:
    forgetful = """
async def leak(own, session):
    page = await notes.read(session, own.bot.id, "x")
    return {"data": page}


async def careful(own, session):
    page = await notes.read(session, own.bot.id, "x")
    await access_log.record(session, own.bot.id, own.actor.id, "notes.read", "page")
    return {"data": page}


async def peek(session, rows, actor):
    await sealed.open_rows(session, rows, viewer=actor.id)


async def outer(own, session):
    async def inner():
        return await notes.index(session, own.bot.id)

    await access_log.record(session, own.bot.id, own.actor.id, "notes.index", "")
    return await inner()
"""
    tree = ast.parse(forgetful)
    assert unrecorded("coreman/api/routers/example.py", tree) == {
        ("coreman/api/routers/example.py", "leak"),
        ("coreman/api/routers/example.py", "peek"),
        # 记账写在外层函数里不算：解密发生在 inner 里。
        ("coreman/api/routers/example.py", "inner"),
    }
    # 同样的调用放在核心模块里：只有带 viewer= 的那一处算。
    assert unrecorded("coreman/core/assistants/example.py", tree) == {
        ("coreman/core/assistants/example.py", "peek")
    }


def test_sessions_made_by_the_admin_cli_stay_recognisable() -> None:
    # `deps.via_cli` 靠这个取值认出管理命令建的会话：两边要一起改。
    client = (ROOT / "coreman/admin_cli/client.py").read_text(encoding="utf-8")
    deps = (ROOT / "coreman/api/deps.py").read_text(encoding="utf-8")
    assert 'auth_method="cli"' in client
    assert 'auth_method == "cli"' in deps
