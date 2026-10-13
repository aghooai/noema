"""Health, admission, token estimates and cost (spec-02 §8, spec-11 §1-§2). Hot-path calls are sync and in
memory (IMPL-078)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from noema.core.clock import Deadline
from noema.core.principal import Principal
from noema.core.protocols.config import ConfigSnapshot
from noema.core.values import CallKind, ContentBlock, Cost, UsageLineName, UsageRecord


class HealthRegistry(Protocol):
    """Circuit breakers per (model id, region[, tenant account]); mirrored across pods via Redis pub/sub. Keys are
    Bedrock model ids (`models.<alias>.id`, e.g. "us.anthropic.claude-opus-5-5"), never config aliases, so the
    adapters, decision and generation share one breaker per model whatever alias reached it."""

    def admit(self, model_id: str, region: str, tenant_account: str) -> bool:
        """Sync: False while the breaker is open."""
        ...

    def record(
        self,
        model_id: str,
        region: str,
        tenant_account: str,
        outcome: Literal["ok", "error", "throttled", "timeout"],
        ttft_ms: int | None,
    ) -> None:
        """Sync; the pub/sub mirror is asynchronous."""
        ...

    def state(self, model_id: str, region: str) -> Literal["closed", "open", "half_open"]:
        """Sync."""
        ...


class RetryBudget(Protocol):
    def try_spend(self, tenant_account: str, model_id: str) -> bool:
        """Sync: a local token bucket per (tenant account, Bedrock model id), coarse-synced to Redis every 1 s
        (IMPL-078)."""
        ...

    def note_request(self, tenant_account: str, model_id: str) -> None:
        """Sync: one wire request was sent for (tenant account, Bedrock model id); the adapters call it for every
        request, so a ratio budget (retries <= 10 % of requests) knows its base. Never raises."""
        ...


@dataclass(frozen=True, slots=True)
class QuestionWork:
    """One question of a decision run to reserve: its compiled name, K (predicate 2, score levels, choice options)
    and source. The escalation set Q_k filters platform questions by name through `P_{k-1}.escalation.questions`;
    request questions always pass (spec-11 §1.1, IMPL-068)."""

    name: str
    k: int
    source: Literal["platform", "request"]


@dataclass(frozen=True, slots=True)
class DecisionWork:
    """One decision run to reserve (spec-11 §1.1). `questions` in compiled order; the planner derives hierarchical
    questions (K > L(profile)) and reserves 1 + 3 calls per member. `escalate_budget_ms`: turns deadline_ms_eff +
    slack_ms; /v1/decisions the remaining ctx.deadline; None = no escalation (verification). `input_tokens` is the
    TokenEstimator estimate of the input."""

    profile_id: str
    questions: tuple[QuestionWork, ...]
    stage0_deadline_ms: int
    escalation: Literal["profile_default", "off"]
    escalate_budget_ms: int | None
    kind: Literal["decision_member", "verification_member"]
    input_tokens: int


@dataclass(frozen=True, slots=True)
class GenerationWork:
    """The generation part of a turn; `max_output_tokens` is the request's pin, if any."""

    candidate_profiles: tuple[str, ...]
    input_tokens: int
    max_output_tokens: int | None


@dataclass(frozen=True, slots=True)
class WorkGraph:
    """The worst-case calls and tokens of one request. `calls`: (model alias, kind) -> max calls;
    `tokens_by_model`: alias -> reserved input + output tokens (per-model quota buckets); the totals feed the key
    and project buckets; `streams`: 1 per turn, 0 for /v1/decisions."""

    calls: Mapping[tuple[str, CallKind], int]
    tokens_by_model: Mapping[str, int]
    total_calls: int
    tokens_in: int
    tokens_out: int
    streams: int


class WorkGraphPlanner(Protocol):
    def plan(
        self,
        snapshot: ConfigSnapshot,
        principal: Principal,
        *,
        decisions: Sequence[DecisionWork],
        generation: GenerationWork | None,
    ) -> WorkGraph:
        """Sync and pure (spec-11 §1.1)."""
        ...


class Reservation(Protocol):
    """One per request (spec-11 §1.2)."""

    def consume(self, kind: CallKind, model: str) -> None:
        """Sync, never raises. Above the reserved count is a bug: aghoo_reservation_overrun_total + an ERROR log
        (AssertionError under NOEMA_ENV=test); the call proceeds."""
        ...

    async def reconcile(self, usage: Mapping[str, UsageRecord]) -> None:
        """alias -> actual usage; refunds unused calls and tokens. Idempotent (only the first call counts)."""
        ...

    def headers(self) -> Mapping[str, str]:
        """Sync: the six x-ratelimit-* headers."""
        ...

    async def release(self) -> None:
        """Idempotent; frees the stream slot. Without a prior reconcile it refunds only unconsumed calls (tokens
        stay debited). Shielded (1 s)."""
        ...


class Admission(Protocol):
    async def reserve(
        self, principal: Principal, graph: WorkGraph, *, queue_timeout_s: float, deadline: Deadline | None = None
    ) -> Reservation:
        """Raises `NoemaError(rpm_exceeded | tpm_exceeded | queue_timeout)` with retry_after_s. Cancellation while
        queued leaves nothing debited. `deadline` is the request's deadline: a stream slot's lease expires at
        `deadline` + 30 s, so a dead pod's slot frees itself (spec-11 §1.2); callers reserving a stream pass
        `ctx.deadline`, and None bounds the lease by the implementation's maximum turn lifetime instead. Labeling
        jobs build their `WorkGraph` directly (there is no planner input type for them) and reserve with
        `system_principal`."""
        ...


class RateLimiter(Protocol):
    """A bare GCRA bucket outside the request work graph, e.g. the per-IP auth-failure bucket (spec-09 §2:
    `RedisKeys.gcra("authfail:" + client_ip)`, 10/s, burst 50). Implemented by ops-admission with the admission
    Lua script and the same Redis-down local fallback (limit ÷ NOEMA_EXPECTED_PODS, IMPL-078); the composition root
    injects it into the Authenticator. Backend failures never raise (the local fallback answers); ValueError for
    rate_per_s <= 0, burst < 1 or n < 1."""

    async def check(self, bucket: str, *, rate_per_s: float, burst: int) -> float | None:
        """None while `bucket` has capacity for one unit, else retry_after_s. Debits nothing."""
        ...

    async def debit(self, bucket: str, *, rate_per_s: float, burst: int, n: int = 1) -> float | None:
        """Debit `n` units: None = debited; else retry_after_s (the bucket lacked capacity; nothing debited)."""
        ...


class TokenEstimator(Protocol):
    def estimate(self, model_alias: str, system: Sequence[str], blocks: Sequence[ContentBlock]) -> int:
        """Sync (IMPL-029c)."""
        ...


@dataclass(frozen=True, slots=True)
class UsageLine:
    """One usage-ledger line (spec-11 §2). `model_id`/`region` are the Bedrock id and call region (None only for
    cache_savings); usage and cost are sums of per-call values (cost rounded per call, then summed); `at` None means
    `Clock.now()` at record()."""

    id: str
    tenant_id: str
    project_id: str
    line: UsageLineName
    model_id: str | None
    region: str | None
    usage: UsageRecord
    cost: Cost
    turn_id: str | None = None
    decision_id: str | None = None
    attempt_id: str | None = None
    session_id: str | None = None
    labeling_run_id: str | None = None
    at: datetime | None = None


class CostMeter(Protocol):
    def price(
        self, model_alias: str, region: str, usage: UsageRecord, *, snapshot: ConfigSnapshot | None = None
    ) -> Cost:
        """Sync (IMPL-019): priced with the catalog of `snapshot`, the caller's pinned config (decision, generation
        and labeling pass `ctx.snapshot`); None = the current snapshot, for callers without a pinned one. `region`
        is the tenant's `home_region` (spec-11 §2), never the serving region."""
        ...

    def record(self, line: UsageLine) -> None:
        """Sync: Outbox.put(kind="usage_line") + aghoo_cost_usd_total{tenant, line}."""
        ...
