"""`DecisionEngineImpl`: `run(ctx, req)` is an async context manager over a `DecisionRunImpl` (spec-06 §2.3, §10,
§14; IMPL-032, IMPL-070, IMPL-074).

`__aenter__`: request checks (profile version, quorum, template family), `broker.get` once (fail fast), the input
digest, the stage-0 views (which set `t0`; bounded so they leave 50 ms before `ctx.deadline`, else 504), the L2
lookup, then the dispatch check (`ctx.deadline` must leave at least 50 ms) and the coordinator task. `__aexit__`
cancels in-flight calls, runs the shielded finalize side effects if the caller did not (after a coordinator bug:
only the usage lines), and leaves the run's task group.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from types import TracebackType
from typing import Final

import anyio
import orjson

from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.decision import DecisionContext, DecisionRequest, DecisionRun, DecisionSnapshot
from noema.core.values import JsonValue

from .cache import cacheable, l2_key
from .coordinator import MIN_DISPATCH_S, Coordinator
from .deps import EngineDeps
from .profiles import check_request
from .run import DecisionRunImpl
from .signatures import PEPPER_REF, input_digest, input_view_signature, option_order_seed, tenant_digest_key
from .state import StageInfo
from .usage import UsageSum, usage_of_wire, zero_usage_wire
from .views import ViewsExpired, input_modalities, input_parts, stage_members

_log: Final = logging.getLogger("noema.decision.engine")
MIN_DISPATCH_MS: Final = round(MIN_DISPATCH_S * 1000)
_NO_DISPATCH: Final = "The deadline leaves no time to dispatch the decision."


class DecisionEngineImpl:
    """Implements `DecisionEngine` over `EngineDeps` (composition root: one per process)."""

    def __init__(self, deps: EngineDeps) -> None:
        self.deps = deps

    def run(self, ctx: DecisionContext, req: DecisionRequest) -> AbstractAsyncContextManager[DecisionRun]:
        return RunContext(self.deps, ctx, req)

    async def decide(self, ctx: DecisionContext, req: DecisionRequest) -> DecisionSnapshot:
        async with self.run(ctx, req) as run:
            return await run.finalize()


class RunContext:
    """The `async with engine.run(ctx, req) as run:` context manager."""

    def __init__(self, deps: EngineDeps, ctx: DecisionContext, req: DecisionRequest) -> None:
        self._deps, self._ctx, self._req = deps, ctx, req
        self._run: DecisionRunImpl | None = None
        self._stack: AsyncExitStack | None = None
        self._tg: anyio.abc.TaskGroup | None = None

    async def __aenter__(self) -> DecisionRunImpl:
        run = await self._prepare()
        self._run = run
        if run.coordinator is not None:
            stack = AsyncExitStack()
            self._tg = await stack.enter_async_context(anyio.create_task_group())
            self._stack = stack
            self._tg.start_soon(run.coordinator.run, self._tg)
        return run

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> bool | None:
        run = self._run
        try:
            with anyio.CancelScope(shield=True):  # a client abort must not lose the row or the usage lines
                if run is not None:
                    await run.cancel()
                    if run.failure is None:
                        await run.finalize()
                    else:  # a coordinator bug: no row and no L2 put, but the dispatched calls are billed
                        run.record_failure_usage()
        finally:
            if self._tg is not None:
                self._tg.cancel_scope.cancel()
            if self._stack is not None:
                await self._stack.aclose()
        return None

    async def _prepare(self) -> DecisionRunImpl:
        deps, ctx, req = self._deps, self._ctx, self._req
        snapshot, tenant_id = ctx.snapshot, ctx.tenant.tenant_id
        profile = check_request(snapshot, req)
        await deps.broker.get(tenant_id)  # fail fast (no row, no usage); adapters fetch credentials per call
        decision_id = ctx.decision_id or deps.ids.new("dec")
        seed = option_order_seed(decision_id)
        pepper = await deps.secrets.get(PEPPER_REF)
        digest = input_digest(tenant_digest_key(pepper, tenant_id), req.input)
        clock = deps.clock
        started_at, t_start = clock.now(), clock.monotonic()
        try:  # the views must leave MIN_DISPATCH_MS before ctx.deadline (else the dispatch check would fail)
            members = await stage_members(
                snapshot,
                profile,
                req.input,
                deps.views,
                ctx.principal,
                clock=clock,
                until=ctx.deadline.at - MIN_DISPATCH_S,
            )
        except ViewsExpired:
            raise NoemaError(ErrorCode.DEADLINE_EXCEEDED, _NO_DISPATCH) from None
        t0 = clock.monotonic()  # the decision clock starts once the stage-0 views are resolved (IMPL-074)
        uncapped = t0 + req.deadline_ms / 1000
        stage0 = StageInfo(
            index=0,
            profile=profile,
            members=members,
            t0=t0,
            deadline=min(uncapped, ctx.deadline.at),
            deadline_ms=req.deadline_ms,
            min_quorum=req.min_quorum,
            view_signature=input_view_signature([(m.member_id, m.view_kind) for m in members]),
            capped=ctx.deadline.at < uncapped,
        )
        epoch = deps.calibrators.epoch(tenant_id)
        key: str | None = None
        if cacheable(ctx, epoch):
            key = l2_key(deps.keys, ctx, req, digest=digest, view_signature=stage0.view_signature, epoch=epoch)
        else:
            deps.telemetry.instruments.add("aghoo_cache_misses_total", layer="l2", reason="bypass")
        if key is not None:
            artifact = await deps.cache.get(key)
            hit = self._from_artifact(artifact, decision_id) if artifact is not None else None
            if hit is not None:
                deps.telemetry.instruments.add("aghoo_cache_hits_total", layer="l2")
                attrs = {"aghoo.profile": profile.id, "aghoo.profile_version": str(profile.version)}
                with deps.telemetry.span("decision", **attrs, **{"aghoo.decision.cache_hit": True}):
                    pass
                return DecisionRunImpl(
                    deps,
                    ctx,
                    req,
                    decision_id=decision_id,
                    digest=digest,
                    seed=seed,
                    started_at=started_at,
                    t_start=t_start,
                    stage0=stage0,
                    l2_key=None,
                    hit=hit[0],
                    fill_usage=hit[1],
                )
        if ctx.deadline.remaining_ms() < MIN_DISPATCH_MS:
            raise NoemaError(ErrorCode.DEADLINE_EXCEEDED, _NO_DISPATCH)
        run = DecisionRunImpl(
            deps,
            ctx,
            req,
            decision_id=decision_id,
            digest=digest,
            seed=seed,
            started_at=started_at,
            t_start=t_start,
            stage0=stage0,
            l2_key=key,
        )
        run.attach(
            Coordinator(
                deps,
                ctx,
                req,
                seed=seed,
                stage0=stage0,
                modalities=input_modalities(input_parts(req.input)),
                on_s1=run.publish_s1,
                on_final=run.publish_final,
                on_fail=run.fail,
            )
        )
        return run

    def _from_artifact(self, artifact: bytes, decision_id: str) -> tuple[dict[str, JsonValue], UsageSum] | None:
        """An L2 hit as a new decision: new id, `created_at` now, the current trace id, `cache.hit` true, usage
        zeroed (members' usage removed); `started_at`/`completed_at` keep the fill-time values. None (a miss)
        for an artifact that does not answer exactly these questions."""
        try:
            result = orjson.loads(artifact)
        except orjson.JSONDecodeError:
            self._deps.telemetry.instruments.add("aghoo_cache_misses_total", layer="l2", reason="error")
            return None
        names = [q.name for q in self._req.questions.questions]
        answers = result.get("answers") if isinstance(result, dict) else None
        prov = result.get("provenance") if isinstance(result, dict) else None
        if not isinstance(answers, list) or not isinstance(prov, dict):
            return None
        if [a.get("name") for a in answers if isinstance(a, Mapping)] != names:
            return None
        usage = result.get("usage")
        fill = usage_of_wire(usage) if isinstance(usage, dict) else UsageSum()
        result["id"] = decision_id
        result["created_at"] = int(self._deps.clock.now().timestamp())
        result["usage"] = zero_usage_wire()
        prov["trace_id"] = self._ctx.trace_id
        prov["cache"] = {"hit": True}
        for stage in prov.get("stages") or []:
            if isinstance(stage, dict):
                stage["usage"] = zero_usage_wire()
        for answer in answers:
            for member in answer.get("members") or [] if isinstance(answer, dict) else []:
                if isinstance(member, dict):
                    member.pop("usage", None)
        return result, fill
