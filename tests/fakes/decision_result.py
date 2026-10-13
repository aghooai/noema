"""Wire `DecisionResult` builder for the fake decision engine and the golden fixtures (spec-06 §7-§12).

Implements, for one flat stage without calibrators: the linear pool (§8.1), largest-remainder rounding to 4 dp and
candidate order (§8.2), classification at close (§7.3), heuristic confidence (§11), quorum info and the native
result shape (§12.1). Output dicts pass `decision.schema.json` and `check_examples.result_invariants`.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from noema.core.protocols.decision import AnswerSummary, CompiledQuestion, OptionSpec
from noema.core.values import JsonValue, UsageRecord

MemberStatus = Literal["ok", "refused", "timeout", "error", "late", "skipped"]
Band = Literal["high", "medium", "low"]
UnavailableReason = Literal["deadline_exceeded", "no_quorum", "all_members_failed", "no_capable_member"]


@dataclass(frozen=True, slots=True)
class MemberResult:
    """One member's result for one question. `dist` is by option key (status ok only)."""

    member_id: str
    model_alias: str
    model_id: str
    provider: Literal["bedrock-converse", "bedrock-mantle-chat"]
    elicitation: Literal["token_logprobs", "verbalized"]
    weight: float
    status: MemberStatus
    latency_ms: int
    dist: Mapping[str, float] | None = None
    optional: bool = False
    input_view: str = "native"
    error_code: str | None = None
    usage: UsageRecord | None = None
    cost_usd: float = 0.0
    region: str | None = None


@dataclass(frozen=True, slots=True)
class BuiltAnswer:
    """The wire answer dict and its `AnswerSummary`."""

    wire: dict[str, JsonValue]
    summary: AnswerSummary
    responded: int


def round_units(p: Mapping[str, float], keys: Sequence[str], u: float = 0.0) -> dict[str, float]:
    """Largest-remainder rounding to 4 dp over `keys` (declared order breaks ties), spec-06 §8.2."""
    target = 10_000 - round(u * 10_000)
    s = {k: round(p.get(k, 0.0) * 10_000, 9) for k in keys}
    base = {k: math.floor(s[k]) for k in keys}
    rem = {k: round(s[k] - base[k], 9) for k in keys}
    for k in sorted(keys, key=lambda k: (-rem[k], keys.index(k)))[: max(0, target - sum(base.values()))]:
        base[k] += 1
    return {k: base[k] / 10_000 for k in keys}


def r4(x: float) -> float:
    return round(x, 4)


def band_of(value: float, bands: Mapping[str, float]) -> Band:
    return "high" if value >= bands["high_gte"] else "low" if value < bands["low_lt"] else "medium"


def ordinal_dispersion(probs: Sequence[float]) -> float:
    k = len(probs)
    mode = max(range(k), key=lambda i: (probs[i], -i))
    mad_u = sum(abs(i - (k - 1) / 2) for i in range(k)) / k
    return max(0.0, 1 - sum(p * abs(i - mode) for i, p in enumerate(probs)) / mad_u)


def _jsd(p: Sequence[float], q: Sequence[float]) -> float:
    m = [(a + b) / 2 for a, b in zip(p, q, strict=True)]

    def kl(x: Sequence[float], y: Sequence[float]) -> float:
        return sum(a * math.log2(a / b) for a, b in zip(x, y, strict=True) if a > 0 and b > 0)

    return (kl(p, m) + kl(q, m)) / 2


def qcount(members: Sequence[MemberResult], statuses: frozenset[str]) -> int:
    """Distinct aliases among non-optional, positive-weight members with one of `statuses` (spec-06 §7.1)."""
    return len({m.model_alias for m in members if not m.optional and m.weight > 0 and m.status in statuses})


def classify(members: Sequence[MemberResult], q_req: int) -> Literal["scored", "refusal"] | UnavailableReason:
    """spec-06 §7.3 (adequacy assumed)."""
    if qcount(members, frozenset({"ok"})) >= q_req:
        return "scored"
    if qcount(members, frozenset({"ok", "refused"})) >= q_req:
        return "refusal"
    non_skipped = [m for m in members if m.status != "skipped"]
    if qcount(non_skipped, frozenset({"ok", "refused", "timeout", "error", "late"})) < q_req:
        return "no_capable_member"
    if non_skipped and all(m.status == "error" for m in non_skipped):
        return "all_members_failed"
    if any(m.status == "timeout" for m in members):
        return "deadline_exceeded"
    return "no_quorum"


def usage_wire(u: UsageRecord, cost_usd: float = 0.0, cost_estimated: bool = True) -> dict[str, JsonValue]:
    """The wire `Usage` object."""
    return {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cached_input_tokens": u.cached_input_tokens,
        "cache_write_input_tokens": u.cache_write_input_tokens,
        "reasoning_tokens": u.reasoning_tokens,
        "cost_usd": round(cost_usd, 6),
        "cost_estimated": cost_estimated,
        "tokens_estimated": u.tokens_estimated,
    }


def _member_wire(m: MemberResult, keys: Sequence[str], w_tilde: float) -> dict[str, JsonValue]:
    out: dict[str, JsonValue] = {
        "member_id": m.member_id,
        "provider": m.provider,
        "model": m.model_id,
        "elicitation": m.elicitation,
        "input_view": m.input_view,
        "status": m.status,
        "weight": r4(w_tilde) if m.status == "ok" else 0.0,
        "latency_ms": m.latency_ms,
    }
    if m.region is not None:
        out["region"] = m.region
    if m.status == "ok" and m.dist is not None:
        measured = [k for k in keys if k in m.dist]
        out["distribution"] = dict(round_units(m.dist, measured))
    if m.status == "error":
        out["error"] = {"code": m.error_code or "upstream_error", "message": "member call failed"}
    if m.status == "skipped":
        out["skip_reason"] = "input_view"
    if m.usage is not None:
        out["usage"] = usage_wire(m.usage, m.cost_usd)
    return out


@dataclass
class _Pool:
    p: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)


def _pool(ok: Sequence[MemberResult], keys: Sequence[str]) -> _Pool:
    wsum = sum(m.weight for m in ok)
    weights = {m.member_id: m.weight / wsum for m in ok}
    pooled = {k: sum(weights[m.member_id] * (m.dist or {}).get(k, 0.0) for m in ok) for k in keys}
    return _Pool(pooled, weights)


def _declared(q: CompiledQuestion) -> tuple[OptionSpec, ...]:
    return q.options


def _argmax(dist: Mapping[str, float], keys: Sequence[str]) -> str:
    """A member's argmax over its measured options; ties in declared order (spec-06 §8.2)."""
    best = keys[0]
    for k in keys[1:]:
        if dist.get(k, 0.0) > dist.get(best, 0.0):
            best = k
    return best


def build_answer(
    q: CompiledQuestion, members: Sequence[MemberResult], *, q_req: int, bands: Mapping[str, float], final: bool
) -> BuiltAnswer:
    """One wire answer (members always included) plus its summary; pending members must already be `late`."""
    keys = [o.key for o in _declared(q)]
    ok = [m for m in members if m.status == "ok" and m.dist is not None]
    quorum: dict[str, JsonValue] = {
        "required": q_req,
        "responded": len(ok),
        "refused": sum(1 for m in members if m.status == "refused"),
        "total": len(members),
    }
    verdict = classify(members, q_req)
    if verdict != "scored":
        pool_weights: dict[str, float] = {}
        wires = [_member_wire(m, keys, pool_weights.get(m.member_id, 0.0)) for m in members]
        kind: Literal["refusal", "unavailable"] = "refusal" if verdict == "refusal" else "unavailable"
        wire: dict[str, JsonValue] = {"type": kind, "name": q.name, "quorum": quorum, "members": list(wires)}
        reason = None if verdict == "refusal" else verdict
        if reason is not None:
            wire["reason"] = reason
        summary = AnswerSummary(q.name, kind, None, (), None, None, None, False, final, reason)
        return BuiltAnswer(wire, summary, len(ok))
    pool = _pool(ok, keys)
    emitted = round_units(pool.p, keys)
    index = {k: i for i, k in enumerate(keys)}
    order = sorted(keys, key=lambda k: (-emitted[k], index[k]))
    argmaxes = [_argmax(m.dist or {}, keys) for m in ok]
    by_key = {o.key: o for o in q.options}
    candidates: list[JsonValue] = []
    for k in order:
        cand: dict[str, JsonValue] = {
            "value": by_key[k].value,
            "probability": emitted[k],
            "raw_score": r4(pool.p[k]),
            "votes": sum(1 for a in argmaxes if a == k),
        }
        if q.type == "score":
            cand["index"] = by_key[k].index
        candidates.append(cand)
    probs_sorted = sorted(pool.p.values(), reverse=True)
    n = len(keys)
    if q.type == "score":
        method = "ordinal_dispersion"
        conf_value = ordinal_dispersion([pool.p[k] for k in keys])
    else:
        method = "normalized_max"
        conf_value = (probs_sorted[0] - 1 / n) / (1 - 1 / n)
    entropy = -sum(p * math.log(p) for p in pool.p.values() if p > 0) / math.log(n)
    agreement = sum(1 for a in argmaxes if a == order[0]) / len(ok)
    divergence: float | None = None
    if len(ok) > 1:
        pooled = [pool.p[k] for k in keys]
        divergence = sum(_jsd([(m.dist or {}).get(k, 0.0) for k in keys], pooled) for m in ok) / len(ok)
    band = band_of(r4(conf_value), bands)
    elicitations = {m.elicitation for m in ok}
    source = (
        "logprob" if elicitations == {"token_logprobs"} else "verbalized" if elicitations == {"verbalized"} else "mixed"
    )
    wire = {
        "type": q.type,
        "name": q.name,
        "candidates": candidates,
        "calibration": {
            "status": "uncalibrated",
            "source": source,
            "member_subset": [m.member_id for m in sorted(ok, key=lambda m: m.member_id)],
        },
        "confidence": {
            "kind": "heuristic",
            "value": r4(conf_value),
            "method": method,
            "band": band,
            "margin": r4(probs_sorted[0] - probs_sorted[1]),
            "normalized_entropy": r4(entropy),
            "agreement": r4(agreement),
            "member_divergence": None if divergence is None else r4(divergence),
        },
        "quorum": quorum,
        "members": [_member_wire(m, keys, pool.weights.get(m.member_id, 0.0)) for m in members],
    }
    top_key = order[0]
    value: str | bool | float | None
    probability: float
    if q.type in ("predicate", "noul"):
        p_true = emitted["true"]
        wire["probability"] = p_true
        value = probability = p_true
    elif q.type == "choice":
        wire["choice"] = str(by_key[top_key].value)
        value, probability = str(by_key[top_key].value), emitted[top_key]
    else:
        wire["score"] = r4(sum(by_key[k].index * emitted[k] for k in keys))
        wire["level"] = str(by_key[top_key].value)
        value, probability = str(by_key[top_key].value), emitted[top_key]
    top = tuple((by_key[k].value, emitted[k]) for k in order[:2])
    summary = AnswerSummary(
        name=q.name,
        type=q.type,
        value=value,
        top=top,
        probability=probability,
        margin=r4(probs_sorted[0] - probs_sorted[1]),
        band=band,
        calibrated=False,
        final=final,
    )
    return BuiltAnswer(wire, summary, len(ok))


def _iso(at_ms: int) -> str:
    return datetime.fromtimestamp(at_ms / 1000, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_result(
    *,
    decision_id: str,
    status: Literal["quorum", "completed", "partial"],
    answers: Sequence[BuiltAnswer],
    profile_id: str,
    profile_version: int,
    question_signature: str,
    prompt_templates: Mapping[str, str],
    deadline_ms: int,
    trace_id: str,
    started_at_ms: int,
    completed_at_ms: int,
    usage: UsageRecord,
    cost_usd: float = 0.0,
    input_digest_key: str = "",
    view_kinds: Sequence[str] = (),
) -> dict[str, Any]:
    """The native DecisionResult dict of one flat stage (spec-06 §12.1)."""
    smallest = min(answers, key=lambda a: a.responded)
    quorum = smallest.wire["quorum"]
    views = ",".join(f"m{i + 1}:{k}" for i, k in enumerate(view_kinds or ["native"] * len(prompt_templates)))
    seed = int.from_bytes(hashlib.sha256(decision_id.encode()).digest()[:4], "big") & 0x7FFFFFFF
    digest = hashlib.sha256(f"{input_digest_key}|{decision_id}".encode()).hexdigest()
    trace = trace_id if len(trace_id) == 32 else hashlib.sha256(trace_id.encode()).hexdigest()[:32]
    return {
        "id": decision_id,
        "object": "decision",
        "created_at": started_at_ms // 1000,
        "status": status,
        "answers": [a.wire for a in answers],
        "provenance": {
            "trace_id": trace,
            "profile_id": profile_id,
            "profile_version": str(profile_version),
            "question_signature": question_signature,
            "input_digest": "hmac-sha256:" + digest,
            "input_view_signature": "sha256:" + hashlib.sha256(views.encode()).hexdigest(),
            "option_order_seed": seed,
            "prompt_templates": dict(prompt_templates),
            "aggregation": {"method": "linear_pool"},
            "deadline_ms": deadline_ms,
            "quorum": quorum,
            "cache": {"hit": False},
            "stages": [],
            "started_at": _iso(started_at_ms),
            "completed_at": _iso(max(completed_at_ms, started_at_ms)),
        },
        "usage": usage_wire(usage, cost_usd),
    }
