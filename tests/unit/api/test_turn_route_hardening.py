"""Wave E edge fixes in the createTurn route: a keyed `stream: true` turn is recorded for replay once it started,
so a client that drops before the first model call and retries with the same key attaches to it instead of
running a second turn (fix 2, IMPL-077); without a client `traceparent` the turn gets a per-request trace id, not
the invalid all-zero one (fix 5)."""

from __future__ import annotations

from typing import Any

import anyio
import httpx
import orjson
import pytest
from asgi_lifespan import LifespanManager
from tests.golden.sse_scenarios import document, router_rules, stream
from tests.unit.turns.harness import TOKEN, USER_BODY, app_for, make_harness, running

from noema.decision.service import trace_id_of
from noema.turns.runner import TurnRunner
from noema.turns.service import TurnService

pytestmark = pytest.mark.unit
SOL = "us.openai.gpt-6.1-sol"


async def _call(app: Any, body: bytes, clock: Any, *, disconnect_after_s: float | None) -> list[dict[str, Any]]:
    """One raw ASGI createTurn with `Idempotency-Key: retry-me`; the client goes away after `disconnect_after_s`."""
    sent: list[dict[str, Any]] = []
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        if disconnect_after_s is None:
            await anyio.sleep_forever()
        await clock.sleep(disconnect_after_s)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    headers = [
        (b"authorization", f"Bearer {TOKEN}".encode()),
        (b"content-type", b"application/json"),
        (b"idempotency-key", b"retry-me"),
        (b"content-length", str(len(body)).encode()),
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/turns",
        "raw_path": b"/v1/turns",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 5000),
        "server": ("t", 80),
        "root_path": "",
    }
    await app(scope, receive, send)
    return sent


async def test_a_keyed_stream_dropped_before_its_first_model_call_is_attached_on_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = make_harness(
        scenario=document("r", [stream(SOL, first_event_ms=100, first_text_ms=300, chunks=["Paris."])], router_rules())
    )
    preflight = TurnRunner._preflight

    async def slow_preflight(self: TurnRunner) -> Any:  # e.g. waiting for an uploaded file to become ready
        await self.svc.clock.sleep(2.0)
        return await preflight(self)

    monkeypatch.setattr(TurnRunner, "_preflight", slow_preflight)
    app = app_for(h)
    body = orjson.dumps({**USER_BODY, "stream": True})
    async with running(h), LifespanManager(app) as manager:
        first = await _call(manager.app, body, h.clock, disconnect_after_s=0.5)
        assert first[0]["status"] == 200
        await h.clock.sleep(1.0)  # the client retries the dropped stream with the same key
        async with anyio.create_task_group() as tg:
            tg.start_soon(lambda: _call(manager.app, body, h.clock, disconnect_after_s=None))
            await h.clock.sleep(10.0)
            tg.cancel_scope.cancel()
    created = {
        e.get("turn_id") for tid in h.log.timeline for _, e in h.log.timeline[tid] if e["type"] == "turn.created"
    }
    assert len(created) == 1, f"one Idempotency-Key ran {len(created)} turns"
    assert app.state.contract_violations == []


async def test_a_turn_without_traceparent_gets_a_per_request_trace_id(monkeypatch: pytest.MonkeyPatch) -> None:
    h = make_harness(
        scenario=document("r", [stream(SOL, first_event_ms=100, first_text_ms=300, chunks=["Paris."])], router_rules())
    )
    seen: list[str] = []
    create = TurnService.create

    async def spy(self: TurnService, **kw: Any) -> Any:
        seen.append(kw["trace_id"])
        return await create(self, **kw)

    monkeypatch.setattr(TurnService, "create", spy)
    app = app_for(h)
    body = orjson.dumps({**USER_BODY, "stream": False})
    async with running(h), LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"authorization": f"Bearer {TOKEN}", "content-type": "application/json"}
            resp = await client.post("/v1/turns", content=body, headers={**headers, "x-request-id": "req-trace-1"})
    assert resp.status_code == 200, resp.text
    assert seen == [trace_id_of(None, "req-trace-1")] and seen[0] != "0" * 32


@pytest.mark.parametrize(("data_class", "store"), [("restricted", True), ("confidential", True), ("internal", False)])
async def test_a_keyed_json_turn_whose_content_may_not_be_kept_is_not_stored(data_class: str, store: bool) -> None:
    """Wave E edge fix 10: the idempotency record never keeps a response body that the turn itself may not keep
    (`store: false`, or a data class storing content redacted or not at all): it is completed as not_stored, so a
    retry with the same key runs again (as after a failure before dispatch)."""
    from tests.unit.turns.harness import STICKY_SOL

    h = make_harness(
        scenario=document(
            "r", [stream(SOL, first_event_ms=100, first_text_ms=300, chunks=["SECRET ", "answer."])], router_rules()
        )
    )
    h.seed_session("ses_1", last_seq=3, sticky_spec=STICKY_SOL, data_class=data_class)
    app = app_for(h)
    request = {**USER_BODY, "stream": False, "store": store}
    if store:
        request["session_id"] = "ses_1"
    body = orjson.dumps(request)
    headers = {"authorization": f"Bearer {TOKEN}", "content-type": "application/json", "idempotency-key": "k-1"}
    async with running(h), LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            first = await client.post("/v1/turns", content=body, headers=headers)
            second = await client.post("/v1/turns", content=body, headers=headers)
    assert (first.status_code, second.status_code) == (200, 200)
    assert "x-aghoo-idempotent-replay" not in second.headers  # executed again: nothing was kept to replay
    records = list(h.world.idempotency._records.values())
    assert [r.state for r in records] == ["not_stored"]
    assert all(b"SECRET" not in ((r.response.body or b"") if r.response else b"") for r in records)
    assert app.state.contract_violations == []


async def test_a_keyed_json_turn_with_full_content_is_still_replayed() -> None:
    from tests.unit.turns.harness import STICKY_SOL

    h = make_harness(
        scenario=document("r", [stream(SOL, first_event_ms=100, first_text_ms=300, chunks=["Paris."])], router_rules())
    )
    h.seed_session("ses_1", last_seq=3, sticky_spec=STICKY_SOL, data_class="internal")
    app = app_for(h)
    body = orjson.dumps({**USER_BODY, "stream": False, "session_id": "ses_1"})
    headers = {"authorization": f"Bearer {TOKEN}", "content-type": "application/json", "idempotency-key": "k-1"}
    async with running(h), LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            first = await client.post("/v1/turns", content=body, headers=headers)
            second = await client.post("/v1/turns", content=body, headers=headers)
    assert second.headers["x-aghoo-idempotent-replay"] == "true" and second.json() == first.json()
    assert len(h.world.generation.runs) == 1


async def test_resume_turn_events_checks_turn_visibility_before_the_phase_1_501() -> None:
    """Wave E2 (edge #22, SE1): resumeTurnEvents answers 404 `not_found` for a turn outside V(turn) (another
    developer's turn, an unknown id) and 501 `not_implemented` only for a visible one (spec-09 §3, IMPL-036)."""
    from tests.unit.turns.harness import STICKY_SOL, principal

    h = make_harness(
        scenario=document("r", [stream(SOL, first_event_ms=100, first_text_ms=300, chunks=["Paris."])], router_rules())
    )
    h.seed_session("ses_1", last_seq=3, sticky_spec=STICKY_SOL)
    app = app_for(h)
    h.world.authn.register("tok-peer", principal("prn_peer"))
    body = orjson.dumps({**USER_BODY, "stream": False, "session_id": "ses_1"})
    own = {"authorization": f"Bearer {TOKEN}", "content-type": "application/json"}
    async with running(h), LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            turn = (await client.post("/v1/turns", content=body, headers=own)).json()
            path = f"/v1/turns/{turn['turn_id']}/events"
            mine = await client.get(path, headers=own)
            peer = await client.get(path, headers={"authorization": "Bearer tok-peer"})
            unknown = await client.get("/v1/turns/turn_01J00000000000000000000000/events", headers=own)
    assert (mine.status_code, mine.json()["error"]["code"]) == (501, "not_implemented")
    assert (peer.status_code, peer.json()["error"]["code"]) == (404, "not_found")
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "not_found")
    assert app.state.contract_violations == []
