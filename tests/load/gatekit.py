"""Shared parts of the stack gates in tests/load (L2/L3/Q8 holdback, L5 backup tail, R4 replay, Q9 hierarchical
deadline; `make qa-gates`, make/qa-gates.mk): the gate config, scenario rules, statistics and the results file.

The gates run on an e2e compose stack of their own (wire mock, 2 api replicas) and drive it with the fault suite's
validated turn driver (`tests.fault.kit.run_turns`: every createTurn frame and every non-200 goes through the
`ContractValidator`). Mock latencies are fixed per cell (no distributions) except where a gate is defined on the
probe distributions (L5, Q9: spec-10 §2.2).
"""

from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

import anyio
import httpx
import orjson
from tests.e2e.conftest import CheckedClient, Mock, Stack
from tests.e2e.suite import scenarios as sc
from tests.fault import kit
from tests.fault.kit import TurnRecord
from tests.fault.stackctl import FAULT_LIMITS, activate_doc, fault_doc, next_version, wait_rate_limit
from tests.load.lag import LagSampler, read_lag

Json = dict[str, Any]
Rule = dict[str, Any]

ENABLED: Final = os.environ.get("NOEMA_QA_GATES") == "1"
# The api containers' compose-network IPs (make/qa-gates.mk): the timed driver reads them directly on port 8000, as
# a load balancer would, instead of through docker-proxy's userland copy (not part of Noema; it adds a hop and its
# own CPU to every frame). Empty: the published ports of the bootstrap state.
DIRECT: Final = [ip for ip in os.environ.get("NOEMA_QA_GATES_DIRECT", "").split(",") if ip]
DEEPSEEK_ONLY: Final = "deepseek-only"
# spec-10 §2.2 probe distributions ([p5, p95] log-normal; DeepSeek's 2% tail)
DEEPSEEK_PROBE: Final[Mapping[str, Any]] = {"p5": 470, "p95": 590, "tail_p": 0.02, "tail_ms": [4000, 12000]}
LUNA_PROBE: Final[Mapping[str, Any]] = {"p5": 640, "p95": 880}
HAIKU_PROBE: Final[Mapping[str, Any]] = {"p5": 890, "p95": 1800}


def gate_patch(doc: Json) -> None:
    """The fault config's lifted limits plus L5's DeepSeek-only test profile (router-default's DeepSeek member with
    its 400 ms backup), allowed as a request override for every tenant."""
    doc["decision_profiles"][DEEPSEEK_ONLY] = {
        "version": 1,
        "deadline_ms": 30000,
        "min_quorum": 1,
        "aggregation": "linear_pool",
        "members": [
            {
                "model": "deepseek-3.2",
                "elicitation": "token_logprobs",
                "template": "logprob-codes@2",
                "weight": 1,
                "backup_after_ms": 400,
                "latency_p95_ms": 590,
            }
        ],
    }
    for tenant in doc["tenants"].values():
        allowed = tenant["request_overrides"]["decision_profiles"]
        if DEEPSEEK_ONLY not in allowed:
            allowed.append(DEEPSEEK_ONLY)


async def ensure_gate_config(stack: Stack) -> int:
    """A new config version = bootstrap + FAULT_LIMITS + `gate_patch`, active on both replicas; its version."""
    version = await next_version(stack)
    activated = await activate_doc(stack, fault_doc(version, stack.state["config"].get("prices_ref"), gate_patch))
    await wait_rate_limit(stack, FAULT_LIMITS["rpm_per_key"])
    return activated


# --- scenario rules -------------------------------------------------------------------------------------------
def labeler() -> Rule:
    """A sampled labeling.item answers at once (it must not fail against a generation model)."""
    return {"match": {"prompt_contains": kit.LABELER_PROMPT}, "answer": {"raw_text": "A"}, "latency_ms": 50}


def router_at(
    ms: float, *, intent: Mapping[str, float] = sc.FACTUAL, level: Mapping[str, float] = sc.MODERATE
) -> list[Rule]:
    """The labeler, then every router-default member answering every question at `ms` (no Haiku hang)."""
    return [labeler(), *sc.router(intent=intent, level=level, ms=ms, haiku_hangs_on=None)]


def gate_client(stack: Stack, replica: int, tenant: str) -> CheckedClient:
    """`stack.client(replica, tenant=tenant)`, on the container's own address when DIRECT is set."""
    client = stack.client(replica, tenant=tenant)
    if DIRECT:
        client.http.base_url = httpx.URL(f"http://{DIRECT[replica]}:8000")
    return client


class LagProbe:
    """The driver's own event-loop lag while the block runs (the tests/load/lag.py sampler); `p99_ms` after it."""

    def __init__(self) -> None:
        self.path = Path(tempfile.mkdtemp(prefix="noema-gate-")) / "driver.lag"
        self.sampler = LagSampler(self.path)
        self.p99_ms = 0.0

    async def __aenter__(self) -> LagProbe:
        self.sampler.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.sampler.stop()
        lags = [lag / 1e6 for _t, lag in read_lag(self.path)]
        self.p99_ms = round(pct(lags, 99), 2) if lags else 0.0


# --- the timed driver ------------------------------------------------------------------------------------------
DELTA: Final = b"event: generation.delta"
RAMP_S: Final = 2.0
CREATE_TURN: Final = "createTurn"


async def timed_turn(
    client: CheckedClient, record: TurnRecord, request: Json, headers: Mapping[str, str]
) -> tuple[dict[str, str], bytes]:
    """POST /v1/turns as a stream, timed on the raw bytes (no parsing on the timed path): `ttft_ms` = send -> the
    chunk that holds the first `generation.delta` frame; returns the headers and the whole body (validated later)."""
    sent = {"content-type": "application/json", "accept": "text/event-stream", **headers}
    raw = bytearray()
    record.t_send = t0 = time.monotonic()
    async with client.http.stream("POST", "/v1/turns", content=orjson.dumps(request), headers=sent) as response:
        record.status = response.status_code
        received = dict(response.headers)
        async for chunk in response.aiter_raw():
            if record.ttft_ms is None and DELTA in raw[-len(DELTA) :] + chunk:
                record.ttft_ms = (time.monotonic() - t0) * 1000
            raw += chunk
    record.e2e_ms = (time.monotonic() - t0) * 1000
    return received, bytes(raw)


def validated(client: CheckedClient, record: TurnRecord, request: Json, headers: Mapping[str, str], raw: bytes) -> None:
    """The ContractValidator over the whole response (every frame and the frame-id order for a 200 stream, the
    envelope otherwise); fills `record.events` / `record.envelope`."""
    client.validator.check_response(CREATE_TURN, record.status, headers, raw, request)
    client.checked.append((CREATE_TURN, record.status))
    if record.status == 200:
        record.events = client.validator.check_sse_stream(CREATE_TURN, raw)
    else:
        record.envelope = orjson.loads(raw)


async def drive(
    stack: Stack,
    n: int,
    make_body: Callable[[int, str], Json],
    *,
    concurrency: int,
    tenants: Sequence[str] = kit.TENANTS,
    deadline_ms: int = kit.DEADLINE_MS,
    ramp_s: float = RAMP_S,
) -> tuple[list[TurnRecord], float]:
    """`n` streamed turns, at most `concurrency` at once (the first `concurrency` started evenly over `ramp_s`, so
    the streams do not run in lock-step waves), turn i for tenant `tenants[i % len]` on replica `(i // len) % 2`
    (as `kit.run_turns`), timed on the raw stream and validated after the last one ended; returns the records and
    the driver's event-loop lag p99 in ms (spec-10 §5: a window is valid at <= 2 ms)."""
    clients = {(t, r): gate_client(stack, r, t) for t in tenants for r in (0, 1)}
    headers = {"aghoo-deadline-ms": str(deadline_ms)}
    records = [TurnRecord(i, tenants[i % len(tenants)], (i // len(tenants)) % 2) for i in range(n)]
    bodies = [make_body(r.index, r.tenant) for r in records]
    raws: list[tuple[dict[str, str], bytes]] = [({}, b"")] * n
    limiter = anyio.Semaphore(concurrency)

    async def go(record: TurnRecord) -> None:
        async with limiter:
            client = clients[(record.tenant, record.replica)]
            raws[record.index] = await timed_turn(client, record, bodies[record.index], headers)

    lag = LagProbe()
    async with lag, anyio.create_task_group() as tg:
        for record in records:
            tg.start_soon(go, record)
            if record.index < concurrency:
                await anyio.sleep(ramp_s / concurrency)
    for record in records:
        client = clients[(record.tenant, record.replica)]
        validated(client, record, bodies[record.index], *raws[record.index])
    return records, lag.p99_ms


def steady(records: Sequence[TurnRecord], warmup: int) -> list[TurnRecord]:
    """The records after the first `warmup` sent (the ramp-in: those started before every stream was running)."""
    return sorted(records, key=lambda r: r.t_send)[warmup:]


async def fresh_mock(mock: Mock, scenario: Json) -> int:
    """Empty the mock's record ring (a big ring of prompt-sized records stalls its loop in full GCs), load
    `scenario`; returns the last record seq before the cell."""
    response = await mock.http.post("/_mock/reset")
    assert response.status_code == 200, response.text
    await mock.load(scenario)
    return int((await mock.requests(limit=0))["last_seq"])


def mock_lateness(scenario: Json, calls: Sequence[Json]) -> list[float]:
    """ms by which the mock answered later than its rule says: unary `ok` calls (end - receive - latency_ms) and
    streams (first text sent - receive - first_text_ms); a late mock measures itself, not Noema (spec-10 §5)."""
    rules = scenario["rules"]
    out: list[float] = []
    for call in calls:
        rule = rules[call["rule"]] if isinstance(call.get("rule"), int) else None
        if rule is None or call.get("outcome") != "ok":
            continue
        if "stream" in rule and call.get("t_first_text_sent_ns"):
            out.append((call["t_first_text_sent_ns"] - call["t_req_recv_ns"]) / 1e6 - rule["stream"]["first_text_ms"])
        elif "latency_ms" in rule and call.get("t_done_ns"):
            out.append((call["t_done_ns"] - call["t_req_recv_ns"]) / 1e6 - rule["latency_ms"])
    return out


# --- statistics -----------------------------------------------------------------------------------------------
def pct(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile (the fault suite's; gates compare it with a bound)."""
    return kit.pct(values, q)


def dist(values: Sequence[float]) -> Json:
    """n, p50, p95, p99, max (ms, one decimal)."""
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        **{f"p{q}": round(pct(values, q), 1) for q in (50, 95, 99)},
        "max": round(max(values), 1),
    }


MAX_LAG_MS: Final = 2.0  # spec-10 §5 run validity: driver loop lag p99, and the mock's lateness p99


def validity(driver_lag_p99_ms: float, lateness: Sequence[float]) -> Json:
    late = round(pct(lateness, 99), 2) if lateness else 0.0
    return {
        "driver_lag_p99_ms": driver_lag_p99_ms,
        "mock_lateness_p99_ms": late,
        "valid": driver_lag_p99_ms <= MAX_LAG_MS and late <= MAX_LAG_MS,
    }


def assert_within(numbers: Mapping[str, Any], value: float, bound: float) -> None:
    """`value <= bound`; a late driver or mock only makes the measured latencies longer, so a pass holds in any
    window, while a miss in an invalid window (spec-10 §5) is reported as invalid (re-run), not as a gate failure."""
    if value <= bound:
        return
    assert numbers["valid"], f"invalid window (driver lag or mock lateness > {MAX_LAG_MS} ms), re-run: {numbers}"
    raise AssertionError(f"{value} > {bound}: {numbers}")


def share(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


# --- results (written at session end to NOEMA_QA_GATES_RESULTS) -------------------------------------------------
RESULTS: Final[dict[str, Json]] = {}


def record(name: str, **numbers: Any) -> None:
    RESULTS[name] = numbers
    print(f"\n[gate] {name}: {orjson.dumps(numbers).decode()}")
