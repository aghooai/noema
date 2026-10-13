"""Telemetry (spec-02 §12, IMPL-006). Span and metric names are closed sets declared by `noema.telemetry`;
an undeclared name or attribute key is an error under NOEMA_ENV=test and dropped (and logged) otherwise."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Literal, Protocol

SpanName = Literal[
    "turn",
    "preflight",
    "history.fetch",
    "sts.assume_role",
    "decision",
    "decision.member",
    "decision.escalation",
    "bindings.evaluate",
    "generation.attempt",
    "verification",
    "cache.lookup",
    "media.extract",
    # C2a (labeling.md item 2): the labeling jobs
    "labeling.item",
    "labeling.run",
    "labeling.drift",
]


class Span(Protocol):
    def set(self, key: str, value: str | int | float | bool) -> None:
        """Sync; keys of the attribute registry only; string values are redacted."""
        ...

    def error(self, code: str) -> None:
        """Sync; an ErrorCode or member error code, never a message."""
        ...


class Instruments(Protocol):
    """Every 08 §7 metric plus the IMPL-006 additions, pre-declared. All methods are sync and never raise."""

    def add(self, name: str, value: float = 1, **labels: str) -> None:
        """Counters (*_total)."""
        ...

    def observe(self, name: str, value: float, **labels: str) -> None:
        """Histograms (*_seconds)."""
        ...

    def set(self, name: str, value: float, **labels: str) -> None:
        """Gauges."""
        ...


class Telemetry(Protocol):
    def span(self, name: SpanName, **attrs: str | int | float | bool) -> AbstractContextManager[Span]:
        """Sync."""
        ...

    @property
    def instruments(self) -> Instruments: ...

    def redact(self, text: str) -> str:
        """Sync; the spec-09 §4 patterns; applied by every log handler."""
        ...
