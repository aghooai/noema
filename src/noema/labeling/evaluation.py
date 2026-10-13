"""Fit one binding and record its evidence (06 §2.3 steps 1-4; ADR-003 §1, §3).

`fit_and_evaluate` splits the consensus-labeled uniform examples (time-ordered, class-stratified, the test split
the most recent), fits the artifact on the calibration split, and evaluates both the calibrated and the uncalibrated
(raw linear pool) probabilities on the test split. It returns the parameters, the contract `HoldoutMetrics` dict
(None when the test split is empty) and the `Reliability` dict (`bins` + bootstrap `ci`) of the calibrator row.
Values are rounded to 6 decimals before they are recorded; the gate reads the recorded values.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from noema.core.values import JsonValue
from noema.labeling.consensus import SampledItem, consensus_stats, family_overlap_lift, retained_share
from noema.labeling.dataset import LabeledSet, arrays, split
from noema.labeling.fitting import FittedParams, fit_params, predict
from noema.labeling.metrics import Scores, bootstrap_ci, reliability_bins, scores


@dataclass(frozen=True, slots=True)
class Evaluation:
    params: FittedParams
    holdout: dict[str, JsonValue] | None
    reliability: dict[str, JsonValue]
    calibrated: Scores | None
    uncalibrated: Scores | None
    n_calibration: int


def _r(v: float) -> float:
    return round(v, 6)


def _seed(*parts: str) -> int:
    return int.from_bytes(hashlib.sha256("|".join(parts).encode()).digest()[:8], "big")


def iso_z(at: datetime) -> str:
    """RFC 3339 UTC with milliseconds and `Z`."""
    return at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def fit_and_evaluate(
    ls: LabeledSet,
    sampled: Sequence[SampledItem],
    *,
    dataset_id: str,
    labelers: Sequence[str],
    lift_threshold: float,
    class_n_min: int,
    view_signature: str,
    evaluated_at: datetime,
    epsilon: float,
) -> Evaluation:
    """See the module docstring. `sampled` are all sampled items of the binding (kept and dropped, both slices) for
    the consensus rates and the family-overlap lift; `labelers` are their Bedrock model ids."""
    calib, test = split(ls)
    params = fit_params(ls, arrays(ls, calib), epsilon)
    seed = _seed(dataset_id, view_signature, ",".join(ls.member_ids))
    if not test:
        empty: dict[str, JsonValue] = {"bins": [], "ci": {}}
        return Evaluation(params, None, empty, None, None, len(calib))
    a = arrays(ls, test)
    cal_probs, raw_probs = predict(ls, a, params), predict(ls, a, None)
    cal, raw = scores(ls.kind, ls.options, cal_probs, a.y), scores(ls.kind, ls.options, raw_probs, a.y)
    n_by_class = dict.fromkeys(ls.options, 0)
    for i in test:
        n_by_class[ls.examples[i].label] += 1
    stats = consensus_stats(sampled, ls.options)
    lift = family_overlap_lift(sampled, ls.member_ids, labelers)
    holdout: dict[str, JsonValue] = {
        "dataset_id": dataset_id,
        "label_source": "llm_consensus",
        "labelers": list(labelers),
        "consensus_rate": _r(stats.rate or 0.0),
        "consensus_rate_by_class": {c: _r(r) for c, r in stats.by_class.items()},
        "n": len(test),
        "n_by_class": dict(n_by_class),
        "ece": _r(cal.ece),
        "brier": _r(cal.brier),
        "nll": _r(cal.nll),
        "nll_uncalibrated": _r(raw.nll),
        "flagged_classes": [c for c in ls.options if n_by_class[c] < class_n_min],
        "scope": {
            "target": "llm_consensus_agreement",
            "retained_share": _r(retained_share(sampled)),
            "slices": [
                {"name": f"input_view:{view_signature}", "n": len(test)},
                {"name": "member_subset:" + ",".join(ls.member_ids), "n": len(test)},
            ],
        },
        "family_overlap": {"max_member_lift": _r(lift), "threshold": lift_threshold},
        "evaluated_at": iso_z(evaluated_at),
    }
    reliability: dict[str, JsonValue] = {
        "bins": reliability_bins(ls.kind, ls.options, cal_probs, a.y),
        "ci": bootstrap_ci(ls.kind, ls.options, cal_probs, a.y, seed=seed),
    }
    return Evaluation(params, holdout, reliability, cal, raw, len(calib))
