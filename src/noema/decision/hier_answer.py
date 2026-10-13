"""The scored hierarchical answer (spec-06 §19.6-§19.8): groups, candidates of the expanded groups, group-wise
rounding, member figures and confidence with the unresolved bucket."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from noema.core.protocols.decision import CompiledQuestion, GroupSpec
from noema.core.values import JsonValue

from . import calibration
from .answer import quorum_wire, unscored_answer, vote_base
from .confidence import confidence
from .flat import AnswerContext
from .hierarchical import Combined, combine, member_joint
from .pool import UNITS, argmax, r4
from .state import Slot, StageInfo


def round2_of(slot: Slot, groups: Sequence[GroupSpec], expanded_run: Sequence[str]) -> dict[str, dict[str, float]]:
    """The member's `p(o | g)` for every group round 2 expanded (1.0 for a one-option group, which needs no call)."""
    by_id = {g.id: g for g in groups}
    out: dict[str, dict[str, float]] = {}
    for gid in expanded_run:
        g = by_id[gid]
        out[gid] = {g.options[0].key: 1.0} if len(g.options) == 1 else dict(slot.round2.get(gid, {}))
    return out


def hier_scored(
    q: CompiledQuestion,
    stage: StageInfo,
    slots: Sequence[Slot],
    q_req: int,
    actx: AnswerContext,
    now: float,
    expanded_run: Sequence[str],
) -> dict[str, JsonValue]:
    """The answer from the members ok in both rounds; `unavailable / no_quorum` when their re-pooled expansion
    needs a group round 2 did not expand. `E` always holds >= 2 options (R1), so a scored answer has >= 2
    candidates. Always uncalibrated in Phase 1 (the lookup runs and misses)."""
    ok = [s for s in slots if s.status == "ok" and s.round1 is not None]
    ids = [s.member.member_id for s in ok]
    p1 = {s.member.member_id: dict(s.round1 or {}) for s in ok}
    p2 = {s.member.member_id: round2_of(s, q.groups, expanded_run) for s in ok}
    weights = {s.member.member_id: s.member.cfg.weight for s in ok}
    quorum = quorum_wire(slots, q_req)
    comb = combine(q.groups, ids, weights, p1, p2, expanded_run) if sum(weights.values()) > 0 else None
    if comb is None:
        members = None
        if actx.with_members:  # ok in both rounds: shown with their reads over the groups round 2 expanded
            total = sum(weights.values()) or 1.0
            run_joints = {m: member_joint(q.groups, list(expanded_run), p1[m], p2[m]) for m in ids}
            members = _votes(q, slots, ok, ({m: w / total for m, w in weights.items()}, run_joints), actx, now)
        return unscored_answer(q, "unavailable", "no_quorum", quorum, members)
    key = calibration.binding_key(
        profile_id=stage.profile.id,
        profile_version=stage.profile.version,
        question_signature=q.signature,
        ok_members=ids,
        input_view_signature=stage.view_signature,
        method="hierarchical",
    )
    calibration.lookup(actx.registry, actx.tenant_id, key)  # runs and misses in Phase 1 (spec-06 §19.8)
    expanded_keys = [o.key for g in q.groups if g.id in comb.expanded for o in g.options]
    position = {o.key: o.index for o in q.options}
    declared = sorted(expanded_keys, key=lambda k: position[k])
    emitted = {k: comb.cand_units[k] / UNITS for k in declared}
    order = sorted(declared, key=lambda k: (-comb.cand_units[k], position[k]))
    members_joint = {
        s.member.member_id: member_joint(q.groups, comb.expanded, p1[s.member.member_id], p2[s.member.member_id])
        for s in ok
    }
    votes = dict.fromkeys(declared, 0)
    tops = []
    for _, _, units_m, _ in members_joint.values():
        top = argmax({k: units_m[k] / UNITS for k in declared}, declared)
        tops.append(top)
        if top is not None:
            votes[top] += 1
    by_key = {o.key: o for o in q.options}
    candidates: list[JsonValue] = [
        {"value": by_key[k].value, "probability": emitted[k], "raw_score": r4(comb.joint[k]), "votes": votes[k]}
        for k in order
    ]
    conf = confidence(
        is_score=False,
        order=order,
        prob=comb.joint,
        index=position,
        u=comb.u,
        u_emitted=comb.u_units > 0,
        member_buckets=[[j.get(k, 0.0) for k in order] + [u_m] for j, u_m, _, _ in members_joint.values()],
        pool_buckets=[comb.joint[k] for k in order] + [comb.u],
        agreeing=sum(1 for t in tops if t == order[0]),
        responded=len(ok),
        band_basis=None,
        bands=actx.bands,
    )
    answer: dict[str, JsonValue] = {
        "type": "choice",
        "name": q.name,
        "choice": by_key[order[0]].value,
        "choice_method": "hierarchical",
        "groups": _groups(q.groups, comb),
        "candidates": candidates,
        "calibration": calibration.calibration_wire(
            None, key, calibration.source_of([s.member.cfg.elicitation for s in ok]), key.member_subset
        ),
        "confidence": conf,
        "quorum": quorum,
    }
    if comb.u_units > 0:
        answer["unresolved_mass"] = comb.u_units / UNITS
    if actx.with_members:
        answer["members"] = _votes(q, slots, ok, (comb.weights, members_joint), actx, now)
    return answer


def _groups(groups: Sequence[GroupSpec], comb: Combined) -> list[JsonValue]:
    return [
        {
            "id": g.id,
            "label": g.label,
            "options": [o.key for o in g.options],
            "probability": comb.units[g.id] / UNITS,
            "expanded": g.id in comb.expanded,
        }
        for g in groups
    ]


def _votes(
    q: CompiledQuestion,
    slots: Sequence[Slot],
    ok: Sequence[Slot],
    reads: tuple[Mapping[str, float], Mapping[str, tuple[dict[str, float], float, dict[str, int], int]]],
    actx: AnswerContext,
    now: float,
) -> list[JsonValue]:
    """Every member of the stage, one entry per member (not per call); the members ok in both rounds carry their
    group-wise rounded joint distribution, their unresolved mass and their renormalized weight."""
    weights, joints = reads
    position = {o.key: o.index for o in q.options}
    ok_ids = {s.member.member_id for s in ok}
    out: list[JsonValue] = []
    for s in slots:
        vote = vote_base(s, now, actx.region, with_usage=actx.with_usage)
        mid = s.member.member_id
        if mid in ok_ids:
            _, _, units_m, u_units = joints[mid]
            vote["weight"] = r4(weights[mid])
            vote["distribution"] = {k: units_m[k] / UNITS for k in sorted(units_m, key=lambda k: position[k])}
            if u_units > 0:
                vote["unresolved_mass"] = u_units / UNITS
        out.append(vote)
    return out


def hier_votes(
    q: CompiledQuestion, slots: Sequence[Slot], expanded_run: Sequence[str], actx: AnswerContext, now: float
) -> list[JsonValue]:
    """The members of an unscored hierarchical answer: members ok in both rounds carry their read over the groups
    round 2 expanded."""
    ok = [s for s in slots if s.status == "ok" and s.round1 is not None and expanded_run]
    weights = {s.member.member_id: s.member.cfg.weight for s in ok}
    total = sum(weights.values()) or 1.0
    joints = {
        s.member.member_id: member_joint(
            q.groups, list(expanded_run), s.round1 or {}, round2_of(s, q.groups, expanded_run)
        )
        for s in ok
    }
    return _votes(q, slots, ok, ({m: w / total for m, w in weights.items()}, joints), actx, now)
