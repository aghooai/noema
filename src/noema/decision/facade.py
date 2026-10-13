"""The OpenAI Decisions-compatible facade: OA request -> native, native -> OA response (spec-06 §12.3; IMPL-047,
IMPL-049, IMPL-056). Shapes only: every answer comes from a Bedrock decision profile (D1).

Question i is named internally by its explicit name when that matches the native `Name` pattern, else `q{i}`;
`x_aghoo` is keyed by the client-visible name (the given name, else `q{i}`). Boolean choice values become the
strings "true"/"false" internally and are mapped back in the response.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from noema.core.errors import ErrorCode, NoemaError
from noema.core.values import JsonValue

from .confidence import normalized_max, ordinal_dispersion
from .pool import r4

NATIVE_NAME: Final = re.compile(r"^(?!q[0-9]+$)[a-z][a-z0-9_]{0,63}$")
AUTO_NAME: Final = re.compile(r"^q[0-9]+$")
MAX_IMAGES: Final = 20
UNRESOLVED_LIMIT: Final = 0.001


@dataclass(frozen=True, slots=True)
class FacadeQuestion:
    """One facade question: `given` (the client's name or None), `internal` (the stored native name),
    `effective` (the client-visible key), `wire` (the internal native question dict) and `values` (the declared
    OA choice values, bools kept) for the reverse mapping."""

    given: str | None
    internal: str
    effective: str
    wire: dict[str, JsonValue]
    values: tuple[str | bool, ...]


def _bad(param: str, message: str, details: Mapping[str, JsonValue] | None = None) -> NoemaError:
    return NoemaError(ErrorCode.SCHEMA_VIOLATION, message, param=param, details=details)


def map_questions(questions: Sequence[Mapping[str, Any]]) -> list[FacadeQuestion]:
    """The internal native questions (spec-06 §12.3 step 3). Raises 400 `schema_violation` for duplicate or
    reserved explicit names and for a `true`/`"true"` collision."""
    out: list[FacadeQuestion] = []
    seen: set[str] = set()
    for i, q in enumerate(questions):
        given = q.get("name")
        if given is not None:
            if not isinstance(given, str) or AUTO_NAME.match(given) or given in seen:
                raise _bad(f"questions[{i}].name", "Explicit names must be unique and not q<digits>.")
            seen.add(given)
        internal = given if isinstance(given, str) and NATIVE_NAME.match(given) else f"q{i}"
        wire: dict[str, JsonValue] = {k: v for k, v in q.items() if k != "name"}
        wire["name"] = internal
        values: tuple[str | bool, ...] = ()
        if q.get("type") == "choice":
            raw = [c.get("value") for c in q.get("choices", [])]
            keys = [("true" if v else "false") if isinstance(v, bool) else str(v) for v in raw]
            if len(set(keys)) != len(keys):
                raise _bad(f"questions[{i}].choices", 'Choice values collide (true and "true").')
            values = tuple(v for v in raw if isinstance(v, str | bool))
            wire["choices"] = [{**c, "value": k} for c, k in zip(q.get("choices", []), keys, strict=True)]
        out.append(FacadeQuestion(given if isinstance(given, str) else None, internal, given or f"q{i}", wire, values))
    return out


def map_input(value: str | Sequence[Mapping[str, Any]]) -> JsonValue:
    """The native input: a string as is; messages with string content as one `input_text` part, image `detail`
    dropped, at most 20 images (400 `schema_violation`, `param: input`, `details.max_images`)."""
    if isinstance(value, str):
        return value
    messages: list[JsonValue] = []
    images = 0
    for msg in value:
        content = msg.get("content")
        parts: list[JsonValue] = []
        if isinstance(content, str):
            parts.append({"type": "input_text", "text": content})
        else:
            for part in content or []:
                if part.get("type") == "input_image":
                    images += 1
                    parts.append({"type": "input_image", "image_url": part.get("image_url")})
                else:
                    parts.append({"type": "input_text", "text": part.get("text", "")})
        messages.append({"role": "user", "content": parts})
    if images > MAX_IMAGES:
        raise _bad("input", f"At most {MAX_IMAGES} input_image parts per request.", {"max_images": MAX_IMAGES})
    return messages


def _emitted(answer: Mapping[str, JsonValue]) -> dict[str, float]:
    out: dict[str, float] = {}
    cands = answer.get("candidates")
    for c in cands if isinstance(cands, list) else []:
        if isinstance(c, Mapping):
            value, p = c.get("value"), c.get("probability")
            key = ("true" if value else "false") if isinstance(value, bool) else str(value)
            out[key] = float(p) if isinstance(p, int | float) else 0.0
    return out


def _unresolved(answer: Mapping[str, JsonValue]) -> float:
    u = answer.get("unresolved_mass", 0)
    return float(u) if isinstance(u, int | float) else 0.0


def oa_answer(fq: FacadeQuestion, answer: Mapping[str, JsonValue]) -> tuple[dict[str, JsonValue], str | None]:
    """One OA answer and, when it was mapped from `unavailable` (or a scored answer with an unresolved mass above
    0.001), the reason for `x_aghoo.mapped_unavailable`."""
    kind = answer.get("type")
    name: JsonValue = fq.given
    if kind == "refusal":
        return {"type": "refusal", "name": name}, None
    if kind == "unavailable":
        reason = answer.get("reason")
        return {"type": "refusal", "name": name}, str(reason) if isinstance(reason, str) else "no_quorum"
    if _unresolved(answer) > UNRESOLVED_LIMIT:
        return {"type": "refusal", "name": name}, "no_capable_member"
    emitted = _emitted(answer)
    wire = fq.wire
    if kind in ("predicate", "noul"):
        p = answer.get("probability")
        return {"type": "predicate", "name": name, "probability": p if isinstance(p, int | float) else 0.0}, None
    if kind == "choice":
        keys = [("true" if v else "false") if isinstance(v, bool) else str(v) for v in fq.values]
        probs = [emitted.get(k, 0.0) for k in keys]
        top = max(range(len(keys)), key=lambda j: (probs[j], -j))
        return {
            "type": "choice",
            "name": name,
            "choice": fq.values[top],
            "confidence": r4(normalized_max(probs[top], len(keys))),
            "probabilities": [{"value": v, "probability": p} for v, p in zip(fq.values, probs, strict=True)],
        }, None
    levels = wire.get("levels")
    labels = [str(lv.get("label")) for lv in levels if isinstance(lv, Mapping)] if isinstance(levels, list) else []
    probs = [emitted.get(label, 0.0) for label in labels]
    score = answer.get("score")
    return {
        "type": "score",
        "name": name,
        "score": score if isinstance(score, int | float) else 0.0,
        "confidence": r4(ordinal_dispersion(probs)),
        "probabilities": [
            {"value": i, "label": label, "probability": p}
            for i, (label, p) in enumerate(zip(labels, probs, strict=True))
        ],
    }, None


def oa_response(
    result: Mapping[str, JsonValue], questions: Sequence[FacadeQuestion], *, model: str, ensemble: bool
) -> dict[str, JsonValue]:
    """The `OADecisionResponse` of a native result (answers in request order); `x_aghoo` only for
    `x-aghoo-include: ensemble`."""
    answers = result.get("answers")
    by_name = {str(a.get("name")): a for a in answers if isinstance(a, Mapping)} if isinstance(answers, list) else {}
    out_answers: list[JsonValue] = []
    calibration: dict[str, JsonValue] = {}
    mapped: dict[str, JsonValue] = {}
    for fq in questions:
        native = by_name.get(fq.internal, {"type": "unavailable", "reason": "no_quorum"})
        oa, reason = oa_answer(fq, native)
        out_answers.append(oa)
        if reason is not None:
            mapped[fq.effective] = reason
        if oa["type"] != "refusal":
            cal = native.get("calibration")
            calibration[fq.effective] = cal.get("status") if isinstance(cal, Mapping) else "uncalibrated"
    usage = result.get("usage")
    u = usage if isinstance(usage, Mapping) else {}

    def tok(key: str) -> int:
        value = u.get(key, 0)
        return value if isinstance(value, int) else 0

    response: dict[str, JsonValue] = {
        "model": model,
        "answers": out_answers,
        "usage": {
            "input_tokens": tok("input_tokens"),
            "input_tokens_details": {
                "cached_tokens": tok("cached_input_tokens"),
                "cache_write_tokens": tok("cache_write_input_tokens"),
            },
            "output_tokens": tok("output_tokens"),
            "output_tokens_details": {"reasoning_tokens": tok("reasoning_tokens")},
            "total_tokens": tok("input_tokens") + tok("output_tokens"),
        },
    }
    if ensemble:
        x: dict[str, JsonValue] = {"decision_id": result.get("id"), "calibration": calibration}
        if mapped:
            x["mapped_unavailable"] = mapped
        response["x_aghoo"] = x
    return response
