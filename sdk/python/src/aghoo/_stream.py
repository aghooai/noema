"""Turn streams (sync and async): SSE events, client-side reconciliation, final text, decision and explicit abort."""

from __future__ import annotations

import json
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from types import TracebackType
from typing import Any, Self

import anyio
import httpx

from ._decision_types import DecisionResult
from ._errors import AghooConnectionError
from ._events import (
    DecisionCompleted,
    DecisionUpdated,
    GenerationCommitted,
    GenerationDelta,
    GenerationRerouted,
    TurnCompleted,
    TurnCreated,
    TurnEvent,
    parse_event,
)
from ._models import CancelResult
from ._sse import SseMessage
from ._transport import OpenStopped


class TurnAccumulator:
    """Client obligations of 03 §3.2: only the committed attempt's deltas are text; `generation.rerouted` drops the
    text from `discard_from_seq` onward and continues with the new attempt; `decision.updated` replaces the decision;
    `turn.completed` is final."""

    __slots__ = ("_deltas", "_final", "_reroutes", "completed", "decision", "turn_id")

    def __init__(self, turn_id: str | None = None) -> None:
        self.turn_id = turn_id
        self.decision: DecisionResult | None = None
        self.completed = False
        self._deltas: dict[str, list[tuple[int, str]]] = {}
        self._reroutes: dict[str, tuple[str, int | None]] = {}
        self._final: str | None = None

    def apply(self, ev: TurnEvent) -> None:
        if isinstance(ev, GenerationDelta):
            self._deltas.setdefault(ev.attempt_id, []).append((ev.seq, ev.text))
        elif isinstance(ev, TurnCreated):
            self.turn_id = ev.turn_id
        elif isinstance(ev, DecisionCompleted | DecisionUpdated):
            self.decision = ev.decision
        elif isinstance(ev, GenerationCommitted):
            self._final = ev.attempt_id
        elif isinstance(ev, GenerationRerouted):
            self._reroutes[ev.to_attempt_id] = (ev.from_attempt_id, ev.discard_from_seq)
            self._final = ev.to_attempt_id
        elif isinstance(ev, TurnCompleted):
            self.completed = True

    @property
    def text(self) -> str:
        """Committed text only: deltas of discarded attempts are excluded."""
        return "" if self._final is None else self._chain(self._final, None, set())

    def _chain(self, attempt: str, before: int | None, seen: set[str]) -> str:
        if attempt in seen:
            return ""
        seen.add(attempt)
        reroute = self._reroutes.get(attempt)
        prefix = "" if reroute is None else self._chain(reroute[0], reroute[1], seen)
        own = "".join(t for s, t in self._deltas.get(attempt, ()) if before is None or s < before)
        return prefix + own


def event_json(data: str) -> Any:
    """Decoded SSE `data`; a frame that is not JSON is a broken stream."""
    try:
        return json.loads(data)
    except ValueError as exc:
        raise AghooConnectionError("malformed SSE event data") from exc


def _event(message: SseMessage) -> TurnEvent:
    return parse_event(event_json(message.data))


class TurnStream:
    """Iterator over a turn's events. The request starts on `__enter__` or the first event (retries happen only before
    the first event); use it as a context manager so the connection is closed. Leaving the stream early is a
    disconnect, not a cancel: call `abort()` to cancel the turn. SSE `error` events are yielded, not raised.
    `abort()` may be called from another thread while the stream is opening."""

    def __init__(
        self,
        open_: Callable[[threading.Event], tuple[httpx.Response, Iterator[SseMessage]]],
        cancel: Callable[[str], CancelResult],
        turn_id: str | None = None,
    ) -> None:
        self._open = open_
        self._cancel = cancel
        self._acc = TurnAccumulator(turn_id)
        self._response: httpx.Response | None = None
        self._messages: Iterator[SseMessage] | None = None
        self._closed = False
        self._opening = False
        self._stop = threading.Event()  # set by abort() before the turn id is known: the open starts no new attempt
        self._abort_requested = False
        self._unopened_abort = False
        self._peeked: TurnEvent | None = None
        self._cancel_lock = threading.Lock()
        self._cancel_sent = False
        self._cancelled: CancelResult | None = None
        self.last_event_id: str | None = None
        """The SSE id of the last event received (send it as `last_event_id` to resume, Phase 2)."""

    @property
    def turn_id(self) -> str | None:
        return self._acc.turn_id

    def __enter__(self) -> Self:
        if not self._closed:
            self._ensure_open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> TurnEvent:
        if self._acc.completed or self._closed:
            raise StopIteration
        messages = self._ensure_open()
        if messages is None:
            raise StopIteration  # aborted before any turn existed
        try:
            if self._peeked is not None:
                ev, self._peeked = self._peeked, None
            else:
                ev = self._read(messages)
            if isinstance(ev, TurnCreated) and self._abort_requested:
                self._send_cancel()
        except BaseException:
            self.close()
            raise
        if isinstance(ev, TurnCompleted):
            self.close()  # turn.completed is always the last event
        return ev

    def _read(self, messages: Iterator[SseMessage]) -> TurnEvent:
        message = next(messages, None)
        if message is None:
            raise AghooConnectionError("stream ended before turn.completed")
        self.last_event_id = message.id
        ev = _event(message)
        self._acc.apply(ev)
        return ev

    def final_text(self) -> str:
        """Consumes the rest of the stream; the committed text (partial if the turn was cancelled or failed)."""
        self._drain()
        return self._acc.text

    def decision(self) -> DecisionResult | None:
        """Consumes the rest of the stream; the final decision (including late `decision.updated` merges), or None."""
        self._drain()
        return self._acc.decision

    def abort(self) -> CancelResult | None:
        """Explicit, final abort: POST /v1/turns/{id}/cancel (the turn id comes with the first event, read when the
        stream opens). The stream then ends normally with `turn.completed`; keep reading it. While the stream is
        opening (e.g. from another thread), the open starts no new attempt and its retry backoff ends; an attempt in
        flight is cancelled as soon as its turn.created arrives, and with no turn the stream ends empty. On a stream
        never opened nothing is sent and the stream is closed. Returns the cancel answer, or None when this call did
        not send it (no turn yet, or another thread is sending it)."""
        if self._acc.completed:
            return self._cancelled  # nothing left to cancel
        self._abort_requested = True
        if self._acc.turn_id is None:
            self._stop.set()  # an open in progress (or about to start) sends no new attempt
            if not self._opening and self._messages is None and not self._closed:
                self._unopened_abort = True  # nothing was started
                self.close()
            return None
        self._send_cancel()
        return self._cancelled

    def close(self) -> None:
        self._closed = True
        if self._response is not None:
            self._response.close()

    def _send_cancel(self) -> None:
        turn_id = self._acc.turn_id
        with self._cancel_lock:
            if self._cancel_sent or turn_id is None:
                return
            self._cancel_sent = True
        try:
            self._cancelled = self._cancel(turn_id)
        except BaseException:
            self._cancel_sent = False  # a later abort() may try again
            raise

    def _ensure_open(self) -> Iterator[SseMessage] | None:
        """The open stream (None when abort() stopped the open before any turn existed)."""
        if self._messages is None:
            self._opening = True
            try:
                self._response, self._messages = self._open(self._stop)
                self._peeked = self._read(self._messages)  # received inside the retry loop: turn.created
                if self._abort_requested:
                    self._send_cancel()  # aborted while the open was in flight: cancel the turn it created
            except OpenStopped:
                self._unopened_abort = True
                self.close()
                return None
            except BaseException:
                self.close()
                raise
            finally:
                self._opening = False
        return self._messages

    def _drain(self) -> None:
        for _ in self:
            pass
        if not self._acc.completed and not self._unopened_abort:
            raise AghooConnectionError("stream ended before turn.completed")


class AsyncTurnStream:
    """Async counterpart of TurnStream: `async with client.turns.stream(...) as s: async for ev in s: ...`.
    `abort()` may be awaited from another task while the stream is opening."""

    def __init__(
        self,
        open_: Callable[[anyio.Event], Awaitable[tuple[httpx.Response, AsyncIterator[SseMessage]]]],
        cancel: Callable[[str], Awaitable[CancelResult]],
        turn_id: str | None = None,
    ) -> None:
        self._open = open_
        self._cancel = cancel
        self._acc = TurnAccumulator(turn_id)
        self._response: httpx.Response | None = None
        self._messages: AsyncIterator[SseMessage] | None = None
        self._closed = False
        self._stop: anyio.Event | None = None  # exists while opening (created inside the event loop)
        self._abort_requested = False
        self._unopened_abort = False
        self._peeked: TurnEvent | None = None
        self._cancel_sent = False
        self._cancelled: CancelResult | None = None
        self.last_event_id: str | None = None

    @property
    def turn_id(self) -> str | None:
        return self._acc.turn_id

    async def __aenter__(self) -> Self:
        if not self._closed:
            await self._ensure_open()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> TurnEvent:
        if self._acc.completed or self._closed:
            raise StopAsyncIteration
        messages = await self._ensure_open()
        if messages is None:
            raise StopAsyncIteration  # aborted before any turn existed
        try:
            if self._peeked is not None:
                ev, self._peeked = self._peeked, None
            else:
                ev = await self._read(messages)
            if isinstance(ev, TurnCreated) and self._abort_requested:
                await self._send_cancel()
        except BaseException:
            await self.aclose()
            raise
        if isinstance(ev, TurnCompleted):
            await self.aclose()  # turn.completed is always the last event
        return ev

    async def _read(self, messages: AsyncIterator[SseMessage]) -> TurnEvent:
        message = await anext(messages, None)
        if message is None:
            raise AghooConnectionError("stream ended before turn.completed")
        self.last_event_id = message.id
        ev = _event(message)
        self._acc.apply(ev)
        return ev

    async def final_text(self) -> str:
        await self._drain()
        return self._acc.text

    async def decision(self) -> DecisionResult | None:
        await self._drain()
        return self._acc.decision

    async def abort(self) -> CancelResult | None:
        """See TurnStream.abort; from another task while opening, the open starts no new attempt."""
        if self._acc.completed:
            return self._cancelled  # nothing left to cancel
        self._abort_requested = True
        if self._acc.turn_id is None:
            if self._stop is not None:
                self._stop.set()  # opening: no new attempt; an attempt in flight is cancelled at turn.created
            elif self._messages is None and not self._closed:
                self._unopened_abort = True  # nothing was started
                await self.aclose()
            return None
        await self._send_cancel()
        return self._cancelled

    async def aclose(self) -> None:
        self._closed = True
        if self._response is not None:
            await self._response.aclose()

    async def _send_cancel(self) -> None:
        turn_id = self._acc.turn_id
        if self._cancel_sent or turn_id is None:
            return
        self._cancel_sent = True  # before the await: a concurrent abort() does not send a second cancel
        try:
            self._cancelled = await self._cancel(turn_id)
        except BaseException:
            self._cancel_sent = False
            raise

    async def _ensure_open(self) -> AsyncIterator[SseMessage] | None:
        if self._messages is None:
            self._stop = anyio.Event()
            try:
                self._response, self._messages = await self._open(self._stop)
                self._peeked = await self._read(self._messages)
                if self._abort_requested:
                    await self._send_cancel()  # aborted while the open was in flight
            except OpenStopped:
                self._unopened_abort = True
                await self.aclose()
                return None
            except BaseException:
                await self.aclose()
                raise
            finally:
                self._stop = None
        return self._messages

    async def _drain(self) -> None:
        async for _ in self:
            pass
        if not self._acc.completed and not self._unopened_abort:
            raise AghooConnectionError("stream ended before turn.completed")
