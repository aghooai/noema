"""`output_format: "jev"`: native result -> `JevDecisionResponse` (spec-06 §12.2; IMPL-049).

Every Jev number is a function of the 2-dp map: `probabilities = round(native emitted p, 2)` per declared
option (censored -> 0.0); `choice` is the argmax of that map (declared-order ties); `score` and `confidence` are
computed over the map. A map whose sum strays beyond `max(0.011, 0.005 K)` (only a large censored tail can) becomes
`unavailable / no_capable_member`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from noema.core.protocols.decision import CompiledQuestion
from noema.core.values import JsonValue

from .confidence import normalized_max, ordinal_dispersion

JTOL = 0.011


def _emitted(answer: Mapping[str, JsonValue]) -> dict[str, float]:
    out: dict[str, float] = {}
    cands = answer.get("candidates")
    for c in cands if isinstance(cands, list) else []:
        if isinstance(c, Mapping):
            value, p = c.get("value"), c.get("probability")
            key = ("true" if value else "false") if isinstance(value, bool) else str(value)
            out[key] = float(p) if isinstance(p, int | float) else 0.0
    return out


def _sum_ok(values: Sequence[float]) -> bool:
    return abs(sum(values) - 1) <= max(JTOL, 0.005 * len(values))


def jev_answer(q: CompiledQuestion, answer: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """One Jev answer from one native answer."""
    kind = answer.get("type")
    if kind == "refusal":
        return {"type": "refusal"}
    if kind == "unavailable":
        return {"type": "unavailable", "reason": answer.get("reason")}
    emitted = _emitted(answer)
    if kind in ("predicate", "noul"):
        p = answer.get("probability")
        return {"type": "noul", "noul": round(float(p), 2) if isinstance(p, int | float) else 0.0}
    keys = [o.key for o in q.options]
    rounded = [round(emitted.get(k, 0.0), 2) for k in keys]
    if not _sum_ok(rounded):
        return {"type": "unavailable", "reason": "no_capable_member"}
    if kind == "choice":
        top = max(range(len(keys)), key=lambda i: (rounded[i], -i))
        return {
            "type": "choice",
            "choice": keys[top],
            "probabilities": dict(zip(keys, rounded, strict=True)),
            "confidence": round(normalized_max(rounded[top], len(keys)), 2),
        }
    return {
        "type": "score",
        "score": round(sum(i * v for i, v in enumerate(rounded)), 2),
        "legend": {str(o.index): o.description if o.description is not None else o.key for o in q.options},
        "probabilities": {str(i): v for i, v in enumerate(rounded)},
        "confidence": round(ordinal_dispersion(rounded), 2),
    }


def jev_response(
    result: Mapping[str, JsonValue], questions: Sequence[CompiledQuestion], *, model: str
) -> dict[str, JsonValue]:
    """The `JevDecisionResponse`: `model` = `aghoo/{stage-0 profile}@{version}`, answers keyed by name in answer
    order, `usage` = {input_tokens, output_tokens} of the total, `decision_id` = the stored native id."""
    answers = result.get("answers")
    by_name = {str(a.get("name")): a for a in answers if isinstance(a, Mapping)} if isinstance(answers, list) else {}
    usage = result.get("usage")
    u = usage if isinstance(usage, Mapping) else {}
    return {
        "model": model,
        "answers": {q.name: jev_answer(q, by_name.get(q.name, {"type": "unavailable"})) for q in questions},
        "usage": {"input_tokens": u.get("input_tokens", 0), "output_tokens": u.get("output_tokens", 0)},
        "decision_id": result.get("id"),
    }
