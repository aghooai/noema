"""T-ADM-3 at module level (spec-11 §8; the load-run half is QA's): a reservation from `DefaultWorkGraphPlanner`
absorbs every call a turn can issue, so `aghoo_reservation_overrun_total` stays 0. The generation walks are
enumerated independently from IMPL-016 (A1, at most one spec restart, at most 2 failure-driven attempts: the
profile's next member, the default profile's first member, or a same-member throttling retry; race waves start
every member)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator

import pytest
from tests.contract_internal.kits import principal
from tests.fakes.clock import FakeClock
from tests.fakes.config import FakeConfigRegistry, example_snapshot
from tests.fakes.telemetry import FakeTelemetry

from noema.core.protocols.config import ConfigSnapshot
from noema.core.protocols.ops import DecisionWork, GenerationWork, QuestionWork
from noema.core.redis_keys import RedisKeys
from noema.core.values import CallKind
from noema.ops.admission import DefaultWorkGraphPlanner, GcraAdmission

pytestmark = pytest.mark.unit
ROUTER = tuple(
    QuestionWork(n, k, "platform")
    for n, k in (
        ("intent", 10),
        ("complexity", 4),
        ("needs_fresh_info", 2),
        ("sensitive_data", 2),
        ("injection_suspected", 2),
    )
)
VERIFY = (QuestionWork("answers_question", 2, "platform"), QuestionWork("unsupported_claims", 2, "platform"))


def wave(snap: ConfigSnapshot, profile_id: str, index: int = 0) -> tuple[str, ...]:
    p = snap.generation_profile(profile_id)
    return tuple(m.model for m in p.members) if p.mode == "race" else (p.members[index].model,)


def walks(snap: ConfigSnapshot, candidates: tuple[str, ...]) -> Iterator[Counter[str]]:
    """Every attempt sequence a turn may run, as calls per alias."""
    default = snap.routing.default_profile

    def step(profile: str, index: int, restarts: int, failures: int, calls: Counter[str]) -> Iterator[Counter[str]]:
        yield calls
        if restarts == 0:
            for target in candidates:
                yield from step(target, 0, 1, failures, calls + Counter(wave(snap, target)))
        if failures < 2:
            p = snap.generation_profile(profile)
            options = [(profile, index, (p.members[index].model,) if p.mode != "race" else wave(snap, profile))]
            if p.mode == "fallback" and index + 1 < len(p.members):
                options.append((profile, index + 1, (p.members[index + 1].model,)))
            if profile != default:
                options.append((default, 0, wave(snap, default)))
            for target, i, started in options:
                yield from step(target, i, restarts, failures + 1, calls + Counter(started))

    for first in candidates:
        yield from step(first, 0, 0, 0, Counter(wave(snap, first)))


@pytest.mark.parametrize(
    "candidates",
    [("sol-fast", "opus-deep", "glm-reason"), ("race-sol-glm", "sol-fast"), ("opus-deep",), ("glm-reason", "sol-fast")],
)
async def test_tadm3_no_turn_walk_overruns_its_reservation(candidates: tuple[str, ...]) -> None:
    snap = example_snapshot()
    work = [
        DecisionWork("router-default", ROUTER, 1000, "profile_default", 1050, "decision_member", 100),
        DecisionWork("answer-verifier", VERIFY, 4000, "profile_default", None, "verification_member", 100),
    ]
    all_candidates = tuple(dict.fromkeys((*candidates, snap.routing.default_profile)))
    graph = DefaultWorkGraphPlanner().plan(
        snap, principal(), decisions=work, generation=GenerationWork(candidates, 100, None)
    )
    telemetry = FakeTelemetry()
    config = FakeConfigRegistry(snap)
    decision_calls: dict[tuple[str, CallKind], int] = {("deepseek-3.2", "decision_member"): 10}
    decision_calls[("luna-6", "decision_member")] = 5
    decision_calls |= {("haiku-5.5", "decision_member"): 5, ("haiku-5.5", "verification_member"): 2}
    decision_calls |= {("luna-6", "verification_member"): 2}  # every member, every question, DeepSeek's backups
    n_walks = 0
    for calls in walks(snap, all_candidates):
        n_walks += 1
        admission = GcraAdmission(  # one per walk: the walks are alternatives, not a load
            FakeClock(), config, RedisKeys("ns:unit:"), None, env="prod", instruments=telemetry.instruments
        )
        res = await admission.reserve(principal(), graph, queue_timeout_s=1)
        for (alias, kind), n in decision_calls.items():
            for _ in range(n):
                res.consume(kind, alias)
        for alias, n in calls.items():
            for _ in range(n):
                res.consume("generation", alias)
        await res.release()
    assert n_walks > 10
    assert telemetry.counter("aghoo_reservation_overrun_total") == 0
