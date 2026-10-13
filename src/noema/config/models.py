"""Typed, frozen views of a validated platform config (`config.schema.json` rev 6 plus spec-05).

The loader builds these after every check passed, so required keys are present and defaults are filled. Identity
fields (`TenantCfg.id`, `ModelCfg.alias`, `TemplateCfg.ref`, `DecisionProfileCfg.id`, `GenerationProfileCfg.id`) are
the mapping keys of the config. Lists become tuples and maps become read-only mappings.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Literal

from noema.core.protocols.adapters import StreamTimeouts
from noema.core.values import DataClass, Effort

# --- code constants (spec-05 §1.4-§1.5, IMPL-084, IMPL-029c) ---------------------------------------------------------
DEFAULT_CONTEXT_WINDOW: Final = 128_000  # UNVERIFIED placeholder until set per model from the model card
GEN_DEFAULT_MAX_TOKENS: Final = 4096
CHARS_PER_TOKEN_V1: Final = 4.0  # ModelCapabilities.chars_per_token for every Phase 1 model (not config)
# Wave E2 defect 32 (K1): the TokenEstimator's input fit per model alias, (chars per token, per-request overhead
# tokens), from the 48 answered live calls of tests/cassettes/live-20261011T07* (`python -m
# noema.ops.cost.estimator_fit`); an alias not listed counts CHARS_PER_TOKEN_V1 and no overhead. A Phase 0 refit
# overrides it without a code change (`HeuristicTokenEstimator(table=load_table(path))`).
TOKEN_ESTIMATOR_V1: Final[Mapping[str, tuple[float, int]]] = MappingProxyType(
    {
        "deepseek-3.2": (4.12, 7),
        "glm-5.3": (4.0, 25),  # one prompt length in the data: chars/token not fitted (V1), the overhead only
        "haiku-5.5": (3.06, 10),  # the three Claude models: one tokenizer, one fit
        "luna-6": (3.99, 0),
        "opus-5.5": (3.06, 10),
        "sol-6.1": (4.61, 11),
        "sonnet-5.5": (3.06, 10),
    }
)
DEFAULT_DECISION_PROFILE: Final = "lightweight-model-decide"  # P1-T1 fill (IMPL-031, D6)
# spec-06 §5.5: the reasoning reserve R per effort of the verbalized templates and labeler@1 (part of the template
# contract: a change is a new template version; `models.<alias>.reasoning_reserve_tokens` is a floor).
EFFORT_RESERVE: Final[Mapping[Effort, int]] = MappingProxyType(
    {"none": 0, "low": 512, "medium": 1024, "high": 2048, "xhigh": 4096, "max": 4096}
)
DECISION_PROMPT_OVERHEAD: Final = 600  # spec-11 §1.1: template tokens around the input, per decision call

_SOL_FAST: Final = StreamTimeouts(connect_s=3, first_event_s=5, first_answer_s=8, idle_s=30)
_GLM_REASON: Final = StreamTimeouts(connect_s=3, first_event_s=10, first_answer_s=30, idle_s=30)
ROUTE_TIMEOUTS_V1_DEFAULT: Final = StreamTimeouts(connect_s=3, first_event_s=30, first_answer_s=30, idle_s=30)
_ALL_EFFORTS: Final[tuple[Effort | None, ...]] = (None, "none", "low", "medium", "high", "xhigh", "max")
# (model alias, effective effort) -> timeouts (04:L127-132); any other pair gets ROUTE_TIMEOUTS_V1_DEFAULT.
ROUTE_TIMEOUTS_V1: Final[Mapping[tuple[str, Effort | None], StreamTimeouts]] = MappingProxyType(
    {
        **{("sol-6.1", e): _SOL_FAST for e in (None, "none", "low")},
        **{("glm-5.3", e): _GLM_REASON for e in _ALL_EFFORTS},
    }
)


def route_timeouts_v1(model_alias: str, effort: Effort | None) -> StreamTimeouts:
    """ROUTE_TIMEOUTS_V1 lookup with the permissive default row."""
    return ROUTE_TIMEOUTS_V1.get((model_alias, effort), ROUTE_TIMEOUTS_V1_DEFAULT)


ConfigModality = Literal["text", "image", "document", "audio", "video"]
Elicitation = Literal["token_logprobs", "verbalized", "sampling", "native_decision"]
ModelRole = Literal["decision", "generation", "labeler", "selector"]


def _empty_map() -> Mapping[str, ModelQuotaCfg]:
    return MappingProxyType({})


def _key() -> str:
    # Placeholder default for identity fields; the loader always sets them from the mapping key.
    return ""


KEY_FIELD: Final = "is_key"  # dataclass field metadata marking an identity field filled from the mapping key


def key_field() -> str:
    """A dataclass field filled from the enclosing mapping key."""
    return field(default_factory=_key, metadata={KEY_FIELD: True})


# --- tenants -----------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ModelQuotaCfg:
    rpm: int
    tpm: int


@dataclass(frozen=True, slots=True)
class TenantLimitsCfg:
    """Per-tenant overrides of `platform.limits`; None = the platform default (spec-05 §1.2)."""

    rpm_per_key: int | None = None
    tpm_per_key: int | None = None
    rpm_per_project: int | None = None
    tpm_per_project: int | None = None
    concurrent_streams_per_project: int | None = None
    model_quotas: Mapping[str, ModelQuotaCfg] = field(default_factory=_empty_map)


@dataclass(frozen=True, slots=True)
class TenantDefaultsCfg:
    """`decision_profile` is always set (the P1-T1 fill, IMPL-031)."""

    decision_profile: str
    generation_profile: str | None = None


@dataclass(frozen=True, slots=True)
class RequestOverridesCfg:
    """Profiles a request may select explicitly (allowlist)."""

    decision_profiles: tuple[str, ...] = ()
    generation_profiles: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TenantCfg:
    """`tenants.<id>`. `allowed_models` None = every configured model; `request_overrides` None = none declared."""

    aws_account_id: str
    invoke_role_arn: str
    external_id_ref: str
    home_region: str
    defaults: TenantDefaultsCfg
    id: str = key_field()
    allowed_models: tuple[str, ...] | None = None
    request_overrides: RequestOverridesCfg | None = None
    limits: TenantLimitsCfg | None = None


# --- models and templates ----------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ModelControlsCfg:
    """`models.<alias>.controls`; an absent `structured_output`/`reasoning` means false."""

    temperature: bool
    effort: tuple[Effort, ...]
    logprobs: bool
    top_logprobs_max: int | None = None
    structured_output: bool = False
    reasoning: bool = False


@dataclass(frozen=True, slots=True)
class ModelCfg:
    """`models.<alias>`. `id` is the Bedrock model or inference-profile id; `context_window` absent =
    DEFAULT_CONTEXT_WINDOW (IMPL-084); `reasoning_reserve_tokens` absent = 0."""

    adapter: Literal["bedrock-converse", "bedrock-mantle-chat"]
    id: str
    roles: frozenset[ModelRole]
    modalities: frozenset[ConfigModality]
    controls: ModelControlsCfg
    alias: str = key_field()
    max_output_tokens_cap: int | None = None
    price_ref: str | None = None
    reasoning_reserve_tokens: int = 0
    context_window: int = DEFAULT_CONTEXT_WINDOW


@dataclass(frozen=True, slots=True)
class TemplateCfg:
    """`templates.<name@version>`."""

    elicitation: Elicitation
    max_choices: int
    ref: str = key_field()


# --- decision profiles -------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DecisionMemberCfg:
    """One decision member (`DecisionProfile.members[]`)."""

    model: str
    elicitation: Elicitation
    template: str
    weight: float
    effort: Effort | None = None
    samples: int | None = None
    optional: bool = False
    backup_after_ms: int | None = None
    latency_p95_ms: int | None = None


@dataclass(frozen=True, slots=True)
class EscalationWhenCfg:
    confidence_below: float | None = None
    margin_below: float | None = None
    any_refused: bool | None = None
    unavailable: bool | None = None


@dataclass(frozen=True, slots=True)
class EscalationCfg:
    """`questions` None = every escalatable platform question (IMPL-068)."""

    to_profile: str
    when: EscalationWhenCfg
    questions: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class ChoiceCfg:
    value: str
    description: str | None = None


@dataclass(frozen=True, slots=True)
class LevelCfg:
    label: str
    description: str | None = None


@dataclass(frozen=True, slots=True)
class CriteriaCfg:
    """Predicate criteria text for the true and false sides."""

    true: str | None = None
    false: str | None = None


@dataclass(frozen=True, slots=True)
class ConfigQuestionCfg:
    """A built-in (platform) question of a decision profile."""

    type: Literal["predicate", "choice", "score"]
    name: str
    instructions: str
    criteria: CriteriaCfg | None = None
    choices: tuple[ChoiceCfg, ...] | None = None
    dynamic_choices: Literal["candidates"] | None = None
    levels: tuple[LevelCfg, ...] | None = None


@dataclass(frozen=True, slots=True)
class DecisionProfileCfg:
    """`decision_profiles.<id>` (the active version, IMPL-031). `modality_policy` and `batch_questions` are
    filled from the schema defaults."""

    version: int
    deadline_ms: int
    min_quorum: int
    members: tuple[DecisionMemberCfg, ...]
    aggregation: Literal["linear_pool", "log_pool", "stacked"]
    modality_policy: Literal["derive_for_text_members", "skip_incapable_members"]
    batch_questions: bool
    id: str = key_field()
    escalation: EscalationCfg | None = None
    swap_note: str | None = None
    questions: tuple[ConfigQuestionCfg, ...] = ()
    commit_critical: tuple[str, ...] = ()


# --- generation profiles -----------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class GenMemberCfg:
    model: str
    effort: Effort | None = None
    max_output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class SelectorCfg:
    decision_profile: str
    question: str


@dataclass(frozen=True, slots=True)
class GenerationProfileCfg:
    """`generation_profiles.<id>`; best_of_n and fuse are config-only in Phase 1 (P1-G1, IMPL-036)."""

    mode: Literal["single", "fallback", "race", "best_of_n", "fuse"]
    members: tuple[GenMemberCfg, ...]
    id: str = key_field()
    selector: SelectorCfg | None = None
    synthesizer: GenMemberCfg | None = None
    streaming: Literal["live", "after_selection"] | None = None
    cost_multiplier_max: float | None = None


# --- routing -----------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class BindingWhenCfg:
    """`routing.bindings[].when`; `source` is filled with its default "platform"."""

    answer: str
    source: Literal["platform", "request"] = "platform"
    mode: Literal["rule", "expected_utility"] | None = None
    value_in: tuple[str | bool, ...] | None = None
    level_in: tuple[str, ...] | None = None
    p_max_gte: float | None = None
    probability_gte: float | None = None
    margin_gte: float | None = None
    confidence_band: Literal["high", "medium", "low"] | None = None
    requires_calibrated: bool | None = None
    heuristic_threshold: bool | None = None
    eval_ref: str | None = None
    answer_type: Literal["predicate", "choice", "score"] | None = None


@dataclass(frozen=True, slots=True)
class BindingThenCfg:
    """`routing.bindings[].then`; `generation_profile` is a profile id or "intent_routes[<answer>]"."""

    generation_profile: str | None = None
    effort: Effort | None = None
    max_output_tokens: int | None = None
    conditioning: Literal["direct", "cover_top2_and_clarify", "clarify_first", "none"] | None = None
    disable_tools: bool | None = None
    spotlight: Literal["standard", "strict"] | None = None
    response_cache: Literal["on", "off"] | None = None
    log_content: Literal["full", "redacted", "none"] | None = None
    verify_answer: bool | None = None


@dataclass(frozen=True, slots=True)
class BindingCfg:
    when: BindingWhenCfg
    then: BindingThenCfg


@dataclass(frozen=True, slots=True)
class ConfidenceBandsCfg:
    high_gte: float
    low_lt: float


@dataclass(frozen=True, slots=True)
class VerificationCfg:
    decision_profile: str | None = None
    questions: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class SpeculationCfg:
    """`commit_on_quorum` is always false in Phase 1 (P1-D1)."""

    holdback_ms: int
    commit_on_quorum: bool = False
    restart_on_conditioning_change: bool | None = None
    max_hold_ms: int | None = None
    slack_ms: int | None = None
    proof_ref: str | None = None


@dataclass(frozen=True, slots=True)
class RoutingCfg:
    """`routing`; `default_profile` is required in Phase 1 (P1-R1)."""

    policy_version: int
    router_profile: str
    intent_routes: Mapping[str, str]
    uncertain_profile: str
    default_profile: str
    confidence_bands: ConfidenceBandsCfg
    bindings: tuple[BindingCfg, ...]
    speculation: SpeculationCfg
    verification: VerificationCfg | None = None


# --- labeling ----------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class LabelingSampleCfg:
    uniform_rate: float
    uncertainty_rate: float
    max_per_day_per_tenant: int | None = None


@dataclass(frozen=True, slots=True)
class AcceptanceCfg:
    """Calibrator acceptance thresholds (ADR-003 §3), filled from the schema defaults."""

    ece_max: float
    n_min: int
    class_n_min: int
    consensus_min: float
    drift_ece_max: float
    subset_min_share: float


@dataclass(frozen=True, slots=True)
class LabelingCfg:
    labelers: tuple[str, ...]
    rule: Literal["unanimous"]
    sample: LabelingSampleCfg
    runs_in: Literal["tenant_account"]
    family_overlap_lift_max: float
    acceptance: AcceptanceCfg


# --- platform (spec-05 §1.1; every field filled) -------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PlatformLimitsCfg:
    max_json_body_bytes: int
    rpm_per_key: int
    tpm_per_key: int
    rpm_per_project: int
    tpm_per_project: int
    concurrent_streams_per_project: int
    admission_queue_timeout_ms: int


@dataclass(frozen=True, slots=True)
class IdempotencyCfg:
    ttl_s: int


@dataclass(frozen=True, slots=True)
class EventsCfg:
    resume_window_s: int


@dataclass(frozen=True, slots=True)
class SessionsCfg:
    queue_timeout_ms: int


@dataclass(frozen=True, slots=True)
class DecisionCacheCfg:
    ttl_s: int


@dataclass(frozen=True, slots=True)
class CostCfg:
    """`prices_ref` None = every price TBD (IMPL-019)."""

    cancel_overrun_tokens: int
    prices_ref: str | None = None


@dataclass(frozen=True, slots=True)
class MediaCfg:
    inline_wait_ms: int


@dataclass(frozen=True, slots=True)
class CredentialsCfg:
    mantle_auth: Literal["bearer_short_term", "sigv4"]


@dataclass(frozen=True, slots=True)
class DataClassPolicyCfg:
    store_content: Literal["full", "redacted", "metadata_only"]
    retain_days: int
    l2_cache: bool


@dataclass(frozen=True, slots=True)
class DataClassesCfg:
    """The data-class policy table, overrides merged per field over the spec-05 §1.1 defaults."""

    public: DataClassPolicyCfg
    internal: DataClassPolicyCfg
    confidential: DataClassPolicyCfg
    restricted: DataClassPolicyCfg

    def for_class(self, data_class: DataClass) -> DataClassPolicyCfg:
        """The policy of one data class."""
        policy: DataClassPolicyCfg = getattr(self, data_class)
        return policy


@dataclass(frozen=True, slots=True)
class PlatformCfg:
    limits: PlatformLimitsCfg
    idempotency: IdempotencyCfg
    events: EventsCfg
    sessions: SessionsCfg
    decision_cache: DecisionCacheCfg
    cost: CostCfg
    media: MediaCfg
    credentials: CredentialsCfg
    data_classes: DataClassesCfg


# --- the whole config ----------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class NoemaConfig:
    """The validated, filled platform config."""

    config_version: int
    tenants: Mapping[str, TenantCfg]
    models: Mapping[str, ModelCfg]
    templates: Mapping[str, TemplateCfg]
    decision_profiles: Mapping[str, DecisionProfileCfg]
    generation_profiles: Mapping[str, GenerationProfileCfg]
    routing: RoutingCfg
    labeling: LabelingCfg
    platform: PlatformCfg
