"""Validate proxied call arguments against the operation's schemas.

A pragmatic subset of JSON Schema as OpenAPI uses it: types (with `nullable` and 3.1 type lists),
`enum`/`const`, string and array lengths, numeric bounds, object properties, `required` and
`additionalProperties`, `items`, and `allOf`/`anyOf`/`oneOf` (`oneOf` is checked like `anyOf`: a
loose description must not reject a valid request). `pattern` and `format` are left to the
business system, which validates again anyway; running a system-supplied regular expression on
model input here could stall the event loop.
"""

from __future__ import annotations

from typing import Any

MAX_ERRORS = 5
MAX_DEPTH = 64


def _type_ok(value: Any, kind: str) -> bool:
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return (isinstance(value, int) and not isinstance(value, bool)) or (
            isinstance(value, float) and value.is_integer()
        )
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "array":
        return isinstance(value, list)
    if kind == "object":
        return isinstance(value, dict)
    if kind == "null":
        return value is None
    return True


class Validator:
    def __init__(self, defs: dict[str, Any]) -> None:
        self.defs = defs

    def resolve(self, schema: Any) -> Any:
        """Follow `$ref` chains; an unknown or cyclic reference accepts anything."""
        seen: set[str] = set()
        while isinstance(schema, dict) and "$ref" in schema:
            ref = schema["$ref"]
            if not isinstance(ref, str) or ref in seen or ref not in self.defs:
                return {}
            seen.add(ref)
            schema = self.defs[ref]
        return schema if isinstance(schema, dict) else {}

    def errors(self, value: Any, schema: Any, path: str = "") -> list[str]:
        found: list[str] = []
        self._check(value, schema, path or "$", found, 0)
        return found[:MAX_ERRORS]

    def _check(self, value: Any, schema: Any, path: str, found: list[str], depth: int) -> None:
        if len(found) >= MAX_ERRORS or depth > MAX_DEPTH:
            return
        schema = self.resolve(schema)
        for part in schema.get("allOf") or []:
            self._check(value, part, path, found, depth + 1)
        for key in ("anyOf", "oneOf"):
            options = schema.get(key)
            if isinstance(options, list) and options:
                if not any(self._fits(value, option, depth) for option in options):
                    found.append(f"{path}: does not match any allowed shape")
                    return
        kind = schema.get("type")
        kinds = kind if isinstance(kind, list) else [kind] if isinstance(kind, str) else []
        if value is None and (schema.get("nullable") is True or "null" in kinds):
            return
        if kinds and not any(_type_ok(value, k) for k in kinds):
            found.append(f"{path}: expected {' or '.join(k for k in kinds if k != 'null')}")
            return
        if "const" in schema and value != schema["const"]:
            found.append(f"{path}: must be {schema['const']!r}")
        enum = schema.get("enum")
        if isinstance(enum, list) and value not in enum:
            shown = ", ".join(repr(v) for v in enum[:20])
            found.append(f"{path}: must be one of {shown}" + (" …" if len(enum) > 20 else ""))
        if isinstance(value, str):
            self._bounds(len(value), schema, "minLength", "maxLength", path, "characters", found)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            self._number(value, schema, path, found)
        elif isinstance(value, list):
            self._bounds(len(value), schema, "minItems", "maxItems", path, "items", found)
            if "items" in schema:
                for index, item in enumerate(value):
                    self._check(item, schema["items"], f"{path}[{index}]", found, depth + 1)
        elif isinstance(value, dict):
            self._object(value, schema, path, found, depth)

    def _fits(self, value: Any, schema: Any, depth: int) -> bool:
        probe: list[str] = []
        self._check(value, schema, "$", probe, depth + 1)
        return not probe

    @staticmethod
    def _bounds(
        size: int,
        schema: dict[str, Any],
        low: str,
        high: str,
        path: str,
        unit: str,
        found: list[str],
    ) -> None:
        if isinstance(schema.get(low), int) and size < schema[low]:
            found.append(f"{path}: needs at least {schema[low]} {unit}")
        if isinstance(schema.get(high), int) and size > schema[high]:
            found.append(f"{path}: allows at most {schema[high]} {unit}")

    @staticmethod
    def _number(value: float, schema: dict[str, Any], path: str, found: list[str]) -> None:
        low, high = schema.get("minimum"), schema.get("maximum")
        ex_low, ex_high = schema.get("exclusiveMinimum"), schema.get("exclusiveMaximum")
        # OpenAPI 3.0 marks minimum/maximum exclusive with booleans; 3.1 gives the bound itself.
        if ex_low is True:
            ex_low, low = low, None
        elif ex_low is False:
            ex_low = None
        if ex_high is True:
            ex_high, high = high, None
        elif ex_high is False:
            ex_high = None
        if isinstance(low, (int, float)) and value < low:
            found.append(f"{path}: must be ≥ {low}")
        if isinstance(high, (int, float)) and value > high:
            found.append(f"{path}: must be ≤ {high}")
        if isinstance(ex_low, (int, float)) and value <= ex_low:
            found.append(f"{path}: must be > {ex_low}")
        if isinstance(ex_high, (int, float)) and value >= ex_high:
            found.append(f"{path}: must be < {ex_high}")

    def _object(
        self, value: dict[str, Any], schema: dict[str, Any], path: str, found: list[str], depth: int
    ) -> None:
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        assert isinstance(properties, dict)
        for name in schema.get("required") or []:
            prop = self.resolve(properties.get(name, {}))
            if name not in value and prop.get("readOnly") is not True:
                found.append(f"{path}.{name}: required")
        extra = schema.get("additionalProperties")
        for name, item in value.items():
            if name in properties:
                self._check(item, properties[name], f"{path}.{name}", found, depth + 1)
            elif extra is False:
                found.append(f"{path}.{name}: not allowed")
            elif isinstance(extra, dict):
                self._check(item, extra, f"{path}.{name}", found, depth + 1)


def coerce(value: Any, schema: Any, validator: Validator) -> Any:
    """Path and query values may arrive as strings; convert them when the schema is typed."""
    resolved = validator.resolve(schema)
    kind = resolved.get("type")
    kinds = kind if isinstance(kind, list) else [kind]
    if isinstance(value, list):
        return [coerce(item, resolved.get("items"), validator) for item in value]
    if not isinstance(value, str):
        return value
    try:
        if "integer" in kinds:
            return int(value)
        if "number" in kinds:
            return float(value)
    except ValueError:
        return value
    if "boolean" in kinds and value in ("true", "false"):
        return value == "true"
    return value
