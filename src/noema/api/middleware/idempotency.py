"""Step 7: the idempotency hook (IMPL-077, IMPL-085, spec-11 §5, spec-04 §1 step 7).

Authorization (step 6) needs resource lookups, so the route calls the hook after it: `deps.begin_idempotency(...)`
-> `IdempotencyHook.begin`. Without an `Idempotency-Key` header it returns None at once. Otherwise it calls
`IdempotencyStore.begin(principal, method, route template, key, fingerprint, resource_id=...)`:
- `New`: returns None (the route runs); the lease is renewed every 10 s while the request runs, and the ingress
  middleware calls `complete` on **every** exit path: with the final response iff the route called
  `mark_dispatched()` (a model call was dispatched), else None (not stored, so a retry re-executes). Wave E: a JSON
  response the route `withhold()`s (its content may not be kept: `store: false`, or a data class that stores
  content redacted or not at all) is not stored either (an SSE record holds only the turn id);
- `Replay`: returns the stored response with `x-aghoo-idempotent-replay: true` (a streamed turn re-attaches to its
  event log, or 410 `resume_window_expired` once the log expired);
- `InFlight`: a streaming duplicate attaches to the original's event log (one running on another pod whose mirror
  is unavailable waits for the original to finish, then is its replay: 410 while the mirror stays down, IMPL-021);
  a JSON duplicate waits for its result;
- `Conflict`: raises 409 `idempotency_key_reused`.

The fingerprint is sha256 of the canonical JSON body (sorted keys, compact) + "\\n" + Aghoo-Version (spec-11 §5);
routes with non-JSON bodies (uploads) pass their own. Wave E: the store sees it bound to the caller's principal
(`principal_fingerprint`), so another principal of the project reusing a key with the same body is a `Conflict`
(409), never a replay or an attach of someone else's response (spec-09 §3 creator-only visibility); keys stay
project-scoped (IMPL-077), so such a reuse is refused rather than run.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Final

import anyio
import orjson
from starlette.responses import Response

from noema.api.sse_frame import SendBytes, SseResponse, WriteOutcome
from noema.contracts.bundle import RawOperation
from noema.core.clock import Clock, Deadline
from noema.core.errors import ErrorCode, NoemaError
from noema.core.principal import Principal
from noema.core.protocols.events import SseFrame, SseWriter, TurnEventLog
from noema.core.protocols.storage import (
    Conflict,
    IdempotencyStore,
    IdemToken,
    InFlight,
    New,
    Replay,
    StoredResponse,
)
from noema.core.values import JsonValue

log = logging.getLogger("noema.api.idempotency")

KEY_PARAM: Final = "Idempotency-Key"
REPLAY_HEADER: Final = "x-aghoo-idempotent-replay"
HEARTBEAT_S: Final = 10.0
DEFAULT_AWAIT_MS: Final = 120_000  # wait limit of a JSON duplicate on operations without Aghoo-Deadline-Ms
_NOT_REPLAYED: Final = frozenset({"content-length", "transfer-encoding", "connection", REPLAY_HEADER})
_SSE_OWN: Final = frozenset({"content-type", "cache-control", "x-accel-buffering"})
_REQUEST_ID: Final = "x-request-id"


def request_fingerprint(body: JsonValue, version: str | None) -> str:
    """sha256 hex of the canonical body (b"" for a body-less request) + b"\\n" + the Aghoo-Version."""
    canonical = b"" if body is None else orjson.dumps(body, option=orjson.OPT_SORT_KEYS)
    return hashlib.sha256(canonical + b"\n" + (version or "").encode()).hexdigest()


def principal_fingerprint(fingerprint: str, principal_id: str) -> str:
    """The fingerprint the store compares: the request's, bound to its principal (see the module docstring)."""
    return hashlib.sha256(f"{fingerprint}\n{principal_id}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class CapturedResponse:
    """The final response of a request as the ingress middleware saw it (body empty for SSE)."""

    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes
    sse: bool


def _replay_headers(stored: StoredResponse, *, sse: bool) -> dict[str, str]:
    """The stored headers minus the hop-by-hop ones, plus the replay header. The original `x-request-id` is
    dropped (the ingress then sends this request's id, IMPL-033), except on a stored error envelope, whose
    `error.request_id` names the original request (spec-08 §4: the header equals `error.request_id`)."""
    drop = _NOT_REPLAYED | _SSE_OWN if sse else _NOT_REPLAYED
    if sse or stored.status < 400:
        drop = drop | {_REQUEST_ID}
    headers = {k: v for k, v in stored.headers if k.lower() not in drop}
    headers[REPLAY_HEADER] = "true"
    return headers


class IdempotencyHook:
    """The per-request idempotency state of one authenticated operation (see the module docstring)."""

    def __init__(
        self,
        *,
        store: IdempotencyStore,
        events: TurnEventLog,
        sse: SseWriter,
        clock: Clock,
        principal: Principal,
        operation: RawOperation,
        key: str | None,
        body: JsonValue,
        version: str | None,
        wait_deadline: Deadline,
        fingerprint: str | None = None,
    ) -> None:
        self._store = store
        self._events = events
        self._sse = sse
        self._clock = clock
        self._principal = principal
        self._op = operation
        self._key = key
        self._body = body
        self._version = version
        self._wait_deadline = wait_deadline
        self._fingerprint = fingerprint  # precomputed by the ingress for a large body (off the event loop)
        self._begun = False
        self._completed = False
        self.start_soon: Callable[[Callable[[], Coroutine[Any, Any, None]]], object] | None = None  # by the ingress
        self.token: IdemToken | None = None  # set when begin() returned New
        self.resource_id: str | None = None
        self.dispatched = False
        self.withheld = False

    @property
    def key(self) -> str | None:
        """The request's `Idempotency-Key`, None when absent."""
        return self._key

    @property
    def begun(self) -> bool:
        """True once the route called `begin()` (the test-mode validator requires it for a 2xx with a key)."""
        return self._begun

    async def begin(
        self, *, resource_id: str | None = None, stream: bool = False, fingerprint: str | None = None
    ) -> Response | None:
        """Claim the key. Returns None when the route must run (no key, or `New`), else the response to return as
        is (replay or attach). Raises `NoemaError(idempotency_key_reused)` on a fingerprint conflict, and the
        store's errors (500 `internal_error` when Postgres is down, 504/500 while waiting). Call it at most once,
        after authorization; `resource_id` is the pre-generated `turn_`/`dec_` id, `stream` whether this request
        streams (SSE)."""
        if self._begun:
            raise RuntimeError("IdempotencyHook.begin() called twice")
        self._begun = True
        if self._key is None:
            return None
        fp = fingerprint or self._fingerprint or request_fingerprint(self._body, self._version)
        fp = principal_fingerprint(fp, self._principal.principal_id)
        outcome = await self._store.begin(
            self._principal, self._op.method, self._op.path, self._key, fp, resource_id=resource_id
        )
        if isinstance(outcome, New):
            self.token, self.resource_id = outcome.token, resource_id
            if self.start_soon is not None:
                self.start_soon(self._heartbeat)
            return None
        if isinstance(outcome, Conflict):
            raise NoemaError(
                ErrorCode.IDEMPOTENCY_KEY_REUSED,
                "This Idempotency-Key was used with a different request.",
                param=KEY_PARAM,
            )
        if isinstance(outcome, Replay):
            return await self._replay(outcome.response)
        return await self._attach(outcome, stream)

    def mark_dispatched(self) -> None:
        """Record that a model call was dispatched: the final response (2xx, or e.g. a 503 with
        `details.decision_id`) is then stored for replay (IMPL-077)."""
        self.dispatched = True

    def withhold(self) -> None:
        """The response's content may not be kept: a JSON response is completed as not stored (a retry runs
        again); an SSE record (the turn id only) is unaffected."""
        self.withheld = True

    async def _frames(self, turn_id: str) -> AsyncIterator[SseFrame] | None:
        if self._events.is_local(turn_id):
            return self._events.subscribe(turn_id, after=0)
        return await self._events.attach_remote(turn_id, after=0)

    def _sse_response(self, frames: AsyncIterator[SseFrame], headers: dict[str, str]) -> Response:
        async def write(send: SendBytes) -> WriteOutcome:
            return await self._sse.run(frames, send)

        return SseResponse(write, headers=headers)

    async def _replay(self, stored: StoredResponse) -> Response:
        if stored.sse_turn_id is not None:
            frames = await self._frames(stored.sse_turn_id)
            if frames is None:
                raise NoemaError(
                    ErrorCode.RESUME_WINDOW_EXPIRED,
                    "The event stream of the original request has expired.",
                    details={"turn_id": stored.sse_turn_id},
                )
            return self._sse_response(frames, _replay_headers(stored, sse=True))
        return Response(stored.body or b"", status_code=stored.status, headers=_replay_headers(stored, sse=False))

    async def _attach(self, inflight: InFlight, stream: bool) -> Response:
        if stream and inflight.resource_id is not None:
            frames = await self._frames(inflight.resource_id)
            if frames is not None:
                return self._sse_response(frames, {REPLAY_HEADER: "true"})
            # running on another pod and its mirror is unavailable (Redis down): wait for the original to finish,
            # then its replay (410 while the mirror stays unavailable; IMPL-021, Wave E2)
        stored = await self._store.await_result(inflight.token, deadline=self._wait_deadline)
        return await self._replay(stored)

    async def _heartbeat(self) -> None:
        token = self.token
        assert token is not None
        while True:
            await self._clock.sleep(HEARTBEAT_S)
            if self._completed:
                return
            try:
                alive = await self._store.heartbeat(token)
            except Exception:
                log.warning("idempotency heartbeat failed", exc_info=True)
                continue
            if not alive:
                log.warning("idempotency lease lost; the request carries on (record %s)", token.record_id)
                return

    async def finish(self, response: CapturedResponse | None) -> None:
        """`complete` the record exactly once (no-op without a `New` token): the captured response iff a model call
        was dispatched, else None. Shielded from cancellation; store errors are logged, never raised."""
        if self.token is None or self._completed:
            return
        self._completed = True
        stored: StoredResponse | None = None
        if self.dispatched and response is not None:
            if not response.sse:
                if not self.withheld:
                    stored = StoredResponse(response.status, response.headers, response.body, None)
            elif self.resource_id is not None:
                stored = StoredResponse(response.status, response.headers, None, self.resource_id)
            else:
                log.error("a streamed response without resource_id can't be stored for replay")
        with anyio.CancelScope(shield=True):
            try:
                await self._store.complete(self.token, stored)
            except Exception:
                log.error("idempotency complete failed (record %s)", self.token.record_id, exc_info=True)
