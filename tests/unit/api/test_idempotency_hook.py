"""Step 7: the idempotency hook (IMPL-077, spec-11 §5): begin after authz, complete on every exit path."""

from __future__ import annotations

from typing import Any

import anyio
import orjson
import pytest
from fastapi import APIRouter, Request
from starlette.responses import Response
from tests.unit.api.apifakes import DEV_KEY, TURN_BODY, FakeServices, asgi_client, auth, make_app, principal

from noema.api import deps
from noema.api.middleware.idempotency import HEARTBEAT_S, principal_fingerprint, request_fingerprint
from noema.api.sse_frame import SseResponse, write_frames
from noema.contracts.validate import ContractValidator
from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.storage import StoredResponse

pytestmark = pytest.mark.unit
JSON = {"content-type": "application/json"}
TURN_ID = "turn_01J0000000000000000000IDEM"
EVENTS: list[dict[str, Any]] = [
    {"type": "turn.created", "turn_id": TURN_ID, "session_id": None, "created_at": 1},
    {"type": "error", "code": "all_routes_unavailable", "message": "Unavailable.", "retryable": True},
]


def session(n: int) -> dict[str, Any]:
    """A schema-valid Session whose `last_seq` counts the route executions."""
    return {
        "id": "ses_1",
        "created_at": 1,
        "last_seq": n,
        "data_class": "internal",
        "retention": {"policy": "default", "expires_at": None},
        "metadata": {},
    }


class Probe:
    """Routes on createSession (JSON) and createTurn (SSE) driven by the test."""

    def __init__(self, services: FakeServices) -> None:
        self.calls = 0
        self.dispatch = True
        self.fail_after_begin: NoemaError | Exception | None = None
        self.gate: anyio.Event | None = None
        self.router = APIRouter()

        @self.router.post("/v1/sessions", name="createSession")
        async def create_session(request: Request) -> Response:
            replay = await deps.begin_idempotency(request, resource_id="ses_1")
            if replay is not None:
                return replay
            self.calls += 1
            if self.gate is not None:
                await self.gate.wait()
            if self.fail_after_begin is not None:
                raise self.fail_after_begin
            if self.dispatch:
                deps.mark_model_dispatched(request)
            return deps.json_response(session(self.calls), status_code=201, headers={"x-extra": "1"})

        @self.router.post("/v1/turns", name="createTurn")
        async def create_turn(request: Request) -> Response:
            replay = await deps.begin_idempotency(request, resource_id=TURN_ID, stream=True)
            if replay is not None:
                return replay
            self.calls += 1
            for event in EVENTS:
                services.events.append(TURN_ID, event)
            deps.mark_model_dispatched(request)

            async def write(send: Any) -> Any:
                return await write_frames(services.events.subscribe(TURN_ID), send, clock=services.clock)

            return SseResponse(write)


def _setup() -> tuple[Any, FakeServices, Probe]:
    services = FakeServices()
    probe = Probe(services)
    app, _ = make_app(services, routers=[probe.router])
    return app, services, probe


def key(k: str = "key-1", **extra: str) -> dict[str, str]:
    return auth(**JSON, **{"idempotency-key": k}, **extra)


def test_the_fingerprint_is_canonical_and_versioned() -> None:
    a = request_fingerprint({"b": 1, "a": [1, {"y": 2, "x": 1}]}, "2026-10-01")
    assert a == request_fingerprint({"a": [1, {"x": 1, "y": 2}], "b": 1}, "2026-10-01")
    assert a != request_fingerprint({"b": 1, "a": [1, {"y": 2, "x": 1}]}, "2026-10-02")
    assert request_fingerprint(None, "v") != request_fingerprint({}, "v")


async def test_without_a_key_the_store_is_not_touched() -> None:
    app, services, _ = _setup()
    async with asgi_client(app) as client:
        resp = await client.post("/v1/sessions", content=b"{}", headers=auth(**JSON))
    assert resp.status_code == 201
    assert services.idempotency.begins == []
    assert services.idempotency.completes == []


async def test_a_dispatched_response_is_stored_and_replayed_without_rerunning() -> None:
    app, services, probe = _setup()
    async with asgi_client(app) as client:
        first = await client.post("/v1/sessions", content=b'{"metadata": {"a": "1"}}', headers=key())
        second = await client.post("/v1/sessions", content=b'{"metadata":{"a":"1"}}', headers=key())
    assert probe.calls == 1
    assert (first.status_code, second.status_code) == (201, 201)
    assert first.json() == second.json() == session(1)
    assert "x-aghoo-idempotent-replay" not in first.headers
    assert second.headers["x-aghoo-idempotent-replay"] == "true"
    assert second.headers["x-extra"] == "1"
    (begin, _) = services.idempotency.begins
    assert (begin["method"], begin["route"], begin["key"], begin["rid"]) == ("POST", "/v1/sessions", "key-1", "ses_1")
    expected = request_fingerprint({"metadata": {"a": "1"}}, "2026-10-01")
    assert begin["fp"] == principal_fingerprint(expected, principal(DEV_KEY).principal_id)  # bound to the caller
    ((_, stored),) = services.idempotency.completes
    assert stored is not None
    assert (stored.status, orjson.loads(stored.body or b""), stored.sse_turn_id) == (201, session(1), None)
    assert ("x-request-id", first.headers["x-request-id"]) in stored.headers


@pytest.mark.parametrize(
    "failure", [None, NoemaError(ErrorCode.PERMISSION_DENIED, "No."), RuntimeError("bug")], ids=["2xx", "403", "500"]
)
async def test_every_pre_call_exit_completes_with_none_so_a_retry_reexecutes(failure: Exception | None) -> None:
    app, services, probe = _setup()
    probe.dispatch = False
    probe.fail_after_begin = failure
    async with asgi_client(app, check_contract=False) as client:
        await client.post("/v1/sessions", content=b"{}", headers=key())
        await client.post("/v1/sessions", content=b"{}", headers=key())
    assert probe.calls == 2  # not stored: the retry ran again
    assert [resp for _, resp in services.idempotency.completes] == [None, None]


async def test_a_reused_key_with_another_body_is_409() -> None:
    app, _, probe = _setup()
    async with asgi_client(app) as client:
        await client.post("/v1/sessions", content=b"{}", headers=key())
        resp = await client.post("/v1/sessions", content=b'{"metadata": {}}', headers=key())
    assert resp.status_code == 409
    error = resp.json()["error"]
    assert (error["code"], error["param"]) == ("idempotency_key_reused", "Idempotency-Key")
    assert probe.calls == 1


async def test_an_in_flight_json_duplicate_waits_for_the_original() -> None:
    app, _, probe = _setup()
    probe.gate = anyio.Event()
    results: dict[str, Any] = {}
    async with asgi_client(app) as client:

        async def call(name: str) -> None:
            results[name] = await client.post("/v1/sessions", content=b"{}", headers=key())

        async with anyio.create_task_group() as tg:
            tg.start_soon(call, "original")
            await anyio.wait_all_tasks_blocked()
            tg.start_soon(call, "duplicate")
            await anyio.wait_all_tasks_blocked()
            assert "duplicate" not in results
            probe.gate.set()
    assert probe.calls == 1
    assert results["duplicate"].json() == results["original"].json()
    assert results["duplicate"].headers["x-aghoo-idempotent-replay"] == "true"


async def test_the_lease_is_renewed_every_10_s_while_the_request_runs() -> None:
    app, services, probe = _setup()
    probe.gate = anyio.Event()
    async with asgi_client(app) as client, anyio.create_task_group() as tg:
        tg.start_soon(lambda: client.post("/v1/sessions", content=b"{}", headers=auth(**JSON)))  # no key: no lease
        tg.start_soon(lambda: client.post("/v1/sessions", content=b"{}", headers=key()))
        await anyio.wait_all_tasks_blocked()
        assert services.idempotency.heartbeats == 0
        for expected in (1, 2):
            services.clock.advance(HEARTBEAT_S)
            await anyio.wait_all_tasks_blocked()
            assert services.idempotency.heartbeats == expected
        probe.gate.set()
    services.clock.advance(HEARTBEAT_S * 3)
    await anyio.wait_all_tasks_blocked()
    assert services.idempotency.heartbeats == 2
    assert services.clock.sleepers == 0


async def test_a_streamed_turn_is_stored_by_turn_id_and_replayed_from_the_event_log() -> None:
    app, services, probe = _setup()
    body = orjson.dumps(TURN_BODY)
    async with asgi_client(app) as client:
        first = await client.post("/v1/turns", content=body, headers=key("turn-key"))
        second = await client.post("/v1/turns", content=body, headers=key("turn-key"))
        services.events.local.clear()  # the event log expired
        expired = await client.post("/v1/turns", content=body, headers=key("turn-key"))
    assert probe.calls == 1
    ((_, stored),) = services.idempotency.completes
    assert stored is not None
    assert (stored.status, stored.body, stored.sse_turn_id) == (200, None, TURN_ID)
    frames = ContractValidator().check_sse_stream("createTurn", second.content)
    assert [f.event for f in frames] == ["turn.created", "error"]
    assert second.content == first.content
    assert second.headers["x-aghoo-idempotent-replay"] == "true"
    assert second.headers["content-type"].startswith("text/event-stream")
    assert expired.status_code == 410
    error = expired.json()["error"]
    assert (error["code"], error["details"]) == ("resume_window_expired", {"turn_id": TURN_ID})


async def test_an_in_flight_streaming_duplicate_attaches_to_the_event_log() -> None:
    app, services, probe = _setup()
    body = orjson.dumps(TURN_BODY)
    principal = services.authn.principals[next(iter(services.authn.principals))]
    outcome = await services.idempotency.begin(
        principal,
        "POST",
        "/v1/turns",
        "k",
        principal_fingerprint(request_fingerprint(TURN_BODY, "2026-10-01"), principal.principal_id),
        resource_id=TURN_ID,
    )
    assert type(outcome).__name__ == "New"
    async with asgi_client(app) as client:
        services.events.remote[TURN_ID] = []  # the turn's log exists (another pod) but holds no events yet
        for event in EVENTS:
            services.events.append(TURN_ID, event)
        attached = await client.post("/v1/turns", content=body, headers=key("k"))
    assert probe.calls == 0
    assert attached.headers["x-aghoo-idempotent-replay"] == "true"
    assert [f.event for f in ContractValidator().check_sse_stream("createTurn", attached.content)] == [
        "turn.created",
        "error",
    ]


async def test_a_replay_echoes_the_retrys_own_request_id() -> None:
    """Wave E edge fix 18 (IMPL-033): the stored response carries the original x-request-id; a replay sends the
    retry's own (valid, client-supplied) id instead."""
    app, _, probe = _setup()
    async with asgi_client(app) as client:
        first = await client.post("/v1/sessions", content=b"{}", headers=key(**{"x-request-id": "client-req-1"}))
        second = await client.post("/v1/sessions", content=b"{}", headers=key(**{"x-request-id": "client-req-2"}))
    assert probe.calls == 1 and second.headers["x-aghoo-idempotent-replay"] == "true"
    assert (first.headers["x-request-id"], second.headers["x-request-id"]) == ("client-req-1", "client-req-2")


@pytest.mark.parametrize("stored", [True, False])
async def test_an_unattachable_cross_pod_duplicate_waits_for_the_original_then_410(stored: bool) -> None:
    """Wave E2 (edge #25, F10; IMPL-021): a streaming duplicate of a turn running on another pod whose mirror is
    unavailable (Redis down) waits for the original to finish, then answers 410 `resume_window_expired` with the
    turn id (500 retryable when the original ended unstored); before, it answered 500 at once."""
    app, services, probe = _setup()
    body = orjson.dumps(TURN_BODY)
    principal = services.authn.principals[next(iter(services.authn.principals))]
    fingerprint = principal_fingerprint(request_fingerprint(TURN_BODY, "2026-10-01"), principal.principal_id)
    original = await services.idempotency.begin(principal, "POST", "/v1/turns", "k", fingerprint, resource_id=TURN_ID)
    token = original.token  # type: ignore[union-attr]
    answers: list[Any] = []
    async with asgi_client(app) as client, anyio.create_task_group() as tg:

        async def duplicate() -> None:
            answers.append(await client.post("/v1/turns", content=body, headers=key("k")))

        tg.start_soon(duplicate)
        await anyio.wait_all_tasks_blocked()
        assert answers == []  # still waiting for the original
        sse = StoredResponse(200, (("content-type", "text/event-stream"),), None, TURN_ID)
        await services.idempotency.complete(token, sse if stored else None)
    (answer,) = answers
    error = answer.json()["error"]
    assert probe.calls == 0
    if stored:
        assert (answer.status_code, error["code"], error["details"]) == (
            410,
            "resume_window_expired",
            {"turn_id": TURN_ID},
        )
    else:
        assert (answer.status_code, error["code"], error["retryable"]) == (500, "internal_error", True)
