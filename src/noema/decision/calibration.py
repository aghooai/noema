"""Calibrator binding and application (spec-06 §8.3-§8.4, §19.8; ADR-003 §1-§2; IMPL-055, IMPL-072).

The binding is stage-bound: the answering stage's profile and version, the question signature, the sorted ok
member ids, the stage's view signature and the choice method. An artifact applies whole (per-member transforms
plus the pooled transform) or not at all; hierarchical answers stay uncalibrated in Phase 1 (no hierarchical
transform is defined, so even a registry hit for one is not applied).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Final, Literal

from noema.core.protocols.labeling import Calibrator, CalibratorBindingKey, CalibratorRegistry
from noema.core.values import JsonValue

_log: Final = logging.getLogger("noema.decision.calibration")
Source = Literal["logprob", "verbalized", "mixed"]


def binding_key(
    *,
    profile_id: str,
    profile_version: int,
    question_signature: str,
    ok_members: Sequence[str],
    input_view_signature: str,
    method: Literal["flat", "hierarchical"],
) -> CalibratorBindingKey:
    """The `CalibratorBindingKey` of one answer of one stage."""
    return CalibratorBindingKey(
        profile_id=profile_id,
        profile_version=str(profile_version),
        question_signature=question_signature,
        member_subset=tuple(sorted(ok_members)),
        input_view_signature=input_view_signature,
        choice_method=method,
    )


def lookup(registry: CalibratorRegistry, tenant_id: str, key: CalibratorBindingKey) -> Calibrator | None:
    """The active artifact for `key` (tenant first, then platform); never one for a hierarchical answer."""
    try:
        cal = registry.lookup(tenant_id, key)
    except Exception:  # a registry bug must not fail an answer: serve uncalibrated
        _log.exception("calibrator lookup failed")
        return None
    if cal is not None and key.choice_method == "hierarchical":
        _log.warning("a hierarchical calibrator is active but Phase 1 defines no hierarchical transform: ignored")
        return None
    return cal


def source_of(elicitations: Sequence[str]) -> Source:
    """`logprob` if every ok member is token_logprobs, `verbalized` if every one is verbalized, else `mixed`."""
    kinds = set(elicitations)
    if kinds == {"token_logprobs"}:
        return "logprob"
    if kinds == {"verbalized"}:
        return "verbalized"
    return "mixed"


def binding_wire(key: CalibratorBindingKey) -> dict[str, JsonValue]:
    """The contract `CalibrationBinding` of a key."""
    return {
        "profile_id": key.profile_id,
        "profile_version": key.profile_version,
        "question_signature": key.question_signature,
        "input_view_signature": key.input_view_signature,
        "choice_method": key.choice_method,
    }


def calibration_wire(
    cal: Calibrator | None, key: CalibratorBindingKey, source: Source, member_subset: Sequence[str]
) -> dict[str, JsonValue]:
    """`Calibration` of an answer: `calibrated` with the artifact's method, id, binding and holdout, else
    `uncalibrated` with the source and member subset."""
    subset: list[JsonValue] = list(member_subset)
    if cal is None:
        return {"status": "uncalibrated", "source": source, "member_subset": subset}
    return {
        "status": "calibrated",
        "source": source,
        "method": cal.method,
        "calibrator_id": cal.id,
        "member_subset": subset,
        "binding": binding_wire(key),
        "holdout": dict(cal.holdout),
    }


def apply_member(cal: Calibrator, member_id: str, dist: Mapping[str, float], unresolved: float) -> dict[str, float]:
    """The member's calibrated distribution (preserves its unresolved mass)."""
    return dict(cal.member(member_id).apply(dist, unresolved))


def apply_pooled(cal: Calibrator, pool: Mapping[str, float], unresolved: float) -> dict[str, float]:
    """The pooled transform over the linear pool (preserves the unresolved mass)."""
    return dict(cal.pooled.apply(pool, unresolved))
