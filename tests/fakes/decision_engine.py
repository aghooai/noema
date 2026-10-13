"""`FakeDecisionEngine` / `FakeDecisionRun` (spec-02 §5, spec-06 §6-§7, §10), driven by the scenario DSL.

One flat stage (no escalation, no backups, no calibrators; documented fake limits). Per (question, member) a call
task waits for its barrier and latency on the clock and reports ok (the rule's `dist` by option key), refused, an
error code (the IMPL-076 member mapping), or never answers (hang). A coordinator closes answers per spec-06 §7.2
(all required terminal; quorum impossible; the stage deadline -> `timeout`; `cancel()` -> `late`), resolves S1 when
every commit-critical answer is final (`status: quorum` with provisional answers while others are open), and the
final snapshot when every answer is closed. `hold_until_cancel=True` ignores the stage deadline, so S1 resolves
only through `cancel()` (spec-04 G04b) or its own answers; when S1 came by itself, the answers still open close
(`timeout`) at `ctx.deadline`, so `updates()` always ends. `finalize()` records usage lines (decision/verification,
waste for calls that never finished) through the cost meter and, when `ctx.turn_id` is None, inserts the row (a DB
failure goes to the outbox as `decision_row`, never raised) and offers to labeling; for turns the final snapshot
carries `row` (iff `ctx.store`) and `row_for(policy)` re-renders its `input` under a stricter content policy. The
run's id is `ctx.decision_id` when set.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

import anyio
import anyio.abc
import anyio.lowlevel

from noema.config.models import DecisionMemberCfg, ModelCfg
from noema.core.clock import Clock
from noema.core.errors import ErrorCode, NoemaError
from noema.core.ids import IdGen
from noema.core.protocols.decision import (
    CompiledQuestion,
    DecisionContext,
    DecisionInput,
    DecisionRequest,
    DecisionSnapshot,
    Msg,
    VerificationInput,
)
from noema.core.protocols.labeling import ContentRef, LabelingSampler
from noema.core.protocols.ops import CostMeter, UsageLine
from noema.core.protocols.storage import Database, Outbox, OutboxRecord
from noema.core.values import ContentPolicy, Cost, JsonValue, UsageRecord
from noema.telemetry.redact import redact

from .clock import single_errors
from .decision_result import MemberResult, build_answer, build_result
from .scenario import NoRuleMatched, RequestInfo
from .scenario_harness import ScenarioHarness

MIN_DISPATCH_MS: Final = 50
_FAULT_CODES: Final[Mapping[str, str | None]] = {
    "throttling": "throttled",
    "access_denied": "tenant_access_misconfigured",
    "validation": "invalid_request",
    "http_5xx": "upstream_error",
    "model_timeout": "upstream_error",
    "connect_reset": "upstream_error",
    "exception_event": "upstream_error",
    "malformed": "parse_error",
    "truncated_json": "parse_error",
    "reasoning_only": "reasoning_only",
    "empty_text": "empty_output",
    "hang_before_first_byte": None,
    "stall_after": None,
}
_VERBALIZED_CODES: Final = {"missing_letter": "missing_options", "sum_out_of_range": "sum_out_of_range"}


@dataclass(frozen=True, slots=True)
class Member:
    """One stage member: positional id `m{i+1}`, its config and model."""

    member_id: str
    cfg: DecisionMemberCfg
    model: ModelCfg


@dataclass(frozen=True, slots=True)
class MemberPlan:
    """How one member call ends: at `at_ms` (from the stage start) with `status`, or never (`status` None)."""

    status: Literal["ok", "refused", "error"] | None
    at_ms: float = 0.0
    dist: Mapping[str, float] | None = None
    error_code: str | None = None
    barrier: str | None = None
    usage: UsageRecord | None = None


PlanFn = Callable[[CompiledQuestion, Member, DecisionContext, DecisionRequest, str], MemberPlan]


def input_text(value: DecisionInput) -> str:
    """The text of a decision input (what a scenario's `prompt_contains` can see besides the question)."""
    if isinstance(value, str):
        return value
    if isinstance(value, VerificationInput):
        return value.question + "\n" + "\n".join(text for _, text in value.answers)
    if isinstance(value, tuple):
        texts: list[str] = []
        for msg in value:
            if isinstance(msg, Msg):
                texts.extend(p.text for p in msg.parts if p.text is not None)
        return "\n".join(texts)
    return json.dumps(value, sort_keys=True, default=str)


def member_prompt(q: CompiledQuestion, text: str) -> str:
    """A decision-prompt stand-in: the `<app_question>` JSON (declared letter order) and the input."""
    aq = {
        "instructions": q.instructions,
        "options": [
            {"letter": chr(65 + j), "value": o.key, "description": o.description} for j, o in enumerate(q.options)
        ],
    }
    head = f"<app_question>{json.dumps(aq, ensure_ascii=False)}</app_question>\n"
    return f"{head}<untrusted_input>\n{text}\n</untrusted_input>\n"


def _normalized(dist: Mapping[str, float], keys: list[str]) -> dict[str, float] | None:
    raw = {k: max(0.0, float(dist.get(k, 0.0))) for k in keys}
    total = sum(raw.values())
    return None if total <= 0 else {k: v / total for k, v in raw.items()}


def scenario_planner(harness: ScenarioHarness) -> PlanFn:
    """Member plans from the scenario rules (model = the member's Bedrock id; op converse or chat)."""

    def plan(
        q: CompiledQuestion, m: Member, ctx: DecisionContext, req: DecisionRequest, decision_id: str
    ) -> MemberPlan:
        prompt = member_prompt(q, input_text(req.input))
        op: Literal["chat", "converse"] = "chat" if m.model.adapter == "bedrock-mantle-chat" else "converse"
        info = RequestInfo(m.model.id, op, prompt, f"{decision_id}|{q.name}|{m.member_id}", None, m.cfg.effort)
        try:
            res = harness.resolve(info)
        except NoRuleMatched:
            return MemberPlan("error", 0.0, error_code="upstream_error")
        usage = UsageRecord(math.ceil(len(prompt) / 4), 8 if m.cfg.elicitation == "token_logprobs" else 40)
        at = res.latency_ms
        if res.fault is not None:
            kind = res.fault.kind
            if kind == "refusal":
                return MemberPlan("refused", at, barrier=res.barrier, usage=usage)
            if kind == "invalid_verbalized":
                parse_code = _VERBALIZED_CODES.get(res.fault.variant or "", "parse_error")
                return MemberPlan("error", at, error_code=parse_code, barrier=res.barrier, usage=usage)
            fault_code = _FAULT_CODES.get(kind)
            if fault_code is None:
                return MemberPlan(None, barrier=res.barrier)
            return MemberPlan("error", at, error_code=fault_code, barrier=res.barrier, usage=usage)
        answer = res.rule.answer
        if answer is None or answer.dist is None:
            if res.barrier is not None and answer is None:
                return MemberPlan(None, barrier=res.barrier)
            return MemberPlan("error", at, error_code="parse_error", barrier=res.barrier, usage=usage)
        dist = _normalized(answer.dist, [o.key for o in q.options])
        if dist is None:
            return MemberPlan("error", at, error_code="sum_out_of_range", barrier=res.barrier, usage=usage)
        return MemberPlan("ok", at, dist=dist, barrier=res.barrier, usage=usage)

    return plan


@dataclass
class _Slot:
    status: str = "pending"  # pending | ok | refused | error | timeout | late
    at_ms: float = 0.0
    dist: Mapping[str, float] | None = None
    error_code: str | None = None
    usage: UsageRecord | None = None


def usage_line_id(*parts: str) -> str:
    """spec-11 §2 deterministic id: sha256(':'.join(parts)).hexdigest()[:26]."""
    return hashlib.sha256(":".join(parts).encode()).hexdigest()[:26]


_STRICTNESS: Final[Mapping[str, int]] = {"full": 0, "redacted": 1, "metadata_only": 2}


def _strictest(a: ContentPolicy, b: ContentPolicy) -> ContentPolicy:
    return a if _STRICTNESS[a] >= _STRICTNESS[b] else b


def _json(value: object) -> JsonValue:
    """A decision-row value as an outbox payload value (RFC 3339 UTC timestamps, spec-02 §7)."""
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return value  # type: ignore[return-value]  # the other row values are JSON already


class FakeDecisionRun:
    """Implements `DecisionRun` (see the module docstring)."""

    def __init__(self, engine: FakeDecisionEngine, ctx: DecisionContext, req: DecisionRequest) -> None:
        self._engine = engine
        self._clock = engine.clock
        self._ctx = ctx
        self._req = req
        self._id = ctx.decision_id or engine.ids.new("dec")
        profile = ctx.snapshot.decision_profile(req.profile.id)
        self._members = [Member(f"m{i + 1}", m, ctx.snapshot.model(m.model)) for i, m in enumerate(profile.members)]
        self._templates = {m.member_id: m.cfg.template for m in self._members}
        self._questions = req.questions.questions
        self._t0 = self._clock.monotonic()
        self._t0_wall_ms = int(self._clock.now().timestamp() * 1000)
        self._deadline_at = min(self._t0 + req.deadline_ms / 1000, ctx.deadline.at)
        self._slots = {(q.name, m.member_id): _Slot() for q in self._questions for m in self._members}
        self._closed_ms: dict[str, int | None] = {q.name: None for q in self._questions}
        self._s1: DecisionSnapshot | None = None
        self._final: DecisionSnapshot | None = None
        self._s1_event = anyio.Event()
        self._final_event = anyio.Event()
        self._finalized: DecisionSnapshot | None = None
        self._tg: anyio.abc.TaskGroup | None = None
        self._building = False
        self.prompts: dict[tuple[str, str], str] = {}

    @property
    def decision_id(self) -> str:
        return self._id

    # --- lifecycle -------------------------------------------------------------------------------------------------
    def start(self, tg: anyio.abc.TaskGroup) -> None:
        self._tg = tg
        kind: Literal["decision_member", "verification_member"] = (
            "verification_member" if self._ctx.purpose == "verification" else "decision_member"
        )
        for q in self._questions:
            for m in self._members:
                self._ctx.reservation.consume(kind, m.cfg.model)
                self.prompts[(q.name, m.member_id)] = member_prompt(q, input_text(self._req.input))
                tg.start_soon(
                    self._call, q.name, m.member_id, self._engine.planner(q, m, self._ctx, self._req, self._id)
                )
        if not self._engine.hold_until_cancel:
            tg.start_soon(self._timer)

    def _elapsed_ms(self) -> int:
        return max(0, round((self._clock.monotonic() - self._t0) * 1000))

    async def _call(self, qname: str, mid: str, plan: MemberPlan) -> None:
        if plan.barrier is not None and self._engine.harness is not None:
            until = math.inf if self._engine.hold_until_cancel else self._deadline_at
            if not await self._engine.harness.wait_barrier(plan.barrier, until=until):
                return
        if plan.status is None:
            return
        due = self._t0 + plan.at_ms / 1000
        await self._clock.sleep(max(0.0, due - self._clock.monotonic()))
        slot = self._slots[(qname, mid)]
        if slot.status != "pending" or self._closed_ms[qname] is not None:
            return
        slot.status, slot.at_ms, slot.dist, slot.error_code = (
            plan.status,
            self._elapsed_ms(),
            plan.dist,
            plan.error_code,
        )
        slot.usage = plan.usage
        self._schedule()

    async def _timer(self, at: float | None = None) -> None:
        at = self._deadline_at if at is None else at
        await self._clock.sleep(max(0.0, at - self._clock.monotonic()))
        for q in self._questions:
            self._close(q.name, "timeout")
        self._publish()

    def _close(self, qname: str, pending_status: str) -> None:
        if self._closed_ms[qname] is not None:
            return
        for m in self._members:
            slot = self._slots[(qname, m.member_id)]
            if slot.status == "pending":
                slot.status = pending_status
        self._closed_ms[qname] = self._elapsed_ms()

    def _maybe_close(self, qname: str) -> None:
        required = [(m, self._slots[(qname, m.member_id)]) for m in self._members if not m.cfg.optional]
        if all(s.status != "pending" for _, s in required):
            self._close(qname, "late")
            return
        alive = {m.cfg.model for m, s in required if m.cfg.weight > 0 and s.status in ("ok", "pending")}
        if len(alive) < self._req.min_quorum:
            self._close(qname, "late")

    def _schedule(self) -> None:
        """Evaluate closures once the loop settles at this instant, so results landing at the same (virtual) time
        are judged together (e.g. three members failing at once -> all_members_failed, not no_quorum)."""
        if self._tg is None:
            self._evaluate()
        elif not self._building:
            self._building = True
            self._tg.start_soon(self._evaluate_later)

    async def _evaluate_later(self) -> None:
        settle = getattr(self._clock, "settle", None)
        if settle is not None:
            await settle()
        else:
            for _ in range(3):
                await anyio.lowlevel.checkpoint()
        self._building = False
        self._evaluate()

    def _evaluate(self) -> None:
        for q in self._questions:
            if self._closed_ms[q.name] is None:
                self._maybe_close(q.name)
        self._publish()

    def _publish(self) -> None:
        """Freeze S1 and the final snapshot when they are due."""
        closed = {n for n, ms in self._closed_ms.items() if ms is not None}
        all_closed = len(closed) == len(self._questions)
        critical = self._req.questions.commit_critical
        if self._s1 is None and (all_closed or (bool(critical) and critical <= closed)):
            self._set_s1()
        if self._final is None and all_closed:
            self._set_final()

    def _set_s1(self) -> None:
        self._s1 = self._snapshot()
        self._s1_event.set()
        if self._s1.final and self._final is None:
            self._final = self._s1
            self._final_event.set()
        elif self._engine.hold_until_cancel and self._tg is not None:
            self._tg.start_soon(self._timer, self._ctx.deadline.at)  # S1 came by itself: close the rest at the latest

    def _set_final(self) -> None:
        self._final = self._snapshot()
        self._final_event.set()

    # --- snapshots -------------------------------------------------------------------------------------------------
    def _member_results(self, q: CompiledQuestion, now_ms: int) -> list[MemberResult]:
        out = []
        for m in self._members:
            slot = self._slots[(q.name, m.member_id)]
            status = "late" if slot.status == "pending" else slot.status
            closed = self._closed_ms[q.name]
            latency = slot.at_ms if status in ("ok", "refused", "error") else (closed if closed is not None else now_ms)
            out.append(
                MemberResult(
                    member_id=m.member_id,
                    model_alias=m.cfg.model,
                    model_id=m.model.id,
                    provider=m.model.adapter,
                    elicitation="token_logprobs" if m.cfg.elicitation == "token_logprobs" else "verbalized",
                    weight=m.cfg.weight,
                    status=status,  # type: ignore[arg-type]
                    latency_ms=int(latency),
                    dist=slot.dist if status == "ok" else None,
                    optional=m.cfg.optional,
                    error_code=slot.error_code,
                    usage=slot.usage if status in ("ok", "refused", "error") else None,
                    region=self._ctx.tenant.home_region,
                )
            )
        return out

    def _snapshot(self) -> DecisionSnapshot:
        now_ms = self._elapsed_ms()
        bands = self._ctx.snapshot.routing.confidence_bands
        band_map = {"high_gte": bands.high_gte, "low_lt": bands.low_lt}
        built = [
            build_answer(
                q,
                self._member_results(q, now_ms),
                q_req=self._req.min_quorum,
                bands=band_map,
                final=self._closed_ms[q.name] is not None,
            )
            for q in self._questions
        ]
        final_names = frozenset(n for n, ms in self._closed_ms.items() if ms is not None)
        is_final = len(final_names) == len(self._questions)
        status: Literal["quorum", "completed", "partial"] = "quorum"
        if is_final:
            status = "partial" if any(b.summary.type == "unavailable" for b in built) else "completed"
        usage = UsageRecord(0, 0)
        for slot in self._slots.values():
            if slot.usage is not None and slot.status in ("ok", "refused", "error"):
                usage = usage + slot.usage
        result = build_result(
            decision_id=self._id,
            status=status,
            answers=built,
            profile_id=self._req.profile.id,
            profile_version=self._req.profile.version,
            question_signature=self._req.questions.signature,
            prompt_templates=self._templates,
            deadline_ms=self._req.deadline_ms,
            trace_id=self._ctx.trace_id,
            started_at_ms=self._t0_wall_ms,
            completed_at_ms=self._t0_wall_ms + now_ms,
            usage=usage,
            input_digest_key=self._ctx.tenant.tenant_id,
        )
        row = self._row(result, status) if is_final and self._ctx.store and self._ctx.turn_id is not None else None
        return DecisionSnapshot(
            decision_id=self._id,
            status=status,
            final=is_final,
            final_names=final_names,
            answers={b.summary.name: b.summary for b in built},
            result=result,
            row=row,
        )

    def _row(self, result: Mapping[str, Any], status: str, policy: ContentPolicy | None = None) -> dict[str, object]:
        ctx, req = self._ctx, self._req
        policy = _strictest(ctx.content_policy, policy or ctx.content_policy)
        stored_input: object = None
        if policy != "metadata_only":
            text = input_text(req.input)
            stored_input = {"text": redact(text) if policy == "redacted" else text}
        return {
            "id": self._id,
            "tenant_id": ctx.principal.tenant_id,
            "project_id": ctx.principal.project_id,
            "created_at": datetime.fromtimestamp(self._t0_wall_ms / 1000, UTC),
            "principal_id": ctx.principal.principal_id,
            "turn_id": ctx.turn_id,
            "session_id": ctx.session_id,
            "purpose": ctx.purpose,
            "origin": req.origin,
            "output_format": req.output_format,
            "profile_id": req.profile.id,
            "profile_version": str(req.profile.version),
            "question_signature": req.questions.signature,
            "input_view_signature": result["provenance"]["input_view_signature"],
            "input_digest": result["provenance"]["input_digest"],
            "status": status,
            "cache_hit": False,
            "safety_identifier": req.safety_identifier,
            "questions": [dict(q.wire) for q in self._questions],
            "input": stored_input,
            "result": dict(result),
            "expires_at": None,
        }

    # --- DecisionRun ----------------------------------------------------------------------------------------------
    async def commit_critical(self) -> DecisionSnapshot:
        await self._s1_event.wait()
        assert self._s1 is not None
        return self._s1

    async def updates(self) -> AsyncIterator[DecisionSnapshot]:
        s1 = await self.commit_critical()
        if s1.final:
            return
        await self._final_event.wait()
        assert self._final is not None
        yield self._final

    async def cancel(self) -> None:
        await anyio.lowlevel.checkpoint()
        for q in self._questions:
            self._close(q.name, "late")
        self._publish()

    def row_for(self, content_policy: ContentPolicy) -> Mapping[str, object] | None:
        final = self._final
        if final is None or final.row is None:
            return None
        return self._row(final.result, final.status, content_policy)

    async def finalize(self) -> DecisionSnapshot:
        await self._final_event.wait()
        assert self._final is not None
        if self._finalized is None:
            self._finalized = self._final
            with anyio.CancelScope(shield=True):
                await self._side_effects(self._final)
        return self._finalized

    async def _side_effects(self, final: DecisionSnapshot) -> None:
        engine, ctx = self._engine, self._ctx
        if engine.cost is not None:
            self._record_usage(final)
        if ctx.turn_id is None and ctx.store:
            if engine.db is not None:
                row = self._row(final.result, final.status)
                try:
                    async with engine.db.uow(ctx.principal) as uow:
                        await uow.decisions.insert(row)
                except Exception:
                    if engine.outbox is None:
                        raise
                    payload = {k: _json(v) for k, v in row.items() if k not in ("tenant_id", "id")}
                    engine.outbox.put(OutboxRecord("decision_row", self._id, ctx.principal.tenant_id, payload))
            if engine.sampler is not None and ctx.purpose in ("decision", "router"):
                engine.sampler.offer(ctx.tenant.tenant_id, final, ContentRef(self._id, None))

    def _record_usage(self, final: DecisionSnapshot) -> None:
        engine, ctx = self._engine, self._ctx
        assert engine.cost is not None
        normal = "verification" if ctx.purpose == "verification" else "decision"
        groups: dict[tuple[str, str, str], tuple[UsageRecord, Cost]] = {}
        for (qname, mid), slot in self._slots.items():
            member = next(m for m in self._members if m.member_id == mid)
            if slot.status in ("ok", "refused", "error") and slot.usage is not None:
                line, usage = normal, slot.usage
            elif slot.status in ("timeout", "late"):
                prompt = self.prompts.get((qname, mid), "")
                line, usage = "waste", UsageRecord(math.ceil(len(prompt) / 4), 40, tokens_estimated=True)
            else:
                continue
            cost = engine.cost.price(member.cfg.model, ctx.tenant.home_region, usage)
            key = (line, member.model.id, member.cfg.model)
            prev = groups.get(key)
            groups[key] = (
                (usage, cost)
                if prev is None
                else (prev[0] + usage, Cost(prev[1].usd + cost.usd, prev[1].estimated or cost.estimated))
            )
        for (line, model_id, _alias), (usage, cost) in sorted(groups.items()):
            engine.cost.record(
                UsageLine(
                    id=usage_line_id(self._id, line, model_id),
                    tenant_id=ctx.principal.tenant_id,
                    project_id=ctx.principal.project_id,
                    line=line,  # type: ignore[arg-type]
                    model_id=model_id,
                    region=ctx.tenant.home_region,
                    usage=usage,
                    cost=cost,
                    turn_id=ctx.turn_id,
                    decision_id=self._id,
                    session_id=ctx.session_id,
                )
            )


class FakeDecisionEngine:
    """Implements `DecisionEngine`. `planner` (default: `scenario_planner(harness)`) decides every member call;
    `cost`, `db` and `sampler` are optional collaborators of `finalize()`; `runs` lists every run started."""

    def __init__(
        self,
        clock: Clock,
        ids: IdGen,
        *,
        harness: ScenarioHarness | None = None,
        planner: PlanFn | None = None,
        cost: CostMeter | None = None,
        db: Database | None = None,
        sampler: LabelingSampler | None = None,
        outbox: Outbox | None = None,
        hold_until_cancel: bool = False,
    ) -> None:
        if planner is None and harness is None:
            raise ValueError("FakeDecisionEngine needs a scenario harness or a planner")
        self.clock = clock
        self.ids = ids
        self.harness = harness
        self.planner: PlanFn = planner if planner is not None else scenario_planner(harness)  # type: ignore[arg-type]
        self.cost = cost
        self.db = db
        self.sampler = sampler
        self.outbox = outbox  # the decision_row fallback of finalize() (without one a DB failure propagates)
        self.hold_until_cancel = hold_until_cancel
        self.runs: list[FakeDecisionRun] = []

    def run(self, ctx: DecisionContext, req: DecisionRequest) -> AbstractAsyncContextManager[FakeDecisionRun]:
        return self._run(ctx, req)

    @asynccontextmanager
    async def _run(self, ctx: DecisionContext, req: DecisionRequest) -> AsyncIterator[FakeDecisionRun]:
        await anyio.lowlevel.checkpoint()
        if ctx.deadline.remaining_ms() < MIN_DISPATCH_MS:
            raise NoemaError(ErrorCode.DEADLINE_EXCEEDED, "The deadline passed before the decision started.")
        run = FakeDecisionRun(self, ctx, req)
        self.runs.append(run)
        with single_errors():
            async with anyio.create_task_group() as tg:
                run.start(tg)
                try:
                    yield run
                finally:
                    with anyio.CancelScope(shield=True):
                        await run.cancel()
                    tg.cancel_scope.cancel()

    async def decide(self, ctx: DecisionContext, req: DecisionRequest) -> DecisionSnapshot:
        async with self.run(ctx, req) as run:
            return await run.finalize()
