"""Wall-clock timeline of createTurn requests inside the api process (the harness's `serve --trace-out`): selected
async functions of the request path are wrapped at startup and record (name, start, end) into the request's trace
(a contextvar set by the ingress middleware for POST /v1/turns; the turn task inherits it). Postgres and Redis round
trips are recorded too. Diagnosis only: never on during a reported measurement.

`dump(path)` writes one JSON line per trace: `{"t0": ns, "events": [[name, start_ms, dur_ms], ...]}` (relative to the
middleware entry), the last `MAX_TRACES` requests.
"""

from __future__ import annotations

import functools
import importlib
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Final

import orjson

MAX_TRACES: Final = 500
Event = tuple[str, int, int]
_trace: ContextVar[list[Event] | None] = ContextVar("load_trace", default=None)
_traces: deque[tuple[int, list[Event]]] = deque(maxlen=MAX_TRACES)

# (module, class or "" for a module function, attribute, label)
TARGETS: Final = (
    ("noema.api.middleware.ingress", "IngressMiddleware", "_handle", "ingress._handle"),
    ("noema.identity.authn", "ApiKeyAuthenticator", "authenticate", "authn.authenticate"),
    ("noema.turns.service", "TurnService", "authorize", "turn.authorize"),
    ("noema.api.deps", "", "begin_idempotency", "idempotency.begin"),
    ("noema.turns.service", "TurnService", "prepare", "turn.prepare"),
    ("noema.turns.ingress", "", "_history_rows", "prepare.history_rows"),
    ("noema.turns.ingress", "", "_data_class", "prepare.data_class"),
    ("noema.ops.admission.admission", "GcraAdmission", "reserve", "admission.reserve"),
    ("noema.turns.service", "TurnService", "start", "turn.start"),
    ("noema.decision.engine", "RunContext", "__aenter__", "decision.enter"),
    ("noema.adapters.converse", "ConverseAdapter", "complete", "converse.complete"),
    ("noema.adapters.mantle", "MantleAdapter", "complete", "mantle.complete"),
    ("noema.adapters._stream", "StreamSession", "_open_task", "stream.open"),
    ("asyncpg.connection", "Connection", "execute", "pg.execute"),
    ("asyncpg.connection", "Connection", "fetch", "pg.fetch"),
    ("asyncpg.connection", "Connection", "fetchrow", "pg.fetchrow"),
    ("asyncpg.connection", "Connection", "fetchval", "pg.fetchval"),
    ("redis.asyncio.client", "Redis", "execute_command", "redis.command"),
    ("redis.asyncio.client", "Pipeline", "execute", "redis.pipeline"),
)


def _wrap(fn: Callable[..., Awaitable[Any]], label: str) -> Callable[..., Awaitable[Any]]:
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        events = _trace.get()
        if events is None:
            return await fn(*args, **kwargs)
        t0 = time.monotonic_ns()
        name = label
        if label == "redis.command" and len(args) > 1:  # (self, command name, ...)
            name = f"redis.{args[1]!s}"
        try:
            return await fn(*args, **kwargs)
        finally:
            events.append((name, t0, time.monotonic_ns()))

    return wrapper


def _wrap_root(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    @functools.wraps(fn)
    async def wrapper(self: Any, scope: dict[str, Any], receive: Any, send: Any) -> Any:
        if scope.get("type") != "http" or scope.get("method") != "POST" or scope.get("path") != "/v1/turns":
            return await fn(self, scope, receive, send)
        events: list[Event] = []
        _traces.append((time.monotonic_ns(), events))
        token = _trace.set(events)
        try:
            return await fn(self, scope, receive, send)
        finally:
            _trace.reset(token)

    return wrapper


def install() -> None:
    """Wrap every target (a missing one is skipped silently: the list follows the current code)."""
    for module_name, owner_name, attr, label in TARGETS:
        module = importlib.import_module(module_name)
        owner = getattr(module, owner_name, None) if owner_name else module
        fn = getattr(owner, attr, None) if owner is not None else None
        if fn is not None:
            setattr(owner, attr, _wrap(fn, label))
    ingress = importlib.import_module("noema.api.middleware.ingress").IngressMiddleware
    ingress.__call__ = _wrap_root(ingress.__call__)


def dump(path: Path) -> None:
    with path.open("wb") as out:
        for t0, events in list(_traces):
            rows = sorted(((n, (a - t0) / 1e6, (b - a) / 1e6) for n, a, b in events), key=lambda r: r[1])
            out.write(orjson.dumps({"t0": t0, "events": [[n, round(s, 3), round(d, 3)] for n, s, d in rows]}) + b"\n")
