"""`OtelTelemetry` over the in-memory and Prometheus readers: strict vs lenient registry handling, redacted
attributes, error status, the finish-reasons array, static attributes, Prometheus names, shutdown."""

from __future__ import annotations

import logging

import pytest
from opentelemetry.trace import StatusCode

from noema.core.errors import ErrorCode, NoemaError
from noema.telemetry import OtelTelemetry, UndeclaredTelemetry, setup_telemetry


def memory(strict: bool = True, **kw: object) -> tuple[OtelTelemetry, object]:
    setup = setup_telemetry(service_name="t", env="test", memory=True)
    return OtelTelemetry(setup, env="test", strict=strict, **kw), setup  # type: ignore[arg-type]


def test_nested_spans_and_attributes() -> None:
    tel, setup = memory(static_attributes={"aghoo.credentials.source": "sts"})
    with tel.span("turn", **{"aghoo.tenant": "t_acme"}), tel.span("generation.attempt") as span:
        span.set("gen_ai.response.finish_reasons", "stop")
        span.set("gen_ai.usage.output_tokens", 12)
    spans = {s.name: s for s in setup.spans.get_finished_spans()}  # type: ignore[attr-defined]
    attempt, turn = spans["generation.attempt"], spans["turn"]
    assert attempt.parent is not None and attempt.parent.span_id == turn.context.span_id
    assert attempt.attributes["gen_ai.response.finish_reasons"] == ("stop",)
    assert attempt.attributes["aghoo.credentials.source"] == "sts"
    assert turn.attributes["aghoo.tenant"] == "t_acme"


def test_errors_and_cancellation_status() -> None:
    tel, setup = memory()
    with pytest.raises(NoemaError), tel.span("decision"):
        raise NoemaError(ErrorCode.DEADLINE_EXCEEDED, "late")
    with pytest.raises(ValueError, match="secret detail"), tel.span("preflight"):
        raise ValueError("secret detail sk-123")
    spans = {s.name: s for s in setup.spans.get_finished_spans()}  # type: ignore[attr-defined]
    assert spans["decision"].attributes["error.type"] == "deadline_exceeded"
    assert spans["decision"].status.status_code is StatusCode.ERROR
    assert spans["preflight"].attributes["error.type"] == "internal_error"
    assert not spans["preflight"].events  # exception messages are never recorded


def test_lenient_mode_drops_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    tel, setup = memory(strict=False)
    with caplog.at_level(logging.WARNING, logger="noema.telemetry"):
        with tel.span("not.a.span") as span:  # type: ignore[arg-type]
            span.set("gen_ai.request.model", "m")
        with tel.span("turn", **{"bogus.key": "x"}) as span:
            span.set("other.bogus", 1)
        tel.instruments.add("aghoo_nope_total")
        tel.instruments.add("aghoo_cost_usd_total", 1, tenant="t", flavor="x")
    names = [s.name for s in setup.spans.get_finished_spans()]  # type: ignore[attr-defined]
    assert names == ["turn"]
    [turn] = setup.spans.get_finished_spans()  # type: ignore[attr-defined]
    assert "bogus.key" not in turn.attributes and "other.bogus" not in turn.attributes
    assert sum("telemetry item dropped" in r.message for r in caplog.records) == 5


def test_strict_mode_raises() -> None:
    tel, _ = memory(strict=True)
    with pytest.raises(UndeclaredTelemetry):
        tel.instruments.set("aghoo_cost_usd_total", 1.0)
    with pytest.raises(UndeclaredTelemetry), tel.span("turn") as span:
        span.set("x", 1)


def test_redaction_of_string_attributes() -> None:
    tel, setup = memory()
    with tel.span("sts.assume_role") as span:
        span.set("aghoo.tenant", "AKIAIOSFODNN7EXAMPLE")
        span.error("Bearer abcdefghijklmnop")
    [s] = setup.spans.get_finished_spans()  # type: ignore[attr-defined]
    assert s.attributes["aghoo.tenant"] == "[REDACTED:aws_key_id]"
    assert "abcdefghijklmnop" not in str(s.attributes["error.type"])


def test_prometheus_exposition() -> None:
    setup = setup_telemetry(service_name="t", env="dev")
    tel = OtelTelemetry(setup, env="dev")
    tel.instruments.add("aghoo_cache_misses_total", layer="l2", reason="error")
    tel.instruments.observe("aghoo_decision_quorum_reached_seconds", 0.93, profile="router-default")
    text = setup.prometheus_text().decode()
    assert 'aghoo_cache_misses_total{layer="l2",reason="error"} 1.0' in text
    assert 'aghoo_decision_quorum_reached_seconds_bucket{le="1.0",profile="router-default"} 1.0' in text
    setup.shutdown()
    memory_setup = setup_telemetry(service_name="t", env="test", memory=True)
    with pytest.raises(RuntimeError):
        memory_setup.prometheus_text()


def test_two_setups_are_independent() -> None:
    a, b = setup_telemetry(service_name="a", env="dev"), setup_telemetry(service_name="b", env="dev")
    OtelTelemetry(a, env="dev").instruments.add("aghoo_outbox_dropped_total")
    assert b"aghoo_outbox_dropped_total 1.0" in a.prometheus_text()
    assert b"aghoo_outbox_dropped_total 1.0" not in b.prometheus_text()


def test_without_an_exporter_spans_do_not_record_but_keep_valid_ids() -> None:
    from opentelemetry import trace

    setup = setup_telemetry(service_name="t", env="dev")  # no OTLP endpoint, no memory exporter: nothing reads spans
    tel = OtelTelemetry(setup, env="dev")
    with tel.span("generation.attempt", **{"aghoo.tenant": "t_acme"}) as handle:
        handle.set("aghoo.attempt.outcome", "committed")
        current = trace.get_current_span()
        assert not current.is_recording()
        assert current.get_span_context().is_valid  # log lines still carry a trace_id
    setup.shutdown()
    memory = setup_telemetry(service_name="t", env="test", memory=True)
    with OtelTelemetry(memory, env="test").span("generation.attempt"):
        assert trace.get_current_span().is_recording()
    assert memory.spans is not None and len(memory.spans.get_finished_spans()) == 1
