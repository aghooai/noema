"""`AnswerSummary` from a wire answer: what bindings and conditioning read (spec-02 §5; IMPL-002)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from noema.core.protocols.decision import AnswerSummary
from noema.core.values import JsonValue


def _num(value: JsonValue) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def summarize(answer: Mapping[str, JsonValue], *, final: bool) -> AnswerSummary:
    """The flat summary of one native answer (scored: value, top two candidates, p_max or P(true), margin, band,
    calibrated; refusal/unavailable: the reason only)."""
    name = str(answer.get("name"))
    kind = answer.get("type")
    if kind in ("refusal", "unavailable"):
        reason = answer.get("reason")
        return AnswerSummary(
            name=name,
            type="refusal" if kind == "refusal" else "unavailable",
            value=None,
            top=(),
            probability=None,
            margin=None,
            band=None,
            calibrated=False,
            final=final,
            reason=str(reason) if isinstance(reason, str) else None,
        )
    cands = answer.get("candidates")
    candidates: Sequence[JsonValue] = cands if isinstance(cands, list) else []
    top: list[tuple[str | bool, float]] = []
    for c in candidates[:2]:
        if isinstance(c, Mapping):
            cv = c.get("value")
            if isinstance(cv, str | bool):
                top.append((cv, _num(c.get("probability")) or 0.0))
    conf = answer.get("confidence")
    cal = answer.get("calibration")
    margin = _num(conf.get("margin")) if isinstance(conf, Mapping) else None
    band = conf.get("band") if isinstance(conf, Mapping) else None
    calibrated = isinstance(cal, Mapping) and cal.get("status") == "calibrated"
    value: str | bool | float | None
    if kind == "choice":
        choice = answer.get("choice")
        value = choice if isinstance(choice, str) else None
        probability = top[0][1] if top else None
    elif kind == "score":
        level = answer.get("level")
        value = level if isinstance(level, str) else None
        probability = top[0][1] if top else None
    else:
        probability = _num(answer.get("probability"))
        value = probability
    return AnswerSummary(
        name=name,
        type="choice"
        if kind == "choice"
        else "score"
        if kind == "score"
        else "noul"
        if kind == "noul"
        else "predicate",
        value=value,
        top=tuple(top),
        probability=probability,
        margin=margin,
        band="high" if band == "high" else "medium" if band == "medium" else "low" if band == "low" else None,
        calibrated=calibrated,
        final=final,
    )
