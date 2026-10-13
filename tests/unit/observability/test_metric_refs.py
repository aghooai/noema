"""Every metric the Prometheus rules and the Grafana dashboards query exists in the telemetry registry
(src/noema/telemetry/registry.py) with the right kind and labels, and is used as its kind allows."""

from __future__ import annotations

import re

import pytest
from tests.unit.observability import sources

from noema.telemetry.registry import METRICS

# Agreed in Wave F, exported by the infra-perf agent (TurnSlots.held): allowed before it lands in the registry, and
# checked against the registry once it does.
PENDING = {"aghoo_turn_slots_held": ("gauge", ())}
# Not Noema instruments: cAdvisor's pod CPU (noema:pod_cpu_cores:rate5m).
EXTERNAL = {"container_cpu_usage_seconds_total": ("counter", ("pod", "container", "namespace"))}
# Added by the scrape configuration (deploy/observability/prometheus/prometheus.example.yml).
TARGET_LABELS = {"pod", "job", "instance", "namespace"}
HISTOGRAM_SUFFIXES = ("_bucket", "_sum", "_count")
COUNTER_FUNCTIONS = {"rate", "increase", "irate"}
RANGE_GAUGE_FUNCTIONS = {"deriv", "delta", "predict_linear", "changes", "resets"}
# The first-sample clause `c unless increase(c[<range>] offset <d>)` of the "any increase" alerts: a counter is
# exported only after its first add and increase() cannot count the step from absent to that value, so the clause
# reads the counter itself while its series is younger than <d>.
FIRST_SAMPLE = re.compile(r"\b(aghoo_[a-z0-9_]+)\s+unless\s+increase\(\s*\1\[")


def _spec(name: str) -> tuple[str, tuple[str, ...]] | None:
    """(kind, labels) of an instrument name, or None if it is not declared anywhere."""
    if name in METRICS:
        return METRICS[name].kind, METRICS[name].labels
    return PENDING.get(name) or EXTERNAL.get(name)


def _base(name: str) -> tuple[str, str]:
    """(instrument name, histogram suffix or "")."""
    for suffix in HISTOGRAM_SUFFIXES:
        base = name.removesuffix(suffix)
        if base != name and (spec := _spec(base)) is not None and spec[0] == "histogram":
            return base, suffix
    return name, ""


def _expressions() -> list[tuple[str, str]]:
    """(where, expr) of every rule and every dashboard target."""
    out = [(r.get("alert") or r["record"], str(r["expr"])) for r in sources.rules()]
    for file, dash in sources.dashboards().items():
        out += [(f"{file}: {p['title']}", t["expr"]) for p in sources.panels(dash) for t in p["targets"]]
    return out


def _labels_of(expr: str, seen: frozenset[str] = frozenset()) -> set[str]:
    """Every label any instrument of `expr` carries (recording rules expanded), plus the target labels and le."""
    labels = set(TARGET_LABELS) | {"le"}
    records = sources.records()
    for ref in sources.refs(expr):
        if ref.name.startswith("noema:") and ref.name in records and ref.name not in seen:
            labels |= _labels_of(records[ref.name], seen | {ref.name})
        elif (spec := _spec(_base(ref.name)[0])) is not None:
            labels |= set(spec[1])
    return labels


EXPRESSIONS = _expressions()


def test_sources_are_found() -> None:
    assert len(sources.alerts()) >= 20 and len(sources.records()) >= 10
    assert len(sources.dashboards()) == 7


@pytest.mark.parametrize(("where", "expr"), EXPRESSIONS, ids=[w for w, _ in EXPRESSIONS])
def test_every_reference_is_declared_with_its_kind_and_labels(where: str, expr: str) -> None:
    records = sources.records()
    first_sample = set(FIRST_SAMPLE.findall(expr))
    problems = []
    for ref in sources.refs(expr):
        if ref.name.startswith("noema:"):
            if ref.name not in records:
                problems.append(f"{ref.name}: no such recording rule")
            continue
        base, suffix = _base(ref.name)
        spec = _spec(base)
        if spec is None:
            problems.append(f"{ref.name}: not in src/noema/telemetry/registry.py")
            continue
        kind, labels = spec
        allowed = set(labels) | TARGET_LABELS | ({"le"} if suffix == "_bucket" else set())
        problems += [f"{ref.name}: undeclared label {lbl!r}" for lbl in sorted(ref.matchers - allowed)]
        if kind == "histogram" and not suffix:
            problems.append(f"{ref.name}: a histogram is queried through _bucket, _sum or _count")
        raw_first_sample = kind == "counter" and not ref.ranged and ref.name in first_sample
        if (
            (kind == "counter" or suffix)
            and (not ref.ranged or ref.function not in COUNTER_FUNCTIONS)
            and not raw_first_sample
        ):
            problems.append(f"{ref.name}: a {kind} series belongs in rate() / increase() over a range")
        if (
            kind == "gauge"
            and ref.ranged
            and not (ref.function.endswith("_over_time") or ref.function in RANGE_GAUGE_FUNCTIONS)
        ):
            problems.append(f"{ref.name}: a gauge range belongs in a *_over_time function")
    unknown = sources.grouping_labels(expr) - _labels_of(expr)
    problems += [f"grouping label {lbl!r} is on none of the queried metrics" for lbl in sorted(unknown)]
    assert not problems, f"{where}: {problems}"


def test_pending_metrics_match_the_registry_once_declared() -> None:
    for name, (kind, labels) in PENDING.items():
        if name in METRICS:
            assert (METRICS[name].kind, METRICS[name].labels) == (kind, labels), name


def test_template_labels_exist_on_the_queried_metrics() -> None:
    problems = []
    for alert in sources.alerts():
        used = set().union(*(sources.template_labels(str(v)) for v in alert["annotations"].values()))
        problems += [f"{alert['alert']}: $labels.{lbl}" for lbl in sorted(used - _labels_of(alert["expr"]))]
    for file, dash in sources.dashboards().items():
        for panel in sources.panels(dash):
            for t in panel["targets"]:
                unknown = sources.template_labels(t["legendFormat"]) - _labels_of(t["expr"])
                problems += [f"{file}: {panel['title']}: {{{{{lbl}}}}}" for lbl in sorted(unknown)]
    assert not problems
