"""Conformance registrations of the real media-negotiation implementations (spec-02 §14): `InputViewResolverImpl`,
`MediaNegotiatorImpl` and `MediaBudgetPlannerImpl`, over the fake FileService (the real one is media-pipeline's)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from tests.contract_internal.kits import MediaKit
from tests.contract_internal.registry import Harness, register
from tests.fakes.config import example_snapshot
from tests.fakes.world import build_world

from noema.media.budget import MediaBudgetPlannerImpl
from noema.media.negotiation import MediaNegotiatorImpl
from noema.media.views import InputViewResolverImpl


@register("input_view_resolver", "real")
@register("media_negotiator", "real")
@register("media_budget", "real")
@asynccontextmanager
async def real_media(h: Harness) -> AsyncIterator[MediaKit]:
    world = build_world(clock=h.clock)

    def pending_uploads(pending: bool) -> None:
        world.files.auto_ready = not pending

    def reject(file_id: str) -> None:
        world.files.set_status(file_id, "rejected", rejection={"reason": "type_mismatch"})

    yield MediaKit(
        world.files,
        InputViewResolverImpl(world.files),
        MediaNegotiatorImpl(world.files),
        MediaBudgetPlannerImpl(),
        example_snapshot(),
        h.clock,
        pending_uploads=pending_uploads,
        reject=reject,
    )
