"""The per-process turn cap (`noema.api.overload`, E sprint B): above it createTurn answers 429 `queue_timeout` with
`retry-after: 1` before any session, admission or model work; the slot is given back once the turn started (it is
then counted by the turn service) or failed before it did."""

from __future__ import annotations

import math
from typing import Any

import anyio
import httpx
import orjson
import pytest
from asgi_lifespan import LifespanManager
from tests.fakes.clock import FakeClock
from tests.fakes.telemetry import FakeTelemetry
from tests.golden.sse_scenarios import document, router_rules, stream
from tests.unit.turns.harness import STICKY_SOL, TOKEN, USER_BODY, app_for, make_harness, running

from noema.api.overload import (
    DEFAULT_MAX_TURNS,
    HELD_EVERY_S,
    HELD_METRIC,
    MAX_TURNS_ENV,
    TurnSlots,
    export_held,
    max_turns_from_env,
)
from noema.contracts.bundle import VENDOR_DIR
from noema.core.errors import ErrorCode, NoemaError

pytestmark = pytest.mark.unit
SOL = "us.openai.gpt-6.1-sol"


def test_the_cap_comes_from_the_environment() -> None:
    assert max_turns_from_env({}) == DEFAULT_MAX_TURNS
    assert max_turns_from_env({MAX_TURNS_ENV: " 40 "}) == 40
    assert max_turns_from_env({MAX_TURNS_ENV: "0"}) is None  # no cap
    for bad in ("-1", "x", "1.5", "100001"):
        with pytest.raises(ValueError, match=MAX_TURNS_ENV):
            max_turns_from_env({MAX_TURNS_ENV: bad})


def test_slots_count_running_and_starting_turns() -> None:
    slots = TurnSlots(3)
    slots.acquire("t_a", {"t_a": 1})
    slots.acquire("t_a", {"t_a": 1})  # 1 running + 1 starting < 3
    with pytest.raises(NoemaError) as raised:
        slots.acquire("t_a", {"t_a": 1})  # 1 running + 2 starting
    err = raised.value
    assert err.code is ErrorCode.QUEUE_TIMEOUT and err.retry_after_s == 1
    assert err.spec.status == 429 and err.spec.retryable
    slots.release("t_a")
    slots.acquire("t_a", {"t_a": 1})
    assert (slots.starting, slots.shed) == ({"t_a": 2}, 1)
    unlimited = TurnSlots(None)
    for _ in range(1000):
        unlimited.acquire("t_a", {"t_a": 10_000})


def test_one_tenant_at_the_cap_does_not_keep_another_out() -> None:
    """Wave E edge fix 11: share = cap // active tenants. A tenant alone may use the whole cap; a tenant below its
    share is admitted up to `burst` above it, so a newcomer gets in at once and the freed slots go to it."""
    slots = TurnSlots(120)
    assert slots.burst == 12
    running = {"t_flood": 120}
    assert not slots.try_acquire("t_flood", running)  # alone: the whole cap, as before
    for _ in range(12):
        assert slots.try_acquire("t_victim", running)  # below its share (60): admitted at once
    assert not slots.try_acquire("t_victim", running)  # cap + burst held
    running["t_flood"] = 119  # one of the flooder's turns ended
    assert not slots.try_acquire("t_flood", running)  # above its share with the cap held: not refilled
    assert slots.try_acquire("t_victim", running)  # the freed slot goes to the tenant below its share
    assert slots.starting == {"t_victim": 13}
    for _ in range(13):
        slots.release("t_victim")
    assert slots.starting == {}
    assert slots.try_acquire("t_flood", running)  # alone again: back to the whole cap


def _headers() -> dict[str, str]:
    return {"authorization": f"Bearer {TOKEN}", "content-type": "application/json"}


def _body() -> bytes:
    return orjson.dumps({**USER_BODY, "session_id": "ses_1", "stream": False})


async def test_create_turn_sheds_above_the_cap_and_admits_again_once_a_turn_ended() -> None:
    h = make_harness(
        scenario=document(
            "r", [stream(SOL, first_event_ms=100, first_text_ms=3000, chunks=["Paris ", "is ", "big."])], router_rules()
        )
    )
    h.seed_session("ses_1", last_seq=3, sticky_spec=STICKY_SOL)
    app = app_for(h)  # the response validator is on: the 429 envelope is contract-checked
    app.state.turn_slots = slots = TurnSlots(1)
    got: dict[str, httpx.Response] = {}
    async with running(h), LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:

            async def post(name: str, after_s: float = 0.0) -> None:
                await h.clock.sleep(after_s)
                got[name] = await client.post("/v1/turns", content=_body(), headers=_headers())

            async with anyio.create_task_group() as tg:
                tg.start_soon(post, "first")
                tg.start_soon(post, "shed", 1.0)  # the first turn is still waiting for its first text
            await post("after")
    shed: Any = got["shed"]
    assert shed.status_code == 429, shed.text
    assert shed.headers["retry-after"] == "1"
    assert shed.json()["error"]["code"] == "queue_timeout" and shed.json()["error"]["retryable"] is True
    assert got["first"].status_code == 200 and got["after"].status_code == 200
    assert len(h.world.generation.runs) == 2  # the shed request reached no session, admission or model
    assert (slots.starting, slots.shed) == ({}, 1)
    assert app.state.contract_violations == []


async def test_a_keyed_duplicate_at_the_cap_attaches_instead_of_being_shed() -> None:
    """Wave E edge fix 19: a keyed request found at the cap waits for step 7; the duplicate of the running turn
    attaches (no session, admission or model work) and replays its result."""
    h = make_harness(
        scenario=document(
            "r", [stream(SOL, first_event_ms=100, first_text_ms=3000, chunks=["Paris ", "is ", "big."])], router_rules()
        )
    )
    h.seed_session("ses_1", last_seq=3, sticky_spec=STICKY_SOL)
    app = app_for(h)
    app.state.turn_slots = slots = TurnSlots(1)
    got: dict[str, httpx.Response] = {}
    keyed = {**_headers(), "Idempotency-Key": "same-key"}
    async with running(h), LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:

            async def post(name: str, after_s: float = 0.0, headers: dict[str, str] = keyed) -> None:
                await h.clock.sleep(after_s)
                got[name] = await client.post("/v1/turns", content=_body(), headers=headers)

            async with anyio.create_task_group() as tg:
                tg.start_soon(post, "first")
                tg.start_soon(post, "duplicate", 1.0)
                tg.start_soon(post, "new", 1.5, {**_headers(), "Idempotency-Key": "other-key"})
    assert got["first"].status_code == 200
    assert got["duplicate"].status_code == 200, got["duplicate"].text
    assert got["duplicate"].headers["x-aghoo-idempotent-replay"] == "true"
    assert got["duplicate"].json() == got["first"].json()
    assert got["new"].status_code == 429 and got["new"].json()["error"]["code"] == "queue_timeout"
    assert len(h.world.generation.runs) == 1 and (slots.starting, slots.shed) == ({}, 1)
    assert app.state.contract_violations == []


async def test_decisions_and_the_facade_are_under_the_cap() -> None:
    """Wave E edge fix 25: /v1/decisions and the facade fan out through the same pools, so they take a slot too."""
    from tests.unit.decision.app import client, headers, make

    app, _, ew = make()
    app.state.turn_slots = slots = TurnSlots(1)
    slots.starting["t_acme"] = 1  # e.g. a turn of the same tenant being started
    examples = VENDOR_DIR / "examples"
    body = (examples / "decision-request.valid.json").read_bytes()
    facade = (examples / "openai-request.valid.json").read_bytes()
    async with client(app) as http, ew.clock.autojump():
        native = await http.post("/v1/decisions", content=body, headers=headers())
        compat = await http.post("/openai/v1/decisions", content=facade, headers=headers())
    for resp in (native, compat):
        assert resp.status_code == 429 and resp.json()["error"]["code"] == "queue_timeout", resp.text
        assert resp.headers["retry-after"] == "1"
    assert ew.world.adapters.calls() == [] and slots.shed == 2 and slots.starting == {"t_acme": 1}


def test_the_share_is_max_min_fair() -> None:
    """Wave E3: a tenant holding less than an equal split keeps it and the rest is split among the others (water
    filling). C5: the victim, unevenly split between the two processes, held 62 of 120 next to a flooder's 47 and
    was refused under `cap // tenants` (60); its max-min share is 73."""
    from noema.api.overload import fair_share

    assert fair_share([], 120) == math.inf and fair_share([100], 120) == math.inf
    assert fair_share([120], 120) == 120 and fair_share([0, 120], 120) == 120
    assert fair_share([60, 60], 120) == 60 and fair_share([10, 200], 120) == 110
    assert fair_share([47, 62, 30], 120) == 45 and fair_share([47, 73], 120) == 73
    slots = TurnSlots(120)
    running = {"t_victim": 62, "t_small": 5, "t_flood": 43}
    slots.starting = {"t_small": 10}  # 120 held; shares: max-min level 58, cap // 3 = 40
    assert slots.try_acquire("t_flood", running)  # 43 < 58: below its share, within the burst
    assert not slots.try_acquire("t_victim", running)  # 62 >= 58: at its share, the cap holds


def test_a_saturated_tenants_waiting_turns_do_not_count_against_the_others() -> None:
    """Wave E3, C5: the victim at 68 slots of one process next to a flooder's 40 running and 12 waiting turns was
    refused at the cap; the waiting turns wait for the flooder's own limit, so they are not counted for others."""
    slots = TurnSlots(120)
    running = {"t_victim": 68, "t_flood": 40}
    assert slots.try_acquire("t_flood", running, turn=True, now=0.0)  # (not saturated yet)
    slots.saturate("t_flood", now=0.0)
    for _ in range(11):
        assert slots.try_acquire("t_flood", running, turn=True, now=0.5)
    assert not slots.try_acquire("t_flood", running, turn=True, now=0.5)  # 12 waiting: the bound
    assert slots.held() == 0 + 12 and sum(running.values()) + 12 == 120
    assert slots.try_acquire("t_victim", running, turn=True, now=0.5)  # counted as 108 held for it
    assert slots.try_acquire("t_victim", running, now=2.5) is False  # saturation over: 121 held, at its share


async def test_the_held_slots_are_exported_every_second() -> None:
    """`aghoo_turn_slots_held` (the 08 §9 streams-per-pod signal) follows `held()`: running plus admitted turns."""
    slots, telemetry, clock = TurnSlots(3), FakeTelemetry(), FakeClock()
    slots.turns = {"turn_1": object()}
    async with anyio.create_task_group() as tg:
        tg.start_soon(export_held, slots, telemetry.instruments, clock)
        await clock.settle()
        slots.acquire("t_a", {"t_a": 1})
        await clock.advance(HELD_EVERY_S)
        slots.turns = {}  # the turn ended
        slots.release("t_a")  # the admitted one failed before it started
        await clock.advance(HELD_EVERY_S)
        tg.cancel_scope.cancel()
    assert [m.value for m in telemetry.metrics if m.name == HELD_METRIC] == [1, 2, 0]
