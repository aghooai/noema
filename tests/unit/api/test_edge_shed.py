"""Wave E3: the edge shed (`noema.api.shed`): a createTurn / createDecision / facade request the per-process cap
refuses is answered before the middleware stack (no body read, no authentication, no gate), with the same bytes the
route's shed sends; the edge decides exactly as `TurnSlots.try_acquire` would; unknown keys, keyed requests and
everything below the cap go on to the ingress pipeline unchanged."""

from __future__ import annotations

import random
from collections.abc import Mapping
from typing import Any

import httpx
import orjson
import pytest
from asgi_lifespan import LifespanManager
from tests.unit.turns.harness import USER_BODY, make_harness, principal, running

from noema.api.app import create_app
from noema.api.overload import TurnSlots
from noema.api.routes.turns import router
from noema.core.principal import Principal
from noema.identity.keycache import KeyCache, KeyRow

pytestmark = pytest.mark.unit
KEY_ID = "01J9ZZZZZZZZZZZZZZZZZZZZZZ"
TOKEN = f"ak_live_{KEY_ID}.{'s' * 43}"
UNCACHED = f"ak_live_01J9YYYYYYYYYYYYYYYYYYYYYY.{'s' * 43}"


class CachedAuthn:
    """The fake authenticator plus a real `KeyCache` holding TOKEN's row (as ApiKeyAuthenticator.cache does)."""

    def __init__(self, inner: Any, who: Principal, now: float) -> None:
        self.inner, self.lookups = inner, 0
        self.cache = KeyCache()
        row = KeyRow(who.tenant_id, who.project_id, who.principal_id, "h", 1, "live", who.scopes, who.roles, None,
                     None, ())  # fmt: skip
        self.cache.put(KEY_ID, row, now)

    async def authenticate(self, headers: Mapping[str, str], client_ip: str) -> Principal:
        self.lookups += 1
        return await self.inner.authenticate(headers, client_ip)  # type: ignore[no-any-return]


def _app(held: dict[str, int] | None = None, cap: int = 4) -> tuple[Any, Any, CachedAuthn, TurnSlots]:
    h = make_harness()
    who = principal()
    h.world.authn.register(TOKEN, who)
    authn = CachedAuthn(h.world.authn, who, h.world.clock.monotonic())
    h.override("authn", authn)
    app = create_app(services=h.service.svc, routers=[router], validate=False, trusted_proxies=())
    app.state.turn_slots = slots = TurnSlots(cap)
    slots.starting.update(held or {})
    return h, app, authn, slots


async def _post(app: Any, h: Any, headers: dict[str, str], path: str = "/v1/turns", then: Any = None) -> Any:
    """One request (and, with `then(post)`, more on the same running app); the first response or `then`'s result."""
    body = orjson.dumps({**USER_BODY, "session_id": "ses_1", "stream": False})
    async with running(h), LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:

            async def post(headers: dict[str, str]) -> httpx.Response:
                return await client.post(path, content=body, headers=headers)

            first = await post(headers)
            return first if then is None else await then(first, post)


def _headers(token: str = TOKEN, **extra: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}", "content-type": "application/json", "x-request-id": "req_e3", **extra}


async def test_an_over_cap_request_gets_the_routes_429_bytes_before_authentication() -> None:
    h, app, authn, slots = _app({"t_acme": 4})  # the tenant holds the whole cap
    edge = await _post(app, h, _headers())
    assert edge.status_code == 429 and authn.lookups == 0  # never authenticated
    assert edge.headers["retry-after"] == "1" and edge.headers["x-request-id"] == "req_e3"
    assert edge.json()["error"]["code"] == "queue_timeout" and slots.shed == 1
    h2, app2, authn2, _ = _app({"t_acme": 4})
    app2.edge_shed = False  # the route's shed after the full ingress
    route = await _post(app2, h2, _headers())
    assert authn2.lookups == 1
    assert (route.status_code, route.headers.raw, route.content) == (edge.status_code, edge.headers.raw, edge.content)


@pytest.mark.parametrize("path", ["/v1/turns", "/v1/decisions", "/openai/v1/decisions"])
async def test_the_capped_operations_are_shed_at_the_edge(path: str) -> None:
    h, app, authn, slots = _app({"t_acme": 4})
    response = await _post(app, h, _headers(), path)
    assert (response.status_code, authn.lookups, slots.shed) == (429, 0, 1)


@pytest.mark.parametrize(
    ("headers", "held"),
    [
        (_headers(), {"t_acme": 3}),  # below the cap
        (_headers(UNCACHED), {"t_acme": 4}),  # a key id this process has not cached: authentication decides
        (_headers(**{"Idempotency-Key": "k1"}), {"t_acme": 4}),  # keyed: step 7 decides
        ({"content-type": "application/json"}, {"t_acme": 4}),  # no key: 401
        (_headers(), {"t_other": 4}),  # another tenant holds the cap; this one is below its share
    ],
    ids=["below-cap", "uncached-key", "keyed", "no-key", "below-share"],
)
async def test_everything_else_goes_through_the_ingress(headers: dict[str, str], held: dict[str, int]) -> None:
    h, app, authn, _ = _app(held)
    await _post(app, h, headers)
    assert authn.lookups == 1  # the request reached authentication (whatever the route then answered)


def test_the_edge_decides_as_the_slots_would() -> None:
    rng = random.Random(3)
    for _ in range(3000):
        cap = rng.choice([1, 4, 10, 120])
        slots = TurnSlots(cap)
        tenants = ["t_a", "t_b", "t_c", "t_d"][: rng.randrange(1, 5)]
        running = {t: rng.randrange(0, cap + 3) for t in tenants if rng.random() < 0.7}
        slots.turns = {f"turn_{i}": None for i in range(sum(running.values()))}
        slots.starting = {t: rng.randrange(1, 4) for t in tenants if rng.random() < 0.4}
        caller = rng.choice([*tenants, "t_new"])
        peek = TurnSlots(cap)
        peek.starting = dict(slots.starting)
        refused = not peek.try_acquire(caller, running)
        assert slots.sheds_early(lambda c=caller: c, lambda r=running: r) is refused  # type: ignore[misc]
        assert slots.sheds_early(lambda: None, lambda r=running: r) is False  # type: ignore[misc]


def test_a_saturated_tenant_holds_at_most_burst_pending_turns() -> None:
    slots = TurnSlots(40)  # burst 4
    slots.saturate("t_flood", now=10.0)
    for _ in range(4):
        assert slots.try_acquire("t_flood", {}, turn=True, now=10.5)
    assert not slots.try_acquire("t_flood", {}, turn=True, now=10.5)  # 4 pending turns: refused at once
    assert slots.try_acquire("t_flood", {}, now=10.5)  # a decision is not a pending turn
    assert slots.try_acquire("t_other", {}, turn=True, now=10.5)  # other tenants are unaffected
    slots.release("t_flood", turn=True)
    assert slots.try_acquire("t_flood", {}, turn=True, now=10.5)  # one pending turn started: one more may wait
    assert not slots.try_acquire("t_flood", {}, turn=True, now=11.9)
    assert slots.try_acquire("t_flood", {}, turn=True, now=12.0)  # SATURATED_S after the timeout: over
    assert slots.saturated == {} and slots.pending_turns == {"t_flood": 5, "t_other": 1}
    for _ in range(5):
        slots.release("t_flood", turn=True)
    assert slots.pending_turns == {"t_other": 1} and slots.starting == {"t_flood": 1, "t_other": 1}
    uncapped = TurnSlots(None)
    uncapped.saturate("t_flood", now=0.0)
    assert uncapped.saturated == {} and uncapped.try_acquire("t_flood", {}, turn=True, now=0.0)


def test_the_edge_refuses_a_saturated_tenants_turns_below_the_cap() -> None:
    slots = TurnSlots(40)
    slots.pending_turns["t_flood"] = slots.starting["t_flood"] = 4
    assert not slots.sheds_early(lambda: "t_flood", dict, turn=True, now=1.0)  # not saturated
    slots.saturate("t_flood", now=1.0)
    assert slots.sheds_early(lambda: "t_flood", dict, turn=True, now=1.5)
    assert not slots.sheds_early(lambda: "t_flood", dict, now=1.5)  # its decisions are not bounded
    assert not slots.sheds_early(lambda: "t_victim", dict, turn=True, now=1.5)
    assert not slots.sheds_early(lambda: None, dict, turn=True, now=1.5)  # unknown: authentication decides
    assert not slots.sheds_early(lambda: "t_flood", dict, turn=True, now=3.0)  # expired


class TimingOut:
    """An admission whose stream-slot wait always times out (the tenant's own limit is full)."""

    async def reserve(self, *args: Any, **kwargs: Any) -> Any:
        from noema.core.errors import ErrorCode, NoemaError

        raise NoemaError(ErrorCode.QUEUE_TIMEOUT, "No capacity became free in time; retry shortly.", retry_after_s=1)


async def test_an_admission_queue_timeout_saturates_the_tenant_and_its_next_turns_are_shed_at_the_edge() -> None:
    from tests.unit.turns.harness import STICKY_SOL

    h, app, authn, slots = _app(cap=10)  # burst 1
    h.seed_session("ses_1", last_seq=3, sticky_spec=STICKY_SOL)
    h.override("admission", TimingOut())

    async def then(first: httpx.Response, post: Any) -> httpx.Response:
        assert first.status_code == 429 and authn.lookups == 1  # admission's queue_timeout, after the full ingress
        assert "t_acme" in slots.saturated and slots.shed == 0 and slots.pending_turns == {}
        slots.pending_turns["t_acme"] = slots.starting["t_acme"] = 1  # one of its turns is still waiting
        return await post(_headers())  # type: ignore[no-any-return]

    second = await _post(app, h, _headers(), then=then)
    assert second.status_code == 429 and authn.lookups == 1 and slots.shed == 1  # refused before authentication
    assert second.json()["error"]["code"] == "queue_timeout" and second.headers["retry-after"] == "1"
