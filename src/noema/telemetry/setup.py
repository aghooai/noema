"""OpenTelemetry provider setup (spec-02 §12, 08 §7).

`setup_telemetry` builds process-local tracer and meter providers; it never touches the OTel global providers, so
several instances can coexist (tests, multi-app processes). Exporters:
- traces: OTLP over HTTP when `otlp_endpoint` is set, plus an in-memory exporter when `memory=True` (tests). With
  neither, nothing could ever read a span, so the provider samples nothing (`ALWAYS_OFF`): spans are
  non-recording (valid ids, so log lines keep their `trace_id`, and the context still propagates), which saves the
  SDK's per-span recording on every request (E sprint B: ~2/3 of a span's cost);
- metrics: a Prometheus reader on its own `CollectorRegistry`, or an in-memory reader when `memory=True`. In
  Prometheus mode `serve_prometheus(port)` exposes it on the internal metrics port (`GET /metrics` on
  NOEMA_METRICS_PORT, spec-11 §7); `composition.start` runs it in every process, so no API route serves metrics.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, MetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF
from prometheus_client import CollectorRegistry, generate_latest, start_http_server

INSTRUMENTATION_SCOPE: Final = "noema"


@dataclass(frozen=True, slots=True)
class TelemetrySetup:
    """The providers of one process. `spans` and `metrics_memory` are set only in memory mode; `prometheus` only
    in Prometheus mode."""

    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    reader: MetricReader
    spans: InMemorySpanExporter | None
    metrics_memory: InMemoryMetricReader | None
    prometheus: CollectorRegistry | None

    def prometheus_text(self) -> bytes:
        """The Prometheus exposition of every instrument (Prometheus mode); raises RuntimeError otherwise."""
        if self.prometheus is None:
            raise RuntimeError("telemetry was not set up with the Prometheus reader")
        return generate_latest(self.prometheus)

    def serve_prometheus(self, port: int, *, host: str = "0.0.0.0") -> tuple[int, Callable[[], None]]:  # noqa: S104
        """Serve the exposition on `host:port` from a daemon thread (`port` 0 = ephemeral); returns the bound port
        and the function that stops the server. Raises RuntimeError outside Prometheus mode and OSError when the
        port cannot be bound."""
        if self.prometheus is None:
            raise RuntimeError("telemetry was not set up with the Prometheus reader")
        server, thread = start_http_server(port, addr=host, registry=self.prometheus)

        def stop() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        return int(server.server_address[1]), stop

    def shutdown(self) -> None:
        """Flush and shut down both providers (process stop)."""
        self.tracer_provider.shutdown()
        self.meter_provider.shutdown()


def setup_telemetry(
    *, service_name: str, env: str, memory: bool = False, otlp_endpoint: str | None = None
) -> TelemetrySetup:
    """Build the providers for one process (`service_name` e.g. "noema-api"; `env` = NOEMA_ENV)."""
    resource = Resource.create({"service.name": service_name, "deployment.environment.name": env})
    exported = memory or bool(otlp_endpoint)
    tracer_provider = (
        TracerProvider(resource=resource) if exported else TracerProvider(resource=resource, sampler=ALWAYS_OFF)
    )
    spans: InMemorySpanExporter | None = None
    if memory:
        spans = InMemorySpanExporter()
        tracer_provider.add_span_processor(SimpleSpanProcessor(spans))
    if otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint)))
    prometheus: CollectorRegistry | None = None
    metrics_memory: InMemoryMetricReader | None = None
    reader: MetricReader
    if memory:
        metrics_memory = InMemoryMetricReader()
        reader = metrics_memory
    else:
        from opentelemetry.exporter.prometheus import PrometheusMetricReader

        prometheus = CollectorRegistry(auto_describe=True)
        reader = PrometheusMetricReader(disable_target_info=True, scope_info_enabled=False, registry=prometheus)
    meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    return TelemetrySetup(
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        reader=reader,
        spans=spans,
        metrics_memory=metrics_memory,
        prometheus=prometheus,
    )
