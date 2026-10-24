"""In-process search over one catalog's operations. Chinese text is split into character bigrams.

Ranking is tiered before BM25, so an operation the query names outranks one that merely mentions
the words in its description:

0. the whole query is an operationId or an alias of the operation;
1. the whole query is a module's name, display name or alias;
2. query terms appear in the operationId, summary, aliases, or the module's names or description;
3. query terms appear only in the operation's description, path or module limits.

A module description counts with the names: it is the module's summary, and it is often the only
place an English-only module is described in Chinese.

Within a tier, operations covering more of the query come first. Coverage is counted per query
unit, each English word or run of Chinese characters weighing the same, so two stray bigrams of a
Chinese phrase cannot outweigh the English word the user typed. On a tie: more of the query in
the module's names and description, so operations of the module that is about the subject come
before others that only mention it; then more of it in the operation's own operationId, aliases
and summary, which orders the operations within that module; then the less risky operation (a
lookup before a change); then BM25. Every tier is kept: an exact match ranks first without hiding
the other related operations.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, NamedTuple

_WORD = re.compile(r"[A-Za-z]+|\d+")
_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]+")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_SPACE = re.compile(r"[\s_\-]+")
K1 = 1.2
B = 0.75
# Fields in the order their name is reported as the match reason when they tie.
FIELDS = ("operation_id", "alias", "summary", "module", "description")
STRONG = frozenset(FIELDS[:4])
OWN = frozenset(FIELDS[:3])
RISK_ORDER = {"read": 0, "write": 1, "destructive": 2, "financial": 3}


class Hit(NamedTuple):
    op_id: str
    # Ascending sort key: (tier, -coverage, -module coverage, -own coverage, risk, -bm25).
    key: tuple[int, float, float, float, int, float]
    match: str


def _stem(word: str) -> str:
    """Plural to singular, enough for `markets` to meet `market`."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokens(text: str) -> list[str]:
    out: list[str] = []
    for word in _WORD.findall(text):
        parts = [_stem(p.lower()) for p in _CAMEL.findall(word)] or [_stem(word.lower())]
        out.extend(parts)
        if len(parts) > 1:
            out.append(_stem(word.lower()))
    for run in _CJK.findall(text):
        if len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i : i + 2] for i in range(len(run) - 1))
    return out


def units(text: str) -> list[set[str]]:
    """The query's words and Chinese runs, each as the set of its search tokens."""
    out = [set(tokens(word)) for word in _WORD.findall(text)]
    out += [set(tokens(run)) for run in _CJK.findall(text)]
    return [unit for unit in out if unit]


def _coverage(parts: list[set[str]], found: set[str]) -> float:
    return sum(len(unit & found) / len(unit) for unit in parts) / len(parts) if parts else 0.0


def normalize(text: str) -> str:
    return _SPACE.sub(" ", text.strip().lower())


def _module_names(module: dict[str, Any]) -> list[str]:
    names = [module["name"], module.get("display_name") or ""]
    return [n for n in names + list(module.get("aliases") or []) if n]


class SearchIndex:
    def __init__(self, compiled: dict[str, Any]) -> None:
        modules = {m["name"]: m for m in compiled.get("modules", [])}
        self.docs: dict[str, Counter[str]] = {}
        self.fields: dict[str, dict[str, set[str]]] = {}
        self.exact: dict[str, set[str]] = {}
        self.module_exact: dict[str, set[str]] = {}
        self.lengths: dict[str, int] = {}
        self.module_of: dict[str, str] = {}
        self.risk: dict[str, int] = {}
        frequency: Counter[str] = Counter()
        for name, module in modules.items():
            for label in _module_names(module):
                self.module_exact.setdefault(normalize(label), set()).add(name)
        for op_id, op in compiled.get("operations", {}).items():
            self.module_of[op_id] = op.get("module", "")
            self.risk[op_id] = RISK_ORDER.get(op.get("risk", ""), len(RISK_ORDER))
            module = modules.get(self.module_of[op_id]) or {"name": self.module_of[op_id]}
            body = tokens(op.get("description", ""))
            body += tokens(module.get("limits", ""))
            for segment in str(op.get("path", "")).split("/"):
                if segment and not segment.startswith("{"):
                    body += tokens(segment)
            fields = {
                "operation_id": tokens(op_id),
                "alias": tokens(" ".join(op.get("aliases") or [])),
                "summary": tokens(op.get("summary", "")),
                "module": tokens(" ".join([*_module_names(module), module.get("description", "")])),
                "description": body,
            }
            words = (
                fields["operation_id"] * 2
                + fields["alias"] * 2
                + fields["summary"] * 2
                + fields["module"]
                + body
            )
            counts = Counter(words)
            self.docs[op_id] = counts
            self.fields[op_id] = {name: set(value) for name, value in fields.items()}
            self.lengths[op_id] = len(words)
            frequency.update(counts.keys())
            for label in [op_id, *(op.get("aliases") or [])]:
                self.exact.setdefault(normalize(label), set()).add(op_id)
        total = len(self.docs)
        self.average = (sum(self.lengths.values()) / total) if total else 0.0
        self.idf = {
            term: math.log(1 + (total - n + 0.5) / (n + 0.5)) for term, n in frequency.items()
        }

    def _bm25(self, op_id: str, terms: set[str]) -> float:
        counts = self.docs[op_id]
        length = self.lengths[op_id]
        score = 0.0
        for term in terms:
            tf = counts.get(term)
            if not tf:
                continue
            norm = tf + K1 * (1 - B + B * length / (self.average or 1))
            score += self.idf[term] * tf * (K1 + 1) / norm
        return score

    def search(self, query: str) -> list[Hit]:
        terms = set(tokens(query))
        parts = units(query)
        whole = normalize(query)
        exact = self.exact.get(whole, set())
        in_module = self.module_exact.get(whole, set())
        hits: list[Hit] = []
        for op_id in self.docs:
            fields = self.fields[op_id]
            matched = {name: len(terms & fields[name]) for name in FIELDS}
            strong = set().union(*(terms & fields[name] for name in STRONG)) if terms else set()
            score = self._bm25(op_id, terms)
            if op_id in exact:
                tier = 0
                match = "alias" if normalize(op_id) != whole else "operation_id"
            elif self.module_of[op_id] in in_module:
                tier, match = 1, "module"
            elif strong:
                tier = 2
                match = max(FIELDS[:4], key=lambda name: (matched[name], -FIELDS.index(name)))
            elif score > 0:
                tier, match = 3, "description"
            else:
                continue
            own = set().union(*(terms & fields[name] for name in OWN)) if terms else set()
            key = (
                tier,
                -_coverage(parts, strong),
                -_coverage(parts, terms & fields["module"]),
                -_coverage(parts, own),
                self.risk[op_id],
                -score,
            )
            hits.append(Hit(op_id, key, match))
        hits.sort(key=lambda hit: (hit.key, hit.op_id))
        return hits
