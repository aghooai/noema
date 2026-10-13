"""The turn routes through the real ingress middleware and the response validator: `stream: false`, getTurn,
cancelTurn, resumeTurnEvents (Phase-1 501), and T-ING-1 (spec-04 §1, §13): each ingress step's error appears only
when every earlier step passed, and every pre-call exit completes the idempotency record with None."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import orjson
import pytest
from asgi_lifespan import LifespanManager
from tests.golden.sse_scenarios import document, router_rules, stream

from noema.core.errors import ErrorCode, NoemaError

from .harness import STICKY_SOL, TOKEN, USER_BODY, TurnHarness, app_for, make_harness, principal, running

pytestmark = pytest.mark.unit

SOL = "us.openai.gpt-6.1-sol"


def _h(**stream_kw: Any) -> TurnHarness:
    kw: dict[str, Any] = {"first_event_ms": 100, "first_text_ms": 720, "chunks": ["Paris ", "is ", "big."]}
    kw.update(stream_kw)
    h = make_harness(scenario=document("r", [stream(SOL, **kw)], router_rules()))
    h.seed_session("ses_1", last_seq=3, sticky_spec=STICKY_SOL)
    return h


@asynccontextmanager
async def client(h: TurnHarness, **app_kw: Any) -> AsyncIterator[httpx.AsyncClient]:
    app = app_for(h, **app_kw)
    async with running(h), LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            yield c
    assert app.state.contract_violations == []


def _hdr(key: str | None = None, **extra: str) -> dict[str, str]:
    out = {"authorization": f"Bearer {TOKEN}", "content-type": "application/json", **extra}
    if key is not None:
        out["Idempotency-Key"] = key
    return out


def _body(**extra: Any) -> bytes:
    return orjson.dumps({**USER_BODY, "session_id": "ses_1", **extra})


async def test_stream_false_returns_the_turn_object_and_get_turn_reads_it_back() -> None:
    h = _h()
    async with client(h) as c:
        r = await c.post("/v1/turns", content=_body(stream=False), headers=_hdr())
        assert r.status_code == 200, r.text
        assert r.headers["x-ratelimit-limit-requests"]
        turn = r.json()
        assert turn["text"] == "Paris is big." and turn["seq"] == 4 and turn["session_id"] == "ses_1"
        assert turn["routing"]["decision_id"] == turn["decision"]["id"]
        assert [a["outcome"] for a in turn["attempts"]] == ["committed"]
        got = await c.get(f"/v1/turns/{turn['turn_id']}", headers=_hdr())
        assert got.status_code == 200
        stored = got.json()
        assert stored["text"] == turn["text"] and stored["routing"] == turn["routing"]
        assert stored["timings"] == turn["timings"] and stored["attempts"] == turn["attempts"]
        assert (await c.get("/v1/turns/turn_01J00000000000000000000000", headers=_hdr())).status_code == 404
        assert (await c.get("/v1/turns/not-a-turn", headers=_hdr())).status_code == 404
        events = await c.get(f"/v1/turns/{turn['turn_id']}/events", headers=_hdr())
        assert events.status_code == 501 and events.json()["error"]["code"] == "not_implemented"
        cancel = await c.post(f"/v1/turns/{turn['turn_id']}/cancel", headers=_hdr())
        assert cancel.status_code == 202 and cancel.json() == {"turn_id": turn["turn_id"], "status": "completed"}
        missing = await c.post("/v1/turns/turn_01J00000000000000000000000/cancel", headers=_hdr())
        assert missing.status_code == 404


async def test_stream_false_failures_map_to_http_statuses() -> None:
    h = _h(fault={"kind": "stall_after", "n": 1})
    async with client(h) as c:
        r = await c.post("/v1/turns", content=_body(stream=False), headers=_hdr())
    assert r.status_code == 503 and r.json()["error"]["code"] == "all_routes_unavailable"  # upstream_stalled
    h2 = _h(first_text_ms=5000)
    async with client(h2) as c:
        r = await c.post("/v1/turns", content=_body(stream=False), headers=_hdr(**{"Aghoo-Deadline-Ms": "1500"}))
    assert r.status_code == 504 and r.json()["error"]["code"] == "deadline_exceeded"


async def _records(h: TurnHarness) -> list[str]:
    return [rec.state for rec in h.world.idempotency._records.values()]


async def test_ing_1_each_step_fails_only_after_the_earlier_ones_passed() -> None:
    h = _h()
    async with client(h) as c:
        # step 6 (session lookup) before step 8 (strategy)
        r = await c.post("/v1/turns", content=_body(session_id="ses_nope", strategy="hedge"), headers=_hdr("k6"))
        assert (r.status_code, r.json()["error"]["code"]) == (404, "session_not_found")
        # step 8 before step 9 (credentials) and step 10 (session lock)
        h.world.broker.fail("t_acme", "access_denied")
        r = await c.post("/v1/turns", content=_body(strategy="speculative"), headers=_hdr("k8"))
        assert (r.status_code, r.json()["error"]["param"]) == (422, "strategy")
        r = await c.post("/v1/turns", content=_body(decisions={"profile": "nope"}), headers=_hdr("k8b"))
        assert (r.status_code, r.json()["error"]["code"]) == (403, "profile_not_allowed")
        # step 9 before step 10
        lease = await _hold_session(h)
        r = await c.post("/v1/turns", content=_body(), headers=_hdr("k9"))
        assert (r.status_code, r.json()["error"]["code"]) == (503, "tenant_access_misconfigured")
        h.world.broker.fail("t_acme", None)
        # step 10 before step 11 (admission): busy after the queue timeout, nothing reserved
        r = await c.post("/v1/turns", content=_body(), headers=_hdr("k10"))
        assert (r.status_code, r.json()["error"]["code"]) == (409, "session_busy")
        await _release(h, lease)
        # step 11: admission 429 releases the lease (no seq consumed)
        original = h.world.admission.reserve

        async def deny(*a: Any, **k: Any) -> Any:
            raise NoemaError(ErrorCode.RPM_EXCEEDED, "Rate limit exceeded.", retry_after_s=3)

        h.world.admission.reserve = deny  # type: ignore[method-assign]
        r = await c.post("/v1/turns", content=_body(), headers=_hdr("k11"))
        assert (r.status_code, r.headers["retry-after"]) == (429, "3")
        h.world.admission.reserve = original  # type: ignore[method-assign]
        session = h.world.db.store.rows("session")[0]
        assert session["active_turn_id"] is None and session["last_seq"] == 3
        # every pre-call exit was completed with None (re-executable)
        assert set(await _records(h)) == {"not_stored"}
        # and a retry with the same key now runs
        r = await c.post("/v1/turns", content=_body(), headers=_hdr("k11"))
        assert r.status_code == 200 and "completed" in await _records(h)
        # step 7 before step 8: the stored key reused with another body
        r = await c.post("/v1/turns", content=_body(strategy="hedge"), headers=_hdr("k11"))
        assert (r.status_code, r.json()["error"]["code"]) == (409, "idempotency_key_reused")


async def test_a_failure_while_starting_the_turn_frees_the_session_for_the_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # step 12 failing after the runner was registered (here: appending turn.created) gives back the lease, the
    # reservation and the owner entry, so the client's retry runs instead of waiting out the lease (409 session_busy)
    from noema.turns.runner import TurnRunner

    original = TurnRunner.emit_created
    failures = [RuntimeError("the event log refused turn.created")]

    def once(self: TurnRunner) -> None:
        if failures:
            raise failures.pop()
        original(self)

    monkeypatch.setattr(TurnRunner, "emit_created", once)
    h = _h()
    async with client(h) as c:
        r = await c.post("/v1/turns", content=_body(stream=False), headers=_hdr("k-start"))
        assert (r.status_code, r.json()["error"]["code"]) == (500, "internal_error")
        session = h.world.db.store.rows("session")[0]
        assert session["active_turn_id"] is None and session["last_seq"] == 3
        assert h.world.admission.live_streams("t_acme", "p1") == 0
        r = await c.post("/v1/turns", content=_body(stream=False), headers=_hdr("k-start"))
        assert r.status_code == 200 and r.json()["seq"] == 4


async def _hold_session(h: TurnHarness) -> Any:
    from noema.turns import session_order

    return await session_order.acquire(
        h.world.db,
        h.coordinator,
        h.clock,
        principal(),
        session_id="ses_1",
        turn_id="turn_holder",
        expected_seq=None,
        queue_timeout_ms=2000,
    )


async def _release(h: TurnHarness, lease: Any) -> None:
    from noema.turns import session_order

    await session_order.release(h.world.db, h.coordinator, principal(), lease)


async def test_role_and_scope_failures_are_403_before_any_lookup() -> None:
    from noema.core.principal import Principal

    h = _h()
    auditor = Principal("t_acme", "p1", "prn_aud", "key_2", frozenset({"auditor"}), frozenset({"turns"}), "r")
    async with client(h, who=auditor) as c:
        r = await c.post("/v1/turns", content=_body(session_id="ses_nope"), headers=_hdr())
        assert (r.status_code, r.json()["error"]["code"]) == (403, "permission_denied")
        for headers in (_hdr(), _hdr("k-403")):  # the folded ingress (no key) and the keyed one
            r = await c.post("/v1/turns", content=_body(), headers=headers)
            assert (r.status_code, r.json()["error"]["code"]) == (403, "permission_denied")
    assert sum(h.world.broker.gets.values()) == 0  # no credential fetch (STS) for a role failure (Wave E fix)


def _count_units(h: TurnHarness, monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], list[int]]:
    """Record every unit of work the world's database opens, and how many were open when step 12 began."""
    opened: list[str] = []
    at_start: list[int] = []
    uow = h.world.db.uow

    def counting_uow(p: Any) -> Any:
        opened.append("uow")
        return uow(p)

    monkeypatch.setattr(h.world.db, "uow", counting_uow)
    start = h.service.start

    async def counting_start(prepared: Any, **kw: Any) -> Any:
        at_start.append(len(opened))
        return await start(prepared, **kw)

    monkeypatch.setattr(h.service, "start", counting_start)
    return opened, at_start


async def test_a_turn_without_a_key_runs_steps_6_to_10_in_one_unit_of_work(monkeypatch: pytest.MonkeyPatch) -> None:
    # E sprint A: the session read, the lease and the history read share one transaction before the turn starts
    h = _h()
    _, at_start = _count_units(h, monkeypatch)
    async with client(h) as c:
        r = await c.post("/v1/turns", content=_body(stream=False), headers=_hdr())
        assert r.status_code == 200 and r.json()["seq"] == 4, r.text
        r = await c.post("/v1/turns", content=_body(stream=False), headers=_hdr("k-unfolded"))
        assert r.status_code == 200 and r.json()["seq"] == 5, r.text
    folded, unfolded = at_start[0], at_start[1] - at_start[0]
    assert folded == 1
    assert unfolded > folded  # the keyed request runs step 6 in its own unit of work


async def test_ing_1_holds_in_the_folded_unit_of_work() -> None:
    # T-ING-1's order for requests without an Idempotency-Key (steps 6-10 in one unit of work)
    h = _h()
    async with client(h) as c:
        r = await c.post("/v1/turns", content=_body(session_id="ses_nope", strategy="hedge"), headers=_hdr())
        assert (r.status_code, r.json()["error"]["code"]) == (404, "session_not_found")
        h.world.broker.fail("t_acme", "access_denied")
        r = await c.post("/v1/turns", content=_body(strategy="speculative"), headers=_hdr())
        assert (r.status_code, r.json()["error"]["param"]) == (422, "strategy")
        lease = await _hold_session(h)
        r = await c.post("/v1/turns", content=_body(), headers=_hdr())
        assert (r.status_code, r.json()["error"]["code"]) == (503, "tenant_access_misconfigured")
        h.world.broker.fail("t_acme", None)
        r = await c.post("/v1/turns", content=_body(expected_seq=2), headers=_hdr())
        assert (r.status_code, r.json()["error"]["code"]) == (409, "session_conflict")
        r = await c.post("/v1/turns", content=_body(), headers=_hdr())
        assert (r.status_code, r.json()["error"]["code"]) == (409, "session_busy")
        await _release(h, lease)
        original = h.world.admission.reserve

        async def deny(*a: Any, **k: Any) -> Any:
            raise NoemaError(ErrorCode.RPM_EXCEEDED, "Rate limit exceeded.", retry_after_s=3)

        h.world.admission.reserve = deny  # type: ignore[method-assign]
        r = await c.post("/v1/turns", content=_body(), headers=_hdr())
        assert (r.status_code, r.headers["retry-after"]) == (429, "3")
        h.world.admission.reserve = original  # type: ignore[method-assign]
        session = h.world.db.store.rows("session")[0]
        assert session["active_turn_id"] is None and session["last_seq"] == 3  # the 429 released the lease
        r = await c.post("/v1/turns", content=_body(stream=False), headers=_hdr())
        assert r.status_code == 200 and r.json()["seq"] == 4
