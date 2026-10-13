"""`StreamingGenerationEngine` (spec-02 §10): builds one `StreamingProfileRun` per `run()` and owns its tasks."""

from __future__ import annotations

import random
from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from typing import Final, Literal

import anyio

from noema.core.clock import Clock
from noema.core.errors import ErrorCode, NoemaError
from noema.core.ids import IdGen
from noema.core.protocols.adapters import AdapterRegistry
from noema.core.protocols.credentials import CredentialBroker
from noema.core.protocols.generation import AttemptBudget, AttemptSpec, PromptBuilder, TurnContext
from noema.core.protocols.ops import CostMeter, HealthRegistry, RetryBudget, TokenEstimator
from noema.core.protocols.telemetry import Telemetry

from .run import GenerationDeps
from .walk import StreamingProfileRun

RUNNABLE_MODES: Final = frozenset({"single", "fallback", "race"})


@contextmanager
def _single_errors() -> Iterator[None]:
    """Re-raise a lone exception out of the task group's exception group (the context manager stays transparent
    to what its body raises)."""
    try:
        yield
    except BaseExceptionGroup as group:
        if len(group.exceptions) == 1:
            raise group.exceptions[0] from None
        raise


class StreamingGenerationEngine:
    """Implements `GenerationEngine` over the adapters' `stream()` (spec-04 §3, §4.1, §6).

    `run()` fails fast on the tenant's credentials (`CredentialBroker.get` once, IMPL-076; the adapters fetch and
    assert them again per call) and raises `NoemaError(feature_not_available)` for `best_of_n`/`fuse` profiles
    (Phase 2+; unreachable through the Phase 1 config rules and ingress). `rng` drives the throttling jitter (a
    seeded `random.Random` in tests)."""

    def __init__(
        self,
        *,
        clock: Clock,
        ids: IdGen,
        adapters: AdapterRegistry,
        broker: CredentialBroker,
        health: HealthRegistry,
        retry_budget: RetryBudget,
        estimator: TokenEstimator,
        cost: CostMeter,
        telemetry: Telemetry,
        rng: random.Random | None = None,
    ) -> None:
        self._broker = broker
        self._deps = GenerationDeps(
            clock=clock,
            ids=ids,
            adapters=adapters,
            health=health,
            retry_budget=retry_budget,
            estimator=estimator,
            cost=cost,
            telemetry=telemetry,
            rng=rng if rng is not None else random.Random(),  # noqa: S311 (backoff jitter, not cryptography)
        )

    def run(
        self,
        ctx: TurnContext,
        spec: AttemptSpec,
        prompt: PromptBuilder,
        *,
        hold: bool,
        speculative: bool,
        auto_commit: Literal["spec_agrees", "gate"] = "spec_agrees",
        failure_budget: AttemptBudget,
    ) -> AbstractAsyncContextManager[StreamingProfileRun]:
        """See `GenerationEngine.run`. `__aexit__` ends every live attempt (stuck: cancelled(deadline), counted in
        aghoo_stuck_attempts_total) and waits for the attempt tasks."""
        return self._run(ctx, spec, prompt, hold, speculative, auto_commit, failure_budget)

    @asynccontextmanager
    async def _run(
        self,
        ctx: TurnContext,
        spec: AttemptSpec,
        prompt: PromptBuilder,
        hold: bool,
        speculative: bool,
        auto_commit: Literal["spec_agrees", "gate"],
        failure_budget: AttemptBudget,
    ) -> AsyncIterator[StreamingProfileRun]:
        profile = ctx.snapshot.generation_profile(spec.generation_profile)
        if profile.mode not in RUNNABLE_MODES:
            raise NoemaError(ErrorCode.FEATURE_NOT_AVAILABLE, f"Generation mode {profile.mode} is not available.")
        await self._broker.get(ctx.tenant.tenant_id)  # raises TenantAccessMisconfigured (fail fast)
        run = StreamingProfileRun(
            self._deps,
            ctx,
            spec,
            profile,
            prompt,
            hold=hold,
            speculative=speculative,
            auto_commit=auto_commit,
            failure_budget=failure_budget,
        )
        with _single_errors():
            async with anyio.create_task_group() as tg:
                run.start(tg)
                try:
                    yield run
                finally:
                    with anyio.CancelScope(shield=True):
                        await run.close()  # every task of the run ends on its own after this
