"""Step 2: W3C trace context (IMPL-033, spec-04 §1).

A valid `traceparent` (with its `tracestate`) is joined: its span context becomes the OpenTelemetry parent of every
span the request creates. An invalid or missing header is never rejected; the request then starts a new trace
(no parent is attached, so the first span is a root). Parsing follows W3C Trace Context level 1 through the
opentelemetry-api propagator (version `ff`, all-zero ids and malformed fields are invalid).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Final

from opentelemetry import context as otel_context
from opentelemetry import trace as otel_trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from noema.api.middleware.context import TraceContext

_PROPAGATOR: Final = TraceContextTextMapPropagator()
_MAX_TRACESTATE: Final = 512  # W3C: at most 512 characters are propagated


def parse_trace(headers: Mapping[str, str]) -> tuple[TraceContext, otel_context.Context | None]:
    """The request's trace context and, when the client's `traceparent` is valid, the OpenTelemetry context that
    carries it as the remote parent. `headers` keys are lower-case."""
    traceparent = headers.get("traceparent")
    if traceparent is None:
        return TraceContext(), None
    carrier = {"traceparent": traceparent}
    tracestate = headers.get("tracestate")
    if tracestate is not None and len(tracestate) <= _MAX_TRACESTATE:
        carrier["tracestate"] = tracestate
    ctx = _PROPAGATOR.extract(carrier)
    span_ctx = otel_trace.get_current_span(ctx).get_span_context()
    if not span_ctx.is_valid:
        return TraceContext(), None
    state = span_ctx.trace_state.to_header() or None
    info = TraceContext(
        traceparent=traceparent.strip(),
        tracestate=state,
        trace_id=f"{span_ctx.trace_id:032x}",
        parent_span_id=f"{span_ctx.span_id:016x}",
        sampled=bool(span_ctx.trace_flags & otel_trace.TraceFlags.SAMPLED),
    )
    return info, ctx


@contextmanager
def attached(ctx: otel_context.Context | None) -> Iterator[None]:
    """Make `ctx` the current OpenTelemetry context for the duration of the block (no-op for None)."""
    if ctx is None:
        yield
        return
    token = otel_context.attach(ctx)
    try:
        yield
    finally:
        otel_context.detach(token)
