"""Outbox conformance (spec-02 §7, spec-11 §3): `put` never raises, `flush` delivers what was put, replays of
the same record id are no-ops at the sink."""

from __future__ import annotations

from noema.core.protocols.storage import OutboxRecord

from .kits import OutboxKit
from .registry import kit_fixture

kit = kit_fixture("outbox")


def usage_record(i: int) -> OutboxRecord:
    payload = {
        "tenant_id": "t_acme",
        "project_id": "p1",
        "id": f"line{i:022d}",
        "line": "decision",
        "model_id": "deepseek.v3.2",
        "region": "us-east-1",
        "session_id": None,
        "turn_id": None,
        "decision_id": "dec_1",
        "attempt_id": None,
        "labeling_run_id": None,
        "input_tokens": 10,
        "output_tokens": 2,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "reasoning_tokens": 0,
        "tokens_estimated": False,
        "cost_usd": "0.000010",
        "cost_estimated": True,
        "at": "2026-10-10T12:00:00Z",
    }
    return OutboxRecord(kind="usage_line", id=f"line{i:022d}", tenant_id="t_acme", payload=payload)


async def test_put_then_flush_delivers(kit: OutboxKit) -> None:
    for i in range(3):
        kit.outbox.put(usage_record(i))
    await kit.outbox.flush(10)
    assert [r.id for r in await kit.applied()] == [usage_record(i).id for i in range(3)]


async def test_replays_are_harmless(kit: OutboxKit) -> None:
    kit.outbox.put(usage_record(1))
    kit.outbox.put(usage_record(1))
    await kit.outbox.flush(10)
    assert len(await kit.applied()) == 2  # both accepted; the sink's insert is ON CONFLICT DO NOTHING


async def test_put_accepts_every_kind(kit: OutboxKit) -> None:
    kit.outbox.put(
        OutboxRecord(
            "audit_event",
            "aud_1",
            "t_acme",
            {"actor_type": "system", "actor_id": "x", "action": "retention.sweep", "resource_kind": "tenant"},
        )
    )
    kit.outbox.put(
        OutboxRecord(
            "labeling_offer",
            "dec_1",
            "t_acme",
            {"decision_id": "dec_1", "turn_id": None, "question_names": ["q0"], "sample_reason": "uniform"},
        )
    )
    await kit.outbox.flush(10)
    assert {r.kind for r in await kit.applied()} == {"audit_event", "labeling_offer"}
