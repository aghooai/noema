"""HTTP transport shared by the sync and async clients: headers, retries, timeouts and SSE opening.

Retries (IMPL-035) cover connect/read failures, timeouts, statuses whose envelope says `retryable`, and a stream that
ends before its first event; one Idempotency-Key is reused across the retries of a POST. An error the server replayed
from its idempotency store (`x-aghoo-idempotent-replay: true`) is raised at once: the same key can only replay it.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import IO, Any, Final

import anyio
import httpx

from . import _retry
from ._errors import AghooConnectionError, AghooError, AghooTimeoutError, error_from_response
from ._params import RequestOptions
from ._sse import SseMessage, SseParser

API_VERSION: Final = "2026-10-01"
"""The `Aghoo-Version` this SDK was built against (the contract's only supported version)."""
DEFAULT_BASE_URL: Final = "https://api.aghoo.example"
"""The contract's `servers[0].url`; pass `base_url` (or set AGHOO_BASE_URL) for a real deployment."""
DEFAULT_TIMEOUT_S: Final = 600.0
"""Equals the largest `Aghoo-Deadline-Ms` the API accepts."""

REPLAY_HEADER: Final = "x-aghoo-idempotent-replay"

Query = Mapping[str, str | int | bool | None]


class OpenStopped(Exception):
    """A stream's open was stopped by `abort()` before a new attempt: no request of it reached a 2xx stream."""


@dataclass(frozen=True, slots=True)
class Call:
    """One logical API call."""

    method: str
    path: str
    query: Query | None = None
    json: Any = None
    files: Mapping[str, tuple[str, bytes | IO[bytes], str]] | None = None
    data: Mapping[str, str] | None = None
    idempotent: bool = False  # the operation declares Idempotency-Key
    accept: str = "application/json"
    headers: Mapping[str, str] = field(default_factory=dict)


class Config:
    """Client-level settings: API key, base URL, default timeout and retries."""

    def __init__(self, api_key: str | None, base_url: str | None, timeout: float, max_retries: int) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("AGHOO_API_KEY")
        self.base_url = (base_url or os.environ.get("AGHOO_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries

    def build(
        self, http: httpx.Client | httpx.AsyncClient, call: Call, opts: RequestOptions, key: str | None
    ) -> httpx.Request:
        headers = {"Aghoo-Version": API_VERSION, "Accept": call.accept}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if key is not None:
            headers["Idempotency-Key"] = key
        content: bytes | None = None
        if call.json is not None:
            headers["Content-Type"] = "application/json"
            content = json.dumps(call.json, separators=(",", ":"), ensure_ascii=False).encode()
        headers.update(call.headers)
        params = {
            k: str(v).lower() if isinstance(v, bool) else v for k, v in (call.query or {}).items() if v is not None
        }
        return http.build_request(
            call.method,
            self.base_url + call.path,
            params=params,
            headers=headers,
            content=content,
            files=call.files,
            data=call.data,
            timeout=opts.get("timeout", self.timeout),
        )

    @staticmethod
    def key(call: Call, opts: RequestOptions) -> str | None:
        """One Idempotency-Key per logical POST call (given or UUIDv4), reused by every retry."""
        return (opts.get("idempotency_key") or str(uuid.uuid4())) if call.idempotent else None

    def retries(self, opts: RequestOptions) -> int:
        return opts.get("max_retries", self.max_retries)


def _failure(exc: Exception, timeout: float | None) -> AghooConnectionError:
    if isinstance(exc, AghooConnectionError):
        return exc
    if isinstance(exc, httpx.TimeoutException):
        return AghooTimeoutError(f"request timed out after {timeout} s")
    return AghooConnectionError(f"request failed: {exc}")


def _delay(
    failure: AghooError | AghooConnectionError, attempt: int, max_retries: int, replayed: bool = False
) -> float | None:
    if attempt >= max_retries or replayed:
        return None
    if isinstance(failure, AghooError):
        return _retry.delay_s(attempt, failure.retry_after_s) if failure.retryable else None
    return _retry.delay_s(attempt, None)


def _replayed(response: httpx.Response) -> bool:
    value: str | None = response.headers.get(REPLAY_HEADER)
    return value == "true"


def _decode(response: httpx.Response) -> Any:
    try:
        return json.loads(response.content)
    except ValueError as exc:
        raise AghooConnectionError("response body is not valid JSON") from exc


def _first(messages: Iterator[SseMessage]) -> SseMessage:
    for message in messages:
        return message
    raise AghooConnectionError("stream ended before its first event")


def iter_sse(chunks: Iterator[bytes]) -> Iterator[SseMessage]:
    parser = SseParser()
    for chunk in chunks:
        yield from parser.push(chunk)
    yield from parser.end()


async def aiter_sse(chunks: AsyncIterator[bytes]) -> AsyncIterator[SseMessage]:
    parser = SseParser()
    async for chunk in chunks:
        for message in parser.push(chunk):
            yield message
    for message in parser.end():
        yield message


def _guard(messages: Iterator[SseMessage]) -> Iterator[SseMessage]:
    try:
        yield from messages
    except httpx.TransportError as exc:
        raise AghooConnectionError(f"stream interrupted: {exc}") from exc


async def _aguard(messages: AsyncIterator[SseMessage]) -> AsyncIterator[SseMessage]:
    try:
        async for message in messages:
            yield message
    except httpx.TransportError as exc:
        raise AghooConnectionError(f"stream interrupted: {exc}") from exc


class SyncTransport:
    """Blocking transport over an httpx.Client."""

    def __init__(self, cfg: Config, http: httpx.Client) -> None:
        self.cfg = cfg
        self.http = http

    def response(self, call: Call, opts: RequestOptions) -> httpx.Response:
        """The 2xx response of one logical call (body read), after retries; non-2xx raises AghooError."""
        key, max_retries = Config.key(call, opts), self.cfg.retries(opts)
        for attempt in range(max_retries + 1):
            request = self.cfg.build(self.http, call, opts, key)
            failure: AghooError | AghooConnectionError
            replayed = False
            try:
                response = self.http.send(request)
            except httpx.TransportError as exc:
                failure = _failure(exc, opts.get("timeout", self.cfg.timeout))
            else:
                if response.is_success:
                    return response
                replayed = _replayed(response)
                failure = error_from_response(response.status_code, response.headers, response.content)
            delay = _delay(failure, attempt, max_retries, replayed)
            if delay is None:
                raise failure
            _retry.sleep(delay, None)
        raise AssertionError("unreachable")  # pragma: no cover

    def json(self, call: Call, opts: RequestOptions) -> Any:
        return _decode(self.response(call, opts))

    def sse(
        self, call: Call, opts: RequestOptions, stop: threading.Event | None = None
    ) -> tuple[httpx.Response, Iterator[SseMessage]]:
        """Opens an SSE response; the first event is read inside the retry loop. The caller closes the response.
        Once `stop` is set, the backoff ends and no new attempt starts (OpenStopped); an attempt in flight continues."""
        key, max_retries = Config.key(call, opts), self.cfg.retries(opts)
        call = replace(call, accept="text/event-stream")
        for attempt in range(max_retries + 1):
            if stop is not None and stop.is_set():
                raise OpenStopped
            request = self.cfg.build(self.http, call, opts, key)
            failure: AghooError | AghooConnectionError
            response: httpx.Response | None = None
            replayed = False
            try:
                response = self.http.send(request, stream=True)
                if response.is_success:
                    messages = iter_sse(response.iter_bytes())
                    first = _first(messages)
                    return response, _guard(_prepend(first, messages))
                response.read()
                replayed = _replayed(response)
                failure = error_from_response(response.status_code, response.headers, response.content)
            except (httpx.TransportError, AghooConnectionError) as exc:
                failure = _failure(exc, opts.get("timeout", self.cfg.timeout))
            except BaseException:
                if response is not None:
                    response.close()
                raise
            if response is not None:
                response.close()
            delay = _delay(failure, attempt, max_retries, replayed)
            if delay is None:
                raise failure
            _retry.sleep(delay, stop)
        raise AssertionError("unreachable")  # pragma: no cover


class AsyncTransport:
    """Asynchronous transport over an httpx.AsyncClient (asyncio or trio via anyio)."""

    def __init__(self, cfg: Config, http: httpx.AsyncClient) -> None:
        self.cfg = cfg
        self.http = http

    async def response(self, call: Call, opts: RequestOptions) -> httpx.Response:
        key, max_retries = Config.key(call, opts), self.cfg.retries(opts)
        for attempt in range(max_retries + 1):
            request = self.cfg.build(self.http, call, opts, key)
            failure: AghooError | AghooConnectionError
            replayed = False
            try:
                response = await self.http.send(request)
            except httpx.TransportError as exc:
                failure = _failure(exc, opts.get("timeout", self.cfg.timeout))
            else:
                if response.is_success:
                    return response
                replayed = _replayed(response)
                failure = error_from_response(response.status_code, response.headers, response.content)
            delay = _delay(failure, attempt, max_retries, replayed)
            if delay is None:
                raise failure
            await _retry.async_sleep(delay, None)
        raise AssertionError("unreachable")  # pragma: no cover

    async def json(self, call: Call, opts: RequestOptions) -> Any:
        return _decode(await self.response(call, opts))

    async def sse(
        self, call: Call, opts: RequestOptions, stop: anyio.Event | None = None
    ) -> tuple[httpx.Response, AsyncIterator[SseMessage]]:
        """See SyncTransport.sse; a cancellation while opening closes the response (shielded)."""
        key, max_retries = Config.key(call, opts), self.cfg.retries(opts)
        call = replace(call, accept="text/event-stream")
        for attempt in range(max_retries + 1):
            if stop is not None and stop.is_set():
                raise OpenStopped
            request = self.cfg.build(self.http, call, opts, key)
            failure: AghooError | AghooConnectionError
            response: httpx.Response | None = None
            replayed = False
            try:
                response = await self.http.send(request, stream=True)
                if response.is_success:
                    messages = aiter_sse(response.aiter_bytes())
                    first = await _afirst(messages)
                    return response, _aguard(_aprepend(first, messages))
                await response.aread()
                replayed = _replayed(response)
                failure = error_from_response(response.status_code, response.headers, response.content)
            except (httpx.TransportError, AghooConnectionError) as exc:
                failure = _failure(exc, opts.get("timeout", self.cfg.timeout))
            except BaseException:
                if response is not None:
                    with anyio.CancelScope(shield=True):
                        await response.aclose()
                raise
            if response is not None:
                await response.aclose()
            delay = _delay(failure, attempt, max_retries, replayed)
            if delay is None:
                raise failure
            await _retry.async_sleep(delay, stop)
        raise AssertionError("unreachable")  # pragma: no cover


def _prepend(first: SseMessage, rest: Iterator[SseMessage]) -> Iterator[SseMessage]:
    yield first
    yield from rest


async def _aprepend(first: SseMessage, rest: AsyncIterator[SseMessage]) -> AsyncIterator[SseMessage]:
    yield first
    async for message in rest:
        yield message


async def _afirst(messages: AsyncIterator[SseMessage]) -> SseMessage:
    async for message in messages:
        return message
    raise AghooConnectionError("stream ended before its first event")
