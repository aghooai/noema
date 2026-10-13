"""The `ModelRequest` of one member call (spec-06 §5; IMPL-050): every prompt byte is decided here."""

from __future__ import annotations

from collections.abc import Sequence

from noema.core.protocols.adapters import ModelRequest
from noema.core.protocols.config import ConfigSnapshot
from noema.core.protocols.decision import OptionSpec
from noema.core.values import CallKind, Message
from noema.decision.state import StageMember

from .render import app_question, user_blocks
from .templates import max_tokens, top_logprobs


def member_request(
    member: StageMember,
    instructions: str,
    options: Sequence[OptionSpec],
    criteria: tuple[str | None, str | None] | None,
    *,
    call_kind: CallKind,
    snapshot: ConfigSnapshot | None,
) -> ModelRequest:
    """System = the template's static text; user = `[B_q, B_open, *view blocks, B_close]`; `max_tokens` from the
    template contract; logprobs only for `logprob-codes@2`; never retried; never `temperature`."""
    tpl = member.template
    aq = app_question(instructions, options, criteria)
    return ModelRequest(
        model_alias=member.cfg.model,
        system=(tpl.system,),
        messages=(Message("user", user_blocks(aq, member.view_blocks, tpl.tail)),),
        max_tokens=max_tokens(tpl, member.model, member.cfg, len(options)),
        call_kind=call_kind,
        retry="none",
        effort=member.cfg.effort,
        logprobs=tpl.logprobs,
        top_logprobs=top_logprobs(member.model) if tpl.logprobs else None,
        snapshot=snapshot,
    )
