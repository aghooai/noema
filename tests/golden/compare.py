"""Loading and comparing the golden SSE fixtures (spec-04 §4.2 comparison rules).

For the orchestrator's `tests/unit/turns/test_golden_sse.py` (C1):

    fx = load_fixture("G01")                      # scenario, setup, request, events, db
    world.use_scenario(fx["scenario"])            # tests.fakes.world: the fakes run the fixture's scenario
    ...run the turn, collecting (at_ms, event) from the TurnEventLog...
    assert compare(actual, fx) == []

Rules: event `type` order is exact; the fields listed in each expected event's `exact` are exact; ids are
normalized by first appearance (`turn_1`, `att_N` in start order, `dec_1` router, `dec_2` verifier, `ses_1`); a
run of consecutive `generation.delta` events is compared as its concatenated text; `decision.updated` must sit at
its (FakeClock) time when the actual events carry times.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from functools import cache
from pathlib import Path
from typing import Any, Final

import yaml

SSE_DIR: Final = Path(__file__).with_name("sse")
_ID: Final = re.compile(r"\b(turn|att|dec|ses)_[0-9A-Za-z]+\b")
Event = Mapping[str, Any]


def fixture_paths() -> dict[str, Path]:
    """Fixture id -> YAML path (`G01_agree.yaml` -> "G01")."""
    return {p.name.split("_", 1)[0]: p for p in sorted(SSE_DIR.glob("G*.yaml"))}


@cache
def load_fixture(fixture_id: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(fixture_paths()[fixture_id].read_text())
    return loaded


class Normalizer:
    """Maps raw ids to normalized ids by first appearance (`turn_1`, `att_1`, `dec_1`, ...)."""

    def __init__(self) -> None:
        self.map: dict[str, str] = {}
        self.count: dict[str, int] = {}

    def __call__(self, value: Any) -> Any:
        if isinstance(value, str):
            return _ID.sub(self._sub, value)
        if isinstance(value, Mapping):
            return {k: self(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self(v) for v in value]
        return value

    def _sub(self, match: re.Match[str]) -> str:
        raw = match.group(0)
        if raw not in self.map:
            prefix = match.group(1)
            self.count[prefix] = self.count.get(prefix, 0) + 1
            self.map[raw] = f"{prefix}_{self.count[prefix]}"
        return self.map[raw]


def normalize(events: Sequence[Event]) -> list[dict[str, Any]]:
    """Normalized copies of the events (one shared id map for the whole stream)."""
    norm = Normalizer()
    return [norm(dict(e)) for e in events]


def _get(event: Mapping[str, Any], path: str) -> Any:
    node: Any = event
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return _MISSING
        node = node[part]
    return node


_MISSING: Final = object()


def _collapse(items: Sequence[tuple[float | None, Mapping[str, Any]]]) -> list[tuple[float | None, dict[str, Any]]]:
    out: list[tuple[float | None, dict[str, Any]]] = []
    for at, ev in items:
        if ev["type"] == "generation.delta" and out and out[-1][1]["type"] == "generation.delta*":
            out[-1][1]["text"] += ev["text"]
            continue
        if ev["type"] == "generation.delta":
            out.append((at, {"type": "generation.delta*", "attempt_id": ev["attempt_id"], "text": ev["text"]}))
        else:
            out.append((at, dict(ev)))
    return out


def compare(actual: Sequence[Event | tuple[float, Event]], fixture: Mapping[str, Any]) -> list[str]:
    """Problems between the actual events (payloads, or `(at_ms, payload)` pairs) and a fixture; [] = match."""
    timed = [(a[0], a[1]) if isinstance(a, tuple) else (None, a) for a in actual]
    norm = Normalizer()
    got = _collapse([(at, norm(dict(ev))) for at, ev in timed])
    want = _collapse([(e["at"], e["data"]) for e in fixture["events"]])
    exact = [e["exact"] for e in fixture["events"] if e["type"] != "generation.delta"]
    problems: list[str] = []
    got_types, want_types = [e["type"] for _, e in got], [e["type"] for _, e in want]
    if got_types != want_types:
        return [f"event order differs:\n  got  {got_types}\n  want {want_types}"]
    paths = iter(exact)
    for i, ((g_at, g), (w_at, w)) in enumerate(zip(got, want, strict=True)):
        if w["type"] == "generation.delta*":
            if g["text"] != w["text"] or g["attempt_id"] != w["attempt_id"]:
                problems.append(f"#{i} delta run differs: {g['text']!r} != {w['text']!r}")
            continue
        problems += [
            f"#{i} {w['type']}.{path}: {_get(g, path)!r} != {_get(w, path)!r}"
            for path in next(paths)
            if _get(g, path) != _get(w, path)
        ]
        if w["type"] == "decision.updated" and g_at is not None and w_at is not None and round(g_at) != round(w_at):
            problems.append(f"#{i} decision.updated at {g_at} ms, want {w_at} ms")
    return problems


DECISION_EVENTS: Final = frozenset({"decision.completed", "decision.updated"})
TERMINALS: Final = frozenset({"generation.cancelled", "generation.completed"})


def sse_invariants(events: Sequence[Event], *, reached_t_d0: bool = True) -> list[str]:
    """P-SSE-1 (spec-04 §4) over one turn's payloads in log order; [] = all hold.

    `reached_t_d0=False`: the turn ended before its decision clock started (in preflight, or its decision entry
    failed), so it has no `decision.completed`. A restart cancels every live attempt with `spec_changed` at once (T9),
    so one restart is one contiguous run of those cancellations. Under `error{deadline_exceeded}` every attempt the
    deadline stopped has the turn's error as its terminal (T19)."""
    p: list[str] = []
    types = [e["type"] for e in events]
    if not types or types[0] != "turn.created":
        p.append("turn.created must be first")
    if types.count("turn.completed") != 1 or types[-1] != "turn.completed":
        p.append("turn.completed must be last and exactly once")
    if types.count("route.selected") != 1:
        p.append("exactly one route.selected")
    if types.count("generation.committed") > 1:
        p.append("at most one generation.committed")
    if reached_t_d0 and types.count("decision.completed") != 1:
        p.append("decision.completed exactly once")
    if not reached_t_d0 and "decision.completed" in types:
        p.append("no decision.completed before t_d0")
    changed = [i for i, e in enumerate(events) if e["type"] == "generation.cancelled" and e["reason"] == "spec_changed"]
    if sum(1 for k, i in enumerate(changed) if k == 0 or i != changed[k - 1] + 1) > 1:
        p.append("at most one spec_changed")
    committed: str | None = None
    seq = 0
    started = [e["attempt_id"] for e in events if e["type"] == "generation.started"]
    ended = [e["attempt_id"] for e in events if e["type"] in TERMINALS]
    errored = "error" in types
    deadline = any(e["type"] == "error" and e.get("code") == "deadline_exceeded" for e in events)
    for i, e in enumerate(events):
        if e["type"] == "generation.committed":
            committed = e["attempt_id"]
            prev = events[i - 1] if i else {}
            if prev.get("type") != "route.confirmed" or prev.get("attempt_id") != committed:
                p.append("generation.committed must follow route.confirmed of the same attempt")
        if e["type"] == "generation.delta":
            if e["attempt_id"] != committed:
                p.append("deltas only after commit and only for the committed attempt")
            if e["seq"] != seq:
                p.append(f"delta seq {e['seq']} != {seq}")
            seq += 1
        if e["type"] == "error":
            between = types[i + 1 : -1]
            if any(t not in DECISION_EVENTS for t in between):
                p.append("only decision events may sit between error and turn.completed")
    for attempt in started:
        n = ended.count(attempt)
        if n > 1 or (n == 0 and not (deadline or (errored and attempt == (committed or started[-1])))):
            p.append(f"{attempt}: needs exactly one terminal event")
    return p
