"""`bedrock-converse`: Bedrock Converse / ConverseStream over the lean unary path (spec-02 §4.2, ADR-001 §4).

Both operations send the request aiobotocore would build (`noema.adapters._converse_wire`: botocore's endpoint
rules, the rest-json body of `wire.converse_params`, `botocore.auth.SigV4Auth`), signed with the credentials of the
call's own `broker.get` + `assert_creds_for` (IMPL-076; so as fresh as the broker's, whose soft/hard refresh windows
are ADR-001 §4's 600/300 s), through one pooled aiohttp client per (tenant, account, region)
(`noema.adapters._unary`). Nothing ambient shapes a tenant call: no default credential chain, `AWS_PROFILE`, shared
config, configured endpoint URL, proxy or `AWS_BEARER_TOKEN_BEDROCK` (SEC-STS-15); the only retry is the adapter's
own connect retry. Converse responses are parsed as botocore parses them; ConverseStream bodies are decoded as
botocore's `EventStreamBuffer` decodes them (`noema.adapters._eventstream`, Wave E3) and each message parsed as
aiobotocore's event stream parses it (E sprint A: the aiobotocore client cost ~0.7 ms of CPU per member call and
~50 µs per stream event).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any, Final

import anyio
import anyio.to_thread
from aiobotocore.session import AioSession

from noema.adapters._base import AdapterBase, ClientPool, SecurityEventSink, endpoint
from noema.adapters._converse_wire import (
    ConverseModel,
    check_params,
    converse_body,
    converse_url,
    malformed_response,
    parse_event,
    parse_response,
    signed_headers,
)
from noema.adapters._eventstream import EventStreamDecoder
from noema.adapters._stream import PreparedCall, RawStream, StreamSession
from noema.adapters._unary import AiohttpUnary, RawResponse, StreamedResponse, StreamTransport, tls_context
from noema.adapters.classify import MSG_MALFORMED, StreamFault, stream_error, timeout_error
from noema.adapters.wire import CONVERSE_REFUSALS, CONVERSE_STOP, converse_params, converse_usage
from noema.core.clock import Clock, Deadline, deadline_scope
from noema.core.principal import TenantRef
from noema.core.protocols.adapters import (
    AdapterError,
    ErrorEvent,
    MessageStart,
    ModelRequest,
    ModelResponse,
    ReasoningDelta,
    Stop,
    StreamEvent,
    StreamTimeouts,
    TextDelta,
    UsageEvent,
)
from noema.core.protocols.config import ConfigRegistry
from noema.core.protocols.credentials import CredentialBroker, TenantCredentials
from noema.core.protocols.ops import RetryBudget
from noema.core.protocols.telemetry import Telemetry
from noema.core.values import UsageRecord

SERVICE: Final = "bedrock-runtime"
CONNECT_TIMEOUT_S: Final = 3.0  # transport connect timeout (ROUTE_TIMEOUTS_V1 connect_s)
MAX_POOL_CONNECTIONS: Final = 512
_UNSET: Final = (None, None, None, None)
# The data loader's session ignores AWS_PROFILE and every shared config/credentials file.
_ISOLATED_SESSION_VARS: Final = {
    "profile": _UNSET,
    "config_file": (None, None, os.devnull, None),
    "credentials_file": (None, None, os.devnull, None),
}

TransportFactory = Callable[[TenantCredentials], StreamTransport]
# botocore data the lean path needs: parsed once into one loader (`ConverseAdapter.prewarm`).
_SERVICE_MODELS: Final = ("service-2", "endpoint-rule-set-1")
_DATA: Final = ("endpoints", "partitions", "_retry", "sdk-default-configuration")


def _isolated_session() -> AioSession:
    """A session that reads nothing ambient (no profile, no shared config or credentials files)."""
    return AioSession(session_vars=dict(_ISOLATED_SESSION_VARS))


def _loaded_data_loader() -> Any:
    """A botocore data loader with the bedrock-runtime service model, endpoint rules and partitions (sync; run in a
    worker thread)."""
    loader = _isolated_session().get_component("data_loader")
    for type_name in _SERVICE_MODELS:
        loader.load_service_model(SERVICE, type_name)
    for name in _DATA:
        loader.load_data(name)
    return loader


class _ConverseRaw:
    """A ConverseStream event stream (`RawStream`): the body's eventstream messages, each parsed as aiobotocore's
    event stream parses it (`parse_event`; events of a type the model does not know are skipped): `messageStart`,
    `contentBlockDelta.delta.text` / `.reasoningContent.text` (counted, never forwarded), `messageStop.stopReason`,
    `metadata.usage` (the last event). An exception or error message raises botocore's `EventStreamError` from
    `next()`, a corrupt message botocore's `ParserError` (`EventStreamDecoder`), a transport failure aiohttp's
    exception."""

    usage_ends_stream = True

    def __init__(
        self,
        response: StreamedResponse,
        model: ConverseModel,
        clock: Clock,
        on_usage: Callable[[UsageRecord], None],
    ) -> None:
        self._response = response
        self._model = model
        self._decoder = EventStreamDecoder()
        self._clock = clock
        self._on_usage = on_usage
        self._reasoning_chars = 0
        self._text_chars = 0
        self._hidden = False  # reasoning Bedrock hid (redactedContent, a signature without reasoning text)

    async def next(self) -> object | None:
        decoder = self._decoder
        while True:
            while (message := decoder.next()) is not None:  # the complete messages already received
                event = parse_event(message[0], message[1], self._model)
                if event:
                    return event
            data = await self._response.chunk()
            if not data:
                return None  # the end of the body (a partial message is dropped, as aiobotocore drops it)
            decoder.add_data(data)

    def feed(self, raw: object) -> StreamEvent | None:
        if not isinstance(raw, dict):
            return None
        block = raw.get("contentBlockDelta")
        if block is not None:
            delta = block.get("delta") or {}
            text = delta.get("text")
            reasoning = delta.get("reasoningContent")
            if not isinstance(text, str | None) or not isinstance((reasoning or {}).get("text"), str | None):
                raise StreamFault(stream_error("malformed:chunk", MSG_MALFORMED))  # F8: non-string content
            if text:
                self._text_chars += len(text)
                return TextDelta(text)
            if reasoning:
                chars = len(reasoning.get("text") or "")
                if chars:
                    self._reasoning_chars += chars
                    return ReasoningDelta(chars)
                if "redactedContent" in reasoning or (reasoning.get("signature") and not self._reasoning_chars):
                    self._hidden = True
            return None
        if "messageStart" in raw:
            return MessageStart(self._clock.monotonic())
        stop = raw.get("messageStop")
        if stop is not None:
            return Stop(CONVERSE_STOP.get(str(stop.get("stopReason")), "other"))
        metadata = raw.get("metadata")
        if metadata is not None and isinstance(metadata.get("usage"), dict):
            hidden = self._text_chars if self._hidden else None
            usage = converse_usage(metadata["usage"], self._reasoning_chars, hidden_text_chars=hidden)
            self._on_usage(usage)
            return UsageEvent(usage)
        return None

    def finish(self, stopped: bool) -> StreamEvent | None:
        return None if stopped else ErrorEvent(stream_error("malformed:truncated", MSG_MALFORMED))

    async def aclose(self) -> None:
        self._response.close()  # closes the HTTP response without reading further


def _response_of(resp: Mapping[str, Any], region: str, latency_ms: int) -> ModelResponse:
    content = ((resp.get("output") or {}).get("message") or {}).get("content") or []
    texts: list[str] = []
    reasoning_chars = 0
    hidden = False  # reasoning Bedrock hid (redactedContent, a signature without reasoning text)
    for block in content:
        if "text" in block:
            texts.append(block["text"])
        elif "reasoningContent" in block:
            chars = len(((block["reasoningContent"] or {}).get("reasoningText") or {}).get("text") or "")
            reasoning_chars += chars
            hidden = hidden or not chars
    stop_raw = str(resp.get("stopReason") or "end_turn")
    return ModelResponse(
        text="".join(texts),
        logprobs=None,
        refused=stop_raw in CONVERSE_REFUSALS,
        stop_reason=CONVERSE_STOP.get(stop_raw, "other"),
        usage=converse_usage(
            resp.get("usage") or {}, reasoning_chars, hidden_text_chars=sum(map(len, texts)) if hidden else None
        ),
        latency_ms=latency_ms,
        region=region,
    )


class ConverseAdapter(AdapterBase):
    """Implements `Adapter` for `models.<alias>.adapter == "bedrock-converse"` (see the module docstring).

    `endpoint_overrides`: the validated `NOEMA_AWS_ENDPOINT_OVERRIDES` (key `bedrock-runtime`, dev/test only).
    `unary_transport`: builds the transport of a pool key (tests: the wire emulator); default one `AiohttpUnary`
    per (tenant, account, region). `aclose()` closes the pooled clients (lifespan shutdown)."""

    kind = "bedrock-converse"

    def __init__(
        self,
        *,
        config: ConfigRegistry,
        broker: CredentialBroker,
        clock: Clock,
        retry_budget: RetryBudget,
        telemetry: Telemetry | None = None,
        security: SecurityEventSink | None = None,
        endpoint_overrides: Mapping[str, str] | None = None,
        max_pool_connections: int = MAX_POOL_CONNECTIONS,
        unary_transport: TransportFactory | None = None,
    ) -> None:
        super().__init__(
            config=config,
            broker=broker,
            clock=clock,
            retry_budget=retry_budget,
            telemetry=telemetry,
            security=security,
        )
        self._endpoint = endpoint(endpoint_overrides or {}, SERVICE)
        self._max_connections = max_pool_connections
        self._factory = unary_transport
        self._loader: Any = None
        self._wire: ConverseModel | None = None
        self._tls: Any = None
        self._pool: ClientPool[StreamTransport] = ClientPool(self._make_client)

    async def prewarm(self) -> None:
        """Parse botocore's service model, endpoint rules and partitions (the lean path's `ConverseModel`) and
        build the TLS context the pooled clients share, in worker threads (about 50 ms of JSON parsing and 5-20 ms
        of CA bundle loading that would otherwise block the event loop on the first call). Idempotent; the first
        client calls it too, so calling it from the lifespan start only moves that cost out of the first request."""
        if self._loader is None:
            self._loader = await anyio.to_thread.run_sync(_loaded_data_loader)
        if self._wire is None:
            self._wire = await anyio.to_thread.run_sync(ConverseModel, self._loader)
        if self._tls is None and self._factory is None:
            self._tls = await anyio.to_thread.run_sync(tls_context)

    async def _make_client(self, creds: TenantCredentials) -> tuple[StreamTransport, Callable[[], Awaitable[None]]]:
        await self.prewarm()
        if self._factory is not None:
            transport = self._factory(creds)
        else:
            transport = AiohttpUnary(self._tls, max_connections=self._max_connections)
        return transport, transport.aclose

    def _evict(self, tenant_id: str) -> None:
        self._pool.evict(tenant_id)

    async def aclose(self) -> None:
        """Close every pooled client."""
        await self._pool.aclose()

    async def complete(self, req: ModelRequest, *, tenant: TenantRef, deadline: Deadline) -> ModelResponse:
        """Converse over the lean unary path (see the module docstring). Raises `AdapterError`; the deadline bounds
        the whole call, credentials and client included (`timeout:deadline`, and no request is sent once it has
        passed)."""
        model = self._resolve(req)
        async with deadline_scope(self._clock, deadline.at) as scope:
            creds = await self._credentials(tenant)
            params = converse_params(req, model)
            entry = await self._pool.acquire(creds)
            wire = self._wire
            assert wire is not None  # the pool's factory prewarmed

            def send() -> Awaitable[RawResponse]:  # what botocore runs inside `converse()`: validate, build, sign
                check_params(params)
                body = converse_body(params, media=media)
                return entry.client.post(url, signed_headers(creds, url, body), body, connect_s=CONNECT_TIMEOUT_S)

            try:
                url = converse_url(self._endpoint or wire.endpoint(creds.region), model.id)
                media = any("text" not in b for m in params["messages"] for b in m["content"])
                started = self._clock.monotonic()
                raw = await self._send(req, tenant, model.id, send, deadline_at=deadline.at)
                resp = parse_response(raw, wire)
            except AdapterError:
                raise
            except Exception as exc:
                raise AdapterError(self._fail(exc, tenant, model.id)) from None
            finally:
                await self._pool.release(entry)
        if scope.cancelled_caught:
            raise AdapterError(timeout_error("deadline"))
        try:
            response = _response_of(resp, creds.region, round((self._clock.monotonic() - started) * 1000))
        except (AttributeError, KeyError, TypeError, ValueError):  # fields that are not what Converse documents
            raise AdapterError(self._fail(malformed_response(), tenant, model.id)) from None
        self._observe_usage(model.id, response.usage)
        return response

    def stream(
        self, req: ModelRequest, *, tenant: TenantRef, timeouts: StreamTimeouts, deadline: Deadline
    ) -> AbstractAsyncContextManager[AsyncIterator[StreamEvent]]:
        """ConverseStream (spec-02 §4.1 stream semantics, see `noema.adapters._stream`)."""
        return StreamSession(
            lambda: self._prepare_stream(req, tenant, deadline), clock=self._clock, timeouts=timeouts, deadline=deadline
        )

    async def _prepare_stream(self, req: ModelRequest, tenant: TenantRef, deadline: Deadline) -> PreparedCall:
        model = self._resolve(req)
        creds = await self._credentials(tenant)
        params = converse_params(req, model)
        entry = await self._pool.acquire(creds)
        self._open_streams += 1
        wire = self._wire
        assert wire is not None  # the pool's factory prewarmed

        def on_usage(usage: UsageRecord) -> None:
            self._observe_usage(model.id, usage)

        async def open_once(url: str) -> StreamedResponse:  # what botocore runs inside `converse_stream()`
            check_params(params)
            body = converse_body(params, media=any("text" not in b for m in params["messages"] for b in m["content"]))
            response = await entry.client.open(url, signed_headers(creds, url, body), body, connect_s=CONNECT_TIMEOUT_S)
            if response.status >= 300:  # botocore reads the error body and raises its ClientError
                try:
                    parse_response(RawResponse(response.status, response.headers, await response.read()), wire)
                finally:
                    response.close()
            return response

        async def open_stream() -> RawStream:
            url = converse_url(self._endpoint or wire.endpoint(creds.region), model.id, stream=True)
            response = await self._send(req, tenant, model.id, lambda: open_once(url), deadline_at=deadline.at)
            return _ConverseRaw(response, wire, self._clock, on_usage)

        async def release() -> None:
            self._open_streams -= 1
            await self._pool.release(entry)

        return PreparedCall(open_stream, lambda exc: self._fail(exc, tenant, model.id), release)
