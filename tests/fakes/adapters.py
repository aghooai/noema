"""`FakeAdapter` and `FakeAdapterRegistry` (spec-02 §4), driven by the scenario DSL under a `Clock`.

Per call the fake: checks the frozen wire rules of spec-02 §4.2 (logprobs on Converse, media on Mantle, temperature
or an effort the model does not accept -> `AdapterError(invalid_request)`), fetches and asserts the tenant's
credentials when a broker is given (IMPL-076), resolves the scenario rule (the request's Bedrock id, op and prompt
text), waits for its barrier and latency on the clock, and then answers:
- `complete`: a `ModelResponse` (verbalized JSON or logprobs, refusals, usage), or the rule's fault as
  `AdapterError` (classification of spec-02 §4.3; `timeout:deadline` when the deadline passes first);
- `stream`: the rule's `StreamPlan` as `StreamEvent`s at their planned times, enforcing `first_event_s`,
  `first_answer_s` and `idle_s` (each capped by the deadline) with `ErrorEvent(retryable, "timeout:<name>")`.
`retry="default"` retries a `connect_reset` once inside the adapter (no event). Every call is appended to `calls`.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Final, Literal

import anyio.lowlevel
import orjson
from botocore.exceptions import ClientError

from noema.config.models import ModelCfg
from noema.core.clock import Clock, Deadline
from noema.core.errors import ClassifiedError, ErrorKind, NoemaError, TenantAccessCause, TenantAccessMisconfigured
from noema.core.principal import TenantRef
from noema.core.protocols.adapters import (
    AdapterError,
    ErrorEvent,
    MessageStart,
    ModelCapabilities,
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
from noema.core.protocols.credentials import CredentialBroker, assert_creds_for
from noema.core.protocols.ops import RetryBudget
from noema.core.values import DocumentBlock, ImageBlock, TextBlock, UsageRecord

from .scenario import PRE_BYTE_FAULTS, Fault, Op, PlannedEvent, RequestInfo, Resolution, completion, stream_plan
from .scenario_harness import ScenarioHarness

AdapterKind = Literal["bedrock-converse", "bedrock-mantle-chat"]
CHARS_PER_TOKEN_V1: Final = 4.0
_WIRE_STOP: Final[Mapping[str, StopReason]] = {
    "end_turn": "end_turn",
    "max_tokens": "max_tokens",
    "stop_sequence": "stop_sequence",
    "content_filtered": "content_filter",
    "guardrail_intervened": "content_filter",
}


@dataclass(frozen=True, slots=True)
class CallRecord:
    """One adapter entry (complete or stream): what was asked, for whom, and how it ended."""

    request: ModelRequest
    op: Op
    model_id: str
    tenant_id: str
    akid: str | None
    started_at: float
    rule: int | None
    outcome: str


def prompt_text(req: ModelRequest) -> str:
    """The concatenated system + message text a scenario rule matches on (`prompt_contains`)."""
    parts = list(req.system)
    for msg in req.messages:
        parts.extend(b.text for b in msg.content if isinstance(b, TextBlock))
    return "".join(parts)


def _block_json(block: TextBlock | ImageBlock | DocumentBlock) -> dict[str, object]:
    if isinstance(block, TextBlock):
        return {"text": block.text}
    digest = hashlib.sha256(block.data).hexdigest()
    if isinstance(block, ImageBlock):
        return {"image": block.format, "sha256": digest}
    return {"document": block.format, "name": block.name, "sha256": digest}


def canonical_body(req: ModelRequest, model_id: str) -> str:
    """A canonical JSON rendering of the request (the scenario draw key hashes it)."""
    body = {
        "model": model_id,
        "system": list(req.system),
        "messages": [{"role": m.role, "content": [_block_json(b) for b in m.content]} for m in req.messages],
        "max_tokens": req.max_tokens,
        "effort": req.effort,
        "temperature": req.temperature,
        "logprobs": req.logprobs,
        "top_logprobs": req.top_logprobs,
        "stop": list(req.stop),
    }
    return orjson.dumps(body, option=orjson.OPT_SORT_KEYS).decode()


def capabilities_of(model: ModelCfg) -> ModelCapabilities:
    """`ModelCapabilities` of one `models.<alias>` entry (chars_per_token is the IMPL-029c constant)."""
    c = model.controls
    return ModelCapabilities(
        modalities=frozenset(m for m in model.modalities if m in ("text", "image", "document")),
        temperature=c.temperature,
        effort=frozenset(c.effort),
        logprobs=c.logprobs,
        top_logprobs_max=c.top_logprobs_max,
        structured_output=c.structured_output,
        reasoning=c.reasoning,
        max_output_tokens_cap=model.max_output_tokens_cap,
        context_window=model.context_window,
        chars_per_token=CHARS_PER_TOKEN_V1,
    )


def fault_error(fault: Fault) -> ClassifiedError:
    """The `ClassifiedError` of a pre-byte scenario fault (spec-02 §4.3)."""
    kind = fault.kind
    if kind == "throttling":
        return ClassifiedError(ErrorKind.THROTTLED, "throttled", "ThrottlingException", fault.retry_after_s)
    if kind == "access_denied":
        return ClassifiedError(
            ErrorKind.TENANT_ACCESS, "access denied", "AccessDeniedException", cause="model_access_denied"
        )
    if kind == "validation":
        return ClassifiedError(ErrorKind.INVALID_REQUEST, fault.message or "validation failed", "ValidationException")
    if kind == "http_5xx":
        return ClassifiedError(ErrorKind.RETRYABLE, "upstream error", str(fault.status))
    if kind == "model_timeout":
        return ClassifiedError(ErrorKind.RETRYABLE, "model timeout", "ModelTimeoutException")
    if kind == "connect_reset":
        return ClassifiedError(ErrorKind.RETRYABLE, "connection reset", "connect_reset")
    raise ValueError(f"{kind} is not a pre-byte fault")


def classify(exc: BaseException) -> ClassifiedError:
    """spec-02 §4.3 over the exceptions a fake (or botocore/httpx-shaped test double) raises."""
    if isinstance(exc, AdapterError):
        return exc.error
    if isinstance(exc, TenantAccessMisconfigured):
        return ClassifiedError(ErrorKind.TENANT_ACCESS, "tenant access misconfigured", cause=exc.cause)
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in ("ThrottlingException", "ServiceQuotaExceededException"):
            return ClassifiedError(ErrorKind.THROTTLED, "throttled", code)
        if code in ("AccessDeniedException", "UnrecognizedClientException", "ExpiredTokenException"):
            cause: TenantAccessCause = "model_access_denied" if code == "AccessDeniedException" else "access_denied"
            return ClassifiedError(ErrorKind.TENANT_ACCESS, "access denied", code, cause=cause)
        if code == "ValidationException":
            return ClassifiedError(ErrorKind.INVALID_REQUEST, "validation failed", code)
        if code in (
            "ServiceUnavailableException",
            "InternalServerException",
            "ModelTimeoutException",
            "ModelNotReadyException",
            "ModelStreamErrorException",
        ):
            return ClassifiedError(ErrorKind.RETRYABLE, "upstream error", code)
        return ClassifiedError(ErrorKind.FATAL, "provider error", code or None)
    if isinstance(exc, TimeoutError | ConnectionError | OSError):
        return ClassifiedError(ErrorKind.RETRYABLE, "connection failed", type(exc).__name__)
    return ClassifiedError(ErrorKind.FATAL, "unexpected adapter failure", type(exc).__name__)


def _usage(raw: Mapping[str, int] | None, *, estimated: bool = False) -> UsageRecord:
    if raw is None:
        return UsageRecord(0, 0, tokens_estimated=True)
    cached = raw.get("cached_input_tokens", 0)
    written = raw.get("cache_write_input_tokens", 0)
    return UsageRecord(
        input_tokens=raw.get("input_tokens", 0) + cached + written,  # Converse inputTokens + cache read + write
        output_tokens=raw.get("output_tokens", 0),
        cached_input_tokens=cached,
        cache_write_input_tokens=written,
        reasoning_tokens=raw.get("reasoning_tokens", 0),
        tokens_estimated=estimated,
    )


class FakeAdapter:
    """Implements `Adapter` for one adapter kind over a `ScenarioHarness` (see the module docstring)."""

    def __init__(
        self,
        kind: AdapterKind,
        *,
        clock: Clock,
        config: ConfigRegistry,
        harness: ScenarioHarness,
        broker: CredentialBroker | None = None,
        retry_budget: RetryBudget | None = None,
    ) -> None:
        self.kind: AdapterKind = kind
        self._clock = clock
        self._config = config
        self._harness = harness
        self._broker = broker
        self._retry_budget = retry_budget
        self.calls: list[CallRecord] = []
        self.open_streams = 0

    @property
    def harness(self) -> ScenarioHarness:
        return self._harness

    @harness.setter
    def harness(self, harness: ScenarioHarness) -> None:
        """Switch the scenario this adapter answers from (`FakeWorld.use_scenario`)."""
        self._harness = harness

    def capabilities(self, alias: str) -> ModelCapabilities:
        return capabilities_of(self._config.current().model(alias))

    def classify_error(self, exc: BaseException) -> ClassifiedError:
        return classify(exc)

    def _model(self, req: ModelRequest) -> ModelCfg:
        try:
            return (req.snapshot or self._config.current()).model(req.model_alias)
        except NoemaError:
            raise AdapterError(ClassifiedError(ErrorKind.INVALID_REQUEST, "unknown model alias")) from None

    def _check_wire(self, req: ModelRequest, model: ModelCfg) -> None:
        def bad(message: str) -> AdapterError:
            return AdapterError(ClassifiedError(ErrorKind.INVALID_REQUEST, message, "ValidationException"))

        if model.adapter != self.kind:
            raise bad("model is served by another adapter")
        if self.kind == "bedrock-converse" and (req.logprobs or req.top_logprobs is not None):
            raise bad("logprobs are not supported on Converse")
        if self.kind == "bedrock-mantle-chat" and any(
            isinstance(b, ImageBlock | DocumentBlock) for m in req.messages for b in m.content
        ):
            raise bad("Mantle chat members are text only")
        if req.temperature is not None and not model.controls.temperature:
            raise bad("temperature is not supported by this model")
        if req.effort is not None and req.effort not in model.controls.effort:
            raise bad("effort is not supported by this model")

    async def _creds_akid(self, tenant: TenantRef) -> str | None:
        if self._broker is None:
            return None
        try:
            creds = await self._broker.get(tenant.tenant_id)
            assert_creds_for(creds, tenant.tenant_id, tenant.aws_account_id)
        except TenantAccessMisconfigured as exc:
            raise AdapterError(classify(exc)) from None
        return creds.aws.access_key_id

    def _info(self, req: ModelRequest, model: ModelCfg, op: Op, akid: str | None) -> RequestInfo:
        body = canonical_body(req, model.id)
        return RequestInfo(model.id, op, prompt_text(req), body, akid, req.effort, req.max_tokens)

    async def _arrive(self, res: Resolution, deadline: Deadline, delay_ms: float) -> bool:
        """Barrier then latency; False if the deadline came first."""
        if res.barrier is not None and not await self._harness.wait_barrier(res.barrier, until=deadline.at):
            return False
        due = self._clock.monotonic() + delay_ms / 1000.0
        if due > deadline.at:
            await self._clock.sleep(max(0.0, deadline.at - self._clock.monotonic()))
            return False
        await self._clock.sleep(due - self._clock.monotonic())
        return True

    async def _open(
        self, req: ModelRequest, tenant: TenantRef, deadline: Deadline, op: Op
    ) -> tuple[Resolution, RequestInfo, float]:
        """Wire check, credentials, rule, and the pre-byte faults (with one in-adapter connect retry)."""
        await anyio.lowlevel.checkpoint()
        model = self._model(req)
        self._check_wire(req, model)
        akid = await self._creds_akid(tenant)
        started = self._clock.monotonic()
        retried = False
        while True:
            info = self._info(req, model, op, akid)
            res = self._harness.resolve(info)
            self.calls.append(CallRecord(req, op, model.id, tenant.tenant_id, akid, started, res.rule.index, "open"))
            fault = res.fault
            if fault is None or fault.kind not in PRE_BYTE_FAULTS:
                return res, info, started
            delay = res.latency_ms if op in ("converse", "chat") else (res.rule.latency_ms or 0.0)
            if not await self._arrive(res, deadline, delay):
                raise AdapterError(ClassifiedError(ErrorKind.RETRYABLE, "deadline", "timeout:deadline"))
            can_retry = (
                fault.kind == "connect_reset"
                and req.retry == "default"
                and not retried
                and (self._retry_budget is None or self._retry_budget.try_spend(tenant.aws_account_id, model.id))
            )
            if not can_retry:
                raise AdapterError(fault_error(fault))
            retried = True

    async def complete(self, req: ModelRequest, *, tenant: TenantRef, deadline: Deadline) -> ModelResponse:
        op: Op = "chat" if self.kind == "bedrock-mantle-chat" else "converse"
        res, info, started = await self._open(req, tenant, deadline, op)
        answer = completion(res, info, logprobs=req.logprobs)
        if answer.hang or not await self._arrive(res, deadline, answer.latency_ms):
            if answer.hang:
                await self._clock.sleep(max(0.0, deadline.at - self._clock.monotonic()))
            raise AdapterError(ClassifiedError(ErrorKind.RETRYABLE, "deadline", "timeout:deadline"))
        if answer.error is not None:
            raise AdapterError(ClassifiedError(ErrorKind.RETRYABLE, "malformed response", answer.error))
        logprobs = None
        if answer.logprobs is not None and answer.logprobs:
            top = tuple((tok, math.log(p)) for tok, p in answer.logprobs)
            logprobs = (TokenLogprob(token=top[0][0], logprob=top[0][1], top=top),)
        return ModelResponse(
            text=answer.text,
            logprobs=logprobs,
            refused=answer.refused,
            stop_reason=_WIRE_STOP[answer.stop],
            usage=_usage(answer.usage),
            latency_ms=round((self._clock.monotonic() - started) * 1000),
            region=tenant.home_region,
        )

    def stream(
        self, req: ModelRequest, *, tenant: TenantRef, timeouts: StreamTimeouts, deadline: Deadline
    ) -> AbstractAsyncContextManager[AsyncIterator[StreamEvent]]:
        return self._stream(req, tenant, timeouts, deadline)

    @asynccontextmanager
    async def _stream(
        self, req: ModelRequest, tenant: TenantRef, timeouts: StreamTimeouts, deadline: Deadline
    ) -> AsyncIterator[AsyncIterator[StreamEvent]]:
        op: Op = "chat-stream" if self.kind == "bedrock-mantle-chat" else "converse-stream"
        connect_by = deadline.child(round(timeouts.connect_s * 1000))
        res, info, started = await self._open(req, tenant, connect_by, op)
        if res.barrier is not None and not await self._harness.wait_barrier(res.barrier, until=connect_by.at):
            raise AdapterError(ClassifiedError(ErrorKind.RETRYABLE, "connect timeout", "timeout:connect"))
        plan = stream_plan(res, info, probe_defaults=self._harness.scenario.default_latency == "probe")
        gen = self._events(plan.events, plan.hang, started, timeouts, deadline)
        self.open_streams += 1
        try:
            yield gen
        finally:
            self.open_streams -= 1
            await gen.aclose()  # exiting the context = cancel (aclose, no draining)

    def _limit(
        self, t0: float, last: float, got_event: bool, got_text: bool, timeouts: StreamTimeouts, deadline: Deadline
    ) -> tuple[str, float]:
        limits = [("deadline", deadline.at)]
        if not got_event:
            limits.append(("first_event", t0 + timeouts.first_event_s))
        if not got_text:
            limits.append(("first_answer", t0 + timeouts.first_answer_s))
        if got_event:
            limits.append(("idle", last + timeouts.idle_s))
        return min(limits, key=lambda item: item[1])

    async def _events(
        self, planned: tuple[PlannedEvent, ...], hang: bool, t0: float, timeouts: StreamTimeouts, deadline: Deadline
    ) -> AsyncGenerator[StreamEvent]:
        last, got_event, got_text = t0, False, False
        for ev in (*planned, None) if hang else planned:
            due = math.inf if ev is None else t0 + ev.at_ms / 1000.0
            name, limit = self._limit(t0, last, got_event, got_text, timeouts, deadline)
            if due > limit:
                await self._clock.sleep(max(0.0, limit - self._clock.monotonic()))
                yield ErrorEvent(ClassifiedError(ErrorKind.RETRYABLE, f"{name} timeout", f"timeout:{name}"))
                return
            if ev is None:
                return
            await self._clock.sleep(max(0.0, due - self._clock.monotonic()))
            last, got_event = self._clock.monotonic(), True
            if ev.kind == "start":
                yield MessageStart(at=last)
            elif ev.kind == "reasoning":
                yield ReasoningDelta(chars=ev.chars)
            elif ev.kind == "text":
                got_text = True
                yield TextDelta(text=ev.text)
            elif ev.kind == "stop":
                yield Stop(reason=_WIRE_STOP[ev.stop or "end_turn"])
            elif ev.kind == "usage":
                yield UsageEvent(usage=_usage(ev.usage))
            else:
                yield ErrorEvent(ClassifiedError(ErrorKind.RETRYABLE, "stream error", ev.error))
                return


class FakeAdapterRegistry:
    """Implements `AdapterRegistry`: the fake of `models.<alias>.adapter`."""

    def __init__(self, config: ConfigRegistry, adapters: Mapping[str, FakeAdapter]) -> None:
        self._config = config
        self.adapters = dict(adapters)

    def for_model(self, alias: str) -> FakeAdapter:
        kind = self._config.current().model(alias).adapter
        return self.adapters[kind]

    def calls(self) -> list[CallRecord]:
        """Every call of every adapter, in start order."""
        return sorted((c for a in self.adapters.values() for c in a.calls), key=lambda c: c.started_at)
