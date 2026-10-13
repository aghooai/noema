"""Registers the real in-process `InProcessTurnEventLog` and `BackpressureSseWriter` in the event_log and
sse_writer conformance suites (spec-02 §14) on the suite's FakeClock. The Redis-mirrored event log is registered
from tests/integration/streaming/conformance_impls.py."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from tests.contract_internal.kits import EventLogKit, SseWriterKit
from tests.contract_internal.registry import Harness, register

from noema.streaming import BackpressureSseWriter, InProcessTurnEventLog


@register("event_log", "inprocess")
@asynccontextmanager
async def inprocess_event_log(h: Harness) -> AsyncIterator[EventLogKit]:
    yield EventLogKit(InProcessTurnEventLog(h.clock), h.clock)


@register("sse_writer", "backpressure")
@asynccontextmanager
async def backpressure_sse_writer(h: Harness) -> AsyncIterator[SseWriterKit]:
    yield SseWriterKit(BackpressureSseWriter(h.clock), h.clock)
