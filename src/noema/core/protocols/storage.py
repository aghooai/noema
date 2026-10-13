"""Storage, jobs, idempotency and the outbox (spec-02 §7, §7.1; spec-03, spec-11).

Every repository method runs inside the enclosing `UnitOfWork` transaction under its RLS scope. Rows use spec-03
column names. `get` returns None for a missing, deleted or invisible row; the caller maps it to its 404 code.
Pagination is keyset on the stated order: `after` is the opaque `next_cursor` of the previous page; limit 1..200
(default 50); a malformed cursor raises `NoemaError(schema_violation, param="after")`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Protocol

from noema.core.clock import Deadline
from noema.core.principal import Principal
from noema.core.values import JsonValue

# keys = spec-03 column names; values: str, int, bool, float, Decimal, datetime (UTC), JsonValue for jsonb, bytes for
# bytea, list for arrays. tenant_id/project_id are filled from the uow scope.
type Row = Mapping[str, object]


@dataclass(frozen=True, slots=True)
class Page[T]:
    """One page of a keyset-paginated list."""

    data: tuple[T, ...]
    has_more: bool
    next_cursor: str | None


# --- sessions and turns ----------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SessionLease:
    """A held session lease (spec-04 §2)."""

    session_id: str
    turn_id: str
    last_seq: int
    sticky_spec: Mapping[str, JsonValue] | None


@dataclass(frozen=True, slots=True)
class Busy:
    """Another live lease holds the session."""


class LeaseLost(Exception):
    """`finalize_turn` found the lease taken over (the uow rolls back)."""


class SessionRepo(Protocol):
    async def create(self, row: Row) -> Row: ...

    async def get(self, session_id: str) -> Row | None: ...

    async def update(self, session_id: str, **changes: object) -> Row:
        """metadata, data_class, retention, expires_at. Raises `NoemaError(session_not_found)`."""
        ...

    async def list(self, *, owner_principal_id: str | None, after: str | None, limit: int) -> Page[Row]:
        """id DESC (newest first)."""
        ...

    async def mark_deleted(self, session_id: str) -> None:
        """deleted_at = now(); the session.delete job deletes the data."""
        ...

    async def delete(self, session_id: str) -> None:
        """Hard-deletes the session row, deleted or not (the session.delete job and the retention sweeper, spec-03
        §5). Its turns must be deleted first (`TurnRepo.delete_for_session`; the turn foreign key fails otherwise).
        Absent: no-op."""
        ...

    async def acquire(
        self, session_id: str, turn_id: str, *, expected_seq: int | None, lease_s: int
    ) -> SessionLease | Busy:
        """The spec-04 §2 UPDATE. 0 rows: `NoemaError(session_not_found)` (missing/deleted/foreign),
        `NoemaError(session_conflict)` (expected_seq != last_seq), or `Busy`. The caller checks
        `RedisKeys.session_pending(sid)` first: a pending outbox finalize is Busy even when the lease expired."""
        ...

    async def renew(self, lease: SessionLease, lease_s: int) -> bool:
        """False = the lease was taken over."""
        ...

    async def finalize_turn(
        self,
        lease: SessionLease,
        turn: Row,
        attempts: Sequence[Row],
        decisions: Sequence[Row],
        turn_files: Sequence[Row],
        sticky_spec: Mapping[str, JsonValue] | None,
    ) -> int:
        """INSERT turn (seq = lease.last_seq + 1), attempts, decisions, turn_files; UPDATE the session (last_seq,
        sticky_spec, active_turn_id = NULL, lease_expires_at = NULL) WHERE id AND active_turn_id = lease.turn_id AND
        last_seq = lease.last_seq; 0 rows raises `LeaseLost`. Returns the new last_seq. The caller publishes
        session_released after commit."""
        ...

    async def release(self, lease: SessionLease) -> None:
        """active_turn_id, lease_expires_at = NULL iff active_turn_id = lease.turn_id."""
        ...


class TurnRepo(Protocol):
    async def get(self, turn_id: str) -> Row | None: ...

    async def insert(
        self, turn: Row, attempts: Sequence[Row], decisions: Sequence[Row], turn_files: Sequence[Row]
    ) -> None:
        """store: true without a session (session_id and seq NULL)."""
        ...

    async def list_for_session(self, session_id: str, *, after: str | None, limit: int) -> Page[Row]:
        """seq ASC."""
        ...

    async def recent(self, session_id: str, *, max_seq: int, limit: int) -> tuple[Row, ...]:
        """seq DESC, seq <= max_seq."""
        ...

    async def delete(self, turn_id: str) -> None:
        """Cascades to attempt and turn_file."""
        ...

    async def delete_for_session(self, session_id: str) -> int: ...


class AttemptRepo(Protocol):
    async def list_for_turn(self, turn_id: str) -> tuple[Row, ...]:
        """started_at ASC."""
        ...


class DecisionRepo(Protocol):
    async def insert(self, row: Row) -> None:
        """/v1/decisions and the facade (finalize)."""
        ...

    async def get(self, decision_id: str) -> Row | None: ...

    async def delete(self, decision_id: str) -> None: ...

    async def delete_for_session(self, session_id: str) -> int: ...


class FileRepo(Protocol):
    async def create(self, row: Row) -> Row: ...

    async def get(self, file_id: str) -> Row | None: ...

    async def update(self, file_id: str, **changes: object) -> Row:
        """status, media_type, rejection, failure, pages, deleted_at."""
        ...

    async def list_for_session(self, session_id: str) -> tuple[Row, ...]: ...

    async def delete(self, file_id: str) -> None:
        """Cascades to derivative rows; S3 objects are removed by the caller."""
        ...


class DerivativeRepo(Protocol):
    async def upsert(self, row: Row) -> None:
        """Key (file_id, kind). Column `facts` (jsonb, closed per kind): pdf_sanitized `{"active_content": [...]}`
        (the distinct spec-09 §6.3 fact values, sorted), NULL for every other kind."""
        ...

    async def list_for_file(self, file_id: str) -> tuple[Row, ...]: ...


class UsageRepo(Protocol):
    """Rows are written only by `SystemCalls.outbox_apply`."""

    async def report(
        self,
        *,
        group_by: Literal["project", "session", "model", "day", "line"],
        day_from: date,
        day_to: date,
        after: str | None,
        limit: int,
    ) -> Page[Row]:
        """spec-11 §2; rows {key, line, <Usage fields>}."""
        ...


class LabelRepo(Protocol):
    async def create_run(self, row: Row) -> Row: ...

    async def update_run(self, run_id: str, **changes: object) -> Row: ...

    async def open_run(
        self, profile_id: str, profile_version: str, question_name: str, question_signature: str | None = None
    ) -> Row | None:
        """The running run of that question definition (at most one per name and signature), if any.
        `question_signature` None (deprecated, the pre-C2a lookup): the newest running run of the name, whatever
        its signature."""
        ...

    async def insert_item(self, row: Row) -> None: ...

    async def items(self, run_id: str, *, kept: bool | None = None) -> tuple[Row, ...]: ...

    async def items_for_decision(self, decision_id: str) -> tuple[Row, ...]:
        """Every item of the decision, in any run (a retried labeling.item finds what an earlier attempt stored),
        id ASC."""
        ...

    async def kept_items(
        self,
        profile_id: str,
        profile_version: str,
        question_signature: str,
        since: datetime,
        sample_reason: Literal["uniform", "uncertainty"] | None = "uniform",
    ) -> tuple[Row, ...]:
        """The kept items with created_at >= since of every run of that definition (profile, version, question
        signature), created_at then id ASC; `sample_reason` None = both reasons (the fit and drift windows)."""
        ...

    async def list_runs(
        self,
        *,
        profile_id: str | None,
        question_name: str | None,
        status: str | None,
        after: str | None,
        limit: int,
    ) -> Page[Row]:
        """id DESC."""
        ...

    async def list_dropped(
        self, *, run_id: str | None, question_name: str | None, reason: str | None, after: str | None, limit: int
    ) -> Page[Row]:
        """id DESC, kept = false."""
        ...


class CalibratorRepo(Protocol):
    """Writes touch tenant rows only (RLS); platform rows change through admin.* functions."""

    async def insert(self, row: Row) -> None:
        """status fitted."""
        ...

    async def get(self, calibrator_id: str) -> Row | None: ...

    async def update(self, calibrator_id: str, **changes: object) -> Row:
        """status accepted|rejected (+ acceptance, holdout)."""
        ...

    async def list(
        self, *, profile_id: str | None, question_name: str | None, status: str | None, after: str | None, limit: int
    ) -> Page[Row]:
        """Tenant + platform rows, id DESC."""
        ...


class IdempotencyRepo(Protocol):
    """Used only by the IdempotencyStore implementation (spec-11 §5).

    Time: `insert_in_flight` takes absolute timestamps from the store's `Clock` (row columns `lease_expires_at`,
    `expires_at`); `take_over`, `renew` and the expiry checks use the database clock (`now()` + seconds). Real kits
    that drive leases with a virtual clock age the stored columns instead (the db-repos `AgingClock` pattern).
    """

    async def insert_in_flight(self, row: Row) -> bool:
        """False on a (project, scope_hash, key) conflict."""
        ...

    async def get_by_key(self, scope_hash: str, key: str) -> Row | None: ...

    async def take_over(
        self,
        record_id: str,
        *,
        owner: str,
        fingerprint: str,
        lease_s: int,
        ttl_s: int,
        resource_id: str | None = None,
    ) -> bool:
        """Conditional UPDATE: makes the record a fresh in_flight record of `owner` (new fingerprint, lease, TTL,
        `resource_id` = the new request's turn/decision id or NULL; the old response cleared) iff it is still
        expired, not_stored, or in_flight with a lapsed lease. False = a racing taker won (exactly one wins) or the
        record is gone."""
        ...

    async def renew(self, record_id: str, *, owner: str, lease_s: int) -> bool:
        """lease_expires_at = now() + lease_s while `owner` holds the in_flight record; False = lease lost."""
        ...

    async def finish(self, record_id: str, *, owner: str, response: StoredResponse | None) -> None:
        """None -> not_stored, else completed. A no-op unless `owner` still holds the in_flight record (a late
        call never downgrades a completed record). Bodies over `IDEM_INLINE_MAX_BYTES` (1 MiB) are stored by key
        (`response_body_key` = `<tenant_id>/idempotency/<record_id>`, `response_body` NULL)."""
        ...

    async def delete(self, record_id: str) -> None: ...


class AuditRepo(Protocol):
    async def insert(self, row: Row) -> None:
        """Synchronous audit; most audit events go through the outbox."""
        ...


class ProjectRepo(Protocol):
    async def get(self, project_id: str) -> Row | None: ...


class JobRepo(Protocol):
    """Used only by the JobQueue implementation."""

    async def insert(self, row: Row) -> str:
        """ON CONFLICT (job_dedupe) returns the existing id."""
        ...


class UnitOfWork(Protocol):
    """One transaction: `set_config('app.tenant_id', $1, true)` and `set_config('app.project_id', $2, true)` with
    bind parameters (never a string-built SET LOCAL); commit on clean exit, rollback on exception."""

    @property
    def sessions(self) -> SessionRepo: ...
    @property
    def turns(self) -> TurnRepo: ...
    @property
    def attempts(self) -> AttemptRepo: ...
    @property
    def decisions(self) -> DecisionRepo: ...
    @property
    def files(self) -> FileRepo: ...
    @property
    def derivatives(self) -> DerivativeRepo: ...
    @property
    def usage(self) -> UsageRepo: ...
    @property
    def labels(self) -> LabelRepo: ...
    @property
    def calibrators(self) -> CalibratorRepo: ...
    @property
    def idempotency(self) -> IdempotencyRepo: ...
    @property
    def audit(self) -> AuditRepo: ...
    @property
    def projects(self) -> ProjectRepo: ...
    @property
    def jobs(self) -> JobRepo: ...


class Database(Protocol):
    """Transaction factories. All three raise `NoemaError(internal_error)` (retryable) when Postgres is unreachable
    (pool timeout 2 s)."""

    def uow(self, principal: Principal) -> AbstractAsyncContextManager[UnitOfWork]:
        """app.project_id = principal.project_id, or '' (tenant-wide) iff roles ∩ {tenant_admin, auditor}
        (spec-03 §3)."""
        ...

    def tenant_uow(self, tenant_id: str, *, project_id: str | None = None) -> AbstractAsyncContextManager[UnitOfWork]:
        """Jobs, sweepers, drainer follow-ups and labeling: no principal; project_id None = tenant-wide."""
        ...

    def system_uow(self) -> AbstractAsyncContextManager[SystemCalls]:
        """May only call SECURITY DEFINER functions (spec-03 §4)."""
        ...


# --- jobs ----------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Job:
    """A claimed job; `attempt` is 1 on the first run."""

    id: str
    tenant_id: str
    kind: str
    payload: Mapping[str, JsonValue]
    attempt: int


type FinishedJobState = Literal["done", "queued", "dead"]  # `SystemCalls.finish_job`


class PermanentJobError(Exception):
    """Raised by a handler: the job becomes dead at once, without retry."""


class Worker(Protocol):
    async def run(self) -> None:
        """Claims and runs jobs until cancelled; the lease is extended every 20 s. Handler return: done; any other
        exception: retry with backoff, dead after max_attempts (spec-11 §4); a lost lease (extend returns False)
        cancels the handler. On cancellation running handlers get 10 s, then their jobs are left to the reaper."""
        ...


class JobQueue(Protocol):
    """Postgres SKIP LOCKED plus LISTEN/NOTIFY wakeup."""

    async def enqueue(
        self,
        kind: str,
        tenant_id: str,
        payload: Mapping[str, JsonValue],
        *,
        priority: int = 100,
        run_after: datetime | None = None,
        dedupe_key: str | None = None,
        max_attempts: int = 5,
    ) -> str:
        """INSERT under tenant_uow(tenant_id) plus pg_notify('jobs', kind). A live duplicate (same tenant, kind and
        dedupe_key, queued or running) returns the existing job id and inserts nothing. Lower priority value =
        claimed first. `max_attempts` >= 1 (media.process uses 2, spec-07 §3.1)."""
        ...

    def worker(self, kinds: Sequence[str], handler: Callable[[Job], Awaitable[None]], *, concurrency: int) -> Worker:
        """A worker for `kinds`; nothing runs until `Worker.run()`."""
        ...


# --- idempotency (IMPL-077; state machine spec-11 §5) ------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class StoredResponse:
    """A stored response; `sse_turn_id` is set for an SSE turn response (replay attaches to the event log)."""

    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes | None
    sse_turn_id: str | None


@dataclass(frozen=True, slots=True)
class IdemToken:
    scope_hash: str
    key: str
    record_id: str


@dataclass(frozen=True, slots=True)
class New:
    """This request owns the key."""

    token: IdemToken


@dataclass(frozen=True, slots=True)
class InFlight:
    """The original is still running; `resource_id` is its turn/decision id (SSE attach)."""

    token: IdemToken
    resource_id: str | None


@dataclass(frozen=True, slots=True)
class Replay:
    """The original completed; replay its response."""

    response: StoredResponse


@dataclass(frozen=True, slots=True)
class Conflict:
    """Same key, different request fingerprint (409 idempotency_key_reused)."""


class IdempotencyStore(Protocol):
    async def begin(
        self,
        principal: Principal,
        method: str,
        route: str,
        key: str,
        fingerprint: str,
        *,
        resource_id: str | None = None,
    ) -> New | InFlight | Replay | Conflict:
        """`route` is the openapi path template; `resource_id` is the id pre-generated for this request."""
        ...

    async def await_result(self, token: IdemToken, *, deadline: Deadline) -> StoredResponse:
        """Raises `NoemaError(deadline_exceeded)` at the deadline; `NoemaError(internal_error)` (retryable) if the
        record turns not_stored or its lease lapses while waiting."""
        ...

    async def heartbeat(self, token: IdemToken) -> bool:
        """Renews the 30 s lease (call every 10 s); False = lease lost."""
        ...

    async def complete(self, token: IdemToken, resp: StoredResponse | None) -> None:
        """None = not stored (no model call). Shielded (2 s), idempotent. Bodies over 1 MiB go to S3
        `<tenant_id>/idempotency/<record_id>` before the row update; replays read them back (IMPL-085)."""
        ...


# --- outbox (IMPL-022, spec-11 §3) ---------------------------------------------------------------------------------
OutboxKind = Literal["usage_line", "audit_event", "labeling_offer", "decision_row", "turn_finalize"]


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    """`id` is the target primary key (usage-line, audit, decision or turn id; labeling_offer: the decision id).
    Payload timestamps are RFC 3339 UTC and Decimals strings. `session_id` is for turn_finalize only."""

    kind: OutboxKind
    id: str
    tenant_id: str
    payload: Mapping[str, JsonValue]
    session_id: str | None = None


class Outbox(Protocol):
    """Write-behind for records that need not block the response."""

    def put(self, record: OutboxRecord) -> None:
        """Sync, never raises; a batch writer sends it to Postgres, else to the Redis outbox stream."""
        ...

    async def flush(self, timeout_s: float) -> None:
        """Graceful shutdown; whatever is left after the timeout is logged as lost."""
        ...


# --- system calls (spec-03 §4) -------------------------------------------------------------------------------------
type RetentionCandidate = tuple[str, str, str, datetime]  # (tenant_id, kind, id, expires_at); kind = the table name


class SystemCalls(Protocol):
    """`Database.system_uow()`: the SECURITY DEFINER functions granted to noema_app plus two platform reads; each
    call is its own transaction."""

    async def lookup_api_key(self, key_id: str) -> Row | None: ...

    async def touch_api_key(self, key_id: str) -> None: ...

    async def rehash_api_key(self, key_id: str, key_hash: str, pepper_version: int) -> bool: ...

    async def claim_jobs(self, kinds: Sequence[str], n: int, worker: str, lock_s: int) -> tuple[Job, ...]: ...

    async def finish_job(
        self, job_id: str, worker: str, *, ok: bool, error: str | None, permanent: bool = False
    ) -> FinishedJobState | None:
        """ok -> done; else dead (permanent, or attempts >= max_attempts) or queued again with backoff (spec-11 §4).
        The job's new state; None = `worker` no longer held the job (nothing changed)."""
        ...

    async def extend_job(self, job_id: str, worker: str, lock_s: int) -> bool: ...

    async def ensure_periodic(self, kind: str, every_s: int) -> None: ...

    async def outbox_apply(self, records: Sequence[OutboxRecord]) -> tuple[str | None, ...]:
        """Per record: None = applied, else the error."""
        ...

    async def outbox_dead(self, record: OutboxRecord, error: str, *, attempts: int = 0) -> None:
        """Dead-letters the record (idempotent per (kind, id)); `attempts` = its delivery count."""
        ...

    async def reap_stuck(self, older_than_s: int) -> int: ...

    async def retention_candidates(self, limit: int) -> tuple[tuple[str, str, str], ...]:
        """(tenant_id, kind, id), oldest first. Superseded by `retention_page` (rows that keep failing stay at the
        head of every call here)."""
        ...

    async def retention_page(
        self, limit: int, *, after: RetentionCandidate | None = None
    ) -> tuple[RetentionCandidate, ...]:
        """Up to `limit` expired session/turn/decision/file/idempotency_record rows in (expires_at, kind, id,
        tenant_id) order (text compared bytewise), strictly after `after` (the last candidate of the previous page;
        None = from the oldest)."""
        ...

    async def calibrator_epochs(self) -> Mapping[str, int]:
        """scope_key -> n."""
        ...

    async def active_calibrators(self, scope_keys: Sequence[str]) -> tuple[Row, ...]: ...

    async def active_config(self) -> Row | None:
        """platform.config_version WHERE status = 'active'."""
        ...

    async def price_catalog(self, version: int) -> Row | None:
        """platform.price_catalog."""
        ...

    async def set_calibrator_status(self, actor: str, calibrator_id: str, status: str, *, reason: str | None) -> None:
        """The noema_app grant of `admin.set_calibrator_status` (labeling drift job, spec-03 §4): only active ->
        'downgraded' (reason drift_ece | drift_ci); `actor` = 'system:<purpose>'. Refusals raise the db layer's
        admin error (reasons forbidden | not_found | invalid_argument)."""
        ...

    async def bump_epoch(self, actor: str, scope_key: str) -> int:
        """Increments the calibrator epoch of 'platform' or a tenant id (notifies calibrators_changed); the new n."""
        ...
