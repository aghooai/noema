"""The scored flat answer: calibration, the linear pool, rounding, candidates, votes and confidence (spec-06 §8,
§11; IMPL-055, IMPL-060)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from noema.config.models import ConfidenceBandsCfg
from noema.core.protocols.decision import CompiledQuestion
from noema.core.protocols.labeling import Calibrator, CalibratorRegistry
from noema.core.values import JsonValue

from . import calibration
from .answer import quorum_wire, unscored_answer, vote_base
from .confidence import confidence
from .pool import UNITS, argmax, linear_pool, ordered, r4, round_units, unresolved_units
from .state import Slot, StageInfo


@dataclass(frozen=True, slots=True)
class AnswerContext:
    """What every answer of a run reads: the tenant, the bands, whether members are emitted, the call region."""

    tenant_id: str
    region: str
    bands: ConfidenceBandsCfg
    registry: CalibratorRegistry
    with_members: bool = True
    with_usage: bool = True


def _emit_member(dist: dict[str, float], keys: Sequence[str], u: float) -> dict[str, float]:
    measured = [k for k in keys if k in dist]
    return round_units(dist, measured, u)


def flat_scored(
    q: CompiledQuestion,
    stage: StageInfo,
    slots: Sequence[Slot],
    q_req: int,
    actx: AnswerContext,
    now: float,
) -> dict[str, JsonValue]:
    """The scored answer of a flat question from its ok members (classification already said `scored`)."""
    keys = [o.key for o in q.options]
    ok = [s for s in slots if s.status == "ok" and s.dist is not None]
    key = calibration.binding_key(
        profile_id=stage.profile.id,
        profile_version=stage.profile.version,
        question_signature=q.signature,
        ok_members=[s.member.member_id for s in ok],
        input_view_signature=stage.view_signature,
        method="flat",
    )
    cal = calibration.lookup(actx.registry, actx.tenant_id, key)
    q_m: dict[str, dict[str, float]] = {}
    for s in ok:
        assert s.dist is not None
        mid = s.member.member_id
        q_m[mid] = calibration.apply_member(cal, mid, s.dist, s.unresolved) if cal else s.dist
    raw, u, wsum = linear_pool([(s.member.cfg.weight, q_m[s.member.member_id], s.unresolved) for s in ok])
    quorum = quorum_wire(slots, q_req)
    if wsum <= 0:  # unreachable while Qcount(ok) >= 1 (every counting member has weight > 0)
        return unscored_answer(q, "unavailable", "no_quorum", quorum, None)
    prob = calibration.apply_pooled(cal, raw, u) if cal else raw
    emitted = round_units(prob, keys, u)
    order = ordered(emitted, keys)
    u_units = unresolved_units(u)
    member_emitted = {s.member.member_id: _emit_member(q_m[s.member.member_id], keys, s.unresolved) for s in ok}
    votes = dict.fromkeys(keys, 0)
    tops: dict[str, str | None] = {}
    for s in ok:
        top = argmax(member_emitted[s.member.member_id], keys)
        tops[s.member.member_id] = top
        if top is not None:
            votes[top] += 1
    by_key = {o.key: o for o in q.options}
    is_score = q.type == "score"
    candidates: list[JsonValue] = []
    for k in order:
        cand: dict[str, JsonValue] = {
            "value": by_key[k].value,
            "probability": emitted[k],
            "raw_score": r4(raw.get(k, 0.0)),
            "votes": votes[k],
        }
        if is_score:
            cand["index"] = by_key[k].index
        candidates.append(cand)
    conf = confidence(
        is_score=is_score,
        order=order,
        prob=prob,
        index={o.key: o.index for o in q.options},
        u=u,
        u_emitted=u_units > 0,
        member_buckets=[[q_m[s.member.member_id].get(k, 0.0) for k in order] + [s.unresolved] for s in ok],
        pool_buckets=[raw.get(k, 0.0) for k in order] + [u],
        agreeing=sum(1 for t in tops.values() if t == order[0]),
        responded=len(ok),
        band_basis=emitted[order[0]] if cal is not None else None,
        bands=actx.bands,
    )
    source = calibration.source_of([s.member.cfg.elicitation for s in ok])
    answer: dict[str, JsonValue] = {
        "type": q.type,
        "name": q.name,
        "candidates": candidates,
        "calibration": calibration.calibration_wire(cal, key, source, key.member_subset),
        "confidence": conf,
        "quorum": quorum,
    }
    if u_units > 0:
        answer["unresolved_mass"] = u_units / UNITS
    if actx.with_members:
        answer["members"] = _votes(slots, ok, q_m, member_emitted, cal, wsum, keys, actx, now)
    first = by_key[order[0]]
    if q.type == "choice":
        answer["choice"] = first.value
    elif is_score:
        answer["score"] = r4(sum(by_key[k].index * emitted[k] for k in keys))
        answer["level"] = first.value
    else:
        answer["probability"] = emitted["true"]
    return answer


def _votes(
    slots: Sequence[Slot],
    ok: Sequence[Slot],
    q_m: dict[str, dict[str, float]],
    member_emitted: dict[str, dict[str, float]],
    cal: Calibrator | None,
    wsum: float,
    keys: Sequence[str],
    actx: AnswerContext,
    now: float,
) -> list[JsonValue]:
    ok_ids = {s.member.member_id for s in ok}
    out: list[JsonValue] = []
    for s in slots:
        vote = vote_base(s, now, actx.region, with_usage=actx.with_usage)
        mid = s.member.member_id
        if mid in ok_ids and s.dist is not None:
            vote["weight"] = r4(s.member.cfg.weight / wsum)
            vote["distribution"] = dict(_emit_member(s.dist, keys, s.unresolved)) if cal else dict(member_emitted[mid])
            if cal is not None:
                vote["calibrated_distribution"] = dict(member_emitted[mid])
                vote["calibrator_id"] = cal.id
            if s.censored:
                vote["censored_options"] = list(s.censored)
            units = unresolved_units(s.unresolved)
            if units > 0:
                vote["unresolved_mass"] = units / UNITS
        out.append(vote)
    return out


def flat_votes(slots: Sequence[Slot], keys: Sequence[str], actx: AnswerContext, now: float) -> list[JsonValue]:
    """The members of an unscored flat answer: ok members still carry their own distribution (the schema requires
    one for `ok`), with their weight renormalized over the ok members."""
    ok = [s for s in slots if s.status == "ok" and s.dist is not None]
    wsum = sum(s.member.cfg.weight for s in ok) or 1.0
    q_m = {s.member.member_id: s.dist for s in ok if s.dist is not None}
    emitted = {s.member.member_id: _emit_member(q_m[s.member.member_id], keys, s.unresolved) for s in ok}
    return _votes(slots, ok, q_m, emitted, None, wsum, keys, actx, now)
