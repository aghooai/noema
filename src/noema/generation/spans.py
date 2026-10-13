"""`generation.attempt` span attributes (08 §7, IMPL-006): GenAI semconv names through the telemetry mapper."""

from __future__ import annotations

from collections.abc import Sequence

from noema.core.protocols.generation import AssembledPrompt
from noema.core.protocols.telemetry import Span
from noema.core.values import Degradation
from noema.telemetry.genai import CONVERSATION_ID, request_attributes, response_attributes, usage_attributes

from .attempt import Attempt

AttrValue = str | int | float | bool


def start_attributes(
    *,
    model_id: str,
    tenant_id: str,
    route_id: str,
    spec_signature: str,
    session_id: str | None,
    prompt: AssembledPrompt,
    degradations: Sequence[Degradation],
) -> dict[str, AttrValue]:
    """The attributes known when the attempt starts (`chat {gen_ai.request.model}`)."""
    attrs: dict[str, AttrValue] = {
        **request_attributes(model_id),
        "aghoo.tenant": tenant_id,
        "aghoo.profile": route_id,
        "aghoo.attempt.spec_signature": spec_signature,
    }
    if session_id is not None:
        attrs[CONVERSATION_ID] = session_id
    if prompt.conditioning is not None:
        attrs["aghoo.conditioning.answer_mode"] = prompt.conditioning.answer_mode
    if degradations:
        attrs["aghoo.degradation.reason"] = ",".join(sorted({d.reason for d in degradations}))
    return attrs


def annotate_attempt(span: Span, att: Attempt) -> None:
    """Outcome, finish reason, usage, time to first chunk and the error code of a finished attempt."""
    if att.outcome is not None:
        span.set("aghoo.attempt.outcome", att.outcome)
    ttfc = None if att.first_text_at is None else att.first_text_at - att.started_mono
    for key, value in response_attributes(att.stop, time_to_first_chunk_s=ttfc).items():
        span.set(key, value)
    record = att.record
    if record is not None:
        for key, tokens in usage_attributes(record.usage).items():
            span.set(key, tokens)
    if att.error_code is not None:
        span.error(att.error_code)
