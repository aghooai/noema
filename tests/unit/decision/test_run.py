"""The streaming run (spec-06 §10, §17 T-RUN-1/2; IMPL-002, IMPL-070): snapshots, cancel, shielded finalize,
rows, usage lines and the request-level failures of `run().__aenter__`."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any, cast

import anyio
import pytest
from tests.contract_internal.test_decision_engine import d_std
from tests.unit.decision.checks import assert_valid, wire_request
from tests.unit.decision.helpers import DEPARTMENT, FixedCost
from tests.unit.decision.world import DEEPSEEK, HAIKU, LUNA, EngineWorld, answer, engine_world, fault, res, tel

from noema.core.clock import Deadline
from noema.core.errors import ErrorCode, NoemaError
from noema.core.principal import TenantRef
from noema.core.protocols.adapters import Adapter, AdapterRegistry, ModelRequest, ModelResponse
from noema.core.protocols.decision import DecisionRequest, ProfileRef, VerificationInput
from noema.decision.coordinator import Coordinator
from noema.decision.engine import DecisionEngineImpl
from noema.decision.run import line_id


def turn_request(ew: EngineWorld) -> DecisionRequest:
    router = ew.snapshot.decision_profile("router-default")
    qs = ew.compiler.compile(router, [], origin="turn", output_format="native", platform_from=router)
    return DecisionRequest(
        ProfileRef("router-default", 1),
        qs,
        "What is the capital of France?",
        1000,
        2,
        "profile_default",
        frozenset({"members"}),
        "turn",
        "native",
    )


async def test_t_run_1_s1_then_one_final_update() -> None:
    ew = engine_world()
    ew.world.use_scenario(d_std())
    req = turn_request(ew)
    t0 = ew.clock.monotonic()
    async with ew.clock.autojump(), ew.engine.run(ew.context(turn_id="turn_1", escalate=False), req) as run:
        s1 = await run.commit_critical()
        s1_ms = round((ew.clock.monotonic() - t0) * 1000)
        updates = [u async for u in run.updates()]
        final = await run.finalize()
    assert (s1_ms, s1.status, s1.final) == (930, "quorum", False)
    assert s1.final_names == {"intent", "complexity", "needs_fresh_info", "injection_suspected"}
    sensitive: Any = next(a for a in res(s1)["answers"] if a["name"] == "sensitive_data")
    assert [m["status"] for m in sensitive["members"]] == ["ok", "ok", "late"]
    assert sensitive["members"][2]["latency_ms"] == 930 and sensitive["quorum"]["responded"] == 2
    assert res(s1)["provenance"]["completed_at"].endswith("00.930Z")
    assert len(updates) == 1 and updates[0] is final and final.status == "completed"
    assert final.final_names - s1.final_names == {"sensitive_data"}
    m3 = next(a for a in res(final)["answers"] if a["name"] == "sensitive_data")["members"][2]
    assert (m3["status"], m3["latency_ms"]) == ("timeout", 1000)
    wire = {"questions": [dict(q.wire) for q in req.questions.questions], "input": "x"}
    assert_valid(s1.result, wire)
    assert_valid(final.result, wire)
    assert final.row is not None and final.row["turn_id"] == "turn_1" and final.row["status"] == "completed"
    assert tel(ew).counter("aghoo_decision_seconds") > 0


async def test_t_run_2_shielded_finalize_survives_an_outer_cancel() -> None:
    ew = engine_world()
    ew.world.use_scenario(d_std())
    req = turn_request(ew)
    ctx = ew.context(escalate=False)  # /v1/decisions-style: the engine inserts the row itself
    s1_seen = anyio.Event()

    async def body() -> None:
        async with ew.engine.run(ctx, req) as run:
            await run.commit_critical()
            s1_seen.set()
            await anyio.sleep_forever()

    async with ew.clock.autojump(), anyio.create_task_group() as tg:
        tg.start_soon(body)
        await s1_seen.wait()
        tg.cancel_scope.cancel()
    (row,) = ew.world.db.store.rows("decision")
    assert row["status"] in ("completed", "partial")
    lines = ew.world.cost.lines
    assert {ln.line for ln in lines} == {"decision", "waste"}
    waste = [ln for ln in lines if ln.line == "waste"]
    assert all(ln.usage.tokens_estimated and ln.cost.estimated for ln in waste)
    assert {ln.model_id for ln in waste} >= {HAIKU}
    assert len({ln.id for ln in lines}) == len(lines)
    assert all(ln.id == line_id(f"{row['id']}:{ln.line}:{ln.model_id}") for ln in lines)


class CountingReservation:
    def __init__(self) -> None:
        self.consumed: list[tuple[str, str]] = []

    def consume(self, kind: str, model: str) -> None:
        self.consumed.append((kind, model))

    async def reconcile(self, usage: Any) -> None:
        return None

    def headers(self) -> dict[str, str]:
        return {}

    async def release(self) -> None:
        return None


DEPT = {"billing": 0.7, "technical": 0.1, "shipping": 0.1, "other": 0.1}


async def _decide(ew: EngineWorld, req: DecisionRequest, **ctx_kw: Any) -> Any:
    async with ew.clock.autojump():
        return await ew.engine.decide(ew.context(**ctx_kw), req)


async def test_backup_wins_when_the_primary_stalls() -> None:
    ew = engine_world()
    ew.use(
        [
            {"match": {"model": DEEPSEEK, "nth": 1}, "answer": {"dist": DEPT}, "latency_ms": 950},
            {"match": {"model": DEEPSEEK}, "answer": {"dist": DEPT}, "latency_ms": 100},
            answer(LUNA, None, DEPT, 300),
            answer(HAIKU, None, DEPT, 300),
        ]
    )
    req = ew.request([DEPARTMENT])
    reservation = CountingReservation()
    async with ew.clock.autojump():
        snap = await ew.engine.decide(replace(ew.context(), reservation=reservation), req)
    m1: Any = res(snap)["answers"][0]["members"][0]
    assert (m1["status"], m1["latency_ms"]) == ("ok", 500)  # backup sent at 400 ms, answered at 500 ms
    assert m1["usage"]["tokens_estimated"] is True  # the cancelled primary is estimated
    assert reservation.consumed.count(("decision_member", "deepseek-3.2")) == 2
    assert {ln.line for ln in ew.world.cost.lines if ln.model_id == DEEPSEEK} == {"decision", "waste"}


class _ServedIn:
    """An adapter whose calls are served in another region (cross-region inference)."""

    def __init__(self, inner: Adapter, region: str) -> None:
        self._inner, self._region = inner, region

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def complete(self, req: ModelRequest, *, tenant: TenantRef, deadline: Deadline) -> ModelResponse:
        return replace(await self._inner.complete(req, tenant=tenant, deadline=deadline), region=self._region)


class _CrossRegion:
    def __init__(self, inner: AdapterRegistry) -> None:
        self._inner = inner

    def for_model(self, alias: str) -> Adapter:
        return cast(Adapter, _ServedIn(self._inner.for_model(alias), "us-west-2"))


async def test_calls_are_priced_in_the_home_region_with_the_pinned_snapshot() -> None:
    """ops-ledger requests 4 and 6 (spec-11 §2, IMPL-019): completed and cancelled calls are priced with
    `ctx.tenant.home_region` and `ctx.snapshot`; the member and the usage line keep the serving region."""
    cost = FixedCost()
    ew = engine_world(cost=cost)
    ew.engine = DecisionEngineImpl(replace(ew.engine.deps, adapters=_CrossRegion(ew.world.adapters)))
    ew.use(
        [
            {"match": {"model": DEEPSEEK, "nth": 1}, "answer": {"dist": DEPT}, "latency_ms": 950},
            answer(None, None, DEPT, 100),
        ]
    )
    ctx = ew.context()
    async with ew.clock.autojump():
        snap = await ew.engine.decide(ctx, ew.request([DEPARTMENT]))
    assert ctx.tenant.home_region == "us-east-1" and len(cost.priced) == 4  # 3 completed + the cancelled primary
    assert all(region == "us-east-1" and pinned is ctx.snapshot for _, region, pinned in cost.priced)
    assert {m["region"] for m in res(snap)["answers"][0]["members"]} == {"us-west-2"}
    assert {(ln.line, ln.region) for ln in cost.lines} == {("decision", "us-west-2"), ("waste", "us-east-1")}


async def test_no_backup_when_it_does_not_fit_or_the_primary_fails_first() -> None:
    ew = engine_world()
    ew.use([answer(None, None, DEPT, 600)])
    async with ew.clock.autojump():
        await ew.engine.decide(ew.context(), ew.request([DEPARTMENT], deadline_ms=900))
    assert [c.model_id for c in ew.world.adapters.calls()].count(DEEPSEEK) == 1  # 400 + 590 > 900
    ew2 = engine_world()
    ew2.use([fault(DEEPSEEK, "http_5xx", 100), answer(None, None, DEPT, 300)])
    snap = await _decide(ew2, ew2.request([DEPARTMENT]))
    m1: Any = res(snap)["answers"][0]["members"][0]
    assert (m1["status"], m1["error"]["code"]) == ("error", "upstream_error")
    assert [c.model_id for c in ew2.world.adapters.calls()].count(DEEPSEEK) == 1  # failed before 400 ms: final


@pytest.mark.parametrize(
    ("kind", "extra", "status", "code"),
    [
        ("throttling", {}, "error", "throttled"),
        ("http_5xx", {}, "error", "upstream_error"),
        ("validation", {}, "error", "invalid_request"),
        ("access_denied", {}, "error", "tenant_access_misconfigured"),
        ("refusal", {}, "refused", None),
        ("invalid_verbalized", {"variant": "missing_letter"}, "error", "missing_options"),
        ("invalid_verbalized", {"variant": "sum_out_of_range"}, "error", "sum_out_of_range"),
        ("invalid_verbalized", {"variant": "not_json"}, "error", "parse_error"),
        ("reasoning_only", {}, "error", "reasoning_only"),
        ("empty_text", {}, "error", "empty_output"),
        ("malformed", {}, "error", "upstream_error"),
    ],
)
async def test_member_statuses_and_error_codes(kind: str, extra: dict[str, Any], status: str, code: str | None) -> None:
    ew = engine_world()
    bad = fault(HAIKU, kind, 50, **extra)
    bad["answer"] = {"dist": DEPT}  # the rendered (then corrupted) answer of the fault
    ew.use([bad, answer(None, None, DEPT, 300)])
    req = ew.request([DEPARTMENT])
    snap = await _decide(ew, req)
    m3: Any = res(snap)["answers"][0]["members"][2]
    assert m3["status"] == status
    assert (m3.get("error") or {}).get("code") == code
    assert "error" not in m3 or len(m3["error"]["message"]) < 120
    assert_valid(snap.result, wire_request(req))


async def test_circuit_open_and_model_not_allowed_never_call() -> None:
    ew = engine_world()
    ew.world.health.force(HAIKU, "us-east-1", "open")
    ew.use([answer(None, None, DEPT, 300)])
    snap = await _decide(ew, ew.request([DEPARTMENT]))
    m3: Any = res(snap)["answers"][0]["members"][2]
    assert (m3["status"], m3["error"]["code"], m3["latency_ms"]) == ("error", "circuit_open", 0)
    assert HAIKU not in {c.model_id for c in ew.world.adapters.calls()}
    narrow = engine_world(patch={"tenants": {"t_acme": {"allowed_models": ["deepseek-3.2", "luna-6", "sol-6.1"]}}})
    narrow.use([answer(None, None, DEPT, 300)])
    snap2 = await _decide(narrow, narrow.request([DEPARTMENT]))
    assert res(snap2)["answers"][0]["members"][2]["error"]["code"] == "model_not_allowed"


async def test_health_is_recorded_per_call_by_bedrock_id() -> None:
    ew = engine_world()
    ew.use([fault(HAIKU, "throttling", 50), answer(None, None, DEPT, 300)])
    await _decide(ew, ew.request([DEPARTMENT]))
    outcomes = {(r[0], r[3]) for r in ew.world.health.records}
    assert {(DEEPSEEK, "ok"), (LUNA, "ok"), (HAIKU, "throttled")} <= outcomes
    assert all(r[2] == "111122223333" for r in ew.world.health.records)


@pytest.mark.parametrize("kind", ["access_denied", "validation"])
async def test_tenant_access_and_invalid_request_are_not_breaker_signals(kind: str) -> None:
    # adapters request 7: one tenant's broken grant or bad request must not open the shared per-model breaker
    ew = engine_world()
    ew.use([fault(HAIKU, kind, 50), answer(None, None, DEPT, 300)])
    snap = await _decide(ew, ew.request([DEPARTMENT]))
    m3: Any = res(snap)["answers"][0]["members"][2]
    assert m3["status"] == "error"
    assert HAIKU not in {r[0] for r in ew.world.health.records}
    assert {(DEEPSEEK, "ok"), (LUNA, "ok")} <= {(r[0], r[3]) for r in ew.world.health.records}


async def test_a_member_cut_by_the_deadline_is_not_a_breaker_signal() -> None:
    # the caller's deadline is not a provider timeout: a model just slower than the 1,000 ms router deadline (or a
    # lagging event loop) must not open the shared per-(model, region) breaker on every pod
    ew = engine_world()
    ew.use([answer(HAIKU, None, DEPT, 5000), answer(None, None, DEPT, 300)])
    snap = await _decide(ew, ew.request([DEPARTMENT]))
    m3: Any = res(snap)["answers"][0]["members"][2]
    assert m3["status"] == "timeout"
    assert HAIKU not in {r[0] for r in ew.world.health.records}
    assert {(DEEPSEEK, "ok"), (LUNA, "ok")} <= {(r[0], r[3]) for r in ew.world.health.records}


async def test_credentials_fail_fast_before_any_call() -> None:
    ew = engine_world()
    ew.use([answer(None, None, DEPT, 300)])
    ew.world.broker.fail("t_acme", "access_denied")
    with pytest.raises(NoemaError) as err:
        await _decide(ew, ew.request([DEPARTMENT]))
    assert err.value.code is ErrorCode.TENANT_ACCESS_MISCONFIGURED and ew.world.adapters.calls() == []


async def test_dispatch_check_and_request_checks() -> None:
    ew = engine_world()
    ew.use([answer(None, None, DEPT, 300)])
    with pytest.raises(NoemaError) as err:
        await _decide(ew, ew.request([DEPARTMENT]), budget_s=0.04)
    assert err.value.code is ErrorCode.DEADLINE_EXCEEDED and ew.world.db.store.rows("decision") == []

    bad = replace(ew.request([DEPARTMENT]), min_quorum=4)
    with pytest.raises(NoemaError) as err:
        await _decide(ew, bad)
    assert err.value.code is ErrorCode.UNSUPPORTED_PARAMETER and err.value.details == {"max": 3}
    stale = replace(ew.request([DEPARTMENT]), profile=ProfileRef("router-default", 2))
    with pytest.raises(NoemaError) as err:
        await _decide(ew, stale)
    assert err.value.param == "profile" and err.value.details == {"requested": 2, "active": 1}


async def test_native_row_contents_retention_and_labeling_offer() -> None:
    ew = engine_world(patch={"labeling": {"sample": {"uniform_rate": 1.0}}})
    ew.use([answer(None, None, DEPT, 300)])
    req = ew.request([DEPARTMENT], value="Mail jane.doe@example.com today")
    snap = await _decide(ew, req, data_class="confidential", content_policy="redacted")
    (row,) = ew.world.db.store.rows("decision")
    assert row["id"] == snap.decision_id and row["principal_id"] == "prn_dev" and row["origin"] == "native"
    assert row["questions"] == [DEPARTMENT] and row["result"]["answers"][0]["members"]
    assert "jane.doe@example.com" not in str(row["input"]) and "[REDACTED:pii_email]" in str(row["input"])
    assert row["expires_at"] - row["created_at"] == timedelta(days=30)  # confidential retention
    assert row["cache_hit"] is False and row["status"] == "completed"
    assert [o.id for o in ew.world.sampler.offers] == [snap.decision_id]


async def test_metadata_only_stores_no_input_and_is_never_sampled() -> None:
    ew = engine_world(patch={"labeling": {"sample": {"uniform_rate": 1.0}}})
    ew.use([answer(None, None, DEPT, 300)])
    await _decide(ew, ew.request([DEPARTMENT]), data_class="restricted", content_policy="metadata_only")
    (row,) = ew.world.db.store.rows("decision")
    assert row["input"] is None and ew.world.sampler.offers == []


async def test_turn_runs_return_the_row_and_insert_nothing() -> None:
    ew = engine_world()
    ew.use([answer(None, None, DEPT, 300)])
    async with ew.clock.autojump(), ew.engine.run(ew.context(turn_id="turn_9"), ew.request([DEPARTMENT])) as run:
        final = await run.finalize()
        assert run.row_for("metadata_only") is not None
    assert final.row is not None and final.row["turn_id"] == "turn_9"
    assert ew.world.db.store.rows("decision") == [] and ew.world.sampler.offers == []
    assert {ln.turn_id for ln in ew.world.cost.lines} == {"turn_9"}


async def test_verification_calls_go_on_the_verification_line() -> None:
    ew = engine_world()
    ew.use([answer(None, None, {"true": 0.9, "false": 0.1}, 300)])
    verifier = ew.snapshot.decision_profile("answer-verifier")
    qs = ew.compiler.compile(
        verifier, [], origin="verification", output_format="native", select=["answers_question", "unsupported_claims"]
    )
    req = DecisionRequest(
        ProfileRef("answer-verifier", 1),
        qs,
        VerificationInput("Capital?", (("A", "Paris"),)),
        4000,
        2,
        "off",
        frozenset({"members"}),
        "verification",
        "native",
    )
    snap = await _decide(ew, req, purpose="verification", escalate=False, turn_id="turn_1")
    assert {a.type for a in snap.answers.values()} == {"predicate"}
    assert {ln.line for ln in ew.world.cost.lines} == {"verification"}
    calls = ew.world.adapters.calls()
    assert {c.request.call_kind for c in calls} == {"verification_member"}
    assert all("<question>Capital?</question>" in c.request.messages[0].content[2].text for c in calls)  # type: ignore[union-attr]


async def test_all_members_down_is_a_final_partial_result() -> None:
    ew = engine_world()
    ew.use([fault(None, "http_5xx", 50)])
    req = ew.request([DEPARTMENT])
    snap = await _decide(ew, req)
    a: Any = res(snap)["answers"][0]
    assert (snap.status, a["type"], a["reason"]) == ("partial", "unavailable", "all_members_failed")
    assert_valid(snap.result, wire_request(req))


async def test_a_coordinator_bug_still_records_the_usage_of_dispatched_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review finding 1: a coordinator crash is 500 `internal_error` (no row), but the Bedrock calls it dispatched
    keep their usage lines: completed calls on `decision`, the in-flight ones estimated on `waste`."""
    ew = engine_world()
    ew.use([answer(DEEPSEEK, None, DEPT, 100), answer(LUNA, None, DEPT, 500), answer(HAIKU, None, DEPT, 500)])

    def boom(self: Coordinator) -> None:
        raise RuntimeError("coordinator bug")

    monkeypatch.setattr(Coordinator, "_observe_quorum", boom)  # crashes on the first ok member (DeepSeek)
    with pytest.raises(NoemaError) as err:
        await _decide(ew, ew.request([DEPARTMENT]))
    assert err.value.code is ErrorCode.INTERNAL_ERROR
    lines = sorted((ln.line, ln.model_id, ln.usage.tokens_estimated) for ln in ew.world.cost.lines)
    assert lines == [("decision", DEEPSEEK, False), ("waste", HAIKU, True), ("waste", LUNA, True)]
    assert ew.world.db.store.rows("decision") == []


class StalledDb:
    """A Database whose uow hangs for `stall_s` (pool exhausted, lock wait) before failing."""

    def __init__(self, clock: Any, stall_s: float) -> None:
        self.clock, self.stall_s = clock, stall_s

    def uow(self, principal: Any) -> Any:
        return self

    async def __aenter__(self) -> Any:
        await self.clock.sleep(self.stall_s)
        raise RuntimeError("the database did not answer")

    async def __aexit__(self, *exc: object) -> None:
        return None


async def test_a_stalled_row_insert_goes_to_the_outbox_within_the_budget() -> None:
    """Review findings 3 and 7: an insert still hanging when the 2 s finalize budget ends is abandoned and the row is
    queued in the outbox (IMPL-022); the usage lines stand; no labeling offer is made for a row that does not exist."""
    ew = engine_world(patch={"labeling": {"sample": {"uniform_rate": 1.0}}})
    ew.use([answer(None, None, DEPT, 100)])
    stalled: Any = StalledDb(ew.clock, 5.0)
    ew.engine.deps = replace(ew.engine.deps, db=stalled)
    t0 = ew.clock.monotonic()
    snap = await _decide(ew, ew.request([DEPARTMENT]))
    assert ew.clock.monotonic() - t0 == pytest.approx(2.1)  # 100 ms of calls + the 2 s budget, not the 5 s stall
    (queued,) = ew.world.outbox.of_kind("decision_row")
    assert queued.id == snap.decision_id and queued.payload["status"] == "completed"
    assert len(ew.world.cost.lines) == 3 and ew.world.sampler.offers == []


async def test_a_failed_row_insert_is_queued_and_never_offered_to_labeling() -> None:
    """Review finding 7: Postgres down -> the row goes to the outbox, and the labeling offer waits for a stored row
    (spec-06 §16: the labeling job never sees a missing row), so none is made."""
    ew = engine_world(patch={"labeling": {"sample": {"uniform_rate": 1.0}}})
    ew.use([answer(None, None, DEPT, 100)])
    ew.world.db.store.available = False
    snap = await _decide(ew, ew.request([DEPARTMENT]))
    assert [r.id for r in ew.world.outbox.of_kind("decision_row")] == [snap.decision_id]
    assert ew.world.sampler.offers == []


async def test_a_deeply_nested_member_answer_is_that_members_parse_error() -> None:
    """F8 member row: output nested 10,000 levels deep is `error{parse_error}` for that member (counted in
    `aghoo_decision_parse_errors_total`), not an adapter error, and the other members still decide (Wave E fix)."""
    ew = engine_world()
    deep = '{"A": ' + "[" * 10_000 + "]" * 10_000 + ', "B": 0.5}'
    ew.use([{"match": {"model": HAIKU}, "answer": {"raw_text": deep}, "latency_ms": 50}, answer(None, None, DEPT, 300)])
    snap = await _decide(ew, ew.request([DEPARTMENT]))
    m3: Any = res(snap)["answers"][0]["members"][2]
    assert (m3["status"], m3["error"]["code"]) == ("error", "parse_error")
    assert tel(ew).counter("aghoo_decision_parse_errors_total", code="parse_error") == 1
    assert snap.status == "completed"


async def test_unary_member_latency_is_not_a_ttft_breaker_sample() -> None:
    """A decision member call is unary: its whole latency grows with the tenant's input and reasoning, so feeding it
    to the shared breaker's p95-TTFT rule let one tenant open the circuit for every tenant. An `ok` member records
    no TTFT sample (Wave E fix); the outcome still counts for the failure-ratio rule."""
    ew = engine_world()
    ew.use([answer(None, None, DEPT, 300)])
    await _decide(ew, ew.request([DEPARTMENT]))
    oks = [r for r in ew.world.health.records if r[3] == "ok"]
    assert len(oks) == 3 and {r[4] for r in oks} == {None}
