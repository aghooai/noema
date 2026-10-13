"""Usage sums of member calls (spec-06 §6.5; IMPL-020): per member, per stage, per decision and per usage line."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from noema.core.values import Cost, JsonValue, UsageRecord

from .state import ZERO_USAGE, CallRec


@dataclass(slots=True)
class UsageSum:
    """A running sum of per-call usage and per-call rounded cost."""

    usage: UsageRecord = ZERO_USAGE
    usd: Decimal = Decimal(0)
    cost_estimated: bool = False

    def add(self, usage: UsageRecord, cost: Cost | None) -> None:
        self.usage = self.usage + usage
        if cost is not None:
            self.usd += cost.usd
            self.cost_estimated = self.cost_estimated or cost.estimated

    def add_calls(self, calls: Iterable[CallRec]) -> UsageSum:
        """Add every completed or cancelled (estimated) call; failed and pending calls carry no usage."""
        for c in calls:
            if c.outcome in ("completed", "cancelled"):
                self.add(c.usage, c.cost)
        return self

    def merge(self, other: UsageSum) -> None:
        self.usage = self.usage + other.usage
        self.usd += other.usd
        self.cost_estimated = self.cost_estimated or other.cost_estimated

    @property
    def cost(self) -> Cost:
        return Cost(usd=self.usd, estimated=self.cost_estimated)

    def wire(self) -> dict[str, JsonValue]:
        """The contract `Usage` object."""
        u = self.usage
        return {
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cached_input_tokens": u.cached_input_tokens,
            "cache_write_input_tokens": u.cache_write_input_tokens,
            "reasoning_tokens": u.reasoning_tokens,
            "cost_usd": float(self.usd),
            "cost_estimated": self.cost_estimated,
            "tokens_estimated": u.tokens_estimated,
        }


def zero_usage_wire() -> dict[str, JsonValue]:
    """A zeroed `Usage` (L2 hits)."""
    return UsageSum().wire()


def usage_of_wire(wire: dict[str, JsonValue]) -> UsageSum:
    """The `UsageSum` of a contract `Usage` object (the fill-time usage of an L2 artifact)."""

    def i(key: str) -> int:
        value = wire.get(key, 0)
        return value if isinstance(value, int) else 0

    usage = UsageRecord(
        input_tokens=i("input_tokens"),
        output_tokens=i("output_tokens"),
        cached_input_tokens=i("cached_input_tokens"),
        cache_write_input_tokens=i("cache_write_input_tokens"),
        reasoning_tokens=i("reasoning_tokens"),
        tokens_estimated=wire.get("tokens_estimated") is True,
    )
    cost = wire.get("cost_usd", 0)
    usd = Decimal(str(cost)) if isinstance(cost, int | float) else Decimal(0)
    return UsageSum(usage=usage, usd=usd, cost_estimated=wire.get("cost_estimated") is True)
