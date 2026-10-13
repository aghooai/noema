"""Calibrator artifacts: `noema.calibrator` rows to runtime `Calibrator`s and back (ADR-003 §1, spec-03 §2).

`calibrator_from_row` builds the applied unit from an `ops.active_calibrators` row (per-member temperatures and the
pooled transform share one epsilon and are only ever applied together). `artifact_row` is the INSERT row of a
freshly fitted tenant artifact (status `fitted`; the gate then moves it with `CalibratorRepo.update`).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from noema.core.protocols.labeling import Calibrator, CalibratorBindingKey, Transform
from noema.core.values import JsonValue
from noema.labeling.calibrators.transforms import IdentityTransform, PlattTransform, TemperatureTransform
from noema.labeling.fitting import FittedParams

_IDENTITY = IdentityTransform()


@dataclass(frozen=True, slots=True)
class MemberTransforms:
    """`Calibrator.member`: the member's temperature transform (identity for a member without one)."""

    by_member: Mapping[str, Transform] = field(default_factory=dict)

    def __call__(self, member_id: str) -> Transform:
        return self.by_member.get(member_id, _IDENTITY)


def _positive(value: object, name: str) -> float:
    number = float(value) if isinstance(value, int | float | Decimal) and not isinstance(value, bool) else math.nan
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{name} must be a positive number")
    return number


def _finite(value: object, name: str) -> float:
    number = float(value) if isinstance(value, int | float | Decimal) and not isinstance(value, bool) else math.nan
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def binding_key(row: Mapping[str, object]) -> CalibratorBindingKey:
    """The binding of a calibrator or label row (member subset sorted)."""
    subset = row["member_subset"]
    if not isinstance(subset, list | tuple) or not subset:
        raise ValueError("member_subset must be a non-empty list")
    method = row.get("choice_method") or "flat"
    if method not in ("flat", "hierarchical"):
        raise ValueError("choice_method must be flat or hierarchical")
    return CalibratorBindingKey(
        profile_id=str(row["profile_id"]),
        profile_version=str(row["profile_version"]),
        question_signature=str(row["question_signature"]),
        member_subset=tuple(sorted(str(m) for m in subset)),
        input_view_signature=str(row["input_view_signature"]),
        choice_method="hierarchical" if method == "hierarchical" else "flat",
    )


def calibrator_from_row(row: Mapping[str, object]) -> Calibrator:
    """The runtime artifact of one calibrator row. ValueError for a malformed row (the registry skips it, so the
    binding stays uncalibrated)."""
    eps = _positive(row.get("epsilon", 1e-4), "epsilon")
    member_t = row["member_t"]
    pooled = row["pooled"]
    holdout = row.get("holdout")
    if not isinstance(member_t, dict) or not isinstance(pooled, dict):
        raise ValueError("member_t and pooled must be objects")
    key = binding_key(row)
    temps = {str(m): _positive(t, f"member_t.{m}") for m, t in member_t.items()}
    if set(temps) != set(key.member_subset):  # ADR-003 §1: a T_m for every member of the subset, and no other
        raise ValueError("member_t must cover exactly the member subset")
    method = row["method"]
    pooled_transform: Transform
    if method == "temperature":
        pooled_transform = TemperatureTransform(_positive(pooled["temperature"], "pooled.temperature"), eps)
    elif method == "platt":
        pooled_transform = PlattTransform(_finite(pooled["a"], "pooled.a"), _finite(pooled["b"], "pooled.b"), eps)
    else:
        raise ValueError(f"unknown calibration method {method!r}")
    scope = row["scope"]
    if scope not in ("tenant", "platform"):
        raise ValueError(f"unknown scope {scope!r}")
    return Calibrator(
        id=str(row["id"]),
        scope="tenant" if scope == "tenant" else "platform",
        method="temperature" if method == "temperature" else "platt",
        epsilon=eps,
        member_T=temps,
        pooled=pooled_transform,
        member=MemberTransforms({m: TemperatureTransform(t, eps) for m, t in temps.items()}),
        holdout=dict(holdout) if isinstance(holdout, dict) else {},
        key=key,
    )


def artifact_row(
    *,
    calibrator_id: str,
    key: CalibratorBindingKey,
    question_name: str,
    params: FittedParams,
    holdout: Mapping[str, JsonValue] | None,
    acceptance: Mapping[str, JsonValue],
    reliability: Mapping[str, JsonValue],
    fitted_at: datetime,
) -> dict[str, object]:
    """The `noema.calibrator` INSERT row of a fitted tenant artifact (holdout `{}` when there is no test split)."""
    pooled: dict[str, float] = {k: float(v) for k, v in params.pooled.items()}
    return {
        "id": calibrator_id,
        "scope": "tenant",
        "profile_id": key.profile_id,
        "profile_version": key.profile_version,
        "question_name": question_name,
        "question_signature": key.question_signature,
        "member_subset": list(key.member_subset),
        "input_view_signature": key.input_view_signature,
        "choice_method": key.choice_method,
        "method": params.method,
        "member_t": {m: float(t) for m, t in params.member_t.items()},
        "pooled": pooled,
        "epsilon": params.epsilon,
        "holdout": dict(holdout) if holdout is not None else {},
        "acceptance": dict(acceptance),
        "reliability": dict(reliability),
        "status": "fitted",
        "fitted_at": fitted_at,
    }


def subset_key(subset: Sequence[str]) -> tuple[str, ...]:
    """The canonical (sorted) member subset."""
    return tuple(sorted(subset))
