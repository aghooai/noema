"""The edge shed (Wave E3): the per-process cap (`noema.api.overload.TurnSlots`) applied before the middleware stack,
so a request the cap would refuse costs almost nothing (C5: a flooding tenant's refused requests slowed the other
tenants' turns on the shared event loops; at the route a shed cost ~0.7 ms of CPU: the ingress middleware, the
per-IP auth-failure check in Redis, the key verification, the body read and gate, the access log).

`NoemaApp` asks `shed(slots, scope, send)` for every HTTP request. It acts only on unkeyed `POST`s of the capped
operations (createTurn, createDecision, the facade), and only while the process holds at least `cap` slots or, for
createTurn, while some tenant is saturated by its own admission limits. Then it reads the caller's tenant from the
authenticator's in-process key cache (the `Authorization` key id; no Postgres, no Redis, no secret verification).
A tenant the route's check would refuse now (`TurnSlots.sheds_early`: the same policy, see `noema.api.overload`)
gets the typed 429 `queue_timeout` envelope with `retry-after: 1` and `x-request-id` at once, counted in
`aghoo_turns_shed_total` and in one `edge shed` log line per second at most (no access line per refused request:
under a flood the log record and its redaction would cost more than the shed). Everything else goes on to the
ingress pipeline unchanged, where the route's check stays authoritative.

Precedence: a key id the cache does not hold (unknown, malformed, or not used on this process for 60 s) always gets
the full ingress (its 401 or its route-level shed). A cached key id is not verified at the edge, so while the
process is over the cap a wrong secret for it is 429 rather than 401, and the request gate's 400/413/415 are not
reached; a keyed request (`Idempotency-Key`) is never shed here (step 7 decides: its duplicate attaches or replays).
Off with the test-mode response validator (it has to see every response).
"""

from __future__ import annotations

import logging
from typing import Final

from starlette.types import Scope, Send

from noema.api.middleware.context import SERVICES_STATE_KEY
from noema.api.middleware.errors import send_error
from noema.api.middleware.request_id import resolve_request_id
from noema.api.overload import SHED_MESSAGE, SHED_RETRY_AFTER_S, TurnSlots
from noema.api.routes.capacity import running_by_tenant
from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.services import ServiceSet
from noema.identity.apikeys import parse_bearer
from noema.identity.keycache import KeyCache

TURN_PATH: Final = "/v1/turns"
SHED_PATHS: Final = frozenset({TURN_PATH, "/v1/decisions", "/openai/v1/decisions"})
SUMMARY_S: Final = 1.0
log = logging.getLogger("noema.api.shed")


async def shed(slots: TurnSlots, scope: Scope, send: Send) -> bool:
    """True when the request was answered with the 429 here (see the module docstring)."""
    if slots.cap is None or scope["method"] != "POST" or scope["path"] not in SHED_PATHS:
        return False
    turn = scope["path"] == TURN_PATH
    if slots.held() < slots.cap and not (turn and slots.saturated):
        return False  # the common case: every caller is admitted (TurnSlots.sheds_early)
    services: ServiceSet | None = scope.get("state", {}).get(SERVICES_STATE_KEY)
    if services is None:
        return False
    authorization, client_request_id = _headers(scope)
    if authorization is None:
        return False
    now = services.clock.monotonic()
    if not slots.sheds_early(
        lambda: _tenant(services, authorization, now),
        lambda: running_by_tenant(slots),
        turn=turn,
        now=now,
    ):
        return False
    slots.shed += 1
    request_id = resolve_request_id(client_request_id, services.ids)
    err = NoemaError(ErrorCode.QUEUE_TIMEOUT, SHED_MESSAGE, retry_after_s=SHED_RETRY_AFTER_S)
    await send_error(send, err, request_id)
    services.telemetry.instruments.add("aghoo_turns_shed_total")
    _SUMMARY.add(scope["path"], now)
    return True


def _headers(scope: Scope) -> tuple[str | None, str | None]:
    """(Authorization, x-request-id) of an unkeyed request; (None, None) when it carries an Idempotency-Key."""
    authorization = request_id = None
    for raw_name, raw_value in scope.get("headers", ()):
        name = raw_name.lower()
        if name == b"authorization" and authorization is None:
            authorization = raw_value.decode("latin-1")
        elif name == b"x-request-id" and request_id is None:
            request_id = raw_value.decode("latin-1")
        elif name == b"idempotency-key":
            return None, None
    return authorization, request_id


def _tenant(services: ServiceSet, authorization: str, now: float) -> str | None:
    """The tenant of the request's key id from the authenticator's key cache (no verification), else None."""
    cache = getattr(services.authn, "cache", None)
    if not isinstance(cache, KeyCache):
        return None
    parsed = parse_bearer(authorization)
    if parsed is None:
        return None
    entry = cache.get(parsed.key_id, now)
    return None if entry is None or entry.row is None else entry.row.tenant_id


class _Summary:
    """One `edge shed` log line per `SUMMARY_S` at most (a flood must not cost a log record, with its redaction, per
    refused request): the refusals per path since the previous line."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.logged_at = -SUMMARY_S

    def add(self, path: str, now: float) -> None:
        self.counts[path] = self.counts.get(path, 0) + 1
        if now - self.logged_at < SUMMARY_S:
            return
        counts, self.counts, self.logged_at = self.counts, {}, now
        log.warning(
            "edge shed: %d requests refused by the per-process cap (%s)",
            sum(counts.values()),
            ", ".join(f"POST {p} {n}" for p, n in sorted(counts.items())),
            extra={"event": "http.edge_shed", "http_status": 429, "code": "queue_timeout"},
        )


_SUMMARY: Final = _Summary()
