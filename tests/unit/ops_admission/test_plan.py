"""`build_plan` (spec-11 §1.2): bucket names, limits and unit counts, slot sets and caps, the lease."""

from __future__ import annotations

import pytest
from tests.contract_internal.kits import principal
from tests.fakes.clock import FakeClock
from tests.fakes.config import example_snapshot, patched_example, snapshot_from

from noema.core.clock import Deadline
from noema.core.principal import system_principal
from noema.core.protocols.ops import WorkGraph
from noema.core.redis_keys import RedisKeys
from noema.ops.admission.gcra import EPS_MS, WINDOW_MS
from noema.ops.admission.plan import LEASE_GRACE_MS, MAX_REQUEST_MS, build_plan

pytestmark = pytest.mark.unit
KEYS = RedisKeys("ns:unit:")
GRAPH = WorkGraph(
    {("sol-6.1", "generation"): 4, ("luna-6", "decision_member"): 5, ("luna-6", "verification_member"): 2},
    {"sol-6.1": 4000, "luna-6": 900},
    11,
    3000,
    1900,
    1,
)


def test_buckets_slots_and_lease_of_a_turn() -> None:
    limits = {
        "rpm_per_key": 50,
        "concurrent_streams_per_project": 3,
        "model_quotas": {"luna-6": {"rpm": 13, "tpm": 9000}},
    }
    snap = snapshot_from(patched_example({"tenants": {"t_acme": {"limits": limits}}}))
    clock = FakeClock()
    plan = build_plan(snap, KEYS, principal(), GRAPH, Deadline(clock.monotonic() + 2.5, clock), 1_000.0)
    got = [(b.name, b.unit, b.limit, b.n, b.alias) for b in plan.buckets]
    assert got == [
        ("key:key_1:rpm", "requests", 50, 11, None),  # tenant override
        ("key:key_1:tpm", "tokens", 20_000_000, 4900, None),  # platform default
        ("prj:t_acme:p1:rpm", "requests", 30_000, 11, None),
        ("prj:t_acme:p1:tpm", "tokens", 100_000_000, 4900, None),
        ("mdl:t_acme:luna-6:rpm", "requests", 13, 7, "luna-6"),  # both kinds of the alias
        ("mdl:t_acme:luna-6:tpm", "tokens", 9000, 900, "luna-6"),
    ]  # sol-6.1 has no quota: no model bucket
    assert all(b.threshold_ms == WINDOW_MS + EPS_MS for b in plan.buckets) and not plan.labeling
    assert [(s.key, s.cap, s.members) for s in plan.slots] == [
        ("ns:unit:streams:t_acme:p1", 3, (plan.member,)),
        ("ns:unit:streams:t_acme:mdl:luna-6", 3, (plan.member,)),  # ceil(13 / 6)
    ]
    assert plan.member.startswith("req_1:") and plan.slot_keys == {s.key for s in plan.slots}
    assert plan.lease_ms == 1_000.0 + 2_500 + LEASE_GRACE_MS


def test_labeling_plan_has_only_model_buckets_with_headroom() -> None:
    limits = {"model_quotas": {"sol-6.1": {"rpm": 10, "tpm": 1000}}}
    snap = snapshot_from(patched_example({"tenants": {"t_acme": {"limits": limits}}}))
    graph = WorkGraph({("sol-6.1", "labeling"): 2}, {"sol-6.1": 100}, 2, 50, 50, 0)
    who = system_principal("t_acme", purpose="labeling", request_id="req_lab")
    plan = build_plan(snap, KEYS, who, graph, None, 0.0)
    assert [b.name for b in plan.buckets] == ["mdl:t_acme:sol-6.1:rpm", "mdl:t_acme:sol-6.1:tpm"]
    assert plan.labeling and all(b.threshold_ms == 0.8 * WINDOW_MS + EPS_MS for b in plan.buckets)
    assert [s.key for s in plan.slots] == ["ns:unit:streams:t_acme:mdl:sol-6.1"]
    assert [(s.cap, s.limit) for s in plan.slots] == [(2, 1)]  # ceil(10 / 6) places, 20% of them kept free
    assert plan.lease_ms == MAX_REQUEST_MS + LEASE_GRACE_MS  # no deadline: the largest request lifetime


def test_two_streams_take_two_places() -> None:
    graph = WorkGraph({("sol-6.1", "generation"): 1}, {"sol-6.1": 1}, 1, 1, 0, 2)
    plan = build_plan(example_snapshot(), KEYS, principal(), graph, None, 0.0)
    assert plan.slots[0].members == (f"{plan.member}#0", f"{plan.member}#1")
