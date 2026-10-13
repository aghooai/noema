# 02 — Frozen interfaces (`src/noema/core/**`)

Wave A, lead architect. B1 (platform engineer) implements this file verbatim in `src/noema/core/` and freezes it at the interface-freeze tag. After the freeze, changes go only through `.orchestration/requests/<agent>.md` change requests, applied by the integrator at C2. This file includes the B4 fix-round additions and the Wave C2a changes (2026-10-10); where it and `src/noema/core` differ, the code wins (wave-B ruling 1). Decisions are cited as `IMPL-NNN` ([`../IMPL-DECISIONS.md`](../IMPL-DECISIONS.md)).

Conventions:
- **async unless marked `# sync`.** A sync method never does network I/O (IMPL-078).
- Value types are `@dataclass(frozen=True, slots=True)`.
- Protocols are `typing.Protocol` and are not `runtime_checkable`.
- No Pydantic in `core`: strict generated models live in `noema.contracts` (IMPL-041).
- `JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]`.
- Every Protocol below has a fake in `tests/fakes/` and a conformance suite in `tests/contract_internal/` that the fake and the real implementation both pass (§14).
- **Model naming**: a parameter or field called `model`, `alias` or `model_alias` is a config alias (`models.<alias>`). `model_id` is the Bedrock id (`models.<alias>.id`). Wire objects (`generation.started.model`, `TurnObject.attempts[].model`, `UsageRow.key` for `group_by=model`) carry the Bedrock id.
- **Errors**: request-level failures raise `NoemaError(code)` from the `ERROR_TABLE`; a method's comment lists the codes it may raise. Anything else that escapes is a bug and becomes 500 `internal_error`.
- **Cancellation** (anyio): every async method is cancellation-safe. A cancelled call re-raises the cancellation, leaves no partial state except where its comment says so, and never swallows it. "Shielded" steps run under `anyio.CancelScope(shield=True)` with the stated timeout.

## 1. Values (`core/ids.py`, `core/clock.py`, `core/errors.py`, `core/values.py`, `core/principal.py`, `core/text.py`)

```python
# ids.py
IdPrefix = Literal["turn", "dec", "att", "ses", "file", "der", "prj", "prn", "op", "cal", "lrun", "lab", "ldrop", "job", "req", "sbx",
                   "aud", "idem"]                        # "ldrop": dropped label items (LabelingDroppedItem.id ^ldrop_, C2a)
# api key ids are bare ULIDs (IMPL-008); usage-line ids are deterministic hashes (spec-11 §2); tenant ids are config keys
class IdGen(Protocol):
    def new(self, prefix: IdPrefix) -> str: ...            # sync; f"{prefix}_{ULID}" (26 Crockford chars, monotonic per process)

# clock.py
class Clock(Protocol):
    def now(self) -> datetime: ...                         # sync; tz-aware UTC
    def monotonic(self) -> float: ...                      # sync; seconds
    async def sleep(self, seconds: float) -> None: ...
@dataclass(frozen=True, slots=True)
class Deadline:
    at: float                                              # Clock.monotonic() seconds
    clock: Clock
    def remaining_ms(self) -> int: ...                     # sync; max(0, floor((at - now) * 1000))
    def expired(self) -> bool: ...                         # sync
    def child(self, cap_ms: int) -> "Deadline": ...        # sync; min(self.at, now + cap_ms/1000)
    def earliest(self, other: "Deadline | None") -> "Deadline": ...   # sync
    def scope(self) -> AbstractAsyncContextManager[anyio.CancelScope]: ...   # = deadline_scope(self.clock, self.at)
@asynccontextmanager
async def deadline_scope(clock: Clock, at: float) -> AsyncIterator[anyio.CancelScope]: ...
    # `async with deadline_scope(clock, at) as scope:` (or `deadline.scope()`); a watcher task awaits clock.sleep, so it fires
    # under FakeClock too. move_on semantics: the block exits normally with scope.cancelled_caught True and the caller raises
    # its own error (e.g. deadline_exceeded); body exceptions propagate unwrapped (no exception group). (Was a sync scope on
    # anyio's clock, which a virtual clock cannot drive; B4)

# errors.py: exactly IMPL-030 / spec-08 §2 and impl-decisions/contracts.md §B.1 (ErrorType, ErrorCode (36 members), ErrorSpec,
# ERROR_TABLE, NoemaError(code, message, *, param=None, details=None, retry_after_s=None)), plus:
class ErrorKind(StrEnum):
    RETRYABLE = "retryable"; THROTTLED = "throttled"; FATAL = "fatal"; INVALID_REQUEST = "invalid_request"; TENANT_ACCESS = "tenant_access"
TenantAccessCause = Literal["role_not_allowlisted", "external_id_unavailable", "external_id_invalid", "access_denied",
                            "sts_unavailable", "account_mismatch", "model_access_denied", "ambient_account_mismatch"]
@dataclass(frozen=True, slots=True)
class ClassifiedError:
    kind: ErrorKind
    message: str                                           # short, redacted, never model output
    provider_code: str | None = None                       # e.g. "ThrottlingException", "429"
    retry_after_s: float | None = None
    cause: TenantAccessCause | None = None                 # set iff kind == TENANT_ACCESS
class TenantAccessMisconfigured(NoemaError):              # code tenant_access_misconfigured; details = {"cause": cause}
    def __init__(self, cause: TenantAccessCause, message: str = "Tenant access is misconfigured.") -> None: ...
    cause: TenantAccessCause
class SecretMissing(NoemaError):                          # C2a: 500 internal_error (same envelope) when a secret or the requested
    def __init__(self, message: str = "A platform secret is missing.") -> None: ...   # version does not exist; an unreadable
                                                           # backend (throttled, timed out, denied) raises a plain NoemaError

# values.py
Effort = Literal["none", "low", "medium", "high", "xhigh", "max"]
DataClass = Literal["public", "internal", "confidential", "restricted"]
ContentPolicy = Literal["full", "redacted", "metadata_only"]
CallKind = Literal["decision_member", "verification_member", "generation", "labeling"]   # hierarchical round-1 and round-2
                                                           # calls are decision_member calls too (no extra kind; IMPL-027)
UsageLineName = Literal["decision", "generation", "labeling", "verification", "waste", "cache_savings"]
Modality = Literal["text", "image", "document"]            # Phase 1 (audio/video are 415)

@dataclass(frozen=True, slots=True)
class UsageRecord:                                         # disjoint categories; input includes cache reads and writes
    input_tokens: int; output_tokens: int; cached_input_tokens: int = 0; cache_write_input_tokens: int = 0
    reasoning_tokens: int = 0; tokens_estimated: bool = False
    def __add__(self, other: "UsageRecord") -> "UsageRecord": ...   # sync; estimated = a or b
@dataclass(frozen=True, slots=True)
class Cost:
    usd: Decimal; estimated: bool
@dataclass(frozen=True, slots=True)
class Degradation:                                         # mirrors openapi Degradation
    from_: str; to: str; reason: Literal["model_capability", "size_limit", "media_budget", "policy", "param_unsupported",
                                         "active_content_removed", "not_ready", "extraction_failed", "rejected"]
    file_id: str | None = None; part_index: int | None = None; attempt_id: str | None = None; model: str | None = None
    def to_wire(self) -> dict[str, JsonValue]: ...         # sync; the openapi object: from, to, reason, then the set optionals
                                                           # in field order; None fields omitted, never null

# content blocks (provider-neutral; the adapters map them, §4.2)
@dataclass(frozen=True, slots=True)
class TextBlock:     text: str
@dataclass(frozen=True, slots=True)
class ImageBlock:    format: Literal["png", "jpeg", "webp", "gif"]; data: bytes; width: int; height: int   # a re-encoded
                                                                                   # derivative; size from its manifest (TokenEstimator)
@dataclass(frozen=True, slots=True)
class DocumentBlock: format: Literal["pdf", "md", "txt"]; name: str; data: bytes; pages: int = 1   # name = "doc-N" (never a filename)
ContentBlock = TextBlock | ImageBlock | DocumentBlock
@dataclass(frozen=True, slots=True)
class Message:       role: Literal["user", "assistant"]; content: tuple[ContentBlock, ...]

# principal.py
Role = Literal["tenant_admin", "developer", "reviewer", "end_user", "auditor"]
Scope = Literal["turns", "decisions", "sessions", "files.read", "files.write", "usage.read", "admin.read"]
@dataclass(frozen=True, slots=True)
class Principal:
    tenant_id: str; project_id: str; principal_id: str; key_id: str | None
    roles: frozenset[Role]; scopes: frozenset[Scope]; request_id: str
def system_principal(tenant_id: str, *, purpose: str, request_id: str) -> Principal: ...   # sync; jobs (labeling, sweeper,
    # session.delete) acting inside one tenant: principal_id f"system:{purpose}", project_id "" (tenant-wide), key_id None,
    # roles {tenant_admin}, every scope. Admission skips key and project buckets for it (spec-11 §1.2)
@dataclass(frozen=True, slots=True)
class TenantRef:
    tenant_id: str; aws_account_id: str; home_region: str

# text.py: shared by decision and turns/prompt (IMPL-029a); exact code in spec-06 §5.2
def clean_text(s: str) -> str: ...                         # sync
RESERVED_TAGS: Final = ("untrusted_input", "app_question", "decision_context", "question", "answer")
def escape_reserved_tags(s: str) -> str: ...               # sync; one argument (the tags are RESERVED_TAGS); `<`, optional
    # whitespace/format chars, optional `/`, optional whitespace/format chars, a reserved tag -> "&lt;" + the rest (spec-06 §5.2)
def datamark(s: str) -> str: ...                           # sync; re.sub(r"\s+", "ˆ", s), each whitespace run -> exactly one U+02C6,
    # no surrounding spaces. standard: document/OCR-derived blocks (generation and decision); strict: also user and history
    # text (generation only) (IMPL-086, spec-09 §5)
```

## 2. Configuration (`core/protocols/config.py`)

```python
class ConfigSnapshot(Protocol):                            # immutable; one per activated config version
    version: int; digest: str                              # sha256 of the canonical config JSON
    def tenant(self, tenant_id: str) -> "TenantCfg": ...   # sync; raises NoemaError(not_found)
    def model(self, alias: str) -> "ModelCfg": ...         # sync
    def template(self, ref: str) -> "TemplateCfg": ...     # sync
    def decision_profile(self, profile_id: str) -> "DecisionProfileCfg": ...   # sync; active version only (IMPL-031)
    def generation_profile(self, profile_id: str) -> "GenerationProfileCfg": ...  # sync
    routing: "RoutingCfg"; labeling: "LabelingCfg"; platform: "PlatformCfg"      # spec-05 (defaults filled)
    prices: "PriceCatalog"                                 # spec-05 §2; empty catalog if platform.cost.prices_ref is absent
    def tenant_ref(self, tenant_id: str) -> TenantRef: ... # sync; raises NoemaError(not_found)
    def route_timeouts(self, model_alias: str, effort: "Effort | None") -> "StreamTimeouts": ...  # sync; per attempt: the member's
        # alias and effective effort (IMPL-015); ROUTE_TIMEOUTS_V1 (spec-05 §1.4), 04:L127-132 (IMPL-084)
class ConfigRegistry(Protocol):
    def current(self) -> ConfigSnapshot: ...               # sync
    def get(self, version: int) -> ConfigSnapshot: ...     # sync; pinned turns keep their snapshot; the registry keeps the
                                                           # current and the previous 2 versions; older -> NoemaError(not_found)
    def on_change(self, cb: Callable[[ConfigSnapshot], None]) -> None: ...   # sync registration; cb runs after `current` changed
```
The `*Cfg` types are frozen dataclasses generated from `config.schema.json` plus the spec-05 extensions. They are owned by foundation (`noema/config/models.py`). The loader fills defaults (`defaults.decision_profile`, `platform.*`, `labeling.acceptance`), then runs `check_config.semantic_errors` plus the P1 rules (spec-05 §3). A turn pins one snapshot at ingress. `PriceCatalog` is a frozen dataclass of the spec-05 §2 entries with `lookup(model_id: str, region: str, at: datetime) -> PriceEntry | None` (sync).

## 3. Credentials (`core/protocols/credentials.py`)

Exactly ADR-001 §4: `AwsCredentials`, `MantleAuth`, `TenantCredentials`, `CredentialBroker.get(tenant_id)` (**async**; raises `TenantAccessMisconfigured`) / `invalidate(tenant_id)` (sync), and `assert_creds_for(creds, tenant_id, aws_account_id)` (sync; raises `TenantAccessMisconfigured("account_mismatch")`). `SecretStr` is in `core/secret.py`: `SecretStr(value, *, register=False)`; `register=True` adds a value of ≥ 8 characters to the redaction registry while a registered holder lives (`registered_secrets()` -> `(version, values)` for the redactor). Loaders of platform and tenant secrets (`SecretsProvider`, brokers, DSNs) register and hold the object; client-presented credentials are never registered (spec-09 §4).

```python
class SecretsProvider(Protocol):
    async def get(self, ref: str, *, version: str | None = None) -> bytes: ...   # "aws-sm://..." or a "noema/<name>" platform
        # secret; version None = current (the api-key pepper is read by version, IMPL-008); cached in memory for 300 s;
        # raises SecretMissing (a NoemaError(internal_error)) when the secret or the version does not exist, and a plain
        # NoemaError(internal_error) when the backend cannot be read (C2a)
```

## 4. Adapters (`core/protocols/adapters.py`)

### 4.1 Types and Protocols

```python
@dataclass(frozen=True, slots=True)
class ModelRequest:
    model_alias: str
    system: tuple[str, ...]                                # system text blocks, in order
    messages: tuple[Message, ...]
    max_tokens: int
    call_kind: CallKind
    retry: Literal["none", "default"]                      # decision/verification/labeling members: "none" (IMPL-050)
    effort: Effort | None = None
    temperature: float | None = None                       # only if the model's controls.temperature is true; else the caller drops it
    logprobs: bool = False
    top_logprobs: int | None = None
    stop: tuple[str, ...] = ()
    snapshot: ConfigSnapshot | None = None                 # the caller's pinned snapshot; the adapter resolves models.<model_alias>
                                                           # from it (no re-resolution mid-turn); None = the registry's current
@dataclass(frozen=True, slots=True)
class TokenLogprob:  token: str; logprob: float; top: tuple[tuple[str, float], ...]
StopReason = Literal["end_turn", "max_tokens", "stop_sequence", "content_filter", "other"]
@dataclass(frozen=True, slots=True)
class ModelResponse:
    text: str                                              # answer text blocks concatenated; reasoning excluded
    logprobs: tuple[TokenLogprob, ...] | None
    refused: bool                                          # provider-signalled refusal only (IMPL-053)
    stop_reason: StopReason
    usage: UsageRecord; latency_ms: int; region: str; model_revision: str | None = None
# streaming events (Adapter.stream)
@dataclass(frozen=True, slots=True)
class MessageStart:   at: float
@dataclass(frozen=True, slots=True)
class ReasoningDelta: chars: int                           # counted, never forwarded
@dataclass(frozen=True, slots=True)
class TextDelta:      text: str
@dataclass(frozen=True, slots=True)
class UsageEvent:     usage: UsageRecord
@dataclass(frozen=True, slots=True)
class Stop:           reason: StopReason
@dataclass(frozen=True, slots=True)
class ErrorEvent:     error: ClassifiedError
StreamEvent = MessageStart | ReasoningDelta | TextDelta | UsageEvent | Stop | ErrorEvent
@dataclass(frozen=True, slots=True)
class StreamTimeouts: connect_s: float; first_event_s: float; first_answer_s: float; idle_s: float
@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    modalities: frozenset[Modality]; temperature: bool; effort: frozenset[Effort]; logprobs: bool; top_logprobs_max: int | None
    structured_output: bool; reasoning: bool; max_output_tokens_cap: int | None
    context_window: int                                    # models.<alias>.context_window, else DEFAULT_CONTEXT_WINDOW (IMPL-084)
    chars_per_token: float                                 # 4.0 for every Phase 1 model (code constant, IMPL-029c); not config
class Adapter(Protocol):
    def capabilities(self, alias: str) -> ModelCapabilities: ...                       # sync; current snapshot's models.<alias>
    async def complete(self, req: ModelRequest, *, tenant: TenantRef, deadline: Deadline) -> ModelResponse: ...
        # non-streaming; raises AdapterError(ClassifiedError). Calls broker.get + assert_creds_for before any byte (IMPL-076).
        # The deadline bounds the whole call (connect included); expiry -> AdapterError(retryable, provider_code="timeout:deadline").
        # Cancellation closes the HTTP request; the provider may still bill it (spec-11 §2 estimate).
    def stream(self, req: ModelRequest, *, tenant: TenantRef, timeouts: StreamTimeouts,
               deadline: Deadline) -> AsyncContextManager[AsyncIterator[StreamEvent]]: ...
        # __aenter__ fetches and asserts credentials and opens the request; a failure there raises AdapterError. After it returns,
        # every failure (provider exception event, timeouts, malformed frames) is yielded as one ErrorEvent, which is the last event.
        # A successful stream ends with Stop, then UsageEvent if the provider sent usage (Converse sends metadata after
        # messageStop). Timeouts: connect_s (before __aenter__ returns), first_event_s, first_answer_s (first TextDelta), idle_s
        # (between events), each capped by `deadline`; expiry -> ErrorEvent(retryable, provider_code="timeout:<name>").
        # idle_s is provider silence: measured per provider wait (time the consumer holds an event does not count); after
        # Stop only idle_s and the deadline apply to the wait for the usage (C2a, adapters request 8).
        # Exiting the context = cancel (aclose the provider stream; no draining). req.retry == "default": one in-adapter retry on a
        # connect/DNS/TLS error before any response byte (same attempt, RetryBudget-limited); "none": never retried (IMPL-016, IMPL-050)
    def classify_error(self, exc: BaseException) -> ClassifiedError: ...               # sync
class AdapterError(Exception):
    def __init__(self, error: ClassifiedError) -> None: ...
    error: ClassifiedError
class AdapterRegistry(Protocol):
    def for_model(self, alias: str) -> Adapter: ...                                    # sync
```

### 4.2 Wire mapping (frozen; golden fixtures in `tests/golden/decision_requests/` and `tests/golden/generation_requests/`)

| `ModelRequest` field | `bedrock-converse` (Converse / ConverseStream) | `bedrock-mantle-chat` (`POST {base}/chat/completions`) |
|---|---|---|
| `system` | `system=[{"text": s} for s in system]` | first message `{"role": "system", "content": "".join(system)}` |
| `messages` | `[{"role", "content": [...]}]`; TextBlock → `{"text"}`; ImageBlock → `{"image": {"format", "source": {"bytes"}}}`; DocumentBlock → `{"document": {"format", "name", "source": {"bytes"}}}`. A document needs a text block in the same message (the caller guarantees it) | text only: `{"role", "content": "".join(t.text for TextBlock)}`; any image or document block → `AdapterError(invalid_request)` |
| `max_tokens` | `inferenceConfig.maxTokens` | `max_tokens` |
| `effort` | `outputConfig={"effort": effort}` when set (botocore ≥ 1.43) | not sent (DeepSeek has no effort) |
| `temperature` | `inferenceConfig.temperature` only when not None | `temperature` only when not None |
| `logprobs`, `top_logprobs` | rejected (`invalid_request`) | `"logprobs": true, "top_logprobs": n` |
| `stop` | `inferenceConfig.stopSequences` | `stop` |
| streaming | ConverseStream: `messageStart`, `contentBlockDelta.delta.text` / `.reasoningContent`, `messageStop.stopReason`, `metadata.usage` | `"stream": true, "stream_options": {"include_usage": true}`; read until `[DONE]` |
| non-streaming | Converse | `"stream": false` |
| auth | SigV4 with the tenant's STS credentials | `Authorization: Bearer` (short-term token) or SigV4, per `platform.credentials.mantle_auth` |
| refusal | `stopReason ∈ {content_filtered, guardrail_intervened}` → `refused=True`, `stop_reason=content_filter` | `finish_reason == "content_filter"` or a non-empty `message.refusal` → `refused=True` |
| usage | `inputTokens + cacheReadInputTokens + cacheWriteInputTokens` → `input_tokens` (03:L314), `cacheReadInputTokens` → `cached_input_tokens`, `cacheWriteInputTokens` → `cache_write_input_tokens`, `outputTokens` → output (includes reasoning) | `prompt_tokens`, `completion_tokens`, `prompt_tokens_details.cached_tokens` if present |
| region / endpoint | the tenant's `home_region`; `us.` inference profiles | `https://bedrock-mantle.{home_region}.api.aws/v1` |

Never sent: `temperature` to models with `controls.temperature: false`, `textFormat` (IMPL-054), `additionalModelRequestFields`, `cachePoint` (IMPL-017). `ModelRequest` has no field for a text format or extra model fields; a Phase 2 structured-output path needs one plus new golden requests.

### 4.3 Error classification and mapping (frozen; IMPL-076)

| Provider signal | `ErrorKind` |
|---|---|
| `ThrottlingException`, HTTP 429, `ServiceQuotaExceededException` | `throttled` (`retry_after_s` from the header if present) |
| connect / DNS / TLS errors, HTTP 5xx, `ServiceUnavailableException`, `InternalServerException`, `ModelTimeoutException`, `ModelNotReadyException`, read timeout, a stream exception event | `retryable` |
| `AccessDeniedException`, `UnrecognizedClientException`, `ExpiredTokenException` (the adapter calls `broker.invalidate`), model not enabled (on Converse: `ResourceNotFoundException`, HTTP 404), `TenantAccessMisconfigured` from the broker | `tenant_access` (`cause`: `model_access_denied` / `access_denied` / the broker's cause); a Mantle 404 stays `fatal` |
| `ValidationException`, botocore `ParamValidationError`, HTTP 400 from Mantle | `invalid_request` |
| anything else | `fatal` |

The `ErrorKind` → `MemberVote.error.code` → request `ErrorCode` table is IMPL-076.

*Wave E2 note (IMPL-050 and IMPL-076 amendments, R7).*
- **Lean Converse transport.** Converse, ConverseStream and Mantle `complete()` run without aiobotocore or httpx, with the same wire (§4.2) and the same errors (§4.3).
  - Requests are built byte for byte as aiobotocore builds them and signed with `botocore.auth.SigV4Auth` (signing name `bedrock`). They go over a pooled aiohttp client, and the request line carries the path exactly as signed (`:` → `%3A`).
  - Responses and event streams are parsed with botocore's parsers.
  - Redirects are never followed. A body-read failure after the response headers is not the in-adapter connect retry.
- **Malformed 2xx.** A 2xx Converse body that is not a valid Converse response is `retryable` (`malformed:response`), not "anything else → `fatal`". A ConverseStream payload botocore cannot parse, or a non-string `text`, is `malformed:chunk`.

## 5. Decision (`core/protocols/decision.py`)

```python
Purpose = Literal["decision", "router", "verification"]
Origin = Literal["native", "facade", "turn", "verification"]
OutputFormat = Literal["native", "jev", "openai"]

@dataclass(frozen=True, slots=True)
class ProfileRef:  id: str; version: int

@dataclass(frozen=True, slots=True)
class DecisionContext:
    principal: Principal
    tenant: TenantRef
    snapshot: ConfigSnapshot
    deadline: Deadline                                     # end-to-end (IMPL-023)
    escalate_until: Deadline | None                        # None disables escalation (IMPL-025, IMPL-070)
    reservation: "Reservation"                             # consume("decision_member" | "verification_member", model) per call
    purpose: Purpose
    store: bool                                            # persist; L2 allowed
    data_class: DataClass                                  # session -> project default -> "internal" (IMPL-004)
    content_policy: ContentPolicy                          # whether the decision row stores `input` (IMPL-071)
    trace_id: str
    turn_id: str | None = None
    session_id: str | None = None
    decision_id: str | None = None                         # createDecision's pre-generated dec_ id (before idempotency begin(),
                                                           # spec-04 §1 step 7), used as the run's id; None = the engine mints one
    escalate_slack_ms: int | None = None                   # C2b, turns: with escalate_until set, the engine uses
                                                           # min(t0 + deadline_ms + slack_ms, deadline) from its own t0 (= W)

@dataclass(frozen=True, slots=True)
class DecisionRequest:
    profile: ProfileRef
    questions: "CompiledQuestionSet"
    input: "DecisionInput"                                 # spec-06 §4.1
    deadline_ms: int                                       # effective stage-0 deadline
    min_quorum: int                                        # effective stage-0 quorum
    escalation: Literal["profile_default", "off"]
    include: frozenset[Literal["members", "member_distributions", "stages"]]
    origin: Origin
    output_format: OutputFormat                            # persisted; jev/openai reject a question that needs the hierarchical
                                                           # method (422 unsupported_parameter, IMPL-036); native answers it (IMPL-027)
    safety_identifier: str | None = None

@dataclass(frozen=True, slots=True)
class AnswerSummary:                                       # what bindings and conditioning read (no Pydantic)
    name: str
    type: Literal["predicate", "noul", "choice", "score", "refusal", "unavailable"]
    value: str | bool | float | None                       # choice value, P(true), level label; None for refusal/unavailable
    top: tuple[tuple[str | bool, float], ...]              # (value, emitted probability), best first, ≤ 2 entries
    probability: float | None                              # p_max (choice/score) or P(true)
    margin: float | None
    band: Literal["high", "medium", "low"] | None
    calibrated: bool
    final: bool
    reason: str | None = None                              # unavailable reason

@dataclass(frozen=True, slots=True)
class DecisionSnapshot:
    decision_id: str
    status: Literal["quorum", "completed", "partial"]
    final: bool
    final_names: frozenset[str]
    answers: Mapping[str, AnswerSummary]
    result: Mapping[str, JsonValue]                        # wire-ready native DecisionResult dict (orjson); validated only under
                                                           # NOEMA_CONTRACT_VALIDATE=1 (IMPL-070)
    row: Mapping[str, object] | None = None                # final snapshot only, iff ctx.store and ctx.turn_id is not None: the
        # noema.decision row (spec-03 column names) that the orchestrator inserts in SessionRepo.finalize_turn. The engine
        # inserts the row itself only when ctx.turn_id is None (/v1/decisions, facade), inside finalize()

class DecisionRun(Protocol):                               # never raises for member failures once __aenter__ returned
    @property
    def decision_id(self) -> str: ...
    async def commit_critical(self) -> DecisionSnapshot: ...      # idempotent; cancelling the waiter does not cancel the run
    def updates(self) -> AsyncIterator[DecisionSnapshot]: ...     # yields ≤ 1 (the final), then ends
    async def finalize(self) -> DecisionSnapshot: ...             # final; idempotent; shielded (2 s): usage lines and L2 put always;
        # the row insert (if ctx.store) and the labeling offer only when ctx.turn_id is None (for turns the orchestrator inserts
        # `row` and offers after its finalize commits, spec-04 §10); a DB failure -> outbox (IMPL-022), never raised
    async def cancel(self) -> None: ...                           # idempotent; pending members -> late; the result becomes final
    def row_for(self, content_policy: ContentPolicy) -> Mapping[str, object] | None: ...   # sync; the final `row` with `input`
        # stored under a stricter final turn policy (spec-04 §10): full = input, redacted = Telemetry.redact(text),
        # metadata_only = NULL; a laxer policy is clamped to ctx.content_policy; None before the final snapshot or without a row

class DecisionEngine(Protocol):
    def run(self, ctx: DecisionContext, req: DecisionRequest) -> AsyncContextManager[DecisionRun]: ...
        # __aenter__ may raise NoemaError (request problems, deadline_exceeded before dispatch, tenant_access_misconfigured);
        # __aexit__ cancels in-flight calls and awaits terminal state (IMPL-070)
    async def decide(self, ctx: DecisionContext, req: DecisionRequest) -> DecisionSnapshot: ...  # run + finalize

class QuestionCompiler(Protocol):
    def compile(self, profile: "DecisionProfileCfg", app_questions: Sequence[Mapping[str, JsonValue]], *,
                origin: Origin, output_format: OutputFormat, blocking: Sequence[str] = (),
                select: Sequence[str] | None = None,       # platform question names to keep, in config order (verification)
                platform_from: "DecisionProfileCfg | None" = None,   # turns: questions come from router_profile (IMPL-069)
                ) -> "CompiledQuestionSet": ...            # sync; raises NoemaError

class DecisionCache(Protocol):
    async def get(self, key: str, *, timeout_s: float = 0.02) -> bytes | None: ...      # fail-open: timeout or error -> None
    async def put(self, key: str, artifact: bytes, ttl_s: int, *, timeout_s: float = 0.02) -> None: ...  # errors swallowed + metric
    async def purge_tenant(self, tenant_id: str) -> int: ...      # SCAN RedisKeys.l2_pattern(tenant) + UNLINK; returns the count
```

`CompiledQuestion` (with `method` and `groups` for the hierarchical method), `GroupSpec`, `CompiledQuestionSet`, `OptionSpec`, `DecisionInput`, `Msg` and `VerificationInput` are defined in spec-06 §3.2 and §4.1. They are part of this frozen module. The hierarchical answer reaches callers only through the wire `DecisionResult` (`choice_method`, `groups[]`, `unresolved_mass`); `AnswerSummary` is unchanged.

## 6. Media (`core/protocols/media.py`)

```python
ViewKind = Literal["native", "thumbnail", "ocr_text", "derived_text", "metadata_only", "skipped"]
@dataclass(frozen=True, slots=True)
class ContentPart:                                         # resolved input part (text, json, inline image ref, file ref)
    index: int; kind: Literal["text", "json", "image", "file"]; label: str | None   # label "image-1" / "doc-1" for media
    text: str | None = None; json: JsonValue = None
    file: "FileRef | None" = None                          # set iff kind in {image, file}; inline images are ingested first (IMPL-074)
    page_texts: tuple[str, ...] | None = None              # PDF/Markdown files: text of page i+1 (its Markdown segment, else its
        # OCR segment, else ""; a Markdown upload is one page), pre-read by preflight (read_derivative) for the planner; None = not read
@dataclass(frozen=True, slots=True)
class DecisionViewBudget:
    max_text_tokens: int; max_images: int; max_documents: int; image_long_edge: int
VIEW_BUDGET_V1: Final = DecisionViewBudget(max_text_tokens=6000, max_images=20, max_documents=5, image_long_edge=1568)  # IMPL-075
# the resolver also keeps native images, documents and page images within CONVERSE_CAPS_V1.max_payload_bytes - text_reserve_bytes
# of base64, cut like the count caps (spec-07 §5; part of the versioned view rules, no field)
@dataclass(frozen=True, slots=True)
class ViewPart:                                            # one input part as one member sees it
    part_index: int; label: str | None; kind: ViewKind
    blocks: tuple[ContentBlock, ...]                       # content only, unescaped; decision renders labels and escaping (IMPL-050)
    truncated: bool = False; note: str | None = None       # e.g. "pages 1-12 of 600"
@dataclass(frozen=True, slots=True)
class InputView:
    kind: ViewKind                                         # least adequate modality (spec-06 §4.4)
    by_modality: Mapping[Modality, ViewKind]
    parts: tuple[ViewPart, ...]
    degradations: tuple[Degradation, ...]
class InputViewResolver(Protocol):
    async def resolve(self, member: "DecisionMemberCfg", parts: Sequence[ContentPart], *, policy: Literal[
                      "derive_for_text_members", "skip_incapable_members"], budget: DecisionViewBudget,
                      model: "ModelCfg", principal: Principal) -> InputView: ...
        # model = snapshot.model(member.model) (modalities); principal = the caller (derivative reads go through FileService).
        # Never raises for media problems: an unreadable or not-ready derivative -> metadata_only + degradation not_ready.
        # The view signature is computed only in decision/signatures.py (spec-06 §3.5)

DerivativeKind = Literal["raster", "raster_1568", "thumbnail", "ocr_text", "pdf_sanitized", "markdown", "page_images", "text"]
@dataclass(frozen=True, slots=True)
class DerivativeRef:                                       # raster_1568 is internal: never listed in FileObject, never downloadable
    kind: DerivativeKind; id: str; status: Literal["pending", "ready", "failed"]; sha256: str | None
    bytes: int | None = None; width: int | None = None; height: int | None = None; pages: int | None = None
    text_layer_pages: tuple[int, ...] | None = None        # page_images only: 1-based pages whose text layer has a non-space
                                                           # character (pdf.render facts, ADR-002 §5), ascending; the rest are scanned
    active_content: tuple[str, ...] | None = None          # C2b; pdf_sanitized only: the removed spec-09 §6.3 facts, sorted
                                                           # (derivative.facts); () = none; non-empty -> active_content_removed
FileStatus = Literal["quarantined", "processing", "ready", "partial", "rejected", "failed", "deleted"]
@dataclass(frozen=True, slots=True)
class FileRef:                                             # everything the routes need for FileObject and authz (spec-09 §3)
    file_id: str; sha256: str; declared_type: str; media_type: str | None; status: FileStatus; bytes: int
    origin: Literal["upload", "inline"]; principal_id: str; session_id: str | None; filename: str | None   # display only
    derivatives: tuple[DerivativeRef, ...]; pages: int | None = None
    rejection: Mapping[str, JsonValue] | None = None; failure: Mapping[str, JsonValue] | None = None
class FileService(Protocol):
    async def upload(self, principal: Principal, stream: AsyncIterator[bytes], *, declared_type: str, filename: str,
                     session_id: str | None) -> FileRef: ...
        # spec-07 §3.1; raises file_too_large (413), unsupported_media_type (415), session_not_found (404)
    async def ingest_inline(self, principal: Principal, data_b64: str, media_type: str, *, deadline: Deadline,
                            session_id: str | None = None) -> FileRef: ...
        # IMPL-074: arithmetic size check -> quarantine -> high-priority sandbox job; waits for `raster` only (spec-07 §3.2);
        # raises image_too_large (413), file_rejected (409), file_not_ready (409, the wait expired)
    async def get(self, principal: Principal, file_id: str) -> FileRef: ...             # 404 not_found for foreign ids
    async def delete(self, principal: Principal, file_id: str) -> None: ...             # status deleted; enqueues the media-owned
        # file.purge job (which calls purge); 404 not_found
    async def purge(self, tenant_id: str, file_id: str) -> None: ...  # quarantine + derivative S3 objects, then the file row
        # (derivatives cascade), under tenant_uow; idempotent; S3 failures raise (the job retries). The only path that deletes
        # file objects: the file.purge job, session.delete (identity) and the retention sweeper (ops-ledger) call it
    def watch(self, principal: Principal, file_id: str) -> AsyncIterator[FileRef]: ...  # first = current; ends at a terminal status
        # (ready, partial, rejected, failed, deleted); woken by RedisKeys.file_events(file_id), 2 s poll fallback
    async def open_derivative(self, principal: Principal, file_id: str, kind: DerivativeKind, *, page: int | None = None
                              ) -> tuple[str, AsyncIterator[bytes]]: ...                 # (content type, bytes); 404 not_found
        # (unknown/foreign file, no such kind or page), 409 file_not_ready (pending), 409 file_rejected (spec-07 §6)
    async def read_derivative(self, principal: Principal, file_id: str, kind: DerivativeKind, *, page: int | None = None
                              ) -> bytes: ...                                            # whole object; per-pod LRU (256 MiB) keyed
        # by (tenant_id, sha256); used by InputViewResolver and MediaNegotiator for model blocks

@dataclass(frozen=True, slots=True)
class MediaCaps:                                           # Converse limits per request (02 §8.4); algorithm spec-07 §4.5
    max_images: int = 20; max_image_bytes: int = 3_750_000
    max_documents: int = 5; max_document_bytes: int = 4_500_000
    max_payload_bytes: int = 20_000_000; text_reserve_bytes: int = 2_000_000   # text, JSON framing, system prompt
    max_text_tokens: int = 0                               # set per turn by preflight (spec-04 §9): the derived-text allowance
CONVERSE_CAPS_V1: Final = MediaCaps()
@dataclass(frozen=True, slots=True)
class PlannedPart:
    part_index: int; label: str; origin: Literal["current", "previous"]; kind: Literal["image", "pdf", "markdown"]
    native: bool                                           # image: an ImageBlock slot; pdf: a DocumentBlock slot
    text_pages: int                                        # pdf/markdown: pages 1..text_pages of the text kept (0 = none)
    text_cut: str | None                                   # "pages 1-k of n" or "page 1 of n, partial"; None if whole
    page_images: tuple[int, ...]                           # pdf: pages that may be sent as images, ascending (1-based)
    file_id: str = ""                                      # C2a: the part's file, so a previous-turn part whose part_index repeats
                                                           # a current one is found ("" = not set: match by index and label)
    text_chars: int | None = None                          # C2a: kept length of page 1 for "page 1 of n, partial", else None
@dataclass(frozen=True, slots=True)
class MediaPlan:                                           # model-independent; computed once per turn in preflight
    parts: tuple[PlannedPart, ...]; degradations: tuple[Degradation, ...]   # media_budget only
class MediaNegotiator(Protocol):
    async def negotiate(self, alias: str, model: "ModelCfg", parts: Sequence[ContentPart], plan: MediaPlan, *,
                        principal: Principal) -> tuple[tuple[ContentBlock, ...], tuple[Degradation, ...]]: ...
        # once per attempt (fallbacks included), spec-07 §4; reads bytes with FileService.read_derivative; never raises for media.
        # Blocks for the media parts only (the prompt builder renders text/json parts itself, spec-09 §5). Degradations carry
        # model (the Bedrock id) and attempt_id=None: the generation engine stamps the attempt id (dataclasses.replace)
class MediaBudgetPlanner(Protocol):
    def plan(self, turn_parts: Sequence[ContentPart], history_parts: Sequence[ContentPart], caps: MediaCaps) -> MediaPlan: ...  # sync
        # pure (spec-07 §4.5); question terms = the text parts of turn_parts; page selection and text budgeting read
        # ContentPart.page_texts and the page_images text_layer_pages; a text item without page_texts is bounded by
        # markdown.bytes (+ ocr_text.bytes for a PDF) + pages (spec-07 §4.5 step 2)
# SandboxRunner, SandboxJob, SandboxLimits, SandboxOutput, ExtractResult: exactly ADR-002 §7 (core/protocols/sandbox.py)
```

## 7. Storage, jobs, idempotency, outbox, events (`core/protocols/storage.py`, `events.py`)

```python
Row = Mapping[str, object]   # keys = spec-03 column names; values: str, int, bool, float, Decimal, datetime (UTC), JsonValue
                             # for jsonb, bytes for bytea, list for arrays. tenant_id/project_id are filled from the uow scope.
class UnitOfWork(Protocol):  # one transaction: set_config('app.tenant_id', $1, true) and set_config('app.project_id', $2, true)
                             # (bind parameters, never string-built SET LOCAL); commit on clean exit, rollback on exception
    sessions: "SessionRepo"; turns: "TurnRepo"; attempts: "AttemptRepo"; decisions: "DecisionRepo"; files: "FileRepo"
    derivatives: "DerivativeRepo"; usage: "UsageRepo"; labels: "LabelRepo"; calibrators: "CalibratorRepo"
    idempotency: "IdempotencyRepo"; audit: "AuditRepo"; projects: "ProjectRepo"; jobs: "JobRepo"
class Database(Protocol):
    def uow(self, principal: Principal) -> AsyncContextManager[UnitOfWork]: ...
        # app.project_id = principal.project_id, or '' (tenant-wide) iff roles ∩ {tenant_admin, auditor} (spec-03 §3)
    def tenant_uow(self, tenant_id: str, *, project_id: str | None = None) -> AsyncContextManager[UnitOfWork]: ...
        # jobs, sweepers, drainer follow-ups and labeling: no principal; project_id None = tenant-wide
    def system_uow(self) -> AsyncContextManager["SystemCalls"]: ...   # may only call SECURITY DEFINER functions (spec-03 §4)
    # all three raise NoemaError(internal_error, retryable) when Postgres is unreachable (pool timeout 2 s)
@dataclass(frozen=True, slots=True)
class Page(Generic[T]): data: tuple[T, ...]; has_more: bool; next_cursor: str | None
# Pagination everywhere: keyset on the stated order; `after` is the opaque next_cursor of the previous page (base64url of the
# last row's sort key); limit 1..200, default 50 (openapi `Limit`); a malformed cursor -> NoemaError(schema_violation, param="after").

class JobQueue(Protocol):                                  # Postgres SKIP LOCKED + LISTEN/NOTIFY wakeup
    async def enqueue(self, kind: str, tenant_id: str, payload: Mapping[str, JsonValue], *, priority: int = 100,
                      run_after: datetime | None = None, dedupe_key: str | None = None, max_attempts: int = 5) -> str: ...
        # INSERT under tenant_uow(tenant_id) + pg_notify('jobs', kind); a live duplicate (same tenant, kind, dedupe_key,
        # queued or running) -> returns the existing job id, inserts nothing. Lower priority value = claimed first (spec-11 §4);
        # max_attempts >= 1 (media.process uses 2, spec-07 §3.1)
    def worker(self, kinds: Sequence[str], handler: Callable[["Job"], Awaitable[None]], *, concurrency: int) -> "Worker": ...
@dataclass(frozen=True, slots=True)
class Job: id: str; tenant_id: str; kind: str; payload: Mapping[str, JsonValue]; attempt: int   # attempt: 1 on the first run
class PermanentJobError(Exception): ...                    # handler raises it -> state dead at once, no retry
type FinishedJobState = Literal["done", "queued", "dead"]  # SystemCalls.finish_job (C2a)
type RetentionCandidate = tuple[str, str, str, datetime]   # SystemCalls.retention_page (C2a); kind = the table name
class Worker(Protocol):
    async def run(self) -> None: ...                       # claims and runs jobs until cancelled; the lease is extended every 20 s;
        # handler return -> done; any other exception -> retry with backoff, dead after max_attempts (spec-11 §4); a lost lease
        # (jobs.extend returns false) cancels the handler. Cancellation of run(): running handlers get 10 s, then their jobs
        # are left to the reaper

@dataclass(frozen=True, slots=True)
class StoredResponse: status: int; headers: tuple[tuple[str, str], ...]; body: bytes | None; sse_turn_id: str | None
@dataclass(frozen=True, slots=True)
class IdemToken: scope_hash: str; key: str; record_id: str
@dataclass(frozen=True, slots=True)
class New: token: IdemToken
@dataclass(frozen=True, slots=True)
class InFlight: token: IdemToken; resource_id: str | None   # the turn/decision id of the running original (SSE attach)
@dataclass(frozen=True, slots=True)
class Replay: response: StoredResponse
@dataclass(frozen=True, slots=True)
class Conflict: pass
class IdempotencyStore(Protocol):                          # IMPL-077; state machine in spec-11 §5
    async def begin(self, principal: Principal, method: str, route: str, key: str, fingerprint: str, *,
                    resource_id: str | None = None) -> New | InFlight | Replay | Conflict: ...
        # route = the openapi path template; resource_id = the id pre-generated for this request (turn_/dec_)
    async def await_result(self, token: IdemToken, *, deadline: Deadline) -> StoredResponse: ...
        # raises NoemaError(deadline_exceeded) at the deadline (504); if the record turns not_stored or its lease lapses while
        # waiting -> NoemaError(internal_error) (500, retryable: the client's retry then takes over as New)
    async def heartbeat(self, token: IdemToken) -> bool: ...   # renews the 30 s lease (call every 10 s); False = lease lost
    async def complete(self, token: IdemToken, resp: StoredResponse | None) -> None: ...   # None = not stored (no model call);
        # shielded (2 s); idempotent. A body > 1 MiB goes to S3 <tenant_id>/idempotency/<record_id> (tenant KMS key) before the
        # row update; Replay/await_result read it back transparently; size never yields not_stored (IMPL-085, spec-11 §5)

OutboxKind = Literal["usage_line", "audit_event", "labeling_offer", "decision_row", "turn_finalize"]
@dataclass(frozen=True, slots=True)
class OutboxRecord:
    kind: OutboxKind; id: str; tenant_id: str              # id = the target primary key (usage-line, audit, decision or turn id;
                                                           # labeling_offer: the decision id)
    payload: Mapping[str, JsonValue]                       # spec-11 §3 per kind; timestamps RFC 3339 UTC, Decimal as string
    session_id: str | None = None                          # turn_finalize only (DEL session_pending after apply)
class Outbox(Protocol):                                    # write-behind (IMPL-022)
    def put(self, record: OutboxRecord) -> None: ...       # sync; never raises; batch writer -> Postgres, else Redis Stream
    async def flush(self, timeout_s: float) -> None: ...   # graceful shutdown; whatever is left after the timeout is logged as lost

@dataclass(frozen=True, slots=True)
class SseFrame:
    id: int; type: str                                     # id: 1, 2, … per turn; type = event["type"]
    payload: bytes                                         # orjson(event)
    wire: bytes                                            # b"id: {id}\nevent: {type}\ndata: {payload}\n\n" (pre-serialized)
class TurnEventLog(Protocol):                              # IMPL-021
    def append(self, turn_id: str, event: Mapping[str, JsonValue]) -> int: ...   # sync; event has "type"; assigns the next id
        # (first = 1); serializes once; mirrors to RedisKeys.turn_events (XADD MAXLEN ~20000) asynchronously, best effort.
        # Raises RuntimeError after seal (a bug)
    def subscribe(self, turn_id: str, *, after: int = 0) -> AsyncIterator[SseFrame]: ...   # local log: frames with id > after,
        # then live frames; ends after the turn.completed frame; empty if the turn is not local
    def is_local(self, turn_id: str) -> bool: ...          # sync
    def seal(self, turn_id: str, ttl_s: int) -> None: ...  # sync; after turn.completed; local log kept ttl_s, mirror EXPIRE ttl_s
    async def attach_remote(self, turn_id: str, *, after: int = 0) -> AsyncIterator[SseFrame] | None: ...
        # Redis mirror (XREAD BLOCK); None if the mirror doesn't exist (expired or Redis down)
    async def idle_seconds(self, turn_id: str) -> float | None: ...   # abandon watchdog: seconds since the last frame was delivered
        # to any subscriber (local, or remote via RedisKeys.turn_attach); None while a subscriber is attached
class SseWriter(Protocol):
    async def run(self, frames: AsyncIterator[SseFrame], send: Callable[[bytes], Awaitable[None]], *,
                  queue_events: int = 256, queue_bytes: int = 1 << 20, too_slow_s: float = 30,
                  ping_s: float = 15) -> Literal["completed", "disconnected", "client_too_slow"]: ...
        # 04:L281 backpressure: frames are read into a bounded queue; at the high-water mark consecutive generation.delta frames
        # of one attempt are merged (text concatenated; the last frame's id and seq); still full after too_slow_s -> one
        # best-effort error{client_too_slow, retryable: false} frame (ERROR_TABLE, spec-08 §2) (no id line, 1 s timeout) and "client_too_slow", after which
        # the caller cancels the turn (client_abort). `: ping\n\n` after ping_s without a write. send() raising -> "disconnected"
```

### 7.1 Repositories (`core/protocols/storage.py`; implemented once in `db/repos/`, faked in `tests/fakes/db.py`)

Every method runs in the enclosing `UnitOfWork` transaction, under its RLS scope. `get` returns `None` for a missing, deleted or invisible row (the caller maps it to its 404 code). Rows use spec-03 column names.

```python
@dataclass(frozen=True, slots=True)
class SessionLease: session_id: str; turn_id: str; last_seq: int; sticky_spec: Mapping[str, JsonValue] | None
@dataclass(frozen=True, slots=True)
class Busy: pass                                           # another live lease holds the session
class LeaseLost(Exception): ...                            # finalize_turn found the lease taken over
class SessionRepo(Protocol):
    async def create(self, row: Row) -> Row: ...
    async def get(self, session_id: str) -> Row | None: ...
    async def update(self, session_id: str, **changes: object) -> Row: ...   # metadata, data_class, retention, expires_at;
                                                                             # NoemaError(session_not_found)
    async def list(self, *, owner_principal_id: str | None, after: str | None, limit: int) -> Page[Row]: ...  # id DESC (newest first)
    async def mark_deleted(self, session_id: str) -> None: ...                # deleted_at = now(); the session.delete job deletes
    async def delete(self, session_id: str) -> None: ...                     # hard delete, deleted or not (session.delete job,
        # retention); its turns first (TurnRepo.delete_for_session, else the FK fails); absent: no-op
    async def acquire(self, session_id: str, turn_id: str, *, expected_seq: int | None, lease_s: int) -> SessionLease | Busy: ...
        # the spec-04 §2 UPDATE. 0 rows -> NoemaError(session_not_found) (missing/deleted/foreign) | NoemaError(session_conflict)
        # (expected_seq != last_seq) | Busy. The caller checks RedisKeys.session_pending(sid) before calling: a pending outbox
        # finalize is Busy even when the lease has expired
    async def renew(self, lease: SessionLease, lease_s: int) -> bool: ...    # False = the lease was taken over
    async def finalize_turn(self, lease: SessionLease, turn: Row, attempts: Sequence[Row], decisions: Sequence[Row],
                            turn_files: Sequence[Row], sticky_spec: Mapping[str, JsonValue] | None) -> int: ...
        # INSERT turn (seq = lease.last_seq + 1), attempts, decisions, turn_files; UPDATE session SET last_seq, sticky_spec,
        # active_turn_id = NULL, lease_expires_at = NULL WHERE id AND active_turn_id = lease.turn_id AND last_seq = lease.last_seq;
        # 0 rows -> LeaseLost (the uow rolls back). Returns the new last_seq. The caller publishes session_released after commit
    async def release(self, lease: SessionLease) -> None: ...                # active_turn_id, lease_expires_at = NULL iff
                                                                             # active_turn_id = lease.turn_id
class TurnRepo(Protocol):
    async def get(self, turn_id: str) -> Row | None: ...
    async def insert(self, turn: Row, attempts: Sequence[Row], decisions: Sequence[Row], turn_files: Sequence[Row]) -> None: ...
        # store: true without a session (session_id and seq NULL)
    async def list_for_session(self, session_id: str, *, after: str | None, limit: int) -> Page[Row]: ...   # seq ASC
    async def recent(self, session_id: str, *, max_seq: int, limit: int) -> tuple[Row, ...]: ...   # seq DESC, seq ≤ max_seq
    async def delete(self, turn_id: str) -> None: ...                        # cascades to attempt, turn_file
    async def delete_for_session(self, session_id: str) -> int: ...
class AttemptRepo(Protocol):
    async def list_for_turn(self, turn_id: str) -> tuple[Row, ...]: ...      # started_at ASC
class DecisionRepo(Protocol):
    async def insert(self, row: Row) -> None: ...                            # /v1/decisions and the facade (finalize)
    async def get(self, decision_id: str) -> Row | None: ...
    async def delete(self, decision_id: str) -> None: ...
    async def delete_for_session(self, session_id: str) -> int: ...
class FileRepo(Protocol):
    async def create(self, row: Row) -> Row: ...
    async def get(self, file_id: str) -> Row | None: ...
    async def update(self, file_id: str, **changes: object) -> Row: ...      # status, media_type, rejection, failure, pages, deleted_at
    async def list_for_session(self, session_id: str) -> tuple[Row, ...]: ...
    async def delete(self, file_id: str) -> None: ...                        # cascades to derivative rows; S3 by the caller
class DerivativeRepo(Protocol):
    async def upsert(self, row: Row) -> None: ...                            # key (file_id, kind); column facts (C2a, closed
        # per kind): pdf_sanitized {"active_content": [...]} (distinct spec-09 §6.3 values, sorted), NULL for every other kind
    async def list_for_file(self, file_id: str) -> tuple[Row, ...]: ...
class UsageRepo(Protocol):                                 # rows are written only by ops.outbox_apply
    async def report(self, *, group_by: Literal["project", "session", "model", "day", "line"], day_from: date, day_to: date,
                     after: str | None, limit: int) -> Page[Row]: ...        # spec-11 §2; rows {key, line, <Usage fields>}
class LabelRepo(Protocol):
    async def create_run(self, row: Row) -> Row: ...
    async def update_run(self, run_id: str, **changes: object) -> Row: ...
    async def open_run(self, profile_id: str, profile_version: str, question_name: str,
                       question_signature: str | None = None) -> Row | None: ...   # the running run of that definition (one per
        # name and signature, C2a); None (deprecated, pre-C2a): the newest running run of the name, whatever its signature
    async def insert_item(self, row: Row) -> None: ...
    async def items(self, run_id: str, *, kept: bool | None = None) -> tuple[Row, ...]: ...
    async def items_for_decision(self, decision_id: str) -> tuple[Row, ...]: ...   # every run, id ASC (a retried labeling.item
                                                                             # finds what an earlier attempt stored; C2a)
    async def kept_items(self, profile_id: str, profile_version: str, question_signature: str, since: datetime,
                         sample_reason: Literal["uniform", "uncertainty"] | None = "uniform") -> tuple[Row, ...]: ...
        # C2a: kept items with created_at >= since of every run of that definition, created_at then id ASC; None = both reasons
    async def list_runs(self, *, profile_id: str | None, question_name: str | None, status: str | None,
                        after: str | None, limit: int) -> Page[Row]: ...     # id DESC
    async def list_dropped(self, *, run_id: str | None, question_name: str | None, reason: str | None,
                           after: str | None, limit: int) -> Page[Row]: ...  # id DESC, kept = false
class CalibratorRepo(Protocol):                            # tenant rows only for writes (RLS); platform rows via admin.*
    async def insert(self, row: Row) -> None: ...                            # status fitted
    async def get(self, calibrator_id: str) -> Row | None: ...
    async def update(self, calibrator_id: str, **changes: object) -> Row: ...   # status accepted|rejected (+ acceptance, holdout)
    async def list(self, *, profile_id: str | None, question_name: str | None, status: str | None,
                   after: str | None, limit: int) -> Page[Row]: ...          # tenant + platform rows, id DESC
class IdempotencyRepo(Protocol):                           # used only by the IdempotencyStore implementation (spec-11 §5)
    async def insert_in_flight(self, row: Row) -> bool: ...                  # False on a (project, scope_hash, key) conflict
    async def get_by_key(self, scope_hash: str, key: str) -> Row | None: ...
    async def take_over(self, record_id: str, *, owner: str, fingerprint: str, lease_s: int, ttl_s: int,
                        resource_id: str | None = None) -> bool: ...
        # conditional UPDATE to a fresh in_flight record of `owner` (new fingerprint, lease, TTL, resource_id, response cleared)
        # iff still expired, not_stored or in_flight with a lapsed lease; False = a racing taker won or the record is gone
    async def renew(self, record_id: str, *, owner: str, lease_s: int) -> bool: ...
    async def finish(self, record_id: str, *, owner: str, response: "StoredResponse | None") -> None: ...
        # None -> not_stored, else completed; a no-op unless `owner` still holds the in_flight record (fenced: a late call never
        # downgrades a completed record); bodies over 1 MiB are stored by key (response_body_key, IMPL-085)
    async def delete(self, record_id: str) -> None: ...
class AuditRepo(Protocol):
    async def insert(self, row: Row) -> None: ...                            # synchronous audit; most audit goes through the outbox
class ProjectRepo(Protocol):
    async def get(self, project_id: str) -> Row | None: ...
class JobRepo(Protocol):                                   # used only by the JobQueue implementation
    async def insert(self, row: Row) -> str: ...                             # ON CONFLICT (job_dedupe) -> the existing id
class SystemCalls(Protocol):                               # system_uow: the spec-03 §4 functions granted to noema_app, plus two
                                                           # platform reads; each call is its own transaction
    async def lookup_api_key(self, key_id: str) -> Row | None: ...
    async def touch_api_key(self, key_id: str) -> None: ...
    async def rehash_api_key(self, key_id: str, key_hash: str, pepper_version: int) -> bool: ...
    async def claim_jobs(self, kinds: Sequence[str], n: int, worker: str, lock_s: int) -> tuple[Job, ...]: ...
    async def finish_job(self, job_id: str, worker: str, *, ok: bool, error: str | None, permanent: bool = False
                         ) -> FinishedJobState | None: ...   # C2a: the new state ("done" | "queued" | "dead"); None = the worker no
                                                             # longer held the job (nothing changed)
    async def extend_job(self, job_id: str, worker: str, lock_s: int) -> bool: ...
    async def ensure_periodic(self, kind: str, every_s: int) -> None: ...
    async def outbox_apply(self, records: Sequence[OutboxRecord]) -> tuple[str | None, ...]: ...   # per record: None = applied
    async def outbox_dead(self, record: OutboxRecord, error: str, *, attempts: int = 0) -> None: ...   # attempts = delivery count
    async def reap_stuck(self, older_than_s: int) -> int: ...
    async def retention_candidates(self, limit: int) -> tuple[tuple[str, str, str], ...]: ...   # (tenant_id, kind, id), oldest
        # first; superseded by retention_page (rows that keep failing stay at the head of every call)
    async def retention_page(self, limit: int, *, after: RetentionCandidate | None = None) -> tuple[RetentionCandidate, ...]: ...
        # C2a: RetentionCandidate = (tenant_id, kind, id, expires_at); (expires_at, kind, id, tenant_id) order, text compared
        # bytewise, strictly after `after` (the previous page's last candidate; None = from the oldest)
    async def calibrator_epochs(self) -> Mapping[str, int]: ...                # scope_key -> n
    async def active_calibrators(self, scope_keys: Sequence[str]) -> tuple[Row, ...]: ...
    async def active_config(self) -> Row | None: ...                           # platform.config_version WHERE status = 'active'
    async def price_catalog(self, version: int) -> Row | None: ...             # platform.price_catalog
    async def set_calibrator_status(self, actor: str, calibrator_id: str, status: str, *, reason: str | None) -> None: ...
        # the noema_app grant of admin.set_calibrator_status (labeling drift job): only active -> downgraded (drift_ece |
        # drift_ci); actor = "system:<purpose>"; refusals raise the db layer's admin error (forbidden | not_found | invalid_argument)
    async def bump_epoch(self, actor: str, scope_key: str) -> int: ...          # 'platform' or a tenant id; notifies
                                                                                # calibrators_changed; returns the new n
```

### 7.2 Process services (`core/protocols/services.py`, `core/protocols/runtime.py`, `core/settings.py`, `core/objects.py`)

```python
class ServiceSet(Protocol): ...                            # every composition.Services field (spec-01 §3) as a read-only property,
    # plus runtime and settings; domains type their dependencies with it (never import composition); = api.deps.ApiServices
type JobHandler = Callable[[Job], Awaitable[None]]
type JobHandlers = Mapping[str, JobHandler]               # exact job kind -> handler
# each owner's jobs module (noema.identity.jobs, noema.labeling.jobs, noema.ops.retention, noema.media.worker) exports
#   def make_handlers(services: ServiceSet) -> JobHandlers
def merge_handlers(*maps: JobHandlers) -> dict[str, JobHandler]: ...      # ValueError when two owners claim one kind
def expand_kinds(patterns: Sequence[str], available: Iterable[str]) -> tuple[str, ...]: ...   # --kinds globs -> exact kinds,
    # sorted (jobs.claim matches exactly); ValueError when a pattern matches no handler kind
class ProcessRuntime(Protocol):                            # one per process (core/runtime.py TaskGroupRuntime); sync, on the loop
    @property
    def draining(self) -> bool: ...                        # True from the first shutdown signal on, never reset; /readyz 503
    def spawn(self, name: str, fn: Callable[[], Awaitable[None]]) -> None: ...   # root task group, outlives the request;
        # exceptions are logged (noema.runtime), never raised; cancelled at close; RuntimeError when the runtime is not open
    def on_drain(self, name: str, callback: Callable[[float], Awaitable[None]]) -> None: ...   # callback(grace_s) when draining
        # begins (all hooks concurrently; 30 s in the api); composition.stop waits for them; registered while draining: runs at once
    def local[T](self, key: str, factory: Callable[[], T]) -> T: ...   # process state, created once (key "<package>.<name>",
        # e.g. "turns.registry"); no module-level singletons
@dataclass(frozen=True, slots=True)
class ProcessSettings:                                     # the spec-05 §5 variables (composition.process_settings); services.settings
    env: Env; endpoint_overrides: Mapping[str, str]; contract_validate: bool; expected_pods: int; metrics_port: int | None
    trusted_proxies: tuple[IPv4Network | IPv6Network, ...]; agent_id: str | None
    pod_id: str = "local"; s3_bucket: str = "noema-test"; redis_url: str = "redis://127.0.0.1:26379/0"
    db_dsn_ref: str = "noema/db-dsn"; drain_delay_s: float = 0.0; dev_api_key: SecretStr | None = None
    sandbox_backend: SandboxBackend = "remote"            # Literal["remote", "bwrap", "inproc_rlimit"] (C2a); media-worker only
    spool_dir: str = "/spool"; sandbox_socket: str = "/spool/sandboxd.sock"   # absolute (C2a)
def default_bucket(env: str) -> str: ...                   # objects.py: "noema-<env>"
def tenant_kms_key_alias(env: str, tenant_id: str) -> str: ...   # "alias/noema-<env>-<tenant_id>" (platform account)
def sse_args(env: str, tenant_id: str) -> dict[str, str]: ...    # every tenant PutObject / CreateMultipartUpload: SSE-KMS with the
    # tenant key in staging/prod, AES256 in dev/test
```

## 8. Ops (`core/protocols/ops.py`)

```python
class HealthRegistry(Protocol):                            # keyed by the Bedrock model id (models.<alias>.id), never the alias,
                                                           # so every alias of one model shares one breaker
    def admit(self, model_id: str, region: str, tenant_account: str) -> bool: ...       # sync
    def record(self, model_id: str, region: str, tenant_account: str, outcome: Literal["ok", "error", "throttled", "timeout"],
               ttft_ms: int | None) -> None: ...                                         # sync; pub/sub mirror async
    def state(self, model_id: str, region: str) -> Literal["closed", "open", "half_open"]: ...   # sync
class RetryBudget(Protocol):
    def try_spend(self, tenant_account: str, model_id: str) -> bool: ...                # sync (IMPL-078); per (account, Bedrock id)
    def note_request(self, tenant_account: str, model_id: str) -> None: ...             # sync, never raises (C2a): the adapters call it
                                                                                         # per wire request (base of the 10 % ratio)

@dataclass(frozen=True, slots=True)
class QuestionWork:                                        # one compiled question of a decision run
    name: str; k: int                                      # K: predicate 2, score levels, choice options
    source: Literal["platform", "request"]                 # Q_k filters platform questions by name (P_{k-1}.escalation.questions);
                                                           # request questions always pass (IMPL-068)
@dataclass(frozen=True, slots=True)
class DecisionWork:                                        # one decision run to reserve (spec-11 §1.1)
    profile_id: str                                        # effective stage-0 profile
    questions: tuple[QuestionWork, ...]                    # compiled order (replaces question_ks, B4); the planner derives
                                                           # hierarchical (K > L(profile)) and reserves 1 + 3 calls/member
    stage0_deadline_ms: int                                # effective (request override or profile)
    escalation: Literal["profile_default", "off"]
    escalate_budget_ms: int | None                         # turns: deadline_ms_eff + slack_ms; /v1/decisions: remaining ctx.deadline;
                                                           # None: no escalation (verification)
    kind: Literal["decision_member", "verification_member"]
    input_tokens: int                                      # TokenEstimator estimate of the decision input
@dataclass(frozen=True, slots=True)
class GenerationWork:
    candidate_profiles: tuple[str, ...]                    # computed by the orchestrator (spec-11 §1.1)
    input_tokens: int; max_output_tokens: int | None       # request generation.max_output_tokens, if pinned
@dataclass(frozen=True, slots=True)
class WorkGraph:
    calls: Mapping[tuple[str, CallKind], int]              # (model alias, kind) -> max calls; per-model quota buckets
    tokens_by_model: Mapping[str, int]                     # alias -> reserved input + output tokens; per-model quota buckets
    total_calls: int; tokens_in: int; tokens_out: int      # key / project buckets (≤ the per-model sums, spec-11 §1.1)
    streams: int                                           # concurrent-stream slots: 1 per turn, 0 for /v1/decisions
class WorkGraphPlanner(Protocol):
    def plan(self, snapshot: ConfigSnapshot, principal: Principal, *, decisions: Sequence[DecisionWork],
             generation: GenerationWork | None) -> WorkGraph: ...                       # sync, pure; spec-11 §1.1
class Reservation(Protocol):                               # one per request; spec-11 §1.2
    def consume(self, kind: CallKind, model: str) -> None: ...                          # sync; never raises; above the reserved
        # count = a bug: aghoo_reservation_overrun_total + ERROR log, AssertionError under NOEMA_ENV=test; the call proceeds
    async def reconcile(self, usage: Mapping[str, UsageRecord]) -> None: ...            # alias -> actual usage; refunds unused
        # calls and tokens; idempotent (only the first call counts)
    def headers(self) -> Mapping[str, str]: ...                                          # sync; the six x-ratelimit-* (spec-11 §1.2)
    async def release(self) -> None: ...                   # idempotent; frees the stream slot; without a prior reconcile it refunds
                                                           # only unconsumed calls (tokens stay debited); shielded (1 s)
class Admission(Protocol):
    async def reserve(self, principal: Principal, graph: WorkGraph, *, queue_timeout_s: float,
                      deadline: Deadline | None = None) -> Reservation: ...
        # raises NoemaError(rpm_exceeded | tpm_exceeded | queue_timeout) with retry_after_s; cancellation while queued leaves
        # nothing debited. A stream slot's lease expires at deadline + 30 s (stream callers pass ctx.deadline; None = the
        # implementation's maximum turn lifetime). Labeling jobs build their WorkGraph directly and use system_principal
class RateLimiter(Protocol):                               # a bare GCRA bucket outside the work graph (the per-IP auth-failure
    # bucket "authfail:<ip>", 10/s, burst 50, spec-09 §2); ops-admission's script and Redis-down fallback; never raises for
    # backend failures; ValueError for rate_per_s <= 0, burst < 1 or n < 1
    async def check(self, bucket: str, *, rate_per_s: float, burst: int) -> float | None: ...   # None = capacity for one unit,
                                                                                                # else retry_after_s; debits nothing
    async def debit(self, bucket: str, *, rate_per_s: float, burst: int, n: int = 1) -> float | None: ...   # None = debited

class TokenEstimator(Protocol):
    def estimate(self, model_alias: str, system: Sequence[str], blocks: Sequence[ContentBlock]) -> int: ...   # sync (IMPL-029c)
@dataclass(frozen=True, slots=True)
class UsageLine:                                           # spec-11 §2: granularity, deterministic id, who records which line
    id: str; tenant_id: str; project_id: str; line: UsageLineName
    model_id: str | None; region: str | None               # Bedrock id and call region; None only for cache_savings
    usage: UsageRecord; cost: Cost                         # sums of per-call values (cost rounded per call, then summed)
    turn_id: str | None = None; decision_id: str | None = None; attempt_id: str | None = None; session_id: str | None = None
    labeling_run_id: str | None = None; at: datetime | None = None   # None -> Clock.now() at record()
class CostMeter(Protocol):
    def price(self, model_alias: str, region: str, usage: UsageRecord, *, snapshot: ConfigSnapshot | None = None) -> Cost: ...
        # sync (IMPL-019): the catalog of `snapshot`, the caller's pinned config (decision, generation and labeling pass
        # ctx.snapshot; C2a); None = the current snapshot. `region` = the tenant's home_region (spec-11 §2), never the serving region
    def record(self, line: UsageLine) -> None: ...         # sync; Outbox.put(kind="usage_line") + aghoo_cost_usd_total{tenant, line}
```

*Wave E2 note.* `RetryBudget.try_spend` gates same-model re-sends only; a fallback to another model never spends it (IMPL-016 amendment, R6).

*Wave E3 note (R8, IMPL-016 amendment).* The budget is 10% of **first sends** per (tenant account, Bedrock id) over the sliding 60 s, with no floor (`retries + 1 ≤ 0.1 × first_sends`; first sends = noted requests − granted re-sends − unsent sends), and counts only re-sends the upstream may have received. The in-adapter connect retry of a send that never reached the upstream (`is_connect_error` and not `possibly_delivered`) is exempt, once per call, and its failed send is no first send: `try_spend` recognizes it by the exception the adapter is handling (the frozen signature carries no cause). `HealthRegistry`: the p95-TTFT rule opens a service breaker only when the samples above the threshold come from at least `BreakerPolicy.ttft_min_accounts` (2) distinct tenant accounts, and decision members record no `ttft_ms` (IMPL-078 amendment).

## 9. Labeling and calibration (`core/protocols/labeling.py`; ADR-003)

```python
@dataclass(frozen=True, slots=True)
class CalibratorBindingKey:                                # one key type everywhere; scope is NOT part of the key
    profile_id: str; profile_version: str; question_signature: str
    member_subset: tuple[str, ...]                         # sorted ok member ids of the answering stage
    input_view_signature: str; choice_method: Literal["flat", "hierarchical"] = "flat"
class Transform(Protocol):
    def apply(self, dist: Mapping[str, float], unresolved: float) -> dict[str, float]: ...   # sync; preserves unresolved
@dataclass(frozen=True, slots=True)
class Calibrator:
    id: str; scope: Literal["tenant", "platform"]; method: Literal["temperature", "platt"]; epsilon: float
    member_T: Mapping[str, float]; pooled: Transform; member: Callable[[str], Transform]
    holdout: Mapping[str, JsonValue]; key: CalibratorBindingKey
class CalibratorRegistry(Protocol):                        # per-pod in-memory snapshot (IMPL-072)
    def lookup(self, tenant_id: str, key: CalibratorBindingKey) -> Calibrator | None: ...  # sync; active only; tenant first
    def epoch(self, tenant_id: str) -> str: ...            # sync; f"{platform_n}.{tenant_n}"; "" if unknown -> L2 bypass
    async def refresh(self) -> None: ...                   # called on calibrators_changed and every 5 s: SystemCalls.calibrator_epochs,
        # then active_calibrators(changed scope keys); swaps the snapshot atomically; on error keeps the last snapshot (never raises)
@dataclass(frozen=True, slots=True)
class ContentRef:  decision_id: str; turn_id: str | None
class LabelingSampler(Protocol):
    def offer(self, tenant_id: str, snapshot: DecisionSnapshot, ref: ContentRef) -> None: ...   # sync; never raises; decides
        # in memory (labeling.sample rates); a sampled offer -> Outbox.put(kind="labeling_offer"), applied as a labeling.item job
        # (spec-11 §3-§4)
```

## 10. Generation (`core/protocols/generation.py`)

```python
@dataclass(frozen=True, slots=True)
class AttemptSpec:                                         # equality = the 4-tuple (IMPL-014, IMPL-015)
    generation_profile: str; effort: Effort | None; max_output_tokens: int | None; conditioning_signature: str  # "none" if no block
    def signature(self) -> str: ...                        # sync; "sha256:" + sha256(orjson([profile, effort, max_output_tokens,
                                                           # conditioning_signature])); attempt.spec_signature, aghoo.attempt.spec_signature
@dataclass(frozen=True, slots=True)
class Conditioning:                                        # rendered by turns/conditioning.py (spec-04 §5.2); wire Conditioning + block
    answer_mode: str; signature: str; summary: str; block: str
@dataclass(frozen=True, slots=True)
class AssembledPrompt:                                     # one attempt's prompt for one model
    system: tuple[str, ...]; messages: tuple[Message, ...]; conditioning: Conditioning | None; degradations: tuple[Degradation, ...]
class PromptBuilder(Protocol):                             # implemented by turns/prompt; called by generation at every attempt start
    async def build(self, alias: str) -> AssembledPrompt: ...   # media negotiated for this model (spec-07 §4); standard spotlighting
                                                                 # always, strict if its trigger is final by now (IMPL-086); never raises for media
class AttemptBudget:                                       # concrete (core/protocols/generation.py); turn-wide failure budget (IMPL-016)
    def __init__(self, failures: int = 2) -> None: ...
    def try_take(self) -> bool: ...                        # sync; False when exhausted
    remaining: int
@dataclass(frozen=True, slots=True)
class TurnContext:
    principal: Principal; tenant: TenantRef; snapshot: ConfigSnapshot; deadline: Deadline; reservation: "Reservation"
    turn_id: str; session_id: str | None; trace_id: str
@dataclass(frozen=True, slots=True)
class AttemptRecord:
    attempt_id: str; route_id: str                         # route_id = generation profile id actually used
    model: str                                             # Bedrock id (TurnObject.attempts[].model)
    outcome: Literal["committed", "cancelled", "failed"]
    cancel_reason: Literal["spec_changed", "lost_race", "client_abort", "failed", "deadline"] | None
    usage: UsageRecord; cost: Cost                         # usage estimated for cancelled attempts (IMPL-020)
    finish_reason: Literal["stop", "length", "content_filter", "cancelled"] | None
    error_code: str | None; started_at: datetime; ended_at: datetime; spec_signature: str
    first_text_at: datetime | None = None                  # C2b: this attempt's first TextDelta (or text-less content_filter stop),
                                                           # on the clock of started_at; timings.holdback_ms = commit - this
GenEvent = Mapping[str, JsonValue]                         # exactly the openapi payloads of generation.started / delta / committed /
                                                           # cancelled / completed and error; the orchestrator appends them as given
class ProfileRun(Protocol):                                # one generation profile run (members = attempts, IMPL-011)
    spec: AttemptSpec
    @property
    def upstream_started(self) -> bool: ...                # C2a; sync: True once any attempt received an upstream event
        # (MessageStart, reasoning, text or a stop; usage and error events don't count): decision.completed.arrived is
        # before_generation while False, else during_holdback (spec-04 §3)
    def events(self) -> AsyncIterator[GenEvent]: ...       # attempt-level events in order; deltas only after commit; ends after the
        # last attempt's terminal event. No generation.cancelled for reason deadline (the turn emits error{deadline_exceeded})
    async def first_text(self) -> float | None: ...        # monotonic time of the first buffered answer text, or of a text-less
        # Stop{content_filter} (an empty committable answer, 04:L114: surfaced as finish_reason content_filter, never rerouted);
        # None if the run ended with neither
    async def commit(self, reason: Literal["spec_agrees", "holdback_deadline", "gate"]) -> None: ...  # flushes the buffer;
        # idempotent; a no-op after the run ended. With no attempt buffering (first_text came from an attempt that then
        # failed) it arms the run: the next attempt commits at its own first text with this reason (ruling R2, spec-04 §3)
    async def cancel(self, reason: Literal["spec_changed", "client_abort", "deadline"]) -> None: ...  # idempotent; committed text kept
    async def result(self) -> tuple[AttemptRecord, ...]: ...   # waits for every attempt to be terminal; started_at order
class GenerationEngine(Protocol):
    def run(self, ctx: TurnContext, spec: AttemptSpec, prompt: PromptBuilder, *, hold: bool, speculative: bool,
            auto_commit: Literal["spec_agrees", "gate"] = "spec_agrees",
            failure_budget: AttemptBudget) -> AsyncContextManager[ProfileRun]: ...
        # hold=True buffers until commit(); hold=False commits at the first answer text with reason auto_commit (spec-04 §3).
        # speculative = generation.started.speculative (A1). Same lifecycle as DecisionEngine.run: __aenter__ raises only
        # NoemaError(tenant_access_misconfigured); __aexit__ cancels non-terminal attempts and awaits them. The orchestrator
        # appends route.confirmed before it appends the run's generation.committed event (spec-04 §4)
```

## 11. Auth (`core/protocols/auth.py`)

```python
class Authenticator(Protocol):
    async def authenticate(self, headers: Mapping[str, str], client_ip: str) -> Principal: ...   # 401 invalid_api_key (no echo);
        # 429 rpm_exceeded (retry_after_s) while the per-IP bucket authfail:<client_ip> is empty, before any key verification
        # (spec-09 §2; debited through RateLimiter on each 401)
Action = Literal["turn.create", "turn.read", "turn.cancel", "decision.create", "decision.read", "profile.list",
                 "session.create", "session.read", "session.update", "session.delete", "session.list",
                 "file.upload", "file.read", "file.watch", "file.derivative.read", "file.delete", "usage.read",
                 "admin.calibrators.read", "admin.labeling.read", "admin.labeling.dropped.read"]
                 # file.read = getFile; file.watch = watchFile; file.derivative.read = getFileDerivative (not the auditor)
@dataclass(frozen=True, slots=True)
class ResourceRef:                                         # loaded by the route before check(); spec-09 §3 resource rules
    kind: Literal["turn", "decision", "session", "file", "usage", "admin"]; id: str | None; project_id: str | None
    owner_principal_id: str | None                         # turn/decision creator, session owner, file uploader
    session_owner_principal_id: str | None = None          # turns and files in a session
ListScope = Literal["none", "own", "project", "tenant"]
class Authorizer(Protocol):
    def check(self, principal: Principal, action: Action, resource: ResourceRef | None = None) -> None: ...
        # sync; raises NoemaError(permission_denied | not_found | session_not_found | session_forbidden) per the spec-09 §3
        # matrix (403 only where the operation declares it; otherwise the 404 code)
    def list_scope(self, principal: Principal, action: Action) -> ListScope: ...
        # sync; list operations (session.list; usage.read's project/tenant scope): "none" = role or scope failure -> the route
        # returns an empty page (session.list) or 403 where declared; "own" = owner_principal_id = caller; "project"; "tenant"
```

## 12. Telemetry (`core/protocols/telemetry.py`)

```python
SpanName = Literal["turn", "preflight", "history.fetch", "sts.assume_role", "decision", "decision.member", "decision.escalation",
                   "bindings.evaluate", "generation.attempt", "verification", "cache.lookup", "media.extract",   # IMPL-006
                   "labeling.item", "labeling.run", "labeling.drift"]                                         # C2a (labeling jobs)
# C2a registry additions (noema/telemetry/registry.py): attributes aghoo.media.kind, aghoo.media.status; metrics
# aghoo_labeling_items_total{question,outcome}, aghoo_calibrator_fits_total{status}, aghoo_calibrator_downgrades_total{reason},
# aghoo_label_class_share{profile,question,class}, aghoo_media_jobs_total{kind,status}, aghoo_media_job_seconds{kind},
# aghoo_inline_ingest_seconds, aghoo_sandbox_queue_seconds{kind}, aghoo_admission_rejections_total{code},
# aghoo_admission_degraded, aghoo_admission_refund_failures_total, aghoo_admission_pending_releases
# Wave E3 (IMPL-088, observed in composition): gauges aghoo_db_pool_size{pool}, aghoo_db_pool_in_use{pool},
# aghoo_redis_pool_size, aghoo_redis_pool_in_use; histograms (1 in 16 sampled) aghoo_db_pool_acquire_seconds{pool},
# aghoo_db_command_seconds{pool}, aghoo_redis_pool_acquire_seconds, aghoo_redis_command_seconds{command}
class Span(Protocol):
    def set(self, key: str, value: str | int | float | bool) -> None: ...   # sync; keys of the B1 attribute registry only (08 §7
                                                                             # plus those named by IMPL-006 and spec-06 §16); redacted
    def error(self, code: str) -> None: ...                                  # sync; an ErrorCode or member error code, no message
class Instruments(Protocol):                               # every 08 §7 metric plus the IMPL-006 additions, pre-declared in B1
    def add(self, name: str, value: float = 1, **labels: str) -> None: ...      # counters (*_total)
    def observe(self, name: str, value: float, **labels: str) -> None: ...      # histograms (*_seconds)
    def set(self, name: str, value: float, **labels: str) -> None: ...          # gauges
    # all sync, never raise; an undeclared name or label key is an error under NOEMA_ENV=test, dropped + logged otherwise
class Telemetry(Protocol):
    def span(self, name: SpanName, **attrs: str | int | float | bool) -> ContextManager[Span]: ...   # sync
    instruments: Instruments
    def redact(self, text: str) -> str: ...                # sync; spec-09 §4 patterns; applied by every log handler
```

## 13. Redis keys (`core/redis_keys.py`, frozen)

```python
class RedisKeys:
    def __init__(self, ns: str) -> None: ...               # tests: "ns:<agent>:<worker>:<uuid>:"; prod: "noema:"
    def turn_events(self, turn_id: str) -> str: ...        # Stream (IMPL-021)
    def outbox(self, shard: int) -> str: ...               # Stream; shard = outbox_shard(tenant_id); consumer group "drain"
    @staticmethod
    def outbox_shard(tenant_id: str) -> int: ...           # crc32(tenant_id) % 8
    def l2(self, tenant_id: str, digest: str) -> str: ...  # f"{ns}dec:l2:{tenant_id}:{digest}"
    def l2_pattern(self, tenant_id: str) -> str: ...       # f"{ns}dec:l2:{tenant_id}:*" (purge_tenant)
    def gcra(self, bucket: str) -> str: ...                # string (TAT, ms); bucket names in spec-11 §1.2
    def streams(self, tenant_id: str, project_id: str) -> str: ...   # ZSET reservation_id -> lease expiry (spec-11 §1.2)
    def retry_budget(self, tenant_account: str, model: str) -> str: ...   # coarse 1 s sync of the local budget (IMPL-078)
    def breaker_events(self) -> str: ...                   # pub/sub
    def calibrators_changed(self) -> str: ...              # pub/sub
    def keys_revoked(self) -> str: ...                     # pub/sub
    def idem_done(self, scope_hash: str, key: str) -> str: ...  # pub/sub
    def session_queue(self, session_id: str) -> str: ...   # FIFO tickets (spec-04 §2)
    def session_released(self, session_id: str) -> str: ...  # pub/sub (spec-04 §2)
    def session_pending(self, session_id: str) -> str: ... # set while a turn finalize waits in the outbox (IMPL-022); TTL 86400 s
    def turn_owner(self, turn_id: str) -> str: ...         # running-turn registry {pod, tenant_id, project_id, principal_id,
                                                           # session_owner_principal_id}; TTL 60 s (cancelTurn authz, spec-04 §11)
    def turn_cancel(self) -> str: ...                      # pub/sub; payload turn_id (spec-04 §11)
    def turn_attach(self, turn_id: str) -> str: ...        # last delivered event time (abandon watchdog)
    def file_events(self, file_id: str) -> str: ...        # pub/sub; FileService.watch (spec-07 §3.1)
    def labeling_daily(self, tenant_id: str, day: str) -> str: ...   # C2a: counter of items sampled for one tenant on one UTC day
        # (day "YYYYMMDD"); INCR by the labeling.item job, EXPIRE 2 days: an exact cluster-wide max_per_day_per_tenant
    def config_changed(self) -> str: ...                   # pub/sub
```

Pub/sub payloads are orjson objects; every payload that concerns a tenant carries `tenant_id`, and receivers drop a message whose `tenant_id` doesn't match the resource they hold (spec-09 §7):

| Channel | Payload | Publisher |
|---|---|---|
| `breaker_events` | `{model, region, tenant_account?, state, until_ms?, origin}`; `origin` = a per-instance nonce (a pod ignores its own events; a remote `open` extends an open breaker without making it remote) | adapters |
| `calibrators_changed`, `config_changed`, `keys_revoked` | `{scope_key, n}` / `{version}` / `{tenant_id, key_id}` | notify bridge (from `pg_notify`, spec-11 §4) |
| `idem_done(scope, key)` | `{tenant_id, record_id}` | IdempotencyStore |
| `session_released(sid)` | `{tenant_id, session_id, last_seq}` | turns |
| `turn_cancel` | `{tenant_id, turn_id}` | turns |
| `file_events(fid)` | `{tenant_id, file_id, status}` | media-pipeline |

## 14. Conformance suites (`tests/contract_internal/`, frozen)

| Suite | Implementations under test |
|---|---|
| `test_clock_deadline.py` | `SystemClock`, `FakeClock` |
| `test_config_registry.py` | real (loader over the example yaml), fake: pinning, `get` window, `on_change` |
| `test_secrets_provider.py` | Secrets Manager (moto), env provider (dev/test), fake: versions, cache TTL |
| `test_credential_broker.py` | `StsBroker` (moto), `AmbientBroker` (`NOEMA_ENV=test`), `FakeBroker` |
| `test_adapter.py` | converse and mantle adapters against the wire mock, `FakeAdapter`: golden requests, error classification, `retry="none"` honored, cancel = aclose (also from an outer cancel scope mid-stream, `AdapterKit.open_streams` == 0; C2a), the stream event order and timeouts of §4.1 |
| `test_health_registry.py` | real (2 processes over Redis pub/sub), fake: breaker transitions, `RetryBudget` |
| `test_decision_engine.py` | `DecisionEngineImpl`, `FakeDecisionEngine`: the snapshot contract, cancel, shielded finalize, `row` only for turns |
| `test_question_compiler.py`, `test_decision_cache.py` | real, fake: signatures (spec-06 §3.3 vectors); fail-open within 20 ms |
| `test_generation_engine.py` | `GenerationEngineImpl`, `FakeGenerationEngine`: hold/auto-commit, cancel reasons, `AttemptBudget`, per-attempt `PromptBuilder.build` |
| `test_event_log.py`, `test_sse_writer.py` | in-process + Redis mirror, fake; writer backpressure, merge, ping, `client_too_slow` |
| `test_idempotency.py` | Postgres + Redis store (2 processes), fake |
| `test_admission.py`, `test_work_graph.py` | GCRA Lua, local fallback, fake; the spec-11 §1.1 vectors |
| `test_rate_limiter.py`, `test_process_runtime.py`, `test_composition.py` | `RateLimiter` (GCRA script, local fallback) and `FakeRateLimiter`; `TaskGroupRuntime`; `build()` field set and environment gates (supersets only) |
| `test_cost_meter.py`, `test_token_estimator.py` | real, fake |
| `test_job_queue.py`, `test_outbox.py`, `test_uow_rls.py` | real (per-session DB), fake |
| `test_repos.py` | `db/repos` (per-session DB) and `tests/fakes/db.py`: every §7.1 method, pagination order, `get` → None for foreign rows |
| `test_session_repo.py` | real (2 processes), fake: acquire/renew/finalize/release, `Busy`, `session_conflict`, `LeaseLost` |
| `test_input_view_resolver.py`, `test_file_service.py`, `test_sandbox_runner.py`, `test_media_negotiator.py` | real, fake (`MediaNegotiator` + `MediaBudgetPlanner`) |
| `test_calibrator_registry.py`, `test_labeling_sampler.py` | real (2 processes, ≤ 5 s staleness), fake |
| `test_auth.py` | `Authenticator` (per-session DB), `Authorizer`, fakes: the spec-09 §3 matrix rows, `check` codes and `list_scope` |
| `test_telemetry.py` | real (in-memory exporter), fake: undeclared span/metric names rejected in test mode, `redact` |
| `test_openapi_contract.py` | IMPL-066 assertions |
