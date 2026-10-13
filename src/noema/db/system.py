"""Typed calls of the spec-03 §4 SECURITY DEFINER functions (spec-02 §7.1 `SystemCalls`; IMPL-009, IMPL-044).

`PgSystemCalls` (from `Database.system_uow()`) wraps the functions granted to `noema_app` plus the two platform
reads; each call is one autocommitted statement on an unscoped connection, so RLS shows it no tenant row and only the
definer functions do any work. `PgAdminCalls` wraps `auth.lookup_operator` and `admin.*` for the `noema-admin` CLI
(a `noema_cli` connection; `bootstrap_operator` needs a `noema_owner` connection). Admin refusals raise `AdminError`
with a typed reason; statement errors other than those propagate as asyncpg errors.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from ipaddress import IPv4Network, IPv6Network
from typing import Final, Literal, cast

import asyncpg
import orjson

from noema.core.protocols.storage import FinishedJobState, Job, OutboxRecord, RetentionCandidate, Row
from noema.core.values import JsonValue
from noema.db.engine import DbConnection

_JOB_END_STATES: Final = frozenset({"done", "queued", "dead"})


def _row(record: asyncpg.Record | None) -> Row | None:
    return None if record is None else dict(record.items())


def _rows(records: Sequence[asyncpg.Record]) -> tuple[Row, ...]:
    return tuple(dict(r.items()) for r in records)


def outbox_json(record: OutboxRecord) -> dict[str, JsonValue]:
    """The JSON object `ops.outbox_apply` expects for one record."""
    return {
        "kind": record.kind,
        "id": record.id,
        "tenant_id": record.tenant_id,
        "payload": dict(record.payload),
        "session_id": record.session_id,
    }


class PgSystemCalls:
    """`SystemCalls` over one pooled noema_app connection without tenant scope."""

    def __init__(self, conn: DbConnection) -> None:
        self._conn = conn

    async def lookup_api_key(self, key_id: str) -> Row | None:
        """At most one row: tenant_id, project_id, principal_id, key_hash, pepper_version, env, scopes, roles,
        expires_at, revoked_at, ip_allowlist. None for an unknown key, a disabled principal, a deleted project or
        a deleted tenant (revoked_at/expires_at are the caller's checks)."""
        return _row(await self._conn.fetchrow("SELECT * FROM auth.lookup_api_key($1)", key_id))

    async def touch_api_key(self, key_id: str) -> None:
        """last_used_at = now(), at most once a minute per key."""
        await self._conn.execute("SELECT auth.touch_api_key($1)", key_id)

    async def rehash_api_key(self, key_id: str, key_hash: str, pepper_version: int) -> bool:
        """True iff the hash was replaced (only by a newer pepper version)."""
        return bool(
            await self._conn.fetchval("SELECT auth.rehash_api_key($1, $2, $3)", key_id, key_hash, pepper_version)
        )

    async def claim_jobs(self, kinds: Sequence[str], n: int, worker: str, lock_s: int) -> tuple[Job, ...]:
        """Up to n queued, due jobs of `kinds` (priority, run_after order; SKIP LOCKED), now running for `worker`."""
        records = await self._conn.fetch("SELECT * FROM jobs.claim($1, $2, $3, $4)", list(kinds), n, worker, lock_s)
        return tuple(
            Job(id=r["id"], tenant_id=r["tenant_id"], kind=r["kind"], payload=r["payload"], attempt=r["attempts"])
            for r in records
        )

    async def finish_job(
        self, job_id: str, worker: str, *, ok: bool, error: str | None, permanent: bool = False
    ) -> FinishedJobState | None:
        """done, or retry with backoff / dead (spec-11 §4): the new state; None (a no-op) unless `worker` still
        holds the job."""
        state = await self._conn.fetchval(
            "SELECT jobs.finish($1, $2, $3, $4, $5)", job_id, worker, ok, error, permanent
        )
        if state is None or state in _JOB_END_STATES:
            return cast("FinishedJobState | None", state)
        raise RuntimeError("jobs.finish returned an unknown state")

    async def extend_job(self, job_id: str, worker: str, lock_s: int) -> bool:
        """False = the lease is lost (reaped or claimed by another worker)."""
        return bool(await self._conn.fetchval("SELECT jobs.extend($1, $2, $3)", job_id, worker, lock_s))

    async def ensure_periodic(self, kind: str, every_s: int) -> None:
        """One queued/running '__platform__' job of `kind`, due at the next `every_s` boundary."""
        await self._conn.execute("SELECT jobs.ensure_periodic($1, $2::int * interval '1 second')", kind, every_s)

    async def outbox_apply(self, records: Sequence[OutboxRecord]) -> tuple[str | None, ...]:
        """Per record (input order): None = applied (or an idempotent replay), else an error code such as
        'seq_conflict', 'tenant_mismatch' or 'sqlstate_23503:<constraint>' (never payload values)."""
        if not records:
            return ()
        result = await self._conn.fetchval("SELECT ops.outbox_apply($1::jsonb)", [outbox_json(r) for r in records])
        if not isinstance(result, list) or len(result) != len(records):
            raise RuntimeError("ops.outbox_apply returned a malformed result")
        return tuple(None if code is None else str(code) for code in result)

    async def outbox_dead(self, record: OutboxRecord, error: str, *, attempts: int = 0) -> None:
        """Dead-letters the record (idempotent per (kind, id)); `attempts` = its delivery count."""
        await self._conn.execute(
            "SELECT ops.outbox_dead($1::jsonb, $2)", {**outbox_json(record), "attempts": attempts}, error
        )

    async def reap_stuck(self, older_than_s: int) -> int:
        """Requeues jobs whose lock lapsed more than older_than_s ago; deletes old finished jobs. The requeued count."""
        return int(await self._conn.fetchval("SELECT ops.reap_stuck($1::int * interval '1 second')", older_than_s))

    async def retention_candidates(self, limit: int) -> tuple[tuple[str, str, str], ...]:
        """(tenant_id, kind, id) of expired session/turn/decision/file/idempotency_record rows, oldest first."""
        records = await self._conn.fetch("SELECT * FROM ops.retention_candidates($1)", limit)
        return tuple((r["tenant_id"], r["kind"], r["id"]) for r in records)

    async def retention_page(
        self, limit: int, *, after: RetentionCandidate | None = None
    ) -> tuple[RetentionCandidate, ...]:
        """Expired rows in (expires_at, kind, id, tenant_id) order ("C" collation), strictly after `after`."""
        tenant, kind, row_id, at = after if after is not None else (None, None, None, None)
        records = await self._conn.fetch(
            "SELECT * FROM ops.retention_page($1, $2, $3, $4, $5)", limit, at, kind, row_id, tenant
        )
        return tuple((r["tenant_id"], r["kind"], r["id"], r["expires_at"]) for r in records)

    async def calibrator_epochs(self) -> Mapping[str, int]:
        """scope_key -> n for 'platform' and every live tenant (0 before the first change)."""
        records = await self._conn.fetch("SELECT * FROM ops.calibrator_epochs()")
        return {r["scope_key"]: int(r["n"]) for r in records}

    async def active_calibrators(self, scope_keys: Sequence[str]) -> tuple[Row, ...]:
        """The active artifacts of those scopes ('platform' = platform rows), without acceptance/reliability."""
        return _rows(await self._conn.fetch("SELECT * FROM ops.active_calibrators($1)", list(scope_keys)))

    async def active_config(self) -> Row | None:
        """platform.config_version WHERE status = 'active' (every column)."""
        return _row(await self._conn.fetchrow("SELECT * FROM platform.config_version WHERE status = 'active'"))

    async def price_catalog(self, version: int) -> Row | None:
        """platform.price_catalog of that version (every column)."""
        return _row(await self._conn.fetchrow("SELECT * FROM platform.price_catalog WHERE version = $1", version))

    # Beyond the frozen Protocol (labeling jobs, spec-03 §4): the noema_app grant of these two admin functions.
    async def set_calibrator_status(self, actor: str, calibrator_id: str, status: str, *, reason: str | None) -> None:
        """noema_app may only downgrade an active artifact (status 'downgraded', reason drift_ece|drift_ci);
        `actor` is 'system:<purpose>'. Raises `AdminError` (forbidden, not_found, invalid_argument)."""
        await _admin_call(
            self._conn, "SELECT admin.set_calibrator_status($1, $2, $3, $4)", actor, calibrator_id, status, reason
        )

    async def bump_epoch(self, actor: str, scope_key: str) -> int:
        """Increments the calibrator epoch of 'platform' or a tenant (notifies calibrators_changed); the new n."""
        return _as_int(await _admin_call(self._conn, "SELECT admin.bump_epoch($1, $2)", actor, scope_key))


# --- admin.* (noema-admin CLI) --------------------------------------------------------------------------------------
AdminReason = Literal[
    "operator_invalid", "two_person", "not_found", "invalid_state", "invalid_argument", "conflict", "forbidden"
]
_ADMIN_SQLSTATES: Final[Mapping[str, AdminReason]] = {
    "NOE10": "operator_invalid",
    "NOE11": "two_person",
    "NOE12": "not_found",
    "NOE13": "invalid_state",
    "NOE14": "invalid_argument",
    "NOE15": "conflict",
    "NOE16": "forbidden",
    "23505": "conflict",  # unique_violation: an existing name, key id or subject
}


class AdminError(Exception):
    """An admin.* function refused the call. `reason` is typed; `message` is the function's text (no secrets)."""

    def __init__(self, reason: AdminReason, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason: AdminReason = reason
        self.message: str = message


async def _admin_call(conn: DbConnection, query: str, *args: object) -> object:
    try:
        return await conn.fetchval(query, *args)
    except asyncpg.PostgresError as exc:
        reason = _ADMIN_SQLSTATES.get(getattr(exc, "sqlstate", "") or "")
        if reason is None:
            raise
        raise AdminError(reason, str(getattr(exc, "message", "") or exc)) from exc


async def _admin_rows(conn: DbConnection, query: str, *args: object) -> tuple[Row, ...]:
    try:
        return _rows(await conn.fetch(query, *args))
    except asyncpg.PostgresError as exc:
        reason = _ADMIN_SQLSTATES.get(getattr(exc, "sqlstate", "") or "")
        if reason is None:
            raise
        raise AdminError(reason, str(getattr(exc, "message", "") or exc)) from exc


def _as_int(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise RuntimeError("an admin function returned a non-integer result")


def _as_str(value: object) -> str:
    if isinstance(value, str):
        return value
    raise RuntimeError("an admin function returned a non-text result")


type Cidr = IPv4Network | IPv6Network


class PgAdminCalls:
    """The CLI's admin functions over one connection (noema_cli; noema_owner for `bootstrap_operator`). Each call
    is its own transaction; `operator_id` is the operator the CLI authenticated with `lookup_operator`, whose key
    `prove_operator` put on this connection (0003, Wave E: the database refuses every other operator id with
    operator_invalid)."""

    def __init__(self, conn: DbConnection) -> None:
        self._conn = conn

    async def prove_operator(self, key_id: str, secret: str, pepper_version: int, pepper: str) -> None:
        """Set this connection's operator proof (the session setting `noema.operator_key`), which `admin._operator`
        checks against the operator's key hash on every call. Only this connection can read it."""
        proof = orjson.dumps({"v": pepper_version, "p": pepper, "k": key_id, "s": secret}).decode()
        await self._conn.execute("SELECT pg_catalog.set_config('noema.operator_key', $1, false)", proof)

    async def lookup_operator(self, key_id: str) -> Row | None:
        """(id, name, key_hash, revoked_at) of the operator key, or None."""
        return _row(await self._conn.fetchrow("SELECT * FROM auth.lookup_operator($1)", key_id))

    async def bootstrap_operator(self, name: str, key_id: str, key_hash: str) -> str:
        """The first operator (one-time; noema_owner connection). Raises AdminError(conflict) once one exists."""
        return _as_str(
            await _admin_call(self._conn, "SELECT admin.bootstrap_operator($1, $2, $3)", name, key_id, key_hash)
        )

    async def create_operator(self, operator_id: str, name: str, key_id: str, key_hash: str) -> str:
        return _as_str(
            await _admin_call(
                self._conn, "SELECT admin.create_operator($1, $2, $3, $4)", operator_id, name, key_id, key_hash
            )
        )

    async def rehash_operator(self, operator_id: str, key_hash: str, pepper_version: int) -> bool:
        """Pepper rotation: replaces the calling operator's key hash (`hmac-sha256:v{pepper_version}:<hex>`) iff
        `pepper_version` is newer than the stored hash's (never a downgrade); True iff replaced (audited
        operator.rehash). A malformed hash raises AdminError(invalid_argument)."""
        return bool(
            await _admin_call(
                self._conn, "SELECT admin.rehash_operator($1, $2, $3)", operator_id, key_hash, pepper_version
            )
        )

    async def revoke_operator(self, operator_id: str, target_id: str) -> None:
        """Refuses (invalid_state) to revoke the last live operator."""
        await _admin_call(self._conn, "SELECT admin.revoke_operator($1, $2)", operator_id, target_id)

    async def config_propose(self, operator_id: str, digest: str, body: Mapping[str, JsonValue]) -> int:
        """Stores a validated config (version = body.config_version, above every stored one) as proposed."""
        return _as_int(
            await _admin_call(self._conn, "SELECT admin.config_propose($1, $2, $3::jsonb)", operator_id, digest, body)
        )

    async def config_approve(self, operator_id: str, version: int) -> None:
        """Activates a proposed version (approver != proposer); syncs tenants; notifies config_changed."""
        await _admin_call(self._conn, "SELECT admin.config_approve($1, $2)", operator_id, version)

    async def config_rollback(self, operator_id: str, version: int) -> None:
        """Re-proposes a superseded version; another operator activates it with config_approve."""
        await _admin_call(self._conn, "SELECT admin.config_rollback($1, $2)", operator_id, version)

    async def sync_tenants(self, operator_id: str) -> int:
        return _as_int(await _admin_call(self._conn, "SELECT admin.sync_tenants($1)", operator_id))

    async def list_config_versions(self, operator_id: str) -> tuple[Row, ...]:
        """Newest first; without bodies."""
        return await _admin_rows(self._conn, "SELECT * FROM admin.list_config_versions($1)", operator_id)

    async def config_body(self, operator_id: str, version: int) -> Mapping[str, JsonValue] | None:
        """The stored body of one config version; None for an unknown version."""
        body = await _admin_call(self._conn, "SELECT admin.config_body($1, $2)", operator_id, version)
        if body is None or isinstance(body, dict):
            return body
        raise RuntimeError("admin.config_body returned a non-object body")

    async def import_prices(self, operator_id: str, digest: str, body: Mapping[str, JsonValue]) -> int:
        """Stores a schema-validated prices file as the next catalog version; returns it (prices_ref db:<n>)."""
        return _as_int(
            await _admin_call(self._conn, "SELECT admin.import_prices($1, $2, $3::jsonb)", operator_id, digest, body)
        )

    async def create_project(
        self, operator_id: str, tenant_id: str, name: str, *, default_data_class: str = "internal"
    ) -> str:
        """A new project id ('prj_<ulid>') in a live tenant."""
        return _as_str(
            await _admin_call(
                self._conn,
                "SELECT admin.create_project($1, $2, $3, $4)",
                operator_id,
                tenant_id,
                name,
                default_data_class,
            )
        )

    async def create_principal(
        self, operator_id: str, tenant_id: str, project_id: str, subject: str, roles: Sequence[str]
    ) -> str:
        """A new principal id ('prn_<ulid>'); at least one role."""
        return _as_str(
            await _admin_call(
                self._conn,
                "SELECT admin.create_principal($1, $2, $3, $4, $5)",
                operator_id,
                tenant_id,
                project_id,
                subject,
                list(roles),
            )
        )

    async def create_api_key(
        self,
        operator_id: str,
        *,
        tenant_id: str,
        project_id: str,
        principal_id: str,
        key_id: str,
        key_hash: str,
        pepper_version: int,
        env: Literal["live", "test"],
        scopes: Sequence[str],
        ip_allowlist: Sequence[Cidr] | None = None,
        expires_at: datetime | None = None,
    ) -> None:
        """Stores a key minted by the CLI: key_id (bare ULID) and `hmac-sha256:v{pepper_version}:<hex>` only."""
        await _admin_call(
            self._conn,
            "SELECT admin.create_api_key($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
            operator_id,
            tenant_id,
            project_id,
            principal_id,
            key_id,
            key_hash,
            pepper_version,
            env,
            list(scopes),
            None if ip_allowlist is None else list(ip_allowlist),
            expires_at,
        )

    async def revoke_api_key(self, operator_id: str, key_id: str) -> None:
        """Sets revoked_at and notifies keys_revoked."""
        await _admin_call(self._conn, "SELECT admin.revoke_api_key($1, $2)", operator_id, key_id)

    async def list_projects(self, operator_id: str, tenant_id: str) -> tuple[Row, ...]:
        return await _admin_rows(self._conn, "SELECT * FROM admin.list_projects($1, $2)", operator_id, tenant_id)

    async def list_api_keys(
        self, operator_id: str, tenant_id: str, *, project_id: str | None = None
    ) -> tuple[Row, ...]:
        """Never includes key hashes."""
        return await _admin_rows(
            self._conn, "SELECT * FROM admin.list_api_keys($1, $2, $3)", operator_id, tenant_id, project_id
        )

    async def record_audit(
        self,
        operator_id: str,
        *,
        tenant_id: str | None,
        action: str,
        resource_kind: str,
        resource_id: str | None,
        details: Mapping[str, JsonValue] | None = None,
    ) -> str:
        """An audit row for a CLI action outside the database (tenant None = '__platform__'); its id."""
        return _as_str(
            await _admin_call(
                self._conn,
                "SELECT admin.record_audit($1, $2, $3, $4, $5, $6::jsonb)",
                operator_id,
                tenant_id,
                action,
                resource_kind,
                resource_id,
                dict(details or {}),
            )
        )

    async def calibrator_approve(self, operator_id: str, calibrator_id: str) -> None:
        """First person of the two-person activation (an accepted artifact)."""
        await _admin_call(self._conn, "SELECT admin.calibrator_approve($1, $2)", operator_id, calibrator_id)

    async def calibrator_activate(self, operator_id: str, calibrator_id: str) -> None:
        """Second person (!= the approver): active; the previous active artifact of the binding is retired."""
        await _admin_call(self._conn, "SELECT admin.calibrator_activate($1, $2)", operator_id, calibrator_id)

    async def calibrator_retire(
        self, operator_id: str, calibrator_id: str, *, reason: Literal["manual", "profile_version_retired"] = "manual"
    ) -> None:
        await _admin_call(self._conn, "SELECT admin.calibrator_retire($1, $2, $3)", operator_id, calibrator_id, reason)

    async def upsert_platform_calibrator(self, operator_id: str, row: Mapping[str, JsonValue]) -> str:
        """A platform artifact (noema.calibrator columns; status fitted|accepted|rejected); its id."""
        return _as_str(
            await _admin_call(self._conn, "SELECT admin.upsert_platform_calibrator($1, $2::jsonb)", operator_id, row)
        )

    async def set_calibrator_status(
        self, operator_id: str, calibrator_id: str, status: str, *, reason: str | None = None
    ) -> None:
        """fitted -> accepted|rejected, active -> downgraded (drift_*), any -> retired; never -> active."""
        await _admin_call(
            self._conn,
            "SELECT admin.set_calibrator_status($1, $2, $3, $4)",
            operator_id,
            calibrator_id,
            status,
            reason,
        )

    async def bump_epoch(self, operator_id: str, scope_key: str) -> int:
        return _as_int(await _admin_call(self._conn, "SELECT admin.bump_epoch($1, $2)", operator_id, scope_key))

    async def list_calibrators(
        self, operator_id: str, *, scope_key: str | None = None, status: str | None = None
    ) -> tuple[Row, ...]:
        """scope_key 'platform', a tenant id or None (all); newest first."""
        return await _admin_rows(
            self._conn, "SELECT * FROM admin.list_calibrators($1, $2, $3)", operator_id, scope_key, status
        )
