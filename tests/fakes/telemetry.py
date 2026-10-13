"""`FakeTelemetry` (spec-02 §12): records spans and metric calls in memory, enforcing the same closed registry
as `noema.telemetry.OtelTelemetry` (undeclared names raise `UndeclaredTelemetry` when `strict`, the default in
tests) and redacting string attribute values with the real `redact()`."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field

from noema.core.errors import NoemaError
from noema.telemetry.redact import Redactor
from noema.telemetry.registry import ERROR_ATTRIBUTE, METRICS, MetricKind, UndeclaredTelemetry, check_metric, check_span

AttrValue = str | int | float | bool


@dataclass
class RecordedSpan:
    """One finished (or open) span."""

    name: str
    attributes: dict[str, AttrValue] = field(default_factory=dict)
    error: str | None = None
    parent: RecordedSpan | None = None
    ended: bool = False


@dataclass(frozen=True, slots=True)
class RecordedMetric:
    name: str
    kind: MetricKind
    value: float
    labels: Mapping[str, str]


class FakeSpan:
    def __init__(self, record: RecordedSpan, owner: FakeTelemetry) -> None:
        self.record = record
        self._owner = owner

    def set(self, key: str, value: AttrValue) -> None:
        if self._owner._report(check_span(self.record.name, {key: value})):
            self.record.attributes[key] = self._owner.redact(value) if isinstance(value, str) else value

    def error(self, code: str) -> None:
        self.record.error = code
        self.record.attributes[ERROR_ATTRIBUTE] = code


class FakeInstruments:
    def __init__(self, owner: FakeTelemetry) -> None:
        self._owner = owner

    def _record(self, name: str, kind: MetricKind, value: float, labels: Mapping[str, str]) -> None:
        if self._owner._report(check_metric(name, kind, labels)):
            full = {k: str(labels.get(k, "")) for k in METRICS[name].labels}
            self._owner.metrics.append(RecordedMetric(name, kind, value, full))

    def add(self, name: str, value: float = 1, **labels: str) -> None:
        self._record(name, "counter", value, labels)

    def observe(self, name: str, value: float, **labels: str) -> None:
        self._record(name, "histogram", value, labels)

    def set(self, name: str, value: float, **labels: str) -> None:
        self._record(name, "gauge", value, labels)


class FakeTelemetry:
    """Implements `Telemetry` in memory: `spans` (start order), `metrics` (call order), `dropped` (non-strict)."""

    def __init__(self, *, strict: bool = True, redactor: Redactor | None = None) -> None:
        self.strict = strict
        self._redactor = redactor or Redactor()
        self.spans: list[RecordedSpan] = []
        self.metrics: list[RecordedMetric] = []
        self.dropped: list[str] = []
        self._stack: list[RecordedSpan] = []
        self._instruments = FakeInstruments(self)

    def _report(self, problems: list[str]) -> bool:
        if not problems:
            return True
        if self.strict:
            raise UndeclaredTelemetry("; ".join(problems))
        self.dropped.extend(problems)
        return False

    @property
    def instruments(self) -> FakeInstruments:
        return self._instruments

    def redact(self, text: str) -> str:
        return self._redactor.redact(text)

    def span(self, name: str, **attrs: AttrValue) -> AbstractContextManager[FakeSpan]:
        return self._span(name, attrs)

    @contextmanager
    def _span(self, name: str, attrs: Mapping[str, AttrValue]) -> Iterator[FakeSpan]:
        ok = self._report(check_span(name, attrs))
        clean = {k: self.redact(v) if isinstance(v, str) else v for k, v in attrs.items()} if ok else {}
        record = RecordedSpan(name, clean, parent=self._stack[-1] if self._stack else None)
        if ok:
            self.spans.append(record)
        self._stack.append(record)
        span = FakeSpan(record, self)
        try:
            yield span
        except NoemaError as exc:
            span.error(exc.code.value)
            raise
        except Exception:
            span.error("internal_error")
            raise
        finally:
            self._stack.pop()
            record.ended = True

    def counter(self, name: str, **labels: str) -> float:
        """Sum of a counter's increments whose labels include `labels`."""
        return sum(
            m.value for m in self.metrics if m.name == name and all(m.labels.get(k) == v for k, v in labels.items())
        )
