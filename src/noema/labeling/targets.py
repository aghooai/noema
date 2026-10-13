"""The labelable answers of a stored decision row (IMPL-071, ADR-003 §1, check_examples `answering_stage`).

A target is one sampled answer: its wire question (from `decision.questions`), the stage that answered it (the last
escalation stage listing it, else the provenance stage 0: profile id, version and that stage's input-view
signature), the sorted ids of its ok members, and the `ensemble` document of `records.py`. Refusal, unavailable and
hierarchical answers are not labelable in Phase 1, nor are answers without an ok member distribution.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from noema.core.values import JsonValue
from noema.labeling.labelers import LabelQuestion, question_from_wire


@dataclass(frozen=True, slots=True)
class Target:
    question: LabelQuestion
    profile_id: str
    profile_version: str
    view_signature: str
    member_subset: tuple[str, ...]
    ensemble: dict[str, JsonValue]


def _stage(result: Mapping[str, object], name: str) -> tuple[str, str, str] | None:
    p = result.get("provenance")
    if not isinstance(p, dict):
        return None
    stages = p.get("stages")
    for stage in reversed(stages if isinstance(stages, list) else []):
        if isinstance(stage, dict) and name in (stage.get("questions") or []):
            view = stage.get("input_view_signature", p.get("input_view_signature"))
            return str(stage["profile_id"]), str(stage["profile_version"]), str(view)
    return str(p["profile_id"]), str(p["profile_version"]), str(p["input_view_signature"])


def _members(answer: Mapping[str, object]) -> dict[str, JsonValue]:
    out: dict[str, JsonValue] = {}
    raw = answer.get("members")
    for m in raw if isinstance(raw, list) else []:
        if not isinstance(m, dict) or m.get("status") != "ok" or not isinstance(m.get("distribution"), dict):
            continue
        dist: dict[str, JsonValue] = {str(k): float(v) for k, v in m["distribution"].items()}
        out[str(m["member_id"])] = {
            "dist": dist,
            "unresolved": float(m.get("unresolved_mass") or 0.0),
            "weight": float(m.get("weight") or 0.0),
        }
    return out


def _top(answer: Mapping[str, object]) -> tuple[JsonValue, float | None]:
    candidates = answer.get("candidates")
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
        return None, None
    best = candidates[0]
    probability = best.get("probability")
    value = best.get("value")
    return (value if isinstance(value, str | bool) else None), (
        float(probability) if isinstance(probability, int | float) else None
    )


def _field(answer: Mapping[str, object], obj: str, key: str) -> str | None:
    doc = answer.get(obj)
    value = doc.get(key) if isinstance(doc, dict) else None
    return value if isinstance(value, str) else None


def targets(row: Mapping[str, object], names: Sequence[str]) -> list[Target]:
    """The labelable targets among `names`, in the row's answer order."""
    questions = row.get("questions")
    result = row.get("result")
    if not isinstance(questions, list) or not isinstance(result, dict):
        return []
    answers = {a.get("name"): a for a in result.get("answers") or [] if isinstance(a, dict)}
    wanted = set(names)
    out: list[Target] = []
    for wire in questions:
        if not isinstance(wire, dict) or wire.get("name") not in wanted:
            continue
        answer = answers.get(wire.get("name"))
        if answer is None or answer.get("type") in ("refusal", "unavailable"):
            continue
        if answer.get("choice_method") == "hierarchical":
            continue
        try:
            question = question_from_wire(wire)
            stage = _stage(result, question.name)
        except (KeyError, TypeError, ValueError):
            continue
        members = _members(answer)
        if stage is None or not members:
            continue
        value, probability = _top(answer)
        ensemble: dict[str, JsonValue] = {
            "value": value,
            "probability": probability,
            "band": _field(answer, "confidence", "band"),
            "calibration_status": _field(answer, "calibration", "status") or "uncalibrated",
            "kind": question.kind,
            "options": list(question.keys),
            "members": members,
        }
        out.append(Target(question, *stage, tuple(sorted(members)), ensemble))
    return out
