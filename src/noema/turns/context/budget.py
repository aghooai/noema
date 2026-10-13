"""Candidate generation profiles and the context budget (spec-04 §5.1, §9; spec-11 §1.1; IMPL-045, IMPL-084).

The history and derived-text budgets are model-independent: they use the worst case over the turn's candidate
members `M` (the smallest context window, the largest `max_tokens`).
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from noema.config.models import GEN_DEFAULT_MAX_TOKENS
from noema.core.errors import NoemaError

if TYPE_CHECKING:
    from noema.config.models import GenMemberCfg, ModelCfg
    from noema.core.protocols.config import ConfigSnapshot

SAFETY_MARGIN: Final = 0.05
_ROUTE_REF: Final = re.compile(r"^intent_routes\[[a-z][a-z0-9_]*\]$")


def attempt_max_tokens(model: ModelCfg, spec_max: int | None, member_max: int | None) -> int:
    """`ModelRequest.max_tokens` of one attempt (spec-04 §5.1, IMPL-084):
    `min(cap, (spec ?? member ?? cap ?? 4096) + reasoning_reserve_tokens)`, no `min` without a cap."""
    cap = model.max_output_tokens_cap
    base = spec_max or member_max or cap or GEN_DEFAULT_MAX_TOKENS
    total = base + model.reasoning_reserve_tokens
    return total if cap is None else min(cap, total)


def candidate_profiles(snapshot: ConfigSnapshot, *, spec0_profile: str, pinned: str | None) -> tuple[str, ...]:
    """spec-11 §1.1: the pinned profile if the request pins one, else spec0's profile, every profile a binding or
    `intent_routes` can select, `uncertain_profile` and `default_profile`; `default_profile` is always included
    (the IMPL-016 failure fallback). Sorted; unknown ids dropped."""
    routing = snapshot.routing
    names: set[str] = {routing.default_profile}
    if pinned is not None:
        names.add(pinned)
    else:
        names |= {spec0_profile, routing.uncertain_profile, *routing.intent_routes.values()}
        for b in routing.bindings:
            target = b.then.generation_profile
            if target is not None and _ROUTE_REF.match(target) is None:
                names.add(target)
    return tuple(name for name in sorted(names) if _exists(snapshot, name))


def _exists(snapshot: ConfigSnapshot, profile_id: str) -> bool:
    try:
        snapshot.generation_profile(profile_id)
    except NoemaError:
        return False
    return True


def worst_case_out(snapshot: ConfigSnapshot, member: GenMemberCfg, pins: Iterable[int]) -> int:
    """`out(m)` of spec-11 §1.1: the largest max_tokens any decided spec can give this member."""
    model = snapshot.model(member.model)
    cap = model.max_output_tokens_cap
    candidates = [*pins, member.max_output_tokens or cap or GEN_DEFAULT_MAX_TOKENS]
    total = max(candidates) + model.reasoning_reserve_tokens
    return total if cap is None else min(cap, total)


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """`C` = the smallest context window of `M`, `O` = the largest worst-case max_tokens, `R` = ceil(5 % of C)."""

    context_window: int
    max_output: int
    reserve: int

    def available(self, used: int) -> int:
        """`C − O − R − used`, floored at 0."""
        return max(0, self.context_window - self.max_output - self.reserve - used)


def context_budget(snapshot: ConfigSnapshot, profiles: Sequence[str], request_max: int | None) -> ContextBudget:
    """The IMPL-045 budget over the members of `profiles`."""
    pins = [p for p in (request_max,) if p is not None]
    pins += [b.then.max_output_tokens for b in snapshot.routing.bindings if b.then.max_output_tokens is not None]
    windows: list[int] = []
    outs: list[int] = []
    for name in profiles:
        for member in snapshot.generation_profile(name).members:
            windows.append(snapshot.model(member.model).context_window)
            outs.append(worst_case_out(snapshot, member, pins))
    c = min(windows) if windows else 0
    return ContextBudget(c, max(outs, default=0), math.ceil(SAFETY_MARGIN * c))


def member_aliases(snapshot: ConfigSnapshot, profiles: Sequence[str]) -> tuple[str, ...]:
    """Every member model alias of `profiles`, sorted and unique."""
    return tuple(sorted({m.model for p in profiles for m in snapshot.generation_profile(p).members}))


def profile_usable(
    snapshot: ConfigSnapshot,
    profile_id: str,
    *,
    allowed_models: Sequence[str] | None,
    est_input: int,
    spec_max: int | None,
) -> bool:
    """The hard filter (spec-04 §5.1, IMPL-084): the profile exists, is servable in Phase 1 (not best_of_n/fuse)
    and has a member whose model is allowed for the tenant and fits `est_input + max_tokens <= context_window`."""
    if not _exists(snapshot, profile_id):
        return False
    profile = snapshot.generation_profile(profile_id)
    if profile.mode in ("best_of_n", "fuse"):
        return False
    for member in profile.members:
        if allowed_models is not None and member.model not in allowed_models:
            continue
        model = snapshot.model(member.model)
        if est_input + attempt_max_tokens(model, spec_max, member.max_output_tokens) <= model.context_window:
            return True
    return False
