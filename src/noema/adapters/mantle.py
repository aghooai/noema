"""`bedrock-mantle-chat`: Bedrock Mantle's OpenAI-compatible chat completions (spec-02 §4.2, ADR-001 §6).

`POST https://bedrock-mantle.{home_region}.api.aws/v1/chat/completions` (or the dev/test `mantle` endpoint
override). `stream: false` calls (decision members) take the lean unary path (a pooled aiohttp client per
(tenant, account, region), `noema.adapters._unary`); streams use httpx. Auth per call from the same `broker.get`
result as the account assertion: `Authorization: Bearer` with the broker's short-term token
(`MantleAuth.mode == "bearer"`, the Phase 1 default) or SigV4 with the tenant's credentials (`"sigv4"`; signing
name `bedrock`, UNVERIFIED until the live smoke L3). No fallback between the modes (ADR-001, rejected
alternatives). Non-streaming calls (`stream: false`) carry decision members' `logprobs` / `top_logprobs`; streams
send `stream_options.include_usage` and are read until `data: [DONE]`.
"""

from __future__ import annotations

import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from functools import partial
from typing import Any, Final

import anyio
import anyio.to_thread
import httpx
import orjson
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import ReadOnlyCredentials

from noema.adapters._base import AdapterBase, ClientPool, SecurityEventSink, endpoint
from noema.adapters._stream import PreparedCall, RawStream, StreamSession
from noema.adapters._unary import AiohttpUnary, HttpxUnary, RawResponse, UnaryTransport
from noema.adapters.classify import (
    MSG_MALFORMED,
    ProviderHttpError,
    StreamFault,
    retry_after,
    stream_error,
    timeout_error,
)
from noema.adapters.wire import MANTLE_STOP, mantle_body, mantle_usage
from noema.core.clock import Clock, Deadline, deadline_scope
from noema.core.errors import ClassifiedError, ErrorKind
from noema.core.principal import TenantRef
from noema.core.protocols.adapters import (
    AdapterError,
    ErrorEvent,
    MessageStart,
    ModelRequest,
    ModelResponse,
    ReasoningDelta,
    Stop,
    StopReason,
    StreamEvent,
    StreamTimeouts,
    TextDelta,
    TokenLogprob,
    UsageEvent,
)
from noema.core.protocols.config import ConfigRegistry
from noema.core.protocols.credentials import CredentialBroker, TenantCredentials
from noema.core.protocols.ops import RetryBudget
from noema.core.protocols.telemetry import Telemetry
from noema.core.values import UsageRecord

OVERRIDE_KEY: Final = "mantle"
SIGV4_SERVICE: Final = "bedrock"  # UNVERIFIED Mantle signing name (ADR-001 §6; live smoke L3)
CONNECT_TIMEOUT_S: Final = 3.0
READ_TIMEOUT_S: Final = 300.0
MAX_CONNECTIONS: Final = 512
MAX_KEEPALIVE: Final = 128
_SSE_FIELDS: Final = frozenset({"event", "id", "retry"})


def default_base_url(region: str) -> str:
    """The Mantle OpenAI-compatible base URL of a region."""
    return f"https://bedrock-mantle.{region}.api.aws/v1"


def sigv4_headers(creds: TenantCredentials, url: str, body: bytes, *, service: str = SIGV4_SERVICE) -> dict[str, str]:
    """SigV4 headers for one Mantle request, signed with the tenant's credentials in its home region."""
    token = creds.aws.session_token
    signer = SigV4Auth(
        ReadOnlyCredentials(
            creds.aws.access_key_id,
            creds.aws.secret_access_key.get_secret_value(),
            token.get_secret_value() if token is not None else None,
        ),
        service,
        creds.region,
    )
    request = AWSRequest(method="POST", url=url, data=body, headers={"content-type": "application/json"})
    signer.add_auth(request)
    return {k: str(v) for k, v in request.headers.items()}


def _malformed(code: str) -> StreamFault:
    return StreamFault(stream_error(f"malformed:{code}", MSG_MALFORMED))


def _logprobs(choice: Mapping[str, Any]) -> tuple[TokenLogprob, ...] | None:
    block = choice.get("logprobs")
    entries = block.get("content") if isinstance(block, Mapping) else None
    if not isinstance(entries, list):
        return None
    out: list[TokenLogprob] = []
    for entry in entries:
        tops = entry.get("top_logprobs") or ()
        out.append(
            TokenLogprob(
                token=str(entry["token"]),
                logprob=float(entry["logprob"]),
                top=tuple((str(t["token"]), float(t["logprob"])) for t in tops),
            )
        )
    return tuple(out)


def response_of(data: object, *, logprobs: bool, region: str, latency_ms: int) -> ModelResponse:
    """A non-streaming chat completion -> `ModelResponse` (raises `StreamFault(malformed:response)`)."""
    if not isinstance(data, dict):
        raise _malformed("response")
    try:
        choice = data["choices"][0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
        if not (isinstance(text, str) and isinstance(reasoning, str)):
            raise TypeError("non-string content")
        refusal = message.get("refusal")
        finish = choice.get("finish_reason")
        refused = finish == "content_filter" or bool(refusal)
        stop: StopReason = "content_filter" if refused else MANTLE_STOP.get(str(finish), "other")
        tokens = _logprobs(choice) if logprobs else None
        usage = mantle_usage(data.get("usage") or {}, len(reasoning))
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        raise _malformed("response") from None
    return ModelResponse(
        text=text,
        logprobs=tokens,
        refused=refused,
        stop_reason=stop,
        usage=usage,
        latency_ms=latency_ms,
        region=region,
    )


class _MantleRaw:
    """A chat-completions SSE stream (`RawStream`): `data: <chunk>` lines until `data: [DONE]`. A chunk's
    `delta.reasoning_content` (or `reasoning`) is counted, never forwarded; `delta.refusal` turns the stop into
    `content_filter`; an `{"error": ...}` chunk is a stream exception (retryable); any other non-SSE line, a JSON
    error or a missing `[DONE]` is malformed."""

    usage_ends_stream = False

    def __init__(self, response: httpx.Response, clock: Clock, on_usage: Callable[[UsageRecord], None]) -> None:
        self._response = response
        self._lines = response.aiter_lines().__aiter__()
        self._clock = clock
        self._on_usage = on_usage
        self._started = False
        self._refused = False
        self._done = False
        self._reasoning_chars = 0

    async def next(self) -> object | None:
        while True:
            try:
                line = await self._lines.__anext__()
            except StopAsyncIteration:
                return None
            if not line or line[0] == ":":
                continue  # event separator or comment
            if line.startswith("data:"):
                data = line[5:].strip()
                if data == "[DONE]":
                    self._done = True
                    return None
                chunk: object = orjson.loads(data)
                return chunk
            if line.split(":", 1)[0] not in _SSE_FIELDS:
                raise _malformed("sse_line")

    def feed(self, raw: object) -> StreamEvent | tuple[StreamEvent, ...] | None:
        """Raises `StreamFault(malformed:chunk)` for a chunk of the wrong shape (a choice or delta that is not an
        object, non-string content or reasoning), never a bare TypeError or AttributeError."""
        if not isinstance(raw, dict):
            raise _malformed("chunk")
        error = raw.get("error")
        if error is not None:
            code = error.get("code") or error.get("type") if isinstance(error, dict) else None
            return ErrorEvent(stream_error(str(code or "stream_error")))
        found: list[StreamEvent] = []
        choices = raw.get("choices")
        if choices:
            choice = choices[0] if isinstance(choices, list) else None
            if not isinstance(choice, dict):
                raise _malformed("chunk")
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                raise _malformed("chunk")
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            content = delta.get("content")
            if not all(v is None or isinstance(v, str) for v in (reasoning, content)):
                raise _malformed("chunk")
            if not self._started:
                self._started = True
                found.append(MessageStart(self._clock.monotonic()))
            if reasoning:
                self._reasoning_chars += len(reasoning)
                found.append(ReasoningDelta(len(reasoning)))
            if content:
                found.append(TextDelta(content))
            if delta.get("refusal"):
                self._refused = True
            finish = choice.get("finish_reason")
            if finish:
                found.append(Stop("content_filter" if self._refused else MANTLE_STOP.get(str(finish), "other")))
        usage = raw.get("usage")
        if isinstance(usage, dict) and usage:
            record = mantle_usage(usage, self._reasoning_chars)
            self._on_usage(record)
            found.append(UsageEvent(record))
        if not found:
            return None
        return found[0] if len(found) == 1 else tuple(found)

    def finish(self, stopped: bool) -> StreamEvent | None:
        if stopped:
            return None
        if self._done:  # [DONE] without a finish_reason
            return Stop("content_filter" if self._refused else "other")
        return ErrorEvent(stream_error("malformed:missing_done", MSG_MALFORMED))

    async def aclose(self) -> None:
        with anyio.CancelScope(shield=True):
            await self._response.aclose()  # closes the connection without reading further


class MantleAdapter(AdapterBase):
    """Implements `Adapter` for `models.<alias>.adapter == "bedrock-mantle-chat"` (see the module docstring).

    `endpoint_overrides`: the validated `NOEMA_AWS_ENDPOINT_OVERRIDES` (key `mantle`, a base URL ending in `/v1`;
    dev/test only). `transport`: an httpx transport for every pooled client (tests: the wire emulator).
    `aclose()` closes the pooled clients (lifespan shutdown)."""

    kind = "bedrock-mantle-chat"

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
        transport: httpx.AsyncBaseTransport | None = None,
        max_connections: int = MAX_CONNECTIONS,
        sigv4_service: str = SIGV4_SERVICE,
    ) -> None:
        super().__init__(
            config=config,
            broker=broker,
            clock=clock,
            retry_budget=retry_budget,
            telemetry=telemetry,
            security=security,
        )
        self._base = endpoint(endpoint_overrides or {}, OVERRIDE_KEY)
        self._transport = transport
        self._limits = httpx.Limits(max_connections=max_connections, max_keepalive_connections=MAX_KEEPALIVE)
        self._service = sigv4_service
        self._ssl: ssl.SSLContext | None = None
        self._pool: ClientPool[httpx.AsyncClient] = ClientPool(self._make_client)
        self._max_connections = max_connections
        self._unary: ClientPool[UnaryTransport] = ClientPool(self._make_unary)

    async def prewarm(self) -> None:
        """Build the TLS context every pooled client shares, in a worker thread (loading the CA bundle takes about
        5-20 ms that would otherwise block the event loop on each new client). Idempotent; the first client calls
        it too, so calling it from the lifespan start only moves that cost out of the first request."""
        if self._ssl is None and self._transport is None:
            self._ssl = await anyio.to_thread.run_sync(partial(httpx.create_ssl_context, trust_env=False))

    async def _make_client(self, creds: TenantCredentials) -> tuple[httpx.AsyncClient, Callable[[], Awaitable[None]]]:
        await self.prewarm()
        client = httpx.AsyncClient(
            transport=self._transport,
            limits=self._limits,
            timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
            headers={"user-agent": "noema"},
            trust_env=False,  # never proxies or netrc from the environment
            verify=self._ssl or True,  # True only with an injected transport, which never builds a TLS context
        )
        return client, client.aclose

    async def _make_unary(self, creds: TenantCredentials) -> tuple[UnaryTransport, Callable[[], Awaitable[None]]]:
        """The unary (`stream: false`) transport of a pool key: aiohttp, or the injected httpx transport."""
        await self.prewarm()
        transport: UnaryTransport = (
            HttpxUnary(self._transport)
            if self._transport is not None
            else AiohttpUnary(self._ssl, max_connections=self._max_connections)
        )
        return transport, transport.aclose

    def _evict(self, tenant_id: str) -> None:
        self._pool.evict(tenant_id)
        self._unary.evict(tenant_id)

    async def aclose(self) -> None:
        """Close every pooled client."""
        await self._pool.aclose()
        await self._unary.aclose()

    def _url(self, region: str) -> str:
        return f"{self._base or default_base_url(region)}/chat/completions"

    def _headers(self, creds: TenantCredentials, url: str, body: bytes) -> dict[str, str]:
        mantle = creds.mantle
        if mantle.mode == "sigv4":
            return sigv4_headers(creds, url, body, service=self._service)
        if mantle.bearer_token is None:
            raise AdapterError(ClassifiedError(ErrorKind.FATAL, "the broker issued no Mantle bearer token"))
        return {"content-type": "application/json", "authorization": f"Bearer {mantle.bearer_token.get_secret_value()}"}

    async def _post(
        self, client: httpx.AsyncClient, creds: TenantCredentials, body: bytes, *, connect_s: float
    ) -> httpx.Response:
        """Send the request and wait for the response headers only (the in-adapter retry covers exactly this
        phase, before any response byte); the caller reads the body and closes the response."""
        url = self._url(creds.region)
        request = client.build_request(
            "POST",
            url,
            content=body,
            headers=self._headers(creds, url, body),
            timeout=httpx.Timeout(READ_TIMEOUT_S, connect=connect_s),
        )
        response = await client.send(request, stream=True)
        if response.status_code != 200:
            await response.aclose()
            raise ProviderHttpError(response.status_code, retry_after(response.headers))
        return response

    async def _post_unary(self, client: UnaryTransport, creds: TenantCredentials, url: str, body: bytes) -> RawResponse:
        """One `stream: false` request over the lean unary path; a non-200 status raises `ProviderHttpError`."""
        headers = {**self._headers(creds, url, body), "user-agent": "noema"}
        raw = await client.post(url, headers, body, connect_s=CONNECT_TIMEOUT_S)
        if raw.status != 200:
            raise ProviderHttpError(raw.status, retry_after(raw.headers))
        return raw

    async def complete(self, req: ModelRequest, *, tenant: TenantRef, deadline: Deadline) -> ModelResponse:
        """`stream: false` chat completion over the lean unary path (`noema.adapters._unary`). Raises
        `AdapterError`; the deadline bounds the whole call, credentials and client included (`timeout:deadline`,
        and no request is sent once it has passed)."""
        model = self._resolve(req)
        async with deadline_scope(self._clock, deadline.at) as scope:
            creds = await self._credentials(tenant)
            body = orjson.dumps(mantle_body(req, model, stream=False))
            entry = await self._unary.acquire(creds)
            try:
                url = self._url(creds.region)
                started = self._clock.monotonic()
                raw = await self._send(
                    req,
                    tenant,
                    model.id,
                    lambda: self._post_unary(entry.client, creds, url, body),
                    deadline_at=deadline.at,
                )
                data = orjson.loads(raw.body)
            except AdapterError:
                raise
            except Exception as exc:
                raise AdapterError(self._fail(exc, tenant, model.id)) from None
            finally:
                await self._unary.release(entry)
        if scope.cancelled_caught:
            raise AdapterError(timeout_error("deadline"))
        try:
            result = response_of(
                data,
                logprobs=req.logprobs,
                region=creds.region,
                latency_ms=round((self._clock.monotonic() - started) * 1000),
            )
        except Exception as exc:
            raise AdapterError(self._fail(exc, tenant, model.id)) from None
        self._observe_usage(model.id, result.usage)
        return result

    def stream(
        self, req: ModelRequest, *, tenant: TenantRef, timeouts: StreamTimeouts, deadline: Deadline
    ) -> AbstractAsyncContextManager[AsyncIterator[StreamEvent]]:
        """`stream: true` chat completion (spec-02 §4.1 stream semantics, see `noema.adapters._stream`)."""
        return StreamSession(
            lambda: self._prepare_stream(req, tenant, timeouts, deadline),
            clock=self._clock,
            timeouts=timeouts,
            deadline=deadline,
        )

    async def _prepare_stream(
        self, req: ModelRequest, tenant: TenantRef, timeouts: StreamTimeouts, deadline: Deadline
    ) -> PreparedCall:
        model = self._resolve(req)
        creds = await self._credentials(tenant)
        body = orjson.dumps(mantle_body(req, model, stream=True))
        entry = await self._pool.acquire(creds)
        self._open_streams += 1

        def on_usage(usage: UsageRecord) -> None:
            self._observe_usage(model.id, usage)

        async def open_stream() -> RawStream:
            response = await self._send(
                req,
                tenant,
                model.id,
                lambda: self._post(entry.client, creds, body, connect_s=timeouts.connect_s),
                deadline_at=deadline.at,
            )
            return _MantleRaw(response, self._clock, on_usage)

        async def release() -> None:
            self._open_streams -= 1
            await self._pool.release(entry)

        return PreparedCall(open_stream, lambda exc: self._fail(exc, tenant, model.id), release)
