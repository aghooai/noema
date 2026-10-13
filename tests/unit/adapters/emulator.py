"""`WireEmulator`: an in-process Bedrock (Converse/ConverseStream + Mantle chat) emulator for the real adapters,
driven by the scenario DSL (spec-10 §2.4) in virtual time.

- Converse: `converse_unary(creds)` is the transport of one pool key of the lean Converse path
  (`ConverseAdapter(unary_transport=emulator.converse_unary)`): it answers the signed request (Converse JSON, an AWS
  eventstream body read as it is planned, or an `x-amzn-ErrorType` error response; `connect_reset` raises
  `EndpointConnectionError`), so the adapter parses real wire bytes (eventstream CRCs included); every body is
  captured in the golden form of the call parameters (`wire`: `modelId` from the path, base64 blobs as
  `{"$bytes_b64"}`); `unary_clients` lists the transports created (one per pool key).
- Mantle: `mantle_transport()` is an `httpx.MockTransport` answering `POST .../chat/completions` with JSON or SSE.

Every request is recorded in `requests` (op, model id, the SigV4 access key id or bearer scheme). Latency, barriers
and stream event times follow the scenario on the emulator's `FakeClock`; a hang sleeps until cancelled.
`plan_override` replaces every stream's planned events (exact provider timings the DSL cannot express, e.g. usage
long after the stop).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, cast
from urllib.parse import unquote

import anyio
import httpx
import orjson
from aiobotocore.awsrequest import AioAWSResponse
from botocore.exceptions import EndpointConnectionError
from tests.fakes.clock import FakeClock
from tests.fakes.scenario import PRE_BYTE_FAULTS, Op, PlannedEvent, RequestInfo, Resolution, completion, stream_plan
from tests.fakes.scenario_harness import ScenarioHarness
from tests.unit.adapters.emulator_wire import (
    converse_body,
    converse_error,
    converse_frame,
    event_frame,
    golden_json,
    mantle_chunk,
    mantle_completion,
)

from noema.adapters._unary import RawResponse
from noema.core.protocols.credentials import TenantCredentials

Json = dict[str, Any]
DEFAULT_SCENARIO: Final[Json] = {
    "scenario": "emulator-default",
    "defaults": {"latency": {"fixed_ms": 0}},
    "rules": [{"match": {}, "answer": {"raw_text": "ok"}, "stream": {"chunks": ["ok"]}}],
}
_AKID: Final = re.compile(r"Credential=([A-Z0-9]+)/")
_PATH: Final = re.compile(r"/model/([^/]+)/(converse|converse-stream)$")


@dataclass(frozen=True, slots=True)
class Recorded:
    """One request the emulator received."""

    op: Op
    model: str
    auth: str  # "sigv4" | "bearer" | "none"
    akid: str | None
    bearer: str | None
    headers: Mapping[str, str]
    body: Json


@dataclass
class _Body:
    data: bytes

    async def read(self) -> bytes:
        return self.data

    def close(self) -> None:
        return None


@dataclass
class _EventContent:
    owner: _EventBody

    async def iter_chunks(self) -> AsyncIterator[tuple[bytes, bool]]:
        async for chunk in self.owner.frames():
            yield chunk, True


@dataclass
class _EventBody:
    """The `raw` of an eventstream response: `content.iter_chunks()` yields the planned frames on time."""

    emulator: WireEmulator
    plan: tuple[PlannedEvent, ...]
    hang: bool
    t0: float
    closed: bool = False
    content: _EventContent = field(init=False)

    def __post_init__(self) -> None:
        self.content = _EventContent(self)
        self.emulator.open_streams += 1

    async def frames(self) -> AsyncIterator[bytes]:
        clock = self.emulator.clock
        reasoning = any(e.kind == "reasoning" for e in self.plan)
        text_block = 1 if reasoning else 0
        in_reasoning = False
        for ev in self.plan:
            await clock.sleep(max(0.0, self.t0 + ev.at_ms / 1000 - clock.monotonic()))
            if ev.kind == "reasoning":
                in_reasoning = True
            elif in_reasoning and ev.kind in ("text", "stop"):
                in_reasoning = False
                yield event_frame("contentBlockStop", {"contentBlockIndex": 0})
            yield converse_frame(ev, 0 if ev.kind == "reasoning" else text_block)
            if ev.kind == "error":
                return
        if self.hang:
            await anyio.sleep_forever()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.emulator.open_streams -= 1


class _SseStream(httpx.AsyncByteStream):
    """A Mantle SSE body: the planned chunks on time, then `data: [DONE]` (unless the plan hangs or fails)."""

    def __init__(self, emulator: WireEmulator, plan: tuple[PlannedEvent, ...], hang: bool, t0: float, usage: bool):
        self._emulator = emulator
        self._plan = plan
        self._hang = hang
        self._t0 = t0
        self._usage = usage
        self._closed = False
        emulator.open_streams += 1

    async def __aiter__(self) -> AsyncIterator[bytes]:
        clock = self._emulator.clock
        for ev in self._plan:
            await clock.sleep(max(0.0, self._t0 + ev.at_ms / 1000 - clock.monotonic()))
            line = mantle_chunk(ev, self._usage)
            if line is not None:
                yield line
            if ev.kind == "error":
                return
        if self._hang:
            await anyio.sleep_forever()
        yield b"data: [DONE]\n\n"

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            self._emulator.open_streams -= 1


@dataclass(frozen=True, slots=True)
class _Signed:
    """The request the lean Converse path POSTs, shaped like the botocore request `_converse_send` reads."""

    url: str
    headers: Mapping[str, str]
    body: bytes


def lean_golden(url: str, body: bytes) -> Json:
    """A lean Converse request body in the golden JSON form of the call parameters."""
    match = _PATH.search(url.split("?", 1)[0])
    assert match is not None, url
    data: Json = orjson.loads(body)
    for message in data.get("messages", ()):
        for block in message["content"]:
            for kind in ("image", "document"):
                if kind in block:
                    block[kind]["source"]["bytes"] = {"$bytes_b64": block[kind]["source"]["bytes"]}
    return {"modelId": unquote(match.group(1)), **data}


class ConverseUnary:
    """The emulator as the unary transport of one pool key (see the module docstring)."""

    def __init__(self, emulator: WireEmulator, creds: TenantCredentials) -> None:
        self.emulator, self.creds, self.closed = emulator, creds, False

    async def post(self, url: str, headers: Mapping[str, str], body: bytes, *, connect_s: float) -> RawResponse:
        self.emulator.wire.append(lean_golden(url, body))
        response = await self.emulator._converse_send(_Signed(url, headers, body))
        content = await response.raw.read()
        return RawResponse(response.status_code, {k.lower(): v for k, v in response.headers.items()}, content)

    async def open(self, url: str, headers: Mapping[str, str], body: bytes, *, connect_s: float) -> _Streamed:
        self.emulator.wire.append(lean_golden(url, body))
        return _Streamed(await self.emulator._converse_send(_Signed(url, headers, body)))

    async def aclose(self) -> None:
        self.closed = True


class _Streamed:
    """An emulator response read as it arrives (`StreamedResponse`): the planned eventstream frames on time."""

    def __init__(self, response: AioAWSResponse) -> None:
        self.status = response.status_code
        self.headers = {k.lower(): v for k, v in response.headers.items()}
        self._raw: Any = response.raw
        self._frames: AsyncIterator[bytes] | None = None

    async def chunk(self) -> bytes:
        if self._frames is None:
            self._frames = self._raw.frames()
        try:
            return await anext(self._frames)
        except StopAsyncIteration:
            return b""

    async def read(self) -> bytes:
        data: bytes = await self._raw.read()
        return data

    def close(self) -> None:
        self._raw.close()


def _response(url: str, status: int, headers: dict[str, str], raw: object) -> AioAWSResponse:
    return AioAWSResponse(url, status, cast(Any, headers), raw)  # AWSResponse wraps any mapping in a HeadersDict


def _prompt(system: list[str], messages: list[tuple[str, list[str]]]) -> str:
    return "".join(system) + "".join("".join(texts) for _, texts in messages)


class WireEmulator:
    """See the module docstring. `wire`: captured request bodies in the golden JSON form (Converse call parameters,
    Mantle JSON bodies), in arrival order."""

    def __init__(self, clock: FakeClock, scenario: Mapping[str, Any] | None = None) -> None:
        self.clock = clock
        self.harness = ScenarioHarness(dict(scenario or DEFAULT_SCENARIO), clock)
        self.wire: list[Json] = []
        self.requests: list[Recorded] = []
        self.open_streams = 0
        self.plan_override: tuple[PlannedEvent, ...] | None = None
        self.unary_clients: list[ConverseUnary] = []

    def use_scenario(self, doc: Mapping[str, Any]) -> None:
        self.harness = ScenarioHarness(dict(doc), self.clock)

    def _info(self, model: str, op: Op, prompt: str, body: Json, akid: str | None) -> RequestInfo:
        canonical = hashlib.sha256(orjson.dumps(body, option=orjson.OPT_SORT_KEYS)).hexdigest()
        effort = (body.get("outputConfig") or {}).get("effort")
        return RequestInfo(model, op, prompt, canonical, akid, effort)

    async def _arrive(self, res: Resolution, delay_ms: float) -> None:
        if res.barrier is not None:
            await self.harness.wait_barrier(res.barrier, until=float("inf"))
        await self.clock.sleep(delay_ms / 1000)

    @staticmethod
    def _auth(headers: Mapping[str, str]) -> tuple[str, str | None, str | None]:
        value = headers.get("authorization") or headers.get("Authorization") or ""
        if value.startswith("AWS4-HMAC-SHA256"):
            match = _AKID.search(value)
            return "sigv4", match.group(1) if match else None, None
        if value.startswith("Bearer "):
            return "bearer", None, value[7:]
        return "none", None, None

    # --- Converse -------------------------------------------------------------------------------------------------
    def converse_unary(self, creds: TenantCredentials) -> ConverseUnary:
        """The unary transport of one pool key (`ConverseAdapter(unary_transport=emulator.converse_unary)`)."""
        transport = ConverseUnary(self, creds)
        self.unary_clients.append(transport)
        return transport

    async def _converse_send(self, request: Any, **_: Any) -> AioAWSResponse:
        match = _PATH.search(request.url.split("?", 1)[0])
        assert match is not None, request.url
        model, op = unquote(match.group(1)), match.group(2)
        body = orjson.loads(request.body)
        headers = {k.lower(): (v.decode() if isinstance(v, bytes) else str(v)) for k, v in request.headers.items()}
        scheme, akid, bearer = self._auth(headers)
        self.requests.append(Recorded(op, model, scheme, akid, bearer, headers, body))  # type: ignore[arg-type]
        system = [b["text"] for b in body.get("system", [])]
        messages = [(m["role"], [b["text"] for b in m["content"] if "text" in b]) for m in body["messages"]]
        info = self._info(model, op, _prompt(system, messages), body, akid)  # type: ignore[arg-type]
        res = self.harness.resolve(info)
        t0 = self.clock.monotonic()
        fault = res.fault
        if fault is not None and fault.kind in PRE_BYTE_FAULTS:
            await self._arrive(res, res.latency_ms if op == "converse" else (res.rule.latency_ms or 0.0))
            if fault.kind == "connect_reset":
                raise EndpointConnectionError(endpoint_url=request.url)
            status, err_headers, payload = converse_error(fault)
            return _response(request.url, status, err_headers, _Body(payload))
        if op == "converse":
            answer = completion(res, info, logprobs=False)
            if answer.hang:
                await anyio.sleep_forever()
            await self._arrive(res, answer.latency_ms)
            headers_out = {"content-type": "application/json", "x-amzn-requestid": "emu-request"}
            return _response(request.url, 200, headers_out, _Body(converse_body(answer)))
        if res.barrier is not None:
            await self.harness.wait_barrier(res.barrier, until=float("inf"))
        plan = stream_plan(res, info, probe_defaults=self.harness.scenario.default_latency == "probe")
        if not plan.events and plan.hang:
            await anyio.sleep_forever()  # hang before the first byte: no response headers
        stream_headers = {"content-type": "application/vnd.amazon.eventstream", "x-amzn-requestid": "emu-request"}
        events = plan.events if self.plan_override is None else self.plan_override
        return _response(request.url, 200, stream_headers, _EventBody(self, events, plan.hang, t0))

    # --- Mantle ---------------------------------------------------------------------------------------------------
    def mantle_transport(self) -> httpx.MockTransport:
        """An httpx transport answering Mantle chat completions (`MantleAdapter(transport=...)`)."""
        return httpx.MockTransport(self._mantle_handler)

    async def _mantle_handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions"), request.url
        body = orjson.loads(request.content)
        self.wire.append(golden_json(body))
        headers = {k.lower(): v for k, v in request.headers.items()}
        scheme, akid, bearer = self._auth(headers)
        stream = bool(body.get("stream"))
        op: Op = "chat-stream" if stream else "chat"
        self.requests.append(Recorded(op, body["model"], scheme, akid, bearer, headers, body))
        system = [m["content"] for m in body["messages"] if m["role"] == "system"]
        messages = [(m["role"], [m["content"]]) for m in body["messages"] if m["role"] != "system"]
        info = self._info(body["model"], op, _prompt(system, messages), body, akid)
        res = self.harness.resolve(info)
        t0 = self.clock.monotonic()
        fault = res.fault
        if fault is not None and fault.kind in PRE_BYTE_FAULTS:
            await self._arrive(res, res.latency_ms if op == "chat" else (res.rule.latency_ms or 0.0))
            if fault.kind == "connect_reset":
                raise httpx.ConnectError("connection reset by peer", request=request)
            status, err_headers, _ = converse_error(fault)
            error = {"error": {"message": "request failed", "type": "invalid_request_error", "code": None}}
            out = {k: v for k, v in err_headers.items() if k == "retry-after"}
            return httpx.Response(status, headers=out, content=orjson.dumps(error))
        if not stream:
            answer = completion(res, info, logprobs=bool(body.get("logprobs")))
            if answer.hang:
                await anyio.sleep_forever()
            await self._arrive(res, answer.latency_ms)
            payload = mantle_completion(answer, body.get("top_logprobs"))
            return httpx.Response(200, headers={"content-type": "application/json"}, content=payload)
        if res.barrier is not None:
            await self.harness.wait_barrier(res.barrier, until=float("inf"))
        plan = stream_plan(res, info, probe_defaults=self.harness.scenario.default_latency == "probe")
        if not plan.events and plan.hang:
            await anyio.sleep_forever()
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        events = plan.events if self.plan_override is None else self.plan_override
        sse = _SseStream(self, events, plan.hang, t0, include_usage)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=sse)
