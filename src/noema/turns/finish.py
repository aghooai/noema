"""Turn completion (spec-04 §10 steps 2-7; IMPL-020, IMPL-022, IMPL-071): persistence, usage lines, the
reservation, `turn.completed` and the labeling offer. Runs shielded once the turn's tasks are done."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import timedelta
from typing import TYPE_CHECKING, Final

from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.decision import DecisionSnapshot
from noema.core.protocols.labeling import ContentRef
from noema.core.values import ContentPolicy, JsonValue, UsageRecord
from noema.turns import persist
from noema.turns.bindings import content_policy
from noema.turns.model import TurnResult, TurnStatus
from noema.turns.routing import routing_wire
from noema.turns.spec import same_spec, sticky_json
from noema.turns.views import turn_object

if TYPE_CHECKING:
    from noema.core.protocols.generation import AttemptSpec
    from noema.turns.runner import TurnRunner

log = logging.getLogger("noema.turns")
_STRICTNESS: Final = {"full": 0, "redacted": 1, "metadata_only": 2}


def turn_status(r: TurnRunner) -> TurnStatus:
    """spec-04 §4: cancelled after POST cancel, abandoned by the watchdog, failed after client_too_slow or an
    error, completed when `generation.completed` was sent."""
    if r.cancel_status is not None:
        return r.cancel_status
    if r.completed and r.error is None:
        return "completed"
    return "failed"


def _served(r: TurnRunner) -> tuple[str, AttemptSpec]:
    """(route_id, spec) of the run that served the turn (the committed one, else the last attempt's)."""
    run = r.committed_run or (r.runs[-1] if r.runs else None)
    spec = run.spec if run is not None else r.p.spec0.spec
    attempt = r.committed_attempt or (r.started_order[-1] if r.started_order else None)
    route = r.attempts[attempt][0] if attempt is not None and attempt in r.attempts else spec.generation_profile
    return route, spec


def stored_routing(r: TurnRunner) -> dict[str, JsonValue]:
    """`turn.routing`: decided vs served once S1 was evaluated (matches = S1 ∪ final), else spec0's."""
    route, spec = _served(r)
    if r.decided is not None and r.drun is not None:
        shown = spec.conditioning_signature != "none"
        return r.decided_routing(route, spec, stored=True, conditioning_shown=shown)
    s0 = r.p.spec0
    selection = "fallback" if route != s0.spec.generation_profile else s0.selection
    return routing_wire(r.snapshot, s0.spec, selection=selection, strategy=r.strategy, reason=s0.reason, route_id=route)


def _ms(seconds: float) -> int:
    return round(seconds * 1000)


async def timings(r: TurnRunner, end: float) -> dict[str, JsonValue]:
    """`TurnTimings` (spec-04 §10; monotonic clock)."""
    p = r.p
    a1_first = await r.a1.first_text() if r.a1 is not None else None
    committed = next((rec for rec in r.records if rec.attempt_id == r.committed_attempt), None)
    committed_first = committed.first_text_at if committed is not None else None  # that attempt's own first text
    # the engine commits at max(the commit request, that first text): an armed or auto-committing run at its first
    # text (holdback 0), a buffered answer at the request; `commit_wall` (when the runner saw generation.committed)
    # adds the event's queue hop, which is TTFT overhead, not holdback (C2b perf smoke R3)
    asked = r.commit_asked if r.committed_run is r.a1 else None  # only A1 is ever asked
    holdback: int | None = None
    if r.commit_wall is not None and committed_first is not None:
        holdback = 0 if asked is None else max(0, _ms((asked - committed_first).total_seconds()))
    wasted = sum(rec.usage.output_tokens for rec in r.records if rec.outcome != "committed")
    return {
        "ttft_ms": None if r.first_delta_at is None else _ms(r.first_delta_at - p.t_in),
        "decision_ms": None if r.s1_at is None or r.t_d0 is None else _ms(r.s1_at - r.t_d0),
        "decision_lead_ms": None if a1_first is None or r.s1_at is None else _ms(a1_first - r.s1_at),
        "holdback_ms": holdback,
        "e2e_ms": _ms(end - p.t_in),
        "spec_restarts": 1 if r.restarted else 0,
        "wasted_output_tokens": wasted,
    }


def _decision_rows(r: TurnRunner, policy: ContentPolicy) -> list[Mapping[str, object]]:
    rows: list[Mapping[str, object]] = []
    for run, snap in ((r.drun, r.router_final), (r.verification_run, r.verification)):
        if run is None or snap is None or snap.row is None:
            continue
        stricter = _STRICTNESS[policy] > _STRICTNESS[r.p.store_content]
        row = run.row_for(policy) if stricter else snap.row
        if row is not None:
            rows.append(row)
    return rows


def _alias_usage(r: TurnRunner) -> dict[str, UsageRecord]:
    """Actual usage per model alias (generation attempts and decision member calls) for `reconcile`."""
    p = r.p
    aliases = set(p.aliases) | {m.model for m in p.profile.members}
    verification = p.snapshot.routing.verification
    if verification is not None and verification.decision_profile is not None:
        aliases |= {m.model for m in p.snapshot.decision_profile(verification.decision_profile).members}
    by_id = {p.snapshot.model(a).id: a for a in aliases}
    out: dict[str, UsageRecord] = {}

    def add(model_id: str, usage: UsageRecord) -> None:
        alias = by_id.get(model_id)
        if alias is not None:
            out[alias] = out[alias] + usage if alias in out else usage

    for rec in r.records:
        add(rec.model, rec.usage)
    for snap in (r.router_final, r.verification):
        for model_id, usage in _member_usage(snap):
            add(model_id, usage)
    return out


def _member_usage(snap: DecisionSnapshot | None) -> list[tuple[str, UsageRecord]]:
    out: list[tuple[str, UsageRecord]] = []
    answers = snap.result.get("answers") if snap is not None else None
    for answer in answers if isinstance(answers, list) else []:
        members = answer.get("members") if isinstance(answer, dict) else None
        for m in members if isinstance(members, list) else []:
            usage = m.get("usage") if isinstance(m, dict) else None
            if isinstance(m, dict) and isinstance(usage, dict):
                out.append((str(m.get("model", "")), _usage_record(usage)))
    return out


def _usage_record(u: Mapping[str, JsonValue]) -> UsageRecord:
    def n(key: str) -> int:
        v = u.get(key, 0)
        return v if isinstance(v, int) and not isinstance(v, bool) else 0

    return UsageRecord(
        n("input_tokens"),
        n("output_tokens"),
        n("cached_input_tokens"),
        n("cache_write_input_tokens"),
        n("reasoning_tokens"),
        u.get("tokens_estimated") is True,
    )


def _turn_usage(r: TurnRunner) -> dict[str, JsonValue]:
    """Σ of the turn's lines except cache_savings: every attempt plus the router and verification results."""
    parts: list[Mapping[str, JsonValue]] = [persist.usage_wire(rec.usage, rec.cost) for rec in r.records]
    for snap in (r.router_final or r.final or r.s1, r.verification):
        usage = snap.result.get("usage") if snap is not None else None
        if isinstance(usage, dict):
            parts.append(usage)
    return persist.usage_sum(parts)


def _turn_row(
    r: TurnRunner,
    status: TurnStatus,
    policy: ContentPolicy,
    routing: Mapping[str, JsonValue],
    usage: JsonValue,
    timing: JsonValue,
) -> dict[str, object]:
    p = r.p
    parts = r.preflight.parts if r.preflight is not None else ()
    input_parts, output_text, redactions = persist.stored_content(
        parts,
        "".join(r.text),
        policy,
        r.svc.telemetry.redact,
    )
    created = p.created_at
    retain_days = getattr(p.snapshot.platform.data_classes, p.data_class).retain_days
    degradations = [d.to_wire() for d in r.preflight.degradations] if r.preflight is not None else []
    return {
        "id": r.turn_id,
        "project_id": p.principal.project_id,
        "principal_id": p.principal.principal_id,
        "status": status,
        "content_policy": policy,
        "input_parts": input_parts,
        "output_text": output_text,
        "redactions": redactions,
        "generation_profile": routing.get("route_id"),
        "routing": dict(routing),
        "usage": usage,
        "timings": timing,
        "degradations": degradations,
        "decision_id": r.drun.decision_id if r.drun is not None else None,
        "verification_decision_id": r.verification.decision_id if r.verification is not None else None,
        "retry_of": p.request.retry_of,
        "created_at": created,
        "completed_at": r.clock.now(),
        "expires_at": created + timedelta(days=retain_days) if retain_days > 0 else None,
    }


async def _persist(r: TurnRunner, row: dict[str, object], policy: ContentPolicy) -> persist.FinalizeOutcome:
    p = r.p
    if not p.request.store or r.lease_lost:
        return "skipped"
    sticky: Mapping[str, JsonValue] | None = None
    if r.decided is not None:  # written only if S1 was evaluated (spec-04 §7)
        modalities = r.preflight.modalities if r.preflight is not None else ("text",)
        sticky = sticky_json(r.decided.spec, r.decided.answer_mode, modalities)
    elif p.lease is not None:  # else kept: the value read with the lease (step 6's may predate a FIFO wait)
        sticky = p.lease.sticky_spec
    files = [{"part_index": i, "file_id": f} for i, f in (r.preflight.turn_files if r.preflight is not None else ())]
    return await persist.finalize(
        r.svc.db,
        r.svc.outbox,
        r.coordinator,
        p.principal,
        clock=r.clock,
        lease=p.lease,
        turn=row,
        attempts=persist.attempt_rows(r.records),
        decisions=_decision_rows(r, policy),
        turn_files=files,
        sticky_spec=sticky,
    )


async def finish(r: TurnRunner) -> TurnResult:
    """spec-04 §10 steps 2-7 (see the module docstring)."""
    p = r.p
    r.ensure_route_selected()
    if not r.completed and r.error is None and r.cancel_status is None:
        expired = p.deadline.expired()
        code = ErrorCode.DEADLINE_EXCEEDED if expired else ErrorCode.INTERNAL_ERROR
        r.emit_error(NoemaError(code, "The turn deadline passed." if expired else "The turn failed; retry."))
    status = turn_status(r)
    policy = content_policy(r.tightening().log_content)
    routing = stored_routing(r)
    timing = await timings(r, r.now())
    usage = _turn_usage(r)
    row = _turn_row(r, status, policy, routing, usage, timing)
    outcome = r.finalize_outcome = await _persist(r, row, policy)
    if outcome == "lease_lost":
        r.emit_error(NoemaError(ErrorCode.INTERNAL_ERROR, "The session lease was lost; retry."))
        status = turn_status(r)
    for line in persist.usage_lines(
        r.records, principal=p.principal, region=p.tenant.home_region, turn_id=r.turn_id, session_id=p.session_id
    ):
        r.svc.cost.record(line)
    try:
        await p.reservation.reconcile(_alias_usage(r))
    finally:
        await p.reservation.release()
    await r.coordinator.delete_owner(r.turn_id)
    timing["e2e_ms"] = _ms(r.now() - p.t_in)
    r.emit({"type": "turn.completed", "turn_id": r.turn_id, "usage": usage, "timings": timing})
    r.closed = True
    r.events.seal(r.turn_id, p.snapshot.platform.events.resume_window_s)
    if outcome == "stored" and policy != "metadata_only" and r.router_final is not None and r.drun is not None:
        r.svc.sampler.offer(p.principal.tenant_id, r.router_final, ContentRef(r.drun.decision_id, r.turn_id))
    _metrics(r, status)
    seq = p.lease.last_seq + 1 if p.lease is not None and outcome in ("stored", "outboxed") else None
    obj = turn_object(
        row,
        routing,
        r.records,
        r.router_final,
        r.verification,
        text="".join(r.text),
        session_id=p.session_id,
        seq=seq,
    )
    return TurnResult(status=status, error=r.error, turn=obj)


def _metrics(r: TurnRunner, status: TurnStatus) -> None:
    inst = r.svc.telemetry.instruments
    route, spec = _served(r)
    model = r.attempts[r.committed_attempt][1] if r.committed_attempt in r.attempts else ""
    labels = {"profile": route, "model": model, "strategy": r.strategy}
    if r.first_delta_at is not None:
        inst.observe("aghoo_ttft_seconds", r.first_delta_at - r.p.t_in, **labels)
    inst.observe("aghoo_e2e_seconds", r.now() - r.p.t_in, **labels)
    # aghoo_wasted_output_tokens_total is the generation engine's (RunCore._terminate), counted once per attempt
    if r.decided is not None and r.decided.conditioning is not None and spec.conditioning_signature != "none":
        inst.add("aghoo_conditioning_total", answer_mode=r.decided.conditioning.answer_mode)
    if r.decided is not None and not r.decided.unavailable:
        s0 = r.p.spec0.spec
        stats: dict[str, list[int]] = r.svc.runtime.local("turns.spec_agreement", dict)
        agree, total = stats.setdefault(s0.generation_profile, [0, 0])
        agree += int(same_spec(r.decided.spec, s0, include_conditioning=r.compare_conditioning))
        stats[s0.generation_profile] = [agree, total + 1]
        inst.set("aghoo_spec_agreement_ratio", agree / (total + 1), profile=s0.generation_profile)
    missing = len(r.started_order) - len(r.records)
    if missing > 0:  # aghoo_stuck_attempts_total is the generation engine's (spec-11 §6 attempt watchdog)
        log.warning("turn %s: %d attempts without a record", r.turn_id, missing)
    if status == "failed" and r.error is not None:
        log.info("turn %s failed: %s", r.turn_id, r.error.code.value)
