"""无 exec/eval、无 Python 对象反射的受限 precheck 解释器。

只解释数据运算和控制流；所有值限定为 JSON 类型，所有方法逐项实现。
不把脚本交给 Python 执行，不开放文件、环境变量、模块对象或任意网络访问。
"""

from __future__ import annotations

import ast
import json
import math
import operator
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

MAX_SIZE = 65536
MAX_ITEMS = 10000
MAX_STEPS = 20000


class PrecheckError(ValueError):
    pass


@dataclass(frozen=True)
class PrecheckResult:
    trigger: bool
    prompt_appendix: str = ""
    reason: str = ""


class _Return(Exception):
    def __init__(self, value: Any):
        self.value = value


def _bounded(value: Any, depth: int = 0) -> Any:
    if depth > 20:
        raise PrecheckError("data nesting limit")
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if value.bit_length() > 256:
            raise PrecheckError("integer limit")
    elif type(value) is float:
        if not math.isfinite(value):
            raise PrecheckError("non-finite number")
    elif type(value) is str:
        if len(value) > MAX_SIZE:
            raise PrecheckError("string limit")
    elif type(value) in (list, dict):
        if len(value) > MAX_ITEMS:
            raise PrecheckError("collection limit")
        for key, item in value.items() if type(value) is dict else enumerate(value):
            if type(value) is dict and type(key) not in (str, int, float, bool, type(None)):
                raise PrecheckError("unsupported key")
            _bounded(item, depth + 1)
        # 防重复嵌套引用指数级复制；上游单对象总大小也有限。
        if len(json.dumps(value, ensure_ascii=False)) > MAX_SIZE:
            raise PrecheckError("data size limit")
    else:
        raise PrecheckError("unsupported value type")
    return value


def validate_script(script: str) -> ast.FunctionDef:
    if len(script.encode("utf-8")) > 32768:
        raise PrecheckError("script size limit")
    try:
        module = ast.parse(script)
    except (SyntaxError, RecursionError) as exc:
        raise PrecheckError("invalid syntax") from exc
    nodes = list(ast.walk(module))
    if len(nodes) > 4000:
        raise PrecheckError("syntax size limit")
    for node in nodes:
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            raise PrecheckError("private names are forbidden")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise PrecheckError("private attributes are forbidden")
    functions = [node for node in module.body if isinstance(node, ast.FunctionDef)]
    if len(functions) != 1 or functions[0].name != "should_trigger":
        raise PrecheckError("define exactly one should_trigger(ctx) function")
    fn = functions[0]
    if (
        fn.decorator_list
        or fn.args.vararg
        or fn.args.kwarg
        or fn.args.kwonlyargs
        or fn.args.posonlyargs
        or len(fn.args.args) != 1
        or fn.args.args[0].arg != "ctx"
        or fn.args.defaults
    ):
        raise PrecheckError("expected should_trigger(ctx)")
    for node in module.body:
        if node is fn or (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            continue
        if isinstance(node, ast.Import) and all(
            a.name in {"json", "math", "time", "datetime"} and a.asname is None for a in node.names
        ):
            continue
        raise PrecheckError("only json/math/time/datetime imports are supported")
    return fn


class Interpreter:
    def __init__(self, ctx: dict[str, Any], timeout_seconds: float):
        # JSON roundtrip：调用者也不能将自定义对象送入解释器。
        self.env: dict[str, Any] = {"ctx": _bounded(json.loads(json.dumps(ctx)))}
        self.deadline = time.monotonic() + min(timeout_seconds, 120)
        self.steps = 0

    def step(self) -> None:
        self.steps += 1
        if self.steps > MAX_STEPS or time.monotonic() > self.deadline:
            raise PrecheckError("execution budget exceeded")

    def block(self, statements: list[ast.stmt]) -> None:
        for node in statements:
            self.step()
            if isinstance(node, ast.Return):
                raise _Return(self.expr(node.value) if node.value else None)
            if isinstance(node, ast.Assign) and all(isinstance(t, ast.Name) for t in node.targets):
                value = self.expr(node.value)
                for target in node.targets:
                    assert isinstance(target, ast.Name)
                    if target.id in {"ctx", "json", "math", "time", "datetime"}:
                        raise PrecheckError("reserved binding")
                    self.env[target.id] = value
            elif isinstance(node, ast.If):
                self.block(node.body if self.expr(node.test) else node.orelse)
            elif isinstance(node, ast.For) and isinstance(node.target, ast.Name):
                values = self.expr(node.iter)
                if type(values) not in (list, dict, str):
                    raise PrecheckError("unsupported iterator")
                if len(values) > MAX_ITEMS:
                    raise PrecheckError("iteration limit")
                if node.target.id in {"ctx", "json", "math", "time", "datetime"}:
                    raise PrecheckError("reserved binding")
                for value in values:
                    self.step()
                    self.env[node.target.id] = value
                    self.block(node.body)
                self.block(node.orelse)
            elif isinstance(node, ast.Expr):
                self.expr(node.value)
            elif isinstance(node, ast.Pass):
                pass
            else:
                raise PrecheckError(f"unsupported statement: {type(node).__name__}")

    def expr(self, node: ast.expr) -> Any:
        self.step()
        return _bounded(self._expr(node))

    def _expr(self, node: ast.expr) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in self.env:
                raise PrecheckError("unknown variable")
            return self.env[node.id]
        if isinstance(node, (ast.List, ast.Tuple)):
            return [self.expr(n) for n in node.elts]
        if isinstance(node, ast.Dict):
            if any(k is None for k in node.keys):
                raise PrecheckError("dictionary unpacking is forbidden")
            return {
                self.expr(k): self.expr(v)
                for k, v in zip(node.keys, node.values, strict=True)
                if k is not None
            }
        if isinstance(node, ast.Subscript):
            value = self.expr(node.value)
            key = self.expr(node.slice)
            if type(value) not in (str, list, dict):
                raise PrecheckError("unsupported subscript")
            return value[key]
        if isinstance(node, ast.IfExp):
            return self.expr(node.body if self.expr(node.test) else node.orelse)
        if isinstance(node, ast.BoolOp):
            value = False
            for n in node.values:
                value = self.expr(n)
                if (isinstance(node.op, ast.And) and not value) or (
                    isinstance(node.op, ast.Or) and value
                ):
                    break
            return value
        if isinstance(node, ast.UnaryOp):
            value = self.expr(node.operand)
            if isinstance(node.op, ast.Not):
                return not value
            if type(value) not in (int, float):
                raise PrecheckError("numeric operand required")
            if isinstance(node.op, ast.USub):
                return -value
            if isinstance(node.op, ast.UAdd):
                return value
        if isinstance(node, ast.BinOp):
            left, right = self.expr(node.left), self.expr(node.right)
            if (
                isinstance(node.op, ast.Add)
                and type(left) is type(right)
                and type(left) in (str, list)
            ):
                if len(left) + len(right) > MAX_ITEMS:
                    raise PrecheckError("concatenation limit")
                return left + right
            if type(left) not in (int, float) or type(right) not in (int, float):
                raise PrecheckError("numeric operands required")
            operations = {
                ast.Add: operator.add,
                ast.Sub: operator.sub,
                ast.Mult: operator.mul,
                ast.Div: operator.truediv,
                ast.FloorDiv: operator.floordiv,
                ast.Mod: operator.mod,
            }
            operation = operations.get(type(node.op))
            if operation is None:
                raise PrecheckError("unsupported arithmetic")
            return operation(left, right)
        if isinstance(node, ast.Compare):
            left = self.expr(node.left)
            for op, rhs in zip(node.ops, node.comparators, strict=True):
                right = self.expr(rhs)
                comparisons = {
                    ast.Eq: operator.eq,
                    ast.NotEq: operator.ne,
                    ast.Lt: operator.lt,
                    ast.LtE: operator.le,
                    ast.Gt: operator.gt,
                    ast.GtE: operator.ge,
                }
                if isinstance(op, (ast.In, ast.NotIn)):
                    result = left in right
                    if isinstance(op, ast.NotIn):
                        result = not result
                elif isinstance(op, (ast.Is, ast.IsNot)) and (left is None or right is None):
                    result = (left is right) == isinstance(op, ast.Is)
                elif type(op) in comparisons:
                    result = comparisons[type(op)](left, right)
                else:
                    raise PrecheckError("unsupported comparison")
                if not result:
                    return False
                left = right
            return True
        if isinstance(node, ast.Call):
            if any(k.arg is None for k in node.keywords):
                raise PrecheckError("argument unpacking is forbidden")
            args = [self.expr(a) for a in node.args]
            kwargs = {k.arg: self.expr(k.value) for k in node.keywords if k.arg is not None}
            return self.call(node.func, args, kwargs)
        raise PrecheckError(f"unsupported expression: {type(node).__name__}")

    def call(self, func: ast.expr, args: list[Any], kwargs: dict[str, Any]) -> Any:
        path = ast.unparse(func)
        if path == "range" and not kwargs and 1 <= len(args) <= 3:
            if any(type(a) is not int for a in args):
                raise PrecheckError("integer range required")
            values = range(*args)
            if len(values) > MAX_ITEMS:
                raise PrecheckError("range limit")
            return list(values)
        if path == "json.loads" and len(args) == 1 and not kwargs and type(args[0]) is str:
            return json.loads(args[0])
        if path == "json.dumps" and len(args) == 1 and set(kwargs) <= {"ensure_ascii", "sort_keys"}:
            return json.dumps(args[0], **kwargs)
        if path in {"time.time", "datetime.datetime.now", "datetime.datetime.utcnow"}:
            if args or kwargs:
                raise PrecheckError("clock takes no arguments")
            return time.time() if path == "time.time" else datetime.now(UTC).isoformat()
        builtins: dict[str, Any] = {
            "len": len,
            "str": str,
            "int": int,
            "float": float,
            "bool": bool,
            "abs": abs,
            "min": min,
            "max": max,
            "sum": sum,
            "any": any,
            "all": all,
            "sorted": sorted,
            "round": round,
            "math.ceil": math.ceil,
            "math.floor": math.floor,
            "math.sqrt": math.sqrt,
        }
        if path in builtins and not kwargs:
            return builtins[path](*args)
        if isinstance(func, ast.Attribute) and not kwargs:
            value = self.expr(func.value)
            method = func.attr
            if type(value) is dict and method == "get" and 1 <= len(args) <= 2:
                return value.get(*args)
            if type(value) is dict and method in {"keys", "values"} and not args:
                return list(value.keys() if method == "keys" else value.values())
            if type(value) is str:
                methods: dict[str, Any] = {
                    "strip": value.strip,
                    "lower": value.lower,
                    "upper": value.upper,
                    "startswith": value.startswith,
                    "endswith": value.endswith,
                    "split": value.split,
                }
                if method in methods:
                    return methods[method](*args)
        raise PrecheckError("unsupported function or method")


def run_precheck(
    script: str | None, ctx: dict[str, Any], timeout_seconds: float = 30
) -> PrecheckResult:
    if not script or not script.strip():
        return PrecheckResult(True)
    try:
        fn = validate_script(script)
        interpreter = Interpreter(ctx, timeout_seconds)
        result: Any = None
        try:
            interpreter.block(fn.body)
        except _Return as returned:
            result = returned.value
        if type(result) is not dict or type(result.get("trigger")) is not bool:
            raise PrecheckError("return {trigger: bool, prompt_appendix?: str, reason?: str}")
        if set(result) - {"trigger", "prompt_appendix", "reason"}:
            raise PrecheckError("unknown result fields")
        appendix, reason = result.get("prompt_appendix", ""), result.get("reason", "")
        if (
            type(appendix) is not str
            or len(appendix) > 16000
            or type(reason) is not str
            or len(reason) > 2000
        ):
            raise PrecheckError("invalid result text")
        return PrecheckResult(result["trigger"], appendix, reason)
    except PrecheckError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, ArithmeticError, RecursionError) as exc:
        # 不回传 ctx 值和 Python 异常正文，避免凭证或业务数据出现在错误日志。
        raise PrecheckError(f"precheck failed: {type(exc).__name__}") from exc
