"""`OtelTelemetry`: the real `Telemetry` (spec-02 §12) over OpenTelemetry tracing and metrics.

Names are checked against the closed registry (`registry.py`): under `NOEMA_ENV=test` an undeclared span name,
attribute key, metric, label key or instrument kind raises `UndeclaredTelemetry`; elsewhere the item is dropped and
a warning is logged. String attribute values pass through `redact()`. Instrument calls never raise outside test mode.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from typing import Final

from opentelemetry.metrics import Counter, Histogram, Meter, _Gauge
from opentelemetry.trace import INVALID_SPAN_CONTEXT, NonRecordingSpan, Status, StatusCode, Tracer
from opentelemetry.trace import Span as OtelSpan

from noema.core.errors import NoemaError
from noema.core.protocols.telemetry import SpanName

from .genai import FINISH_REASONS
from .redact import Redactor
from .registry import (
    ERROR_ATTRIBUTE,
    METRICS,
    SECONDS_BUCKETS,
    SPAN_ATTRIBUTES,
    SPAN_NAMES,
    MetricKind,
    UndeclaredTelemetry,
    check_metric,
)
from .setup import INSTRUMENTATION_SCOPE, TelemetrySetup

_log: Final = logging.getLogger("noema.telemetry")
AttrValue = str | int | float | bool


class _Strictness:
    """Raises in test mode, logs (once per problem) otherwise."""

    def __init__(self, strict: bool) -> None:
        self.strict = strict
        self._logged: set[str] = set()

    def report(self, problems: list[str]) -> bool:
        """True when there was nothing to report."""
        if not problems:
            return True
        if self.strict:
            raise UndeclaredTelemetry("; ".join(problems))
        for p in problems:
            if p not in self._logged:
                self._logged.add(p)
                _log.warning("telemetry item dropped: %s", p)
        return False


class SpanHandle:
    """The `Span` of one OTel span: registry-checked, redacted attributes."""

    __slots__ = ("_redactor", "_span", "_strict")

    def __init__(self, span: OtelSpan, redactor: Redactor, strict: _Strictness) -> None:
        self._span = span
        self._redactor = redactor
        self._strict = strict

    def set(self, key: str, value: AttrValue) -> None:
        """Set one declared attribute; strings are redacted."""
        if key not in SPAN_ATTRIBUTES and not self._strict.report(check_span_attr(key)):
            return
        self._span.set_attribute(key, _attr_value(key, value, self._redactor))

    def error(self, code: str) -> None:
        """Mark the span failed with an ErrorCode or member error code (`error.type`); never a message."""
        safe = self._redactor.redact(code)
        self._span.set_attribute(ERROR_ATTRIBUTE, safe)
        self._span.set_status(Status(StatusCode.ERROR, safe))


def check_span_attr(key: str) -> list[str]:
    """Problems with one attribute key (empty = declared)."""
    return [] if key in SPAN_ATTRIBUTES else [f"undeclared span attribute {key!r}"]


def _attr_value(key: str, value: AttrValue, redactor: Redactor) -> AttrValue | tuple[str, ...]:
    if isinstance(value, str):
        value = redactor.redact_value(value)  # memoized: the same ids and names recur on every span
        if key == FINISH_REASONS:  # semconv type string[]
            return (value,)
    return value


class OtelInstruments:
    """`Instruments` over pre-created OTel instruments (every registry metric, created at construction)."""

    def __init__(self, meter: Meter, strict: _Strictness) -> None:
        self._strict = strict
        self._counters: dict[str, Counter] = {}
        self._histograms: dict[str, Histogram] = {}
        self._gauges: dict[str, _Gauge] = {}
        for spec in METRICS.values():
            if spec.kind == "counter":
                self._counters[spec.name] = meter.create_counter(spec.name, description=spec.description)
            elif spec.kind == "histogram":
                self._histograms[spec.name] = meter.create_histogram(
                    spec.name, description=spec.description, explicit_bucket_boundaries_advisory=SECONDS_BUCKETS
                )
            else:
                self._gauges[spec.name] = meter.create_gauge(spec.name, description=spec.description)

    def _labels(self, name: str, kind: MetricKind, labels: Mapping[str, str]) -> dict[str, str] | None:
        if not self._strict.report(check_metric(name, kind, labels)):
            return None
        return {k: str(labels.get(k, "")) for k in METRICS[name].labels}

    def add(self, name: str, value: float = 1, **labels: str) -> None:
        """Counter increment."""
        attrs = self._labels(name, "counter", labels)
        if attrs is not None:
            self._safely(lambda: self._counters[name].add(value, attrs))

    def observe(self, name: str, value: float, **labels: str) -> None:
        """Histogram observation (seconds)."""
        attrs = self._labels(name, "histogram", labels)
        if attrs is not None:
            self._safely(lambda: self._histograms[name].record(value, attrs))

    def set(self, name: str, value: float, **labels: str) -> None:
        """Gauge value."""
        attrs = self._labels(name, "gauge", labels)
        if attrs is not None:
            self._safely(lambda: self._gauges[name].set(value, attrs))

    def _safely(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:  # an SDK failure must never reach the caller (spec-02 §12)
            if self._strict.strict:
                raise
            _log.exception("metric export failed")


class OtelTelemetry:
    """The real `Telemetry`. `strict` defaults to `env == "test"`; `static_attributes` go on every span (e.g.
    `aghoo.credentials.source`, ADR-001 §7)."""

    def __init__(
        self,
        setup: TelemetrySetup,
        *,
        env: str,
        redactor: Redactor | None = None,
        strict: bool | None = None,
        static_attributes: Mapping[str, AttrValue] | None = None,
    ) -> None:
        self.setup = setup
        self._strict = _Strictness(env == "test" if strict is None else strict)
        self._redactor = redactor or Redactor()
        static = dict(static_attributes or {})
        self._strict.report([f"undeclared span attribute {k!r}" for k in static if check_span_attr(k)])
        # redacted once here, not on every span (platform constants such as `aghoo.credentials.source`)
        self._static = {k: _attr_value(k, v, self._redactor) for k, v in static.items()}
        self._tracer: Tracer = setup.tracer_provider.get_tracer(INSTRUMENTATION_SCOPE)
        self._instruments = OtelInstruments(setup.meter_provider.get_meter(INSTRUMENTATION_SCOPE), self._strict)

    @property
    def instruments(self) -> OtelInstruments:
        return self._instruments

    def redact(self, text: str) -> str:
        """spec-09 §4 patterns."""
        return self._redactor.redact(text)

    @property
    def redactor(self) -> Redactor:
        return self._redactor

    def span(self, name: SpanName, **attrs: AttrValue) -> AbstractContextManager[SpanHandle]:
        """A child of the current span. A `NoemaError` escaping the block sets `error.type` to its code; any other
        exception sets `internal_error`; cancellation is not an error. Exception messages are never recorded."""
        return self._span(name, attrs)

    @contextmanager
    def _span(self, name: str, attrs: Mapping[str, AttrValue]) -> Iterator[SpanHandle]:
        if name not in SPAN_NAMES:
            self._strict.report([f"undeclared span name {name!r}"])
            yield SpanHandle(NonRecordingSpan(INVALID_SPAN_CONTEXT), self._redactor, self._strict)  # dropped
            return
        bad = [k for k in attrs if k not in SPAN_ATTRIBUTES]
        if bad and not self._strict.report([f"undeclared span attribute {k!r}" for k in bad]):
            attrs = {k: v for k, v in attrs.items() if k not in bad}
        values = {**self._static, **{k: _attr_value(k, v, self._redactor) for k, v in attrs.items()}}
        with self._tracer.start_as_current_span(
            name, attributes=values, record_exception=False, set_status_on_exception=False
        ) as span:
            handle = SpanHandle(span, self._redactor, self._strict)
            try:
                yield handle
            except NoemaError as exc:
                handle.error(exc.code.value)
                raise
            except Exception:  # cancellation (a BaseException on every anyio backend) is not an error
                handle.error("internal_error")
                raise
