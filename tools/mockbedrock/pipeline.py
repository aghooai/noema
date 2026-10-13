"""The shared path of every model request after its wire validated it (spec-10 §2.2, §2.4): quota, scenario rule,
barrier, pre-byte fault, then the wire renders the completion or paces the stream plan.

The timing rules mirror `tests/fakes/adapters.py`: a call's latency (and a pre-byte fault's delay) counts from the
barrier release, a stream's events from the request's arrival; a pre-byte fault on a stream waits the rule's
`latency_ms` (0 if unset). Rule matching, `nth`, `k`, `window_ms` and barriers mean the same on both. The seeded
draws do not: the fakes key them on a ModelRequest-shaped body, the mock on the canonical wire body, so sampled
latencies, `rate` fault selection, request ids and `pad` letters repeat per implementation but differ between the
fakes and the mock. A test that must behave the same on both uses fixed `latency_ms`, `rate: 1.0`, `nth` or barriers.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import orjson
from tests.fakes.scenario import (
    PRE_BYTE_FAULTS,
    Completion,
    NoRuleMatched,
    Op,
    RequestInfo,
    Resolution,
    StreamPlan,
    completion,
    stream_plan,
)

from .clock import MockClock
from .common import request_id
from .faults import TOKENS_THROTTLED, WireError, internal, pre_byte_error, throttled
from .httpserver import ClientGone, Request, Responder
from .models import ConverseModel
from .state import MockState


@dataclass(slots=True)
class Call:
    """One validated model request."""

    req: Request
    resp: Responder
    rec: dict[str, Any]
    op: Op
    model: str
    prompt: str
    body: Mapping[str, Any]
    akid: str | None
    effort: str | None = None
    max_tokens: int | None = None
    logprobs: bool = False
    top_logprobs: int | None = None
    include_usage: bool = False
    spec: ConverseModel | None = None  # Converse only
    res: Resolution | None = None
    info: RequestInfo | None = None

    @property
    def stream(self) -> bool:
        return self.op.endswith("-stream")

    def request_id(self) -> str:
        """The request id of this call (drawn once, then kept in the record)."""
        rid = self.rec.get("request_id")
        if rid is None:
            rid = self.rec["request_id"] = request_id(self.rec["seq"], None if self.res is None else self.res.draw)
        return str(rid)


class Wire(Protocol):
    """How one API renders answers (Converse/ConverseStream or Mantle)."""

    async def send_error(self, call: Call, err: WireError) -> None: ...

    async def send_completion(self, call: Call, comp: Completion) -> None: ...

    async def send_stream(self, call: Call, plan: StreamPlan) -> None: ...


def ms_to_ns(ms: float) -> int:
    return round(ms * 1e6)


class Pacer:
    """Writes a stream's chunks at their planned times; frames due at the same instant go out as one chunk."""

    def __init__(self, clock: MockClock, resp: Responder, t0_ns: int) -> None:
        self._clock = clock
        self._resp = resp
        self._t0 = t0_ns
        self._buf = bytearray()

    async def wait(self, at_ms: float) -> None:
        due = self._t0 + ms_to_ns(at_ms)
        if due > self._clock.now_ns():
            await self.flush()
            await self._clock.sleep_until_ns(due)

    def add(self, data: bytes) -> None:
        self._buf += data

    async def flush(self) -> None:
        if self._buf:
            data, self._buf = bytes(self._buf), bytearray()
            await self._resp.write(data)


async def run(state: MockState, call: Call, wire: Wire) -> None:
    """Answer one validated model request (records the outcome; cancelled when the client goes away)."""
    rec, clock = call.rec, state.clock
    try:
        await _run(state, call, wire)
        if rec["outcome"] is None:
            rec["outcome"] = "ok"
    except (asyncio.CancelledError, ClientGone):
        rec["outcome"] = rec["outcome"] or "disconnected"
        raise
    finally:
        rec["t_done_ns"] = clock.now_ns()


async def _run(state: MockState, call: Call, wire: Wire) -> None:
    rec, clock = call.rec, state.clock
    tokens = math.ceil(len(call.prompt) / 4) + (call.max_tokens or 0)
    exhausted = state.quotas.admit(call.akid, call.model, tokens, clock.now_ns() / 1e6)  # absolute: survives loads
    if exhausted is not None:
        rec["outcome"] = "throttled_quota"
        await wire.send_error(call, throttled(TOKENS_THROTTLED) if exhausted == "tpm" else throttled())
        return
    runtime, scenario = state.runtime, state.scenario
    if runtime is None or scenario is None:
        rec["outcome"] = "unmatched"
        await wire.send_error(call, internal("mockbedrock: no scenario is loaded"))
        return
    body_key = orjson.dumps(call.body, option=orjson.OPT_SORT_KEYS).decode()
    info = RequestInfo(call.model, call.op, call.prompt, body_key, call.akid, call.effort, call.max_tokens)
    try:
        res = runtime.resolve(info)
    except NoRuleMatched as exc:
        rec["outcome"] = "unmatched"
        await wire.send_error(call, internal(f"mockbedrock: {exc}"))
        return
    finally:
        runtime.requests.clear()  # the fakes' history (prompt + wire body per request); the mock keeps its own log
    call.res, call.info = res, info
    rec.update(rule=res.rule.index, k=res.k, fault=None if res.fault is None else res.fault.kind)
    base = call.req.received_ns
    if res.barrier is not None:
        rec["barrier"] = res.barrier
        await state.wait_barrier(res.barrier)
        base = max(base, clock.now_ns())
    fault = res.fault
    if fault is not None and fault.kind in PRE_BYTE_FAULTS:
        delay = (res.rule.latency_ms or 0.0) if call.stream else res.latency_ms
        await clock.sleep_until_ns(base + ms_to_ns(delay))
        err = pre_byte_error(fault)
        rec["outcome"] = "fault" if err is not None else "reset"
        if err is None:
            call.resp.reset()
        else:
            await wire.send_error(call, err)
        return
    if call.stream:
        if fault is not None:
            rec["outcome"] = "fault"
        await wire.send_stream(call, stream_plan(res, info, probe_defaults=scenario.default_latency == "probe"))
        return
    comp = completion(res, info, logprobs=call.logprobs)
    if fault is not None:
        rec["outcome"] = "fault"
    if comp.hang:
        rec["outcome"] = "hang"
        await call.resp.wait_closed()
        return
    await clock.sleep_until_ns(base + ms_to_ns(comp.latency_ms))
    await wire.send_completion(call, comp)
