"""Telemetry conformance (spec-02 §12, IMPL-006): declared names only (UndeclaredTelemetry in test mode),
redacted string attributes, error codes on spans, labelled metrics, and `redact`."""

from __future__ import annotations

import pytest

from noema.core.errors import ErrorCode, NoemaError
from noema.telemetry import UndeclaredTelemetry

from .kits import TelemetryKit
from .registry import kit_fixture

kit = kit_fixture("telemetry")
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop"


async def test_spans_carry_declared_attributes(kit: TelemetryKit) -> None:
    with kit.telemetry.span("decision", **{"aghoo.profile": "router-default"}) as span:
        span.set("aghoo.decision.cache_hit", False)
        with kit.telemetry.span("decision.member", **{"aghoo.member.id": "m1"}):
            pass
    names = [s[0] for s in kit.spans()]
    assert "decision" in names and "decision.member" in names
    decision = next(s for s in kit.spans() if s[0] == "decision")
    assert decision[1]["aghoo.profile"] == "router-default" and decision[1]["aghoo.decision.cache_hit"] is False


async def test_undeclared_names_are_rejected(kit: TelemetryKit) -> None:
    if not kit.strict:
        pytest.skip("non-strict mode drops instead")
    with pytest.raises(UndeclaredTelemetry), kit.telemetry.span("not.a.span"):  # type: ignore[arg-type]
        pass
    with pytest.raises(UndeclaredTelemetry), kit.telemetry.span("turn", **{"aghoo.secret": "x"}):
        pass
    with pytest.raises(UndeclaredTelemetry):
        kit.telemetry.instruments.add("aghoo_nope_total")
    with pytest.raises(UndeclaredTelemetry):
        kit.telemetry.instruments.add("aghoo_cost_usd_total", 1, tenant="t", flavor="x")
    with pytest.raises(UndeclaredTelemetry):
        kit.telemetry.instruments.observe("aghoo_cost_usd_total", 1.0)  # a counter, not a histogram


async def test_string_attributes_are_redacted(kit: TelemetryKit) -> None:
    with kit.telemetry.span("turn", **{"aghoo.tenant": f"t_acme {JWT}"}) as span:
        span.set("aghoo.degradation.reason", "Bearer abcdefghijklmnop")
    [turn] = [s for s in kit.spans() if s[0] == "turn"]
    assert JWT not in str(turn[1]) and "abcdefghijklmnop" not in str(turn[1])


async def test_errors_set_the_error_code(kit: TelemetryKit) -> None:
    with pytest.raises(NoemaError), kit.telemetry.span("generation.attempt"):
        raise NoemaError(ErrorCode.UPSTREAM_STALLED, "stalled")
    with kit.telemetry.span("verification") as span:
        span.error("all_members_failed")
    errors = {s[0]: s[2] for s in kit.spans()}
    assert errors["generation.attempt"] == "upstream_stalled" and errors["verification"] == "all_members_failed"


async def test_metrics(kit: TelemetryKit) -> None:
    ins = kit.telemetry.instruments
    ins.add("aghoo_cost_usd_total", 0.25, tenant="t_acme", line="decision")
    ins.add("aghoo_cost_usd_total", 0.5, tenant="t_acme", line="decision")
    ins.observe("aghoo_ttft_seconds", 0.4, profile="sol-fast", model="m", strategy="holdback")
    ins.set("aghoo_circuit_state", 2, model="m", region="us-east-1")
    ins.add("aghoo_outbox_dropped_total")
    values = kit.metrics()
    assert values[("aghoo_cost_usd_total", (("line", "decision"), ("tenant", "t_acme")))] == pytest.approx(0.75)
    assert values[("aghoo_circuit_state", (("model", "m"), ("region", "us-east-1")))] == 2
    assert ("aghoo_outbox_dropped_total", ()) in values


async def test_c2a_job_and_admission_names_are_declared(kit: TelemetryKit) -> None:
    # labeling.md item 2, media-pipeline.md items 3/13, ops-admission.md item 1
    for name in ("labeling.item", "labeling.run", "labeling.drift"):
        with kit.telemetry.span(name, **{"aghoo.tenant": "t_acme", "aghoo.profile": "router-default"}) as span:
            span.set("aghoo.member.question", "intent")
    with kit.telemetry.span("media.extract", **{"aghoo.media.kind": "ocr", "aghoo.media.status": "ok"}):
        pass
    ins = kit.telemetry.instruments
    ins.add("aghoo_labeling_items_total", question="intent", outcome="kept")
    ins.add("aghoo_calibrator_fits_total", status="accepted")
    ins.add("aghoo_calibrator_downgrades_total", reason="drift_ece")
    ins.set("aghoo_label_class_share", 0.25, profile="router-default", question="intent", **{"class": "coding"})
    ins.add("aghoo_media_jobs_total", kind="ocr", status="ok")
    ins.observe("aghoo_media_job_seconds", 1.5, kind="ocr")
    ins.observe("aghoo_inline_ingest_seconds", 0.2)
    ins.observe("aghoo_sandbox_queue_seconds", 0.1, kind="ocr")
    ins.add("aghoo_admission_rejections_total", code="rpm_exceeded")
    ins.set("aghoo_admission_degraded", 1)
    ins.add("aghoo_admission_refund_failures_total")
    ins.set("aghoo_admission_pending_releases", 2)
    names = {s[0] for s in kit.spans()}
    assert {"labeling.item", "labeling.run", "labeling.drift", "media.extract"} <= names
    values = kit.metrics()
    assert values[("aghoo_labeling_items_total", (("outcome", "kept"), ("question", "intent")))] == 1
    assert values[("aghoo_admission_degraded", ())] == 1


async def test_redact(kit: TelemetryKit) -> None:
    assert kit.telemetry.redact(f"token {JWT}") == "token [REDACTED:jwt]"
    assert kit.telemetry.redact("dec_01J9ZQ7ABCDEFGHJKMNPQRSTVW doc-1") == "dec_01J9ZQ7ABCDEFGHJKMNPQRSTVW doc-1"
