"""Builders shared by the decision unit tests: stage members and slots over the shipped example config."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from tests.fakes.config import example_snapshot
from tests.fakes.labeling import FakeCalibratorRegistry

from noema.config.models import ConfidenceBandsCfg
from noema.core.protocols.config import ConfigSnapshot
from noema.core.protocols.decision import CompiledQuestion
from noema.core.protocols.labeling import CalibratorRegistry
from noema.core.protocols.ops import UsageLine
from noema.core.values import Cost, UsageRecord
from noema.decision.compiler import compile_question
from noema.decision.elicitation.templates import template
from noema.decision.flat import AnswerContext
from noema.decision.signatures import input_view_signature
from noema.decision.state import MemberStatus, Slot, StageInfo, StageMember

BANDS = ConfidenceBandsCfg(high_gte=0.75, low_lt=0.4)
DEPARTMENT: dict[str, Any] = {
    "type": "choice",
    "name": "department",
    "instructions": "Which team should handle this message?",
    "choices": [
        {"value": "billing", "description": "Payments, refunds, invoices"},
        {"value": "technical", "description": "Bugs and errors"},
        {"value": "shipping", "description": "Delivery problems"},
        {"value": "other", "description": None},
    ],
}
NEEDS_HUMAN: dict[str, Any] = {
    "type": "predicate",
    "name": "needs_human",
    "instructions": "The customer explicitly asks to speak with a human.",
    "criteria": {"true": "Asks for a person, agent or call back", "false": "No such request"},
}
URGENCY: dict[str, Any] = {
    "type": "score",
    "name": "urgency",
    "instructions": "How urgent is the request?",
    "levels": [
        {"label": "low", "description": "Can wait days"},
        {"label": "medium", "description": None},
        {"label": "high", "description": "Blocking now"},
    ],
}


def question(wire: Mapping[str, Any], *, limit: int = 16) -> CompiledQuestion:
    return compile_question(wire, source="request", limit=limit, output_format="native", critical=False, param="q")


def stage(profile_id: str = "router-default", views: Sequence[str] | None = None) -> StageInfo:
    snap = example_snapshot()
    profile = snap.decision_profile(profile_id)
    kinds = list(views or ["native"] * len(profile.members))
    members = []
    for i, m in enumerate(profile.members):
        members.append(
            StageMember(
                member_id=f"m{i + 1}",
                cfg=m,
                model=snap.model(m.model),
                template=template(m.template),
                max_choices=snap.template(m.template).max_choices,
                view=None,
                view_kind=kinds[i],  # type: ignore[arg-type]
                by_modality={"image": kinds[i]} if views is not None else {},  # type: ignore[dict-item]
                skipped=kinds[i] in ("skipped", "metadata_only"),
                view_blocks=(),
            )
        )
    sig = input_view_signature([(m.member_id, m.view_kind) for m in members])
    return StageInfo(0, profile, tuple(members), 0.0, 1.0, profile.deadline_ms, profile.min_quorum, sig)


def slots(st: StageInfo, dists: Sequence[Mapping[str, float] | MemberStatus], **extra: Any) -> list[Slot]:
    out = []
    for member, d in zip(st.members, dists, strict=True):
        if isinstance(d, str):
            slot = Slot(member, status=d)
            if d == "skipped":
                slot.skip_reason = "input_view"
        else:
            slot = Slot(member, status="ok", dist=dict(d), **extra)
        out.append(slot)
    return out


def actx(registry: CalibratorRegistry | None = None, *, members: bool = True) -> AnswerContext:
    return AnswerContext(
        "t_acme", "us-east-1", BANDS, registry or FakeCalibratorRegistry(), with_members=members, with_usage=False
    )


class FixedCost:
    """A CostMeter pricing every call at `per_call` USD (estimated); records the usage lines and the
    `(model_alias, region, snapshot)` of every `price` call."""

    def __init__(self, per_call: str = "0.00002") -> None:
        self.per_call = Decimal(per_call)
        self.lines: list[UsageLine] = []
        self.priced: list[tuple[str, str, ConfigSnapshot | None]] = []

    def price(
        self, model_alias: str, region: str, usage: UsageRecord, *, snapshot: ConfigSnapshot | None = None
    ) -> Cost:
        self.priced.append((model_alias, region, snapshot))
        return Cost(self.per_call, True)

    def record(self, line: UsageLine) -> None:
        self.lines.append(line)
