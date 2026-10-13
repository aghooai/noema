"""Fault-suite kit (spec-10 §4, 06 §7): scenarios, a concurrent SSE turn driver over both api replicas and two
tenants, the S1 checks (fallback success, zero duplicated client-visible text), statistics and the results file.

Every Noema response goes through the stack's `CheckedClient` (ContractValidator): createTurn streams frame by frame
(`LiveTurn`) and whole at the end, every other status as a JSON envelope. Mock latencies are fixed (no
distributions), so every ordering a test relies on has >= 200 ms of margin and the TTFT breaker baselines stay flat:
router members answer at MEMBER_MS (S1 then precedes every first text) and every generation stream starts its text
at FIRST_TEXT_MS.
"""

from __future__ import annotations

import os
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import anyio
import orjson
from tests.e2e.conftest import CheckedClient, Stack
from tests.e2e.suite import scenarios as sc
from tests.e2e.suite.kit import LiveTurn, tag, turn_body
from tests.e2e.suite.kit import data as data
from tests.golden.compare import sse_invariants

from noema.contracts.validate import SseEvent

Json = dict[str, Any]
Rule = dict[str, Any]

TENANTS: Final = ("t_acme", "t_beta")
DEADLINE_MS: Final = 10_000  # spec-10 §4: every row runs with Aghoo-Deadline-Ms: 10000 unless it says otherwise
TURNS: Final = int(os.environ.get("NOEMA_FAULT_TURNS", "200"))  # spec-10 §4: >= 200 turns per row
CONCURRENCY: Final = int(os.environ.get("NOEMA_FAULT_CONCURRENCY", "20"))
MEMBER_MS: Final = 200
FIRST_TEXT_MS: Final = 600
STEP_MS: Final = 50
TEXT: Final = sc.TEXT
CHUNKS: Final = sc.CHUNKS
SOL, OPUS, GLM = sc.SOL, sc.OPUS, sc.GLM
HAIKU, LUNA, DEEPSEEK = sc.HAIKU, sc.LUNA, sc.DEEPSEEK
LABELER_PROMPT: Final = "You label data for quality evaluation"  # labeler@1 (sampled labeling.item jobs)

# Every pre-commit key pinned (spec-10 §5 "overhead scenario"): the decided spec equals spec0, so the commit is at
# the committed attempt's first text and a restart never mixes into a fault's numbers.
PINNED: Final[Mapping[str, Json]] = {
    "sol-fast": {"profile": "sol-fast", "effort": "low", "max_output_tokens": 600},
    "opus-deep": {"profile": "opus-deep", "effort": "high", "max_output_tokens": 4000},
    "glm-reason": {"profile": "glm-reason", "effort": "medium", "max_output_tokens": 4096},
}


# --- scenarios ----------------------------------------------------------------------------------------------------
fault_rule = sc.fault


def sc_low() -> dict[str, float]:
    """A confident `false` predicate distribution."""
    return dict(sc.LOW)


def members(ms: float = MEMBER_MS) -> list[Rule]:
    """Labeler answers first (a sampled labeling.item must not fail against a generation model's breaker), then the
    five router-default questions (factual_qa, moderate, nothing suspected) and every other predicate."""
    labeler = {"match": {"prompt_contains": LABELER_PROMPT}, "answer": {"raw_text": "A"}, "latency_ms": 50}
    return [labeler, *sc.router(ms=ms, haiku_hangs_on=None)]


def gen(
    model: str,
    *,
    first_text_ms: float = FIRST_TEXT_MS,
    fault: Mapping[str, Any] | None = None,
    reasoning: int = 0,
    step_ms: float = STEP_MS,
    marker: str | None = None,
) -> Rule:
    """One ConverseStream rule of `model` streaming TEXT (7 chunks, one every `step_ms` after the first text), after
    `reasoning` reasoning deltas (every 20 ms from the first event); with `marker`, only for turns whose text has it."""
    rule = sc.stream(
        model, first_text_ms=first_text_ms, first_event_ms=100, step_ms=step_ms, fault=fault, reasoning=reasoning
    )
    if marker is not None:
        rule["match"]["prompt_contains"] = marker
    return rule


def gen_fault(model: str, kind: str, ms: float = 100, **extra: Any) -> Rule:
    """A pre-byte (or stream) fault on `model`'s ConverseStream calls."""
    return sc.fault({"model": model, "op": "converse-stream"}, kind, ms, **extra)


def scenario(name: str, *groups: Sequence[Rule]) -> Json:
    return sc.document(name, *groups)


def body(label: str, profile: str | None = None, **extra: Any) -> Json:
    """A streamed createTurn body with a unique text (no L2 hit) and, with `profile`, every pre-commit key pinned."""
    if profile is not None:
        extra = {"generation": dict(PINNED[profile]), "conditioning": "off", **extra}
    return turn_body(f"What is the capital of France? ({tag(label)})", **extra)


# --- the driver ---------------------------------------------------------------------------------------------------
@dataclass
class TurnRecord:
    """One createTurn as the client saw it (times in ms from the request's send, CLOCK_MONOTONIC)."""

    index: int
    tenant: str
    replica: int
    status: int = 0
    events: list[SseEvent] = field(default_factory=list)
    at_ms: list[float] = field(default_factory=list)
    envelope: Json | None = None  # the error body of a non-200 answer
    ttft_ms: float | None = None
    e2e_ms: float = 0.0
    t_send: float = 0.0

    def of(self, kind: str) -> list[Json]:
        return [data(e) for e in self.events if e.event == kind]

    @property
    def kinds(self) -> list[str]:
        return [e.event for e in self.events]

    @property
    def turn_id(self) -> str:
        return str(data(self.events[0])["turn_id"]) if self.events else ""

    @property
    def started(self) -> list[Json]:
        return self.of("generation.started")

    @property
    def committed(self) -> str | None:
        found = self.of("generation.committed")
        return str(found[0]["attempt_id"]) if found else None

    @property
    def error(self) -> Json | None:
        found = self.of("error")
        return found[0] if found else None

    @property
    def completed(self) -> bool:
        """`generation.completed` sent and no error: the turn was served (spec-04 §4 `completed`)."""
        return self.status == 200 and bool(self.of("generation.completed")) and self.error is None

    @property
    def text(self) -> str:
        """What a client renders: every generation.delta text in arrival order."""
        return "".join(str(d["text"]) for d in self.of("generation.delta"))

    def model_of(self, attempt_id: str | None) -> str | None:
        return next((str(a["model"]) for a in self.started if a["attempt_id"] == attempt_id), None)


async def one_turn(client: CheckedClient, record: TurnRecord, request: Json, headers: Mapping[str, str]) -> None:
    """POST /v1/turns as a stream and read it to its end into `record` (validated frame by frame and whole)."""
    sent = {"content-type": "application/json", "accept": "text/event-stream", **headers}
    record.t_send = t0 = time.monotonic()
    async with client.http.stream("POST", "/v1/turns", content=orjson.dumps(request), headers=sent) as response:
        record.status = response.status_code
        if response.status_code != 200:
            await response.aread()
            client.check(response, request)
            record.envelope = response.json()
        else:
            live = LiveTurn(client, response)
            while (event := await live.next()) is not None:
                record.at_ms.append((time.monotonic() - t0) * 1000)
                if event.event == "generation.delta" and record.ttft_ms is None:
                    record.ttft_ms = record.at_ms[-1]
            live.validate()
            record.events = live.events
    record.e2e_ms = (time.monotonic() - t0) * 1000


BodyFn = Callable[[int, str], Json]


async def run_turns(
    stack: Stack,
    n: int,
    make_body: BodyFn,
    *,
    concurrency: int = CONCURRENCY,
    tenants: Sequence[str] = TENANTS,
    deadline_ms: int = DEADLINE_MS,
    pace_s: float = 0.0,
    on_record: Callable[[TurnRecord], Awaitable[None]] | None = None,
) -> list[TurnRecord]:
    """`n` streamed turns, at most `concurrency` at once (and, with `pace_s`, one started every `pace_s`), turn i
    for tenant `tenants[i % len]` on replica `(i // len) % 2`, every one with `Aghoo-Deadline-Ms: deadline_ms`."""
    clients = {(t, r): stack.client(r, tenant=t) for t in tenants for r in (0, 1)}
    headers = {"aghoo-deadline-ms": str(deadline_ms)}
    records = [TurnRecord(i, tenants[i % len(tenants)], (i // len(tenants)) % 2) for i in range(n)]
    limiter = anyio.Semaphore(concurrency)

    async def go(record: TurnRecord) -> None:
        async with limiter:
            client = clients[(record.tenant, record.replica)]
            await one_turn(client, record, make_body(record.index, record.tenant), headers)
            if on_record is not None:
                await on_record(record)

    async with anyio.create_task_group() as tg:
        for record in records:
            tg.start_soon(go, record)
            if pace_s:
                await anyio.sleep(pace_s)
    return records


# --- S1 and the shared assertions (spec-10 §3 S1: asserted inside every fault run) -----------------------------------
def text_problems(records: Sequence[TurnRecord], expected: str = TEXT) -> list[str]:
    """Zero duplicated client-visible text: every delta belongs to the committed attempt, a served turn rendered
    `expected` exactly once, any other turn a prefix of it (nothing repeated, nothing from a failed attempt)."""
    problems: list[str] = []
    for r in records:
        if r.status != 200:
            continue
        foreign = {str(d["attempt_id"]) for d in r.of("generation.delta")} - {r.committed or ""}
        if foreign:
            problems.append(f"turn {r.index}: deltas of uncommitted attempts {sorted(foreign)}")
        if r.completed and r.text != expected:
            problems.append(f"turn {r.index}: rendered {r.text!r}")
        elif not r.completed and not expected.startswith(r.text):
            problems.append(f"turn {r.index}: partial text {r.text!r} is not a prefix of the answer")
    return problems


def protocol_problems(records: Sequence[TurnRecord], *, allow_500: bool = False) -> list[str]:
    """P-SSE-1 on every stream, and never a 500 or an `internal_error` (06 §7 F8 "no 500s from Noema"); with
    `allow_500` (F11: Postgres down) an ingress 500 must still be the retryable `internal_error` envelope."""
    problems: list[str] = []
    for r in records:
        error = (r.envelope or {}).get("error", {})
        expected = allow_500 and error.get("code") == "internal_error" and error.get("retryable") is True
        if r.status >= 500 and r.status != 503 and not (r.status == 500 and expected):
            problems.append(f"turn {r.index}: HTTP {r.status} {r.envelope}")
        if (r.error or {}).get("code") == "internal_error":
            problems.append(f"turn {r.index}: error{{internal_error}}")
        if r.status == 200:
            found = sse_invariants([data(e) for e in r.events], reached_t_d0=bool(r.of("decision.completed")))
            problems.extend(f"turn {r.index}: {p}" for p in found)
    return problems


def first_failed(r: TurnRecord) -> bool:
    """The turn's first attempt failed before the commit (cancelled `failed` or rerouted away from)."""
    if not r.started:
        return False
    a1 = r.started[0]["attempt_id"]
    return any(c["attempt_id"] == a1 and c["reason"] == "failed" for c in r.of("generation.cancelled"))


def fallback_granted(r: TurnRecord) -> bool:
    """The first attempt failed pre-commit and a fallback attempt started (the retry budget granted it)."""
    return first_failed(r) and len(r.started) >= 2


def fallback_numbers(records: Sequence[TurnRecord]) -> Json:
    """S1 fallback success, plus the share the retry budget granted and how those granted fallbacks ended."""
    failed = [r for r in records if first_failed(r)]
    granted = [r for r in failed if fallback_granted(r)]
    return {
        "first_attempt_failed": len(failed),
        "served": sum(r.completed for r in failed),
        "fallback_started": len(granted),
        "fallback_started_served": sum(r.completed for r in granted),
        "not_started_outcomes": counts(outcome(r) for r in failed if not fallback_granted(r)),
    }


def fallback_success(records: Sequence[TurnRecord]) -> tuple[int, int]:
    """S1: (served, n) over the turns whose first attempt failed pre-commit."""
    failed = [r for r in records if first_failed(r)]
    return sum(r.completed for r in failed), len(failed)


def assert_s1(records: Sequence[TurnRecord], *, expected: str = TEXT, allow_500: bool = False) -> None:
    problems = text_problems(records, expected) + protocol_problems(records, allow_500=allow_500)
    assert not problems, problems[:20]


# --- statistics (nearest rank; the fault suite gates on counts and bounds, not on CIs) -----------------------------
def pct(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    assert ordered, "no samples"
    rank = max(1, -(-len(ordered) * q // 100))
    return float(ordered[int(rank) - 1])


def summary(values: Sequence[float]) -> Json:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "p50": round(pct(values, 50), 1),
        "p95": round(pct(values, 95), 1),
        "max": round(max(values), 1),
    }


def counts(values: Iterable[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[str(value)] = out.get(str(value), 0) + 1
    return dict(sorted(out.items()))


def outcome(r: TurnRecord) -> str:
    """`completed`, `error:<code>`, `http:<status>:<code>` or `other`: one label per turn."""
    if r.status != 200:
        return f"http:{r.status}:{(r.envelope or {}).get('error', {}).get('code')}"
    if r.completed:
        return "completed"
    return f"error:{r.error['code']}" if r.error else "other"


# --- the results file (the report's numbers; written by the make target's session) -----------------------------
RESULTS: Final[dict[str, Json]] = {}


def record_result(name: str, **numbers: Any) -> None:
    """Keep one fault row's measured numbers (written as JSON at session end when NOEMA_FAULT_RESULTS is set)."""
    RESULTS[name] = numbers
    print(f"\n[fault] {name}: {orjson.dumps(numbers).decode()}")


# --- the retry budget between rows ----------------------------------------------------------------------------
BUDGET_WINDOW_S: Final = 61.0  # RetryBudgetPolicy.window_s (60 s) + 1: also outlasts the breakers' 30 s window
BREAKER_WINDOW_S: Final = 31.0  # BreakerPolicy.window_s (30 s) + 1
_budget_used_at: list[float] = []


def mark_budget_used() -> None:
    """This row spent fallbacks (or recorded failures): the next row that needs a fresh budget waits it out."""
    _budget_used_at[:] = [time.monotonic()]


async def fresh_retry_budget() -> None:
    """Wait until no earlier row's fallbacks are in the retry-budget windows (60 s per (account, model), summed
    over the pods) nor its failures in the breaker windows (30 s), so every row starts from the same state."""
    if _budget_used_at:
        await anyio.sleep(max(0.0, _budget_used_at[0] + BUDGET_WINDOW_S - time.monotonic()))


def member_votes(r: TurnRecord, model: str | None = None) -> list[Json]:
    """Every member vote of the turn's first decision snapshot (turn decisions always include members)."""
    found = r.of("decision.completed")
    if not found:
        return []
    votes = [m for a in found[0]["decision"]["answers"] for m in (a.get("members") or [])]
    return [m for m in votes if model is None or m["model"] == model]


def tagged(calls: Sequence[Json], marker: str) -> list[Json]:
    """The mock's request records whose body carries `marker` (a turn text tag)."""
    return [c for c in calls if marker in orjson.dumps(c.get("body")).decode()]
