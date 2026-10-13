"""Stage members and their input views (spec-06 §4.2-§4.4; IMPL-058, IMPL-075).

Text-only input needs no resolver call (`native`). With media, every member's view is resolved concurrently,
bounded by the stage's latest start (`ViewsExpired` past it); a member whose view of a media modality is `skipped`
or `metadata_only` does not run (`skipped / input_view`).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Final

import anyio

from noema.config.models import DecisionProfileCfg
from noema.core.clock import Clock, deadline_scope
from noema.core.principal import Principal
from noema.core.protocols.config import ConfigSnapshot
from noema.core.protocols.decision import DecisionInput
from noema.core.protocols.media import VIEW_BUDGET_V1, ContentPart, InputView, InputViewResolver, ViewKind
from noema.core.values import Modality

from .elicitation.render import view_blocks
from .elicitation.templates import template
from .state import StageMember

_log: Final = logging.getLogger("noema.decision.views")
_NOT_RUN: Final = frozenset({"skipped", "metadata_only"})


class ViewsExpired(Exception):
    """The views were not resolved by `until`: stage 0 answers 504, an escalation stage is `skipped_budget`."""


def input_parts(value: DecisionInput) -> tuple[ContentPart, ...]:
    """Every part of a messages input, in order (empty for text, JSON state and verification input)."""
    if isinstance(value, tuple):
        return tuple(p for m in value for p in m.parts)
    return ()


def part_modality(part: ContentPart) -> Modality:
    """`image` for images (inline or uploaded), `document` for other files, `text` otherwise."""
    if part.kind == "image":
        return "image"
    if part.kind == "file":
        media = (part.file.media_type or part.file.declared_type) if part.file is not None else ""
        return "image" if media.startswith("image/") else "document"
    return "text"


def input_modalities(parts: Sequence[ContentPart]) -> frozenset[Modality]:
    """The media modalities of the input (adequacy is checked for each, spec-06 §4.4)."""
    return frozenset(m for m in (part_modality(p) for p in parts) if m != "text")


def _runs(view: InputView) -> bool:
    return view.kind not in _NOT_RUN and not any(v in _NOT_RUN for v in view.by_modality.values())


async def stage_members(
    snapshot: ConfigSnapshot,
    profile: DecisionProfileCfg,
    value: DecisionInput,
    resolver: InputViewResolver,
    principal: Principal,
    *,
    clock: Clock,
    until: float,
) -> tuple[StageMember, ...]:
    """The members of `profile` (`m1`...) with their resolved views and rendered input blocks. Raises
    `ViewsExpired` when media views are still resolving at `until` (monotonic, on `clock`)."""
    parts = input_parts(value)
    media = bool(input_modalities(parts))
    views: list[InputView | None] = [None] * len(profile.members)
    if media:

        async def resolve(i: int) -> None:
            member = profile.members[i]
            try:
                views[i] = await resolver.resolve(
                    member,
                    parts,
                    policy=profile.modality_policy,
                    budget=VIEW_BUDGET_V1,
                    model=snapshot.model(member.model),
                    principal=principal,
                )
            except Exception:  # the resolver never raises for media problems; anything else skips the member
                _log.exception("input view resolution failed (member m%d)", i + 1)
                views[i] = InputView("metadata_only", {}, (), ())

        async with deadline_scope(clock, until) as scope, anyio.create_task_group() as tg:
            for i in range(len(profile.members)):
                tg.start_soon(resolve, i)
        if scope.cancelled_caught:
            raise ViewsExpired
    out: list[StageMember] = []
    for i, cfg in enumerate(profile.members):
        view = views[i]
        runs = view is None or _runs(view)
        kind: ViewKind = "native" if view is None else view.kind
        out.append(
            StageMember(
                member_id=f"m{i + 1}",
                cfg=cfg,
                model=snapshot.model(cfg.model),
                template=template(cfg.template),
                max_choices=snapshot.template(cfg.template).max_choices,
                view=view,
                view_kind=kind,
                by_modality=dict(view.by_modality) if view is not None else {},
                skipped=not runs,
                view_blocks=view_blocks(value, view) if runs else (),
            )
        )
    return tuple(out)
