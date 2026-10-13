"""Model capabilities (spec-02 §4.1) and the probe-verified capability matrix.

`capabilities_of(model)` is what `Adapter.capabilities(alias)` returns: the configured `models.<alias>` controls and
modalities (config is authoritative at run time, IMPL-084, IMPL-043). `PROBED` records what the live probes of
2026-10-08/09 (`probes/results/*.json`, `.orchestration/context/03-env-probes-gates.md`) and the Wave E live smoke
of 2026-10-11 (GLM-5.3 effort, `reports/qa/live-smoke.md`) verified per Bedrock model id;
`probe_discrepancies(snapshot)` lists config claims those probes refuted (the integrator logs them at startup and
the config tests assert none), and `probe_unverified(snapshot)` lists claims no probe covered (e.g. Sonnet 5.5
media, IMPL-043).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

from noema.config.models import CHARS_PER_TOKEN_V1, ModelCfg
from noema.core.protocols.adapters import ModelCapabilities
from noema.core.protocols.config import ConfigSnapshot
from noema.core.values import Effort, Modality

ProbedApi = Literal["bedrock-converse", "bedrock-mantle-chat"]
_PHASE1_MODALITIES: Final[frozenset[str]] = frozenset({"text", "image", "document"})


def capabilities_of(model: ModelCfg) -> ModelCapabilities:
    """The `ModelCapabilities` of one `models.<alias>` entry (audio/video are not Phase 1 modalities)."""
    c = model.controls
    modalities: frozenset[Modality] = frozenset(
        m for m in model.modalities if m == "text" or m == "image" or m == "document"
    )
    return ModelCapabilities(
        modalities=modalities,
        temperature=c.temperature,
        effort=frozenset(c.effort),
        logprobs=c.logprobs,
        top_logprobs_max=c.top_logprobs_max,
        structured_output=c.structured_output,
        reasoning=c.reasoning,
        max_output_tokens_cap=model.max_output_tokens_cap,
        context_window=model.context_window,
        chars_per_token=CHARS_PER_TOKEN_V1,
    )


@dataclass(frozen=True, slots=True)
class ProbedModel:
    """What the probes verified for one Bedrock model id. `None` = not probed. `effort_accepted`/`rejected`:
    `outputConfig.effort` values the service accepted or rejected; `modalities`/`rejected_modalities`: native
    content blocks accepted or rejected (text is always accepted); `apis`: the APIs that served the model."""

    model_id: str
    apis: frozenset[ProbedApi]
    temperature: bool | None
    effort_accepted: frozenset[Effort]
    effort_rejected: frozenset[str]
    modalities: frozenset[Modality]
    rejected_modalities: frozenset[str]
    logprobs: bool | None
    reasoning_stream: bool | None
    note: str = ""


def _probe(
    model_id: str,
    apis: frozenset[ProbedApi],
    *,
    temperature: bool | None,
    accepted: tuple[Effort, ...] = (),
    rejected: tuple[str, ...] = (),
    modalities: tuple[Modality, ...] = ("text",),
    rejected_modalities: tuple[str, ...] = ("audio", "video"),
    logprobs: bool | None = False,
    reasoning_stream: bool | None = None,
    note: str = "",
) -> ProbedModel:
    return ProbedModel(
        model_id,
        apis,
        temperature,
        frozenset(accepted),
        frozenset(rejected),
        frozenset(modalities),
        frozenset(rejected_modalities),
        logprobs,
        reasoning_stream,
        note,
    )


_CONVERSE: Final[frozenset[ProbedApi]] = frozenset({"bedrock-converse"})
_ALL_EFFORT: Final[tuple[Effort, ...]] = ("low", "medium", "high", "max")
PROBED: Final[Mapping[str, ProbedModel]] = MappingProxyType(
    {
        p.model_id: p
        for p in (
            _probe(
                "us.anthropic.claude-haiku-5-5",
                _CONVERSE,
                temperature=False,
                accepted=_ALL_EFFORT,
                rejected=("none",),
                modalities=("text", "image", "document"),
                note="Mantle /v1/chat/completions rejects Claude models",
            ),
            _probe(
                "us.anthropic.claude-opus-5-5",
                _CONVERSE,
                temperature=False,
                accepted=_ALL_EFFORT,
                rejected=("none",),
                modalities=("text", "image", "document"),
                reasoning_stream=True,
                note="streams reasoningContent first; first text 11.6 s at default effort, 2.0 s at low",
            ),
            _probe(
                "us.openai.gpt-6.1-sol",
                _CONVERSE,
                temperature=False,
                accepted=_ALL_EFFORT,
                rejected=("none",),
                modalities=("text", "image"),
                rejected_modalities=("document", "audio", "video"),
            ),
            _probe(
                "us.openai.gpt-6-luna",
                _CONVERSE,
                temperature=False,
                accepted=("none", "low", "medium", "high", "xhigh", "max"),
                rejected=("minimal",),
                modalities=("text", "image"),
                rejected_modalities=("document", "audio", "video"),
                note="on Mantle only /openai/v1/responses (not called in Phase 1); no logprobs",
            ),
            _probe(
                "us.zai.glm-5.3",
                _CONVERSE,
                temperature=True,
                accepted=("low", "medium", "high", "xhigh", "max"),
                rejected=("none",),
                rejected_modalities=("image", "document", "audio", "video"),
                reasoning_stream=True,
                note="~665 reasoning deltas at default effort, none at low; reasoning-only max_tokens stops; effort "
                "none rejected live since 2026-10-11 (ValidationException, tests/cassettes/live-20261011T073404/01)",
            ),
            _probe(
                "deepseek.v3.2",
                frozenset({"bedrock-mantle-chat"}),
                temperature=True,
                rejected_modalities=("image", "document", "audio", "video"),
                logprobs=True,
                note="logprobs on /v1/chat/completions; 4-12 s tail outliers",
            ),
            _probe(
                "us.anthropic.claude-sonnet-5-5",
                _CONVERSE,
                temperature=False,
                rejected_modalities=(),
                logprobs=None,
                note="probed once (default call + temperature rejection); media UNVERIFIED (IMPL-043)",
            ),
        )
    }
)


def _models(snapshot: ConfigSnapshot) -> Mapping[str, ModelCfg]:
    config = getattr(snapshot, "config", None)
    models = getattr(config, "models", None)
    if not isinstance(models, Mapping):
        raise TypeError("snapshot has no models mapping (expected the config loader's snapshot)")
    return models


def probe_discrepancies(models: Mapping[str, ModelCfg] | ConfigSnapshot) -> list[str]:
    """Config claims the probes refuted, one message per claim (empty = consistent). Accepts the `models` mapping
    or a loader snapshot (`snapshot.config.models`)."""
    table = models if isinstance(models, Mapping) else _models(models)
    problems: list[str] = []
    for alias, model in sorted(table.items()):
        probe = PROBED.get(model.id)
        if probe is None:
            continue
        where = f"models.{alias} ({model.id})"
        if model.adapter not in probe.apis:
            problems.append(f"{where}: adapter {model.adapter} was not verified for this model")
        if model.controls.temperature and probe.temperature is False:
            problems.append(f"{where}: controls.temperature is true but Bedrock rejects temperature")
        problems.extend(
            f"{where}: controls.effort lists {effort!r}, which Bedrock rejects"
            for effort in sorted(set(model.controls.effort) & probe.effort_rejected)
        )
        problems.extend(
            f"{where}: modality {modality!r} is rejected by Bedrock"
            for modality in sorted(set(model.modalities) & probe.rejected_modalities)
        )
        if model.controls.logprobs and probe.logprobs is False:
            problems.append(f"{where}: controls.logprobs is true but the model returns no logprobs")
    return problems


def probe_unverified(models: Mapping[str, ModelCfg] | ConfigSnapshot) -> list[str]:
    """Config claims no probe verified (not errors: the live smoke covers them, spec-10 §6)."""
    table = models if isinstance(models, Mapping) else _models(models)
    notes: list[str] = []
    for alias, model in sorted(table.items()):
        probe = PROBED.get(model.id)
        where = f"models.{alias} ({model.id})"
        if probe is None:
            notes.append(f"{where}: not probed")
            continue
        unknown = (set(model.modalities) & _PHASE1_MODALITIES) - {"text"} - probe.modalities - probe.rejected_modalities
        notes.extend(f"{where}: modality {modality!r} is unverified" for modality in sorted(unknown))
        notes.extend(
            f"{where}: effort {effort!r} is unverified"
            for effort in sorted(set(model.controls.effort) - probe.effort_accepted - probe.effort_rejected)
        )
        if model.controls.temperature and probe.temperature is None:
            notes.append(f"{where}: temperature is unverified")
    return notes
