"""迁移门禁（双版本并存）：upgrade() 只能扩展表结构，不能让还在跑的上一版本失败。

升级时先跑迁移、再逐个替换进程，窗口期内上一版本的 api / worker 仍在读写新库。所以
upgrade() 里禁止：

- add_column 加 NOT NULL 且没有 server_default 的列：旧代码的 INSERT 不带这一列就失败；
- drop_column / drop_table：旧代码还在读写；
- alter_column(type_=...) 改类型、alter_column(nullable=False) 收紧约束；
- 重命名表或列：对旧代码等同于删除。

op.create_table 里的 NOT NULL、建/删索引、drop_constraint、放宽为 nullable=True、
数据 UPDATE / DELETE 都不受限。同样的 DDL 写成原生 SQL（op.execute / connection.execute）
也会被识别。只分析 upgrade() 及它直接或间接调用的本模块函数，downgrade() 不检查。

确需例外时写进 ALLOWED，并写明上一版本为什么不受影响。
"""

from __future__ import annotations

import ast
import re
import textwrap
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "migrations" / "versions"

ADD_NOT_NULL = "add_column_not_null_without_default"
UNRESOLVED_COLUMN = "add_column_unresolved"
DROP_COLUMN = "drop_column"
DROP_TABLE = "drop_table"
ALTER_TYPE = "alter_column_type"
TIGHTEN_NULLABLE = "alter_column_not_null"
RENAME = "rename"

_ONE_OFF = "功能未开放且表为空时的一次性迁移（upgrade 开头断言 {table} 为空），上一版本不写入该表"

# {(revision, rule): 理由}。已发布的迁移文件不再改动，按 revision 粒度放行即可。
ALLOWED: dict[tuple[str, str], str] = {
    # escalations.owner_client_key / request_fingerprint / to_platform_user_id
    ("0009", ADD_NOT_NULL): _ONE_OFF.format(table="escalations"),
    # skill_approvals.request_inputs_enc
    ("0012", ADD_NOT_NULL): _ONE_OFF.format(table="skill_approvals"),
}


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    revision: str
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line} [{self.rule}] (revision {self.revision}) {self.detail}"


def _kw(call: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def _arg(call: ast.Call, index: int, name: str) -> ast.expr | None:
    value = _kw(call, name)
    if value is None and 0 <= index < len(call.args):
        value = call.args[index]
    return value


def _is_const(node: ast.expr | None, value: object) -> bool:
    return isinstance(node, ast.Constant) and node.value is value


def _text(node: ast.expr | None) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return "?"


def _callee(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


# ---- 原生 SQL ----------------------------------------------------------------------

_SQL_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_SQL_QUOTED = re.compile(r"'(?:[^']|'')*'")
_SQL_RULES = (
    (DROP_TABLE, re.compile(r"\bDROP\s+TABLE\b", re.I)),
    (DROP_COLUMN, re.compile(r"\bDROP\s+COLUMN\b", re.I)),
    (ALTER_TYPE, re.compile(r"\bALTER\s+COLUMN\s+\S+\s+(?:SET\s+DATA\s+)?TYPE\b", re.I)),
    (TIGHTEN_NULLABLE, re.compile(r"\bSET\s+NOT\s+NULL\b", re.I)),
    (RENAME, re.compile(r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?\S+\s+RENAME\b", re.I)),
)
# ALTER TABLE ... ADD [COLUMN] [IF NOT EXISTS] name type ...；ADD CONSTRAINT / CHECK 等不是加列。
_SQL_ADD = re.compile(
    r"\bADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?!(?:CONSTRAINT|PRIMARY|UNIQUE|CHECK|FOREIGN|EXCLUDE)\b)\w+\s",
    re.I,
)


def _column_clause(sql: str, start: int) -> str:
    """从 ADD 之后截到本子句结束：括号外的逗号或分号（numeric(10,2) 里的逗号不算）。"""
    depth = 0
    for i in range(start, len(sql)):
        ch = sql[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch in ",;" and depth <= 0:
            return sql[start:i]
    return sql[start:]


def _sql_rules(sql: str) -> Iterator[tuple[str, str]]:
    sql = _SQL_QUOTED.sub("''", _SQL_COMMENT.sub(" ", sql))
    for rule, pattern in _SQL_RULES:
        for match in pattern.finditer(sql):
            yield rule, f"原生 SQL：{match.group(0)}"
    for match in _SQL_ADD.finditer(sql):
        clause = re.sub(r"\bIS\s+NOT\s+NULL\b", " ", _column_clause(sql, match.end()), flags=re.I)
        if re.search(r"\bNOT\s+NULL\b", clause, re.I) and not re.search(
            r"\bDEFAULT\b", clause, re.I
        ):
            yield ADD_NOT_NULL, f"原生 SQL：{match.group(0).strip()} … NOT NULL 且无 DEFAULT"


# ---- 模块分析 ----------------------------------------------------------------------


class _Scanner:
    def __init__(self, source: str, path: str) -> None:
        self.tree = ast.parse(source, filename=path)
        self.path = path
        self.functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {
            n.name: n
            for n in self.tree.body
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        self.constants: dict[str, str] = {}
        self.revision = "?"
        for node in self.tree.body:
            target: ast.expr | None = None
            value: ast.expr | None = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign):
                target, value = node.target, node.value
            if isinstance(target, ast.Name) and isinstance(value, ast.Constant):
                if isinstance(value.value, str):
                    self.constants[target.id] = value.value
                if target.id == "revision":
                    self.revision = str(value.value)
        self.found: list[Violation] = []

    def report(self, node: ast.AST, rule: str, detail: str) -> None:
        line = getattr(node, "lineno", 0)
        self.found.append(Violation(self.path, line, self.revision, rule, detail))

    def reachable(self) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
        """upgrade() 以及从它出发能调用到的本模块函数（辅助函数里的 DDL 一样算）。"""
        if "upgrade" not in self.functions:
            return []
        seen, queue = {"upgrade"}, ["upgrade"]
        while queue:
            for node in ast.walk(self.functions[queue.pop()]):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    name = node.func.id
                    if name in self.functions and name not in seen and name != "downgrade":
                        seen.add(name)
                        queue.append(name)
        return [self.functions[name] for name in sorted(seen)]

    def strings(self, node: ast.AST) -> Iterator[str]:
        """execute 参数里能静态拿到的 SQL 文本：字面量、f-string（插值记作 x）、模块级常量。"""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value
        elif isinstance(node, ast.JoinedStr):
            yield "".join(
                v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "x"
                for v in node.values
            )
        elif isinstance(node, ast.Name) and node.id in self.constants:
            yield self.constants[node.id]
        else:
            for child in ast.iter_child_nodes(node):
                yield from self.strings(child)

    def scan(self) -> list[Violation]:
        for func in self.reachable():
            batch_tables = self._batch_receivers(func)
            for node in ast.walk(func):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                receiver, name = node.func.value, node.func.attr
                if name == "execute":
                    for arg in [*node.args, *(k.value for k in node.keywords)]:
                        for sql in self.strings(arg):
                            for rule, detail in _sql_rules(sql):
                                self.report(node, rule, detail)
                elif isinstance(receiver, ast.Name) and receiver.id == "op":
                    self._op(node, name, offset=0, table=None)
                elif isinstance(receiver, ast.Name) and receiver.id in batch_tables:
                    self._op(node, name, offset=-1, table=batch_tables[receiver.id])
        return self.found

    def _batch_receivers(self, func: ast.AST) -> dict[str, str]:
        """`with op.batch_alter_table("t") as batch_op:` 里的 batch_op 等价于绑定了表名的 op。"""
        out: dict[str, str] = {}
        for node in ast.walk(func):
            if isinstance(node, ast.With | ast.AsyncWith):
                for item in node.items:
                    ctx = item.context_expr
                    if (
                        isinstance(ctx, ast.Call)
                        and _callee(ctx) == "batch_alter_table"
                        and isinstance(item.optional_vars, ast.Name)
                    ):
                        out[item.optional_vars.id] = _text(_arg(ctx, 0, "table_name"))
        return out

    def _op(self, call: ast.Call, name: str, *, offset: int, table: str | None) -> None:
        """offset=0 是 op.xxx(table, column, ...)；batch_op.xxx(column, ...) 少一个表名参数。"""
        table_name = table if table is not None else _text(_arg(call, 0, "table_name"))
        column_name = _text(_arg(call, 1 + offset, "column_name"))
        if name == "add_column":
            self._add_column(call, table_name, _arg(call, 1 + offset, "column"))
        elif name == "drop_column":
            self.report(call, DROP_COLUMN, f"{table_name}.{column_name}")
        elif name == "drop_table":
            self.report(call, DROP_TABLE, _text(_arg(call, 0, "table_name")))
        elif name == "rename_table":
            self.report(call, RENAME, f"表 {_text(_arg(call, 0, 'old_table_name'))}")
        elif name == "alter_column":
            target = f"{table_name}.{column_name}"
            if _kw(call, "type_") is not None:
                self.report(call, ALTER_TYPE, f"{target} 改类型")
            nullable = _kw(call, "nullable")
            if nullable is not None and not _is_const(nullable, True):
                self.report(call, TIGHTEN_NULLABLE, f"{target} 的 nullable 不是字面 True")
            if _kw(call, "new_column_name") is not None:
                self.report(call, RENAME, f"列 {target}")

    def _add_column(self, call: ast.Call, table: str, column: ast.expr | None) -> None:
        if not (isinstance(column, ast.Call) and _callee(column) == "Column"):
            detail = f"{table}：列定义请直接写 sa.Column(...)，否则无法静态检查"
            self.report(call, UNRESOLVED_COLUMN, detail)
            return
        target = f"{table}.{_text(column.args[0] if column.args else _kw(column, 'name'))}"
        nullable = _kw(column, "nullable")
        if nullable is None:
            not_null = _is_const(_kw(column, "primary_key"), True)
        elif isinstance(nullable, ast.Constant):
            not_null = nullable.value is False
        else:
            self.report(call, UNRESOLVED_COLUMN, f"{target}：nullable 不是字面量")
            return
        default = _kw(column, "server_default")
        if not_null and (default is None or _is_const(default, None)):
            self.report(call, ADD_NOT_NULL, f"{target} NOT NULL 且无 server_default")


def scan_migration(source: str, path: str) -> list[Violation]:
    return _Scanner(source, path).scan()


def _scan_versions() -> list[Violation]:
    paths = sorted(VERSIONS.glob("[0-9]*.py"))
    assert paths, f"没有找到迁移文件：{VERSIONS}"
    out: list[Violation] = []
    for path in paths:
        rel = path.relative_to(ROOT).as_posix()
        scanner = _Scanner(path.read_text(encoding="utf-8"), rel)
        assert scanner.revision != "?", f"{rel} 缺少 revision"
        out.extend(scanner.scan())
    return out


def test_upgrade_only_expands_schema() -> None:
    unexpected = [v for v in _scan_versions() if (v.revision, v.rule) not in ALLOWED]
    assert not unexpected, (
        "迁移违反双版本并存规则。改成可空列或带 server_default 的列、把删除/改类型"
        "留到下一版本，或在 ALLOWED 里写明上一版本为何不受影响：\n"
        + "\n".join(str(v) for v in unexpected)
    )


def test_allowlist_entries_are_still_needed() -> None:
    hits = {(v.revision, v.rule) for v in _scan_versions()}
    stale = sorted(set(ALLOWED) - hits)
    assert not stale, f"ALLOWED 里这些例外已经匹配不到违规，请删除：{stale}"
    assert all(reason.strip() for reason in ALLOWED.values())


# ---- 规则自测：该拦的拦住，放宽约束 / 建索引 / 数据清理不误伤 --------------------------

_PRELUDE = """
SQL = "DROP TABLE t"


def _helper() -> None:
    op.drop_table("t")


def _col(name):
    return sa.Column(name, sa.Text(), nullable=False)
"""


def _module(body: str) -> str:
    return (
        "import sqlalchemy as sa\nfrom alembic import op\n\n"
        'revision = "9999"\ndown_revision = "9998"\n'
        + _PRELUDE
        + "\n\ndef upgrade() -> None:\n"
        + textwrap.indent(textwrap.dedent(body).strip() or "pass", "    ")
        + "\n\n\ndef downgrade() -> None:\n"
        + '    op.drop_table("t")\n    op.drop_column("t", "c")\n'
    )


def _rules(body: str) -> list[str]:
    return sorted(v.rule for v in scan_migration(_module(body), "x.py"))


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('op.add_column("t", sa.Column("c", sa.Text(), nullable=False))', [ADD_NOT_NULL]),
        (
            'op.add_column("t", sa.Column("c", sa.Text(), nullable=False, server_default=None))',
            [ADD_NOT_NULL],
        ),
        ('op.add_column("t", sa.Column("id", sa.Uuid(), primary_key=True))', [ADD_NOT_NULL]),
        ('op.add_column("t", column=sa.Column("c", sa.Text(), nullable=False))', [ADD_NOT_NULL]),
        ('op.add_column("t", _col("c"))', [UNRESOLVED_COLUMN]),
        ('op.drop_column("t", "c")', [DROP_COLUMN]),
        ('op.drop_table("t")', [DROP_TABLE]),
        ('op.rename_table("t", "t2")', [RENAME]),
        ('op.alter_column("t", "c", type_=sa.BigInteger())', [ALTER_TYPE]),
        ('op.alter_column("t", "c", existing_type=sa.Text(), nullable=False)', [TIGHTEN_NULLABLE]),
        ('op.alter_column("t", "c", new_column_name="d")', [RENAME]),
        (
            """
            with op.batch_alter_table("t") as batch_op:
                batch_op.add_column(sa.Column("c", sa.Text(), nullable=False))
                batch_op.drop_column("d")
            """,
            [ADD_NOT_NULL, DROP_COLUMN],
        ),
        ('op.execute("ALTER TABLE t DROP COLUMN c")', [DROP_COLUMN]),
        ('op.execute("DROP TABLE IF EXISTS t")', [DROP_TABLE]),
        ('op.execute("ALTER TABLE t ALTER COLUMN c SET NOT NULL")', [TIGHTEN_NULLABLE]),
        ('op.execute("ALTER TABLE t ALTER COLUMN c TYPE bigint")', [ALTER_TYPE]),
        ('op.execute("ALTER TABLE t ALTER COLUMN c SET DATA TYPE bigint")', [ALTER_TYPE]),
        ('op.execute("ALTER TABLE t RENAME COLUMN a TO b")', [RENAME]),
        ('op.execute("ALTER TABLE t ADD COLUMN c numeric(10,2) NOT NULL")', [ADD_NOT_NULL]),
        ('op.execute("ALTER TABLE t ADD c text NOT NULL, ADD d text")', [ADD_NOT_NULL]),
        ('op.get_bind().execute(sa.text("ALTER TABLE t DROP COLUMN c"))', [DROP_COLUMN]),
        ('op.execute(f"ALTER TABLE {name} DROP COLUMN c")', [DROP_COLUMN]),
        ("op.execute(SQL)", [DROP_TABLE]),
        ("_helper()", [DROP_TABLE]),
    ],
)
def test_rules_catch_contracting_changes(body: str, expected: list[str]) -> None:
    assert _rules(body) == sorted(expected)


@pytest.mark.parametrize(
    "body",
    [
        """
        op.create_table(
            "t",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("c", sa.Text(), nullable=False),
        )
        """,
        'op.add_column("t", sa.Column("c", sa.Text()))',
        'op.add_column("t", sa.Column("c", sa.Text(), nullable=True))',
        """
        op.add_column(
            "t", sa.Column("c", sa.Text(), nullable=False, server_default=sa.text("''"))
        )
        """,
        'op.create_index("t_c_idx", "t", ["c"])',
        'op.drop_index("t_c_idx", table_name="t")',
        """
        with op.get_context().autocommit_block():
            op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS t_c_idx ON t (c)")
            op.execute("DROP INDEX CONCURRENTLY IF EXISTS t_old_idx")
        """,
        'op.drop_constraint("ck_t_kind", "t", type_="check")',
        'op.execute("ALTER TABLE t DROP CONSTRAINT IF EXISTS ck_t_kind")',
        """op.create_check_constraint("ck_t_kind", "t", "kind IN ('a', 'b', 'c')")""",
        'op.alter_column("t", "c", existing_type=sa.Uuid(), nullable=True)',
        'op.alter_column("t", "c", server_default=sa.text("false"))',
        'op.execute("ALTER TABLE t ALTER COLUMN c DROP NOT NULL")',
        """op.execute("ALTER TABLE t ADD COLUMN c text NOT NULL DEFAULT ''")""",
        'op.execute("ALTER TABLE t ADD CONSTRAINT ck CHECK (c IS NOT NULL) NOT VALID")',
        """op.execute("UPDATE t SET note = 'DROP TABLE x' WHERE c IS NOT NULL")""",
        """op.execute("DELETE FROM t WHERE kind = 'legacy'")""",
        'op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM t)"))',
        'op.execute("CREATE TABLE t (id uuid PRIMARY KEY, c text NOT NULL, type TEXT)")',
        'op.execute("-- DROP TABLE t 只是注释\\nSELECT 1")',
        "",  # downgrade() 里的 drop_table / drop_column 不检查
    ],
)
def test_rules_allow_expanding_and_relaxing_changes(body: str) -> None:
    assert _rules(body) == []


def test_violation_message_points_to_file_line_and_rule() -> None:
    # 表名用 marker：前置的 _helper() 里也有 drop_table("t")，按行文本定位会找错行
    source = _module('op.add_column("t", sa.Column("c", sa.Text()))\nop.drop_table("marker")')
    (violation,) = scan_migration(source, "migrations/versions/9999_x.py")
    expected_line = source.splitlines().index('    op.drop_table("marker")') + 1
    assert violation.line == expected_line
    assert str(violation).startswith(
        f"migrations/versions/9999_x.py:{expected_line} [drop_table] (revision 9999)"
    )
