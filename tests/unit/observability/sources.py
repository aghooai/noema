"""The observability-as-code sources the tests read (deploy/observability): rule files, dashboards, the builder, and
a small PromQL reference scanner (metric names, label matchers, grouping labels, enclosing functions)."""

from __future__ import annotations

import importlib.util
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[3]
OBS = REPO / "deploy" / "observability"
RULES_DIR = OBS / "prometheus" / "rules"
DASHBOARD_DIR = OBS / "grafana" / "dashboards"
BUILDER = OBS / "grafana" / "build_dashboards.py"
DOC = REPO / "docs" / "operations" / "observability.md"
RUNBOOKS = REPO / "docs" / "operations" / "runbooks.md"

# Metric references: Noema instruments, recording rules, and the one external metric (cAdvisor pod CPU).
_REF = re.compile(r"(?<![A-Za-z0-9_:])(aghoo_[a-z0-9_]+|noema:[a-z0-9_:]+|container_[a-z0-9_]+)")
_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
_MATCHER = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:=~|!~|!=|=)")
_GROUPING = re.compile(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\(([^()]*)\)")
_FUNC = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*$")


@dataclass(frozen=True, slots=True)
class Ref:
    """One metric reference in a PromQL expression."""

    name: str
    matchers: frozenset[str]  # label names in its {...} selector
    ranged: bool  # followed by a [range] selector
    function: str  # the function whose argument list encloses it ("" at top level or in an aggregation)


def rule_groups() -> list[tuple[Path, dict[str, Any]]]:
    return [(p, g) for p in sorted(RULES_DIR.glob("*.rules.yml")) for g in yaml.safe_load(p.read_text())["groups"]]


def rules() -> list[dict[str, Any]]:
    return [r for _, g in rule_groups() for r in g["rules"]]


def alerts() -> list[dict[str, Any]]:
    return [r for r in rules() if "alert" in r]


def records() -> dict[str, str]:
    """Recording rule name -> expression."""
    return {r["record"]: r["expr"] for r in rules() if "record" in r}


def dashboards() -> dict[str, dict[str, Any]]:
    """File name -> dashboard JSON."""
    return {p.name: json.loads(p.read_text()) for p in sorted(DASHBOARD_DIR.glob("*.json"))}


def panels(dash: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in dash["panels"] if p["type"] != "row"]


def builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_dashboards", BUILDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _enclosing_function(expr: str, pos: int) -> str:
    depth = 0
    for i in range(pos - 1, -1, -1):
        if expr[i] == ")":
            depth += 1
        elif expr[i] == "(":
            if depth == 0:
                m = _FUNC.search(expr[:i])
                return m.group(1) if m else ""
            depth -= 1
    return ""


def refs(expr: str) -> list[Ref]:
    """Every metric reference of `expr` (string literals blanked first, so label values never match)."""
    text = _STRING.sub(lambda m: '"' + " " * (len(m.group(0)) - 2) + '"', expr)
    out = []
    for m in _REF.finditer(text):
        rest, matchers = text[m.end() :], frozenset[str]()
        stripped = rest.lstrip()
        if stripped.startswith("{"):
            block = stripped[1 : stripped.index("}")]
            matchers = frozenset(_MATCHER.findall(block))
            stripped = stripped[stripped.index("}") + 1 :].lstrip()
        out.append(Ref(m.group(1), matchers, stripped.startswith("["), _enclosing_function(text, m.start())))
    return out


def grouping_labels(expr: str) -> set[str]:
    """Labels named in by / without / on / ignoring / group_left / group_right clauses."""
    text = _STRING.sub('""', expr)
    return {label.strip() for clause in _GROUPING.findall(text) for label in clause.split(",") if label.strip()}


def template_labels(text: str) -> set[str]:
    """`{{pod}}` (legend) and `$labels.pod` (annotation) label names."""
    return set(re.findall(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", text)) | set(
        re.findall(r"\$labels\.([A-Za-z_][A-Za-z0-9_]*)", text)
    )


def markdown_anchors(text: str) -> set[str]:
    """GitHub-style heading anchors plus explicit id= / name= anchors of a Markdown document."""
    anchors = set(re.findall(r"""(?:id|name)=["']([^"']+)["']""", text))
    for heading in re.findall(r"^#{1,6}\s+(.+?)\s*#*\s*$", text, flags=re.MULTILINE):
        slug = re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")
        anchors.add(slug)
    return anchors
