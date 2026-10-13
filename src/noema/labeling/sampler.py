"""`Sampler`: the non-blocking labeling offer (spec-02 §9, spec-06 §16, spec-11 §3; ADR-003 §5).

`offer()` runs on the decision finalize path, so it is sync, allocation-light and never raises. It labels only
final scored answers (not refusal/unavailable, and not hierarchical choices: Phase 1 fits flat bindings only). The
draws are deterministic per decision id: `uniform` when `draw(uniform) < labeling.sample.uniform_rate`, else
`uncertainty` when some labelable answer is in the low band and `draw(uncertainty) < uncertainty_rate`. At most
`max_per_day_per_tenant` offers per UTC day and tenant are made, split evenly over `expected_pods` (the per-pod
share is `ceil(cap / pods)`; the IMPL-078 local-limit convention); the `labeling.item` job then enforces the cap
exactly cluster-wide (`RedisKeys.labeling_daily`), which trims the rounding excess and a pod count above
`expected_pods`. The uniform slice (the only one ECE, fitting and drift read) has priority and is not truncated to
the early hours of a day: when the previous day's uniform demand (draws under `uniform_rate`) exceeded the share,
today's uniform draws are thinned by `share / demand` with a third deterministic draw, so the slice spreads over the
whole day. Uncertainty offers get only the room the uniform slice is not expected to need (`share - min(share,
previous demand)`; half the share before the first full day). Residual bias: a day whose uniform demand jumps above
the previous day's still fills the share early. A sampled offer is `Outbox.put(OutboxRecord(kind="labeling_offer",
id=decision_id, payload={decision_id, turn_id, question_names, sample_reason}))`, applied as a `labeling.item` job;
the counters move only once the put succeeded.
"""

from __future__ import annotations

import hashlib
import logging
import math
from collections.abc import Mapping
from datetime import date
from typing import Final

from noema.core.clock import Clock
from noema.core.protocols.config import ConfigRegistry
from noema.core.protocols.decision import DecisionSnapshot
from noema.core.protocols.labeling import ContentRef
from noema.core.protocols.storage import Outbox, OutboxRecord
from noema.core.values import JsonValue

_TWO_64: Final = float(2**64)
_UNLABELABLE: Final = frozenset({"refusal", "unavailable"})
_log: Final = logging.getLogger("noema.labeling.sampler")


def draw(decision_id: str, salt: str) -> float:
    """A deterministic uniform draw in [0, 1) per (salt, decision id)."""
    digest = hashlib.sha256(f"{salt}:{decision_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / _TWO_64


def _hierarchical(result: Mapping[str, JsonValue]) -> frozenset[str]:
    answers = result.get("answers")
    if not isinstance(answers, list):
        return frozenset()
    return frozenset(
        str(a.get("name")) for a in answers if isinstance(a, dict) and a.get("choice_method") == "hierarchical"
    )


class Sampler:
    """Implements `LabelingSampler` (see the module docstring)."""

    def __init__(self, clock: Clock, config: ConfigRegistry, outbox: Outbox, *, expected_pods: int = 1) -> None:
        self._clock = clock
        self._config = config
        self._outbox = outbox
        self._pods = max(1, expected_pods)
        self._day: date | None = None
        self._uniform: dict[str, int] = {}  # offers made today, per tenant and reason
        self._uncertainty: dict[str, int] = {}
        self._demand: dict[str, int] = {}  # uniform draws today (before the cap)
        self._forecast: dict[str, int] = {}  # the previous day's uniform demand

    def offer(self, tenant_id: str, snapshot: DecisionSnapshot, ref: ContentRef) -> None:
        """Sample in memory and put the offer; errors are logged, never raised."""
        try:
            self._offer(tenant_id, snapshot, ref)
        except Exception:
            _log.exception("labeling offer failed", extra={"decision_id": ref.decision_id})

    def _offer(self, tenant_id: str, snapshot: DecisionSnapshot, ref: ContentRef) -> None:
        sample = self._config.current().labeling.sample
        if sample.uniform_rate <= 0.0 and sample.uncertainty_rate <= 0.0:
            return
        hier = _hierarchical(snapshot.result)
        names = [a.name for a in snapshot.answers.values() if a.type not in _UNLABELABLE and a.name not in hier]
        if not names:
            return
        reason: str | None = None
        if draw(ref.decision_id, "uniform") < sample.uniform_rate:
            reason = "uniform"
        elif (
            sample.uncertainty_rate > 0.0
            and draw(ref.decision_id, "uncertainty") < sample.uncertainty_rate
            and any(a.band == "low" for a in snapshot.answers.values() if a.name in names)
        ):
            reason = "uncertainty"
        if reason is None:
            return
        cap = sample.max_per_day_per_tenant
        if cap is not None and not self._admit(tenant_id, ref.decision_id, reason, math.ceil(cap / self._pods)):
            return
        payload: dict[str, JsonValue] = {
            "decision_id": ref.decision_id,
            "turn_id": ref.turn_id,
            "question_names": list(names),
            "sample_reason": reason,
        }
        self._outbox.put(OutboxRecord(kind="labeling_offer", id=ref.decision_id, tenant_id=tenant_id, payload=payload))
        counts = self._uniform if reason == "uniform" else self._uncertainty
        counts[tenant_id] = counts.get(tenant_id, 0) + 1

    def _admit(self, tenant_id: str, decision_id: str, reason: str, share: int) -> bool:
        """The per-pod daily share: uniform first and spread over the day, uncertainty in the spare room."""
        today = self._clock.now().date()
        if today != self._day:
            self._forecast = self._demand if self._day is not None else {}
            self._day, self._uniform, self._uncertainty, self._demand = today, {}, {}, {}
        if reason == "uniform":
            self._demand[tenant_id] = self._demand.get(tenant_id, 0) + 1
        uniform, uncertainty = self._uniform.get(tenant_id, 0), self._uncertainty.get(tenant_id, 0)
        if uniform + uncertainty >= share:
            return False
        forecast = self._forecast.get(tenant_id)
        if reason == "uniform":
            return forecast is None or forecast <= share or draw(decision_id, "uniform-cap") < share / forecast
        reserve = share // 2 if forecast is None else min(share, forecast)
        return uncertainty < share - reserve
