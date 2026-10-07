"""Contract checks, rule for rule the same as `business-system-contract.spectral.yaml`.

Rule names and severities match the published Spectral ruleset (a test compares them), so a
system that passes Spectral in its own CI sees the same result here. `oas3-schema` is a
structural subset of the OpenAPI schema: enough to compile the catalog, not a full validator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from coreman.core.systems_catalog.refs import Refs

ERROR = "error"
WARN = "warn"
MAX_FINDINGS = 200
# The ruleset's `#Operation` alias; the success-schema rules use the narrower `#JsonSuccess`.
METHODS = ("get", "put", "post", "delete", "patch", "head", "options")
JSON_SUCCESS_METHODS = ("get", "put", "post", "delete", "patch")
OPERATION_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,99}$")
_SUCCESS = re.compile(r"^2\d\d$")
_TEMPLATE = re.compile(r"\{([^{}/]+)\}")
_OPENAPI = re.compile(r"^3\.[01]\.\d+")
_IN = ("query", "header", "path", "cookie")

RULES: dict[str, str] = {
    "oas3-schema": ERROR,
    "path-params": ERROR,
    "operation-operationId": ERROR,
    "operation-operationId-unique": ERROR,
    "operation-tags": ERROR,
    "operation-tag-defined": ERROR,
    "contract-operation-id-pattern": ERROR,
    "contract-summary": ERROR,
    "contract-permission": ERROR,
    "contract-operation-x-agent": ERROR,
    "contract-root-x-agent": ERROR,
    "contract-root-permissions": WARN,
    "contract-tag-description": ERROR,
    "contract-parameter-description": WARN,
    "contract-success-schema": ERROR,
    "contract-success-not-free-form": WARN,
    "contract-enum-size": WARN,
    "contract-local-refs": ERROR,
}


def pointer(*parts: str | int) -> str:
    return "#/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in parts)


@dataclass(frozen=True)
class Finding:
    rule: str
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "rule": self.rule,
            "severity": RULES[self.rule],
            "path": self.path,
            "message": self.message,
        }


def operations(doc: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """(path, method, operation) in document order; only well-formed operation objects."""
    paths = doc.get("paths")
    found: list[tuple[str, str, dict[str, Any]]] = []
    if not isinstance(paths, dict):
        return found
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method in METHODS:
            op = item.get(method)
            if isinstance(op, dict):
                found.append((path, method, op))
    return found


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and len(value) >= 1


class _Linter:
    def __init__(self, doc: dict[str, Any]) -> None:
        self.doc = doc
        self.refs = Refs(doc)
        self.found: list[Finding] = []

    def add(self, rule: str, path: str, message: str) -> None:
        self.found.append(Finding(rule, path, message))

    def run(self) -> list[Finding]:
        self.structure()
        self.root_agent()
        self.tags()
        ids: dict[str, list[str]] = {}
        for path, method, op in operations(self.doc):
            here = ("paths", path, method)
            self.operation(path, method, op, here, ids)
        for op_id, places in ids.items():
            for place in places[1:]:
                self.add(
                    "operation-operationId-unique",
                    place,
                    f'Every operation must have unique "operationId" ({op_id}).',
                )
        self.path_parameters()
        self.walk(self.doc, ())
        return self.found

    def structure(self) -> None:
        doc = self.doc
        version = doc.get("openapi")
        if not isinstance(version, str) or not _OPENAPI.match(version):
            self.add("oas3-schema", pointer("openapi"), '"openapi" must be 3.0.x or 3.1.x.')
        info = doc.get("info")
        if not isinstance(info, dict):
            self.add("oas3-schema", pointer("info"), '"info" must be an object.')
        else:
            for key in ("title", "version"):
                if not isinstance(info.get(key), str):
                    self.add("oas3-schema", pointer("info", key), f'"info.{key}" is required.')
        paths = doc.get("paths")
        if not isinstance(paths, dict):
            self.add("oas3-schema", pointer("paths"), '"paths" must be an object.')
            return
        for path, item in paths.items():
            if not path.startswith("/"):
                self.add("oas3-schema", pointer("paths", path), 'Path keys must start with "/".')
            if not isinstance(item, dict):
                self.add("oas3-schema", pointer("paths", path), "Path items must be objects.")
                continue
            for method in METHODS:
                if method in item and not isinstance(item[method], dict):
                    self.add(
                        "oas3-schema", pointer("paths", path, method), "Operations must be objects."
                    )
            params = item.get("parameters", [])
            self.parameter_shapes(params, ("paths", path, "parameters"))
            for method in METHODS:
                op = item.get(method)
                if not isinstance(op, dict):
                    continue
                if not isinstance(op.get("responses"), dict) or not op["responses"]:
                    self.add(
                        "oas3-schema",
                        pointer("paths", path, method, "responses"),
                        '"responses" must list at least one response.',
                    )
                self.parameter_shapes(
                    op.get("parameters", []), ("paths", path, method, "parameters")
                )
                tags = op.get("tags")
                if tags is not None and (
                    not isinstance(tags, list) or not all(isinstance(t, str) for t in tags)
                ):
                    self.add(
                        "oas3-schema",
                        pointer("paths", path, method, "tags"),
                        '"tags" must be a list of strings.',
                    )
        tags = doc.get("tags")
        if tags is not None and (
            not isinstance(tags, list)
            or not all(isinstance(t, dict) and isinstance(t.get("name"), str) for t in tags)
        ):
            self.add("oas3-schema", pointer("tags"), "Top-level tags need a name.")

    def parameter_shapes(self, params: Any, where: tuple[str, ...]) -> None:
        if not isinstance(params, list):
            self.add("oas3-schema", pointer(*where), '"parameters" must be a list.')
            return
        for index, param in enumerate(params):
            if isinstance(param, dict) and "$ref" in param:
                continue
            if (
                not isinstance(param, dict)
                or not isinstance(param.get("name"), str)
                or param.get("in") not in _IN
            ):
                self.add(
                    "oas3-schema",
                    pointer(*where, index),
                    'Parameters need a "name" and an "in" of query, header, path or cookie.',
                )
            elif param["in"] == "path" and param.get("required") is not True:
                self.add(
                    "oas3-schema",
                    pointer(*where, index),
                    'Path parameters must set "required" to true.',
                )

    def root_agent(self) -> None:
        agent = self.doc.get("x-agent")
        where = pointer("x-agent")
        if agent is None:
            self.add(
                "contract-root-permissions",
                where,
                "Declare x-agent.permissions so the catalog can be filtered by the caller's "
                "permissions.",
            )
            return
        if not isinstance(agent, dict) or set(agent) - {"guide", "permissions"}:
            self.add("contract-root-x-agent", where, "Root x-agent must use the documented keys.")
            agent = agent if isinstance(agent, dict) else {}
        guide = agent.get("guide")
        if guide is not None and (not isinstance(guide, str) or len(guide) > 2000):
            self.add(
                "contract-root-x-agent",
                pointer("x-agent", "guide"),
                "x-agent.guide must be a string of at most 2000 characters.",
            )
        perms = agent.get("permissions")
        if perms is not None and (
            not isinstance(perms, dict)
            or set(perms) - {"operationId", "pointer"}
            or not _non_empty_string(perms.get("operationId"))
            or not isinstance(perms.get("pointer"), str)
            or not perms["pointer"].startswith("/")
        ):
            self.add(
                "contract-root-x-agent",
                pointer("x-agent", "permissions"),
                "x-agent.permissions needs operationId and a JSON Pointer starting with /.",
            )
        if not perms:
            self.add(
                "contract-root-permissions",
                where,
                "Declare x-agent.permissions so the catalog can be filtered by the caller's "
                "permissions.",
            )

    def tags(self) -> None:
        tags = self.doc.get("tags")
        if not isinstance(tags, list):
            return
        for index, tag in enumerate(tags):
            if not isinstance(tag, dict):
                continue
            description = tag.get("description")
            if not description:
                self.add(
                    "contract-tag-description",
                    pointer("tags", index),
                    "Every top-level tag needs a description of at most 200 characters.",
                )
            elif isinstance(description, str) and len(description) > 200:
                self.add(
                    "contract-tag-description",
                    pointer("tags", index, "description"),
                    "Every top-level tag needs a description of at most 200 characters.",
                )

    def defined_tags(self) -> set[str]:
        tags = self.doc.get("tags")
        if not isinstance(tags, list):
            return set()
        return {t["name"] for t in tags if isinstance(t, dict) and isinstance(t.get("name"), str)}

    def operation(
        self,
        path: str,
        method: str,
        op: dict[str, Any],
        here: tuple[str, ...],
        ids: dict[str, list[str]],
    ) -> None:
        op_id = op.get("operationId")
        if not op_id:
            self.add(
                "operation-operationId",
                pointer(*here),
                'Operation must have "operationId".',
            )
        elif isinstance(op_id, str):
            ids.setdefault(op_id, []).append(pointer(*here, "operationId"))
            if not OPERATION_ID.match(op_id):
                self.add(
                    "contract-operation-id-pattern",
                    pointer(*here, "operationId"),
                    "operationId must match ^[A-Za-z][A-Za-z0-9_]{0,99}$.",
                )
        tags = op.get("tags")
        if not isinstance(tags, list) or not tags:
            self.add(
                "operation-tags",
                pointer(*here, "tags") if "tags" in op else pointer(*here),
                'Operation must have non-empty "tags" array.',
            )
        else:
            defined = self.defined_tags()
            for index, tag in enumerate(tags):
                if isinstance(tag, str) and tag not in defined:
                    self.add(
                        "operation-tag-defined",
                        pointer(*here, "tags", index),
                        f'Operation tags must be defined in global tags ("{tag}").',
                    )
        summary = op.get("summary")
        if not summary:
            self.add(
                "contract-summary",
                pointer(*here),
                "Every operation needs a summary of at most 80 characters.",
            )
        elif isinstance(summary, str) and len(summary) > 80:
            self.add(
                "contract-summary",
                pointer(*here, "summary"),
                "Every operation needs a summary of at most 80 characters.",
            )
        permission = op.get("x-permission")
        valid = _non_empty_string(permission) or (
            isinstance(permission, list)
            and len(permission) >= 1
            and all(_non_empty_string(p) for p in permission)
        )
        if not valid:
            self.add(
                "contract-permission",
                pointer(*here, "x-permission") if permission is not None else pointer(*here),
                'Every operation declares x-permission (a code, a list of codes, or "none").',
            )
        if "x-agent" in op:
            self.operation_agent(op["x-agent"], (*here, "x-agent"))
        params = op.get("parameters", [])
        if isinstance(params, list):
            self.parameter_descriptions(params, (*here, "parameters"))
        if method in JSON_SUCCESS_METHODS:
            self.success(op, here)

    def operation_agent(self, agent: Any, where: tuple[str, ...]) -> None:
        message = "Operation-level x-agent must use the documented keys."
        if not isinstance(agent, dict):
            self.add("contract-operation-x-agent", pointer(*where), message)
            return
        valid = {
            "risk": lambda v: v in ("read", "write", "destructive", "financial"),
            "hidden": lambda v: isinstance(v, bool),
            "hint": lambda v: isinstance(v, str) and len(v) <= 500,
            "extra_permissions": lambda v: (
                isinstance(v, list) and all(_non_empty_string(p) for p in v)
            ),
        }
        for key, value in agent.items():
            check = valid.get(key)
            if check is None or not check(value):
                self.add("contract-operation-x-agent", pointer(*where, key), message)

    def parameter_descriptions(self, params: list[Any], where: tuple[str, ...]) -> None:
        for index, raw in enumerate(params):
            param = self.refs.deref(raw)
            if isinstance(param, dict) and not param.get("description"):
                self.add(
                    "contract-parameter-description",
                    pointer(*where, index),
                    "Every parameter needs a description.",
                )

    def success(self, op: dict[str, Any], here: tuple[str, ...]) -> None:
        responses = op.get("responses")
        if not isinstance(responses, dict):
            return
        for code, raw in responses.items():
            if not _SUCCESS.match(code):
                continue
            response = self.refs.deref(raw)
            content = response.get("content") if isinstance(response, dict) else None
            media = content.get("application/json") if isinstance(content, dict) else None
            if media is None:
                continue
            where = (*here, "responses", code, "content", "application/json")
            schema = media.get("schema") if isinstance(media, dict) else None
            if not schema:
                self.add(
                    "contract-success-schema",
                    pointer(*where),
                    "JSON success responses need a schema.",
                )
                continue
            resolved = self.refs.deref(schema)
            if (
                isinstance(resolved, dict)
                and resolved.get("type") == "object"
                and not any(k in resolved for k in ("properties", "allOf", "oneOf", "anyOf"))
            ):
                self.add(
                    "contract-success-not-free-form",
                    pointer(*where, "schema"),
                    "JSON success responses should not be a bare free-form object.",
                )

    def path_parameters(self) -> None:
        paths = self.doc.get("paths")
        if not isinstance(paths, dict):
            return
        for path, item in paths.items():
            if not isinstance(item, dict):
                continue
            template = set(_TEMPLATE.findall(path))
            shared = self.path_params(item.get("parameters"), ("paths", path, "parameters"))
            for method in METHODS:
                op = item.get(method)
                if not isinstance(op, dict):
                    continue
                own = self.path_params(op.get("parameters"), ("paths", path, method, "parameters"))
                declared = {**shared, **own}
                for name in sorted(template - set(declared)):
                    self.add(
                        "path-params",
                        pointer("paths", path, method),
                        f'Operation must define parameter "{{{name}}}" as expected by path.',
                    )
                for name, (where, required) in declared.items():
                    if name not in template:
                        self.add(
                            "path-params",
                            where,
                            f'Parameter "{name}" must be used in path "{path}".',
                        )
                    if not required:
                        self.add(
                            "path-params",
                            where,
                            f'Path parameter "{name}" must have "required" property set to true.',
                        )

    def path_params(self, params: Any, where: tuple[str, ...]) -> dict[str, tuple[str, bool]]:
        out: dict[str, tuple[str, bool]] = {}
        if not isinstance(params, list):
            return out
        for index, raw in enumerate(params):
            param = self.refs.deref(raw)
            if isinstance(param, dict) and param.get("in") == "path":
                name = param.get("name")
                if isinstance(name, str):
                    out[name] = (pointer(*where, index), param.get("required") is True)
        return out

    def walk(self, node: Any, where: tuple[str | int, ...]) -> None:
        stack: list[tuple[Any, tuple[str | int, ...]]] = [(node, where)]
        while stack:
            value, here = stack.pop()
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "$ref" and isinstance(item, str) and not item.startswith("#/"):
                        self.add(
                            "contract-local-refs",
                            pointer(*here, key),
                            "Only local references (#/...) are supported.",
                        )
                    elif key == "enum" and isinstance(item, list) and len(item) > 50:
                        self.add(
                            "contract-enum-size",
                            pointer(*here, key),
                            "Inline enums are limited to 50 values; use x-agent-options for "
                            "larger value sets.",
                        )
                    stack.append((item, (*here, key)))
            elif isinstance(value, list):
                stack.extend((item, (*here, index)) for index, item in enumerate(value))


def lint(doc: dict[str, Any]) -> list[dict[str, str]]:
    """Findings, errors first, at most MAX_FINDINGS (the stored `lint` column)."""
    found = _Linter(doc).run()
    found.sort(key=lambda f: 0 if RULES[f.rule] == ERROR else 1)
    return [f.as_dict() for f in found[:MAX_FINDINGS]]
