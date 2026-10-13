"""noema.telemetry (spec-01 §1, spec-02 §12): OTel setup, the span helper, the GenAI attribute mapper, the
pre-declared metric instruments and `redact()`. Imports only `noema.core` (spec-01 §2 rule 2)."""

from .genai import CACHE_WRITE_ATTRIBUTE, PROVIDER_NAME, finish_reason, request_attributes, usage_attributes
from .logs import JsonFormatter, RedactingFilter, configure_logging
from .otel import OtelInstruments, OtelTelemetry, SpanHandle
from .redact import KINDS, Redactor, default_redactor, redact, register_secret
from .registry import METRICS, SPAN_ATTRIBUTES, SPAN_NAMES, MetricSpec, UndeclaredTelemetry
from .setup import TelemetrySetup, setup_telemetry

__all__ = [
    "CACHE_WRITE_ATTRIBUTE",
    "KINDS",
    "METRICS",
    "PROVIDER_NAME",
    "SPAN_ATTRIBUTES",
    "SPAN_NAMES",
    "JsonFormatter",
    "MetricSpec",
    "OtelInstruments",
    "OtelTelemetry",
    "RedactingFilter",
    "Redactor",
    "SpanHandle",
    "TelemetrySetup",
    "UndeclaredTelemetry",
    "configure_logging",
    "default_redactor",
    "finish_reason",
    "redact",
    "register_secret",
    "request_attributes",
    "setup_telemetry",
    "usage_attributes",
]
