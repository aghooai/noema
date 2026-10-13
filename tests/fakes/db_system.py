"""`FakeSystemCalls` (spec-02 §7.1 `SystemCalls`, spec-03 §4): the SECURITY DEFINER calls over `FakeStore`.

Each call is its own (immediately applied) transaction and ignores RLS, like the definer functions. `outbox_apply`
follows spec-11 §3 per kind (inserts are ON CONFLICT DO NOTHING; `turn_finalize` updates the session iff `last_seq`
still matches, a replay of an applied record is a no-op, otherwise the error `seq_conflict`).
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import anyio.lowlevel

from noema.core.protocols.storage import FinishedJobState, Job, OutboxRecord, RetentionCandidate, Row
from noema.db.system import AdminError

from .db_repos import live_duplicate
from .db_store import PLATFORM_TENANT, FakeStore, pk_of

BACKOFF_BASE_S: Final = 30
RETENTION_TABLES: Final = ("session", "turn", "decision", "file", "idempotency_record")
_EPOCH_2000: Final = datetime(2000, 1, 1, tzinfo=UTC)


def _dt(value: Any) -> Any:
    """RFC 3339 strings of outbox payloads back to datetimes; other values unchanged."""
    if isinstance(value, str) and len(value) >= 20 and value[4] == "-" and value[10] == "T":
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    return value


class FakeSystemCalls:
    """Implements `SystemCalls` over one `FakeStore` (no RLS, like the definer functions)."""

    def __init__(self, store: FakeStore) -> None:
        self._store = store

    @property
    def _now(self) -> datetime:
        return self._store.now()

    async def lookup_api_key(self, key_id: str) -> Row | None:
        await anyio.lowlevel.checkpoint()
        k = self._store.table("api_key").get((key_id,))
        if k is None:
            return None
        p = self._store.table("principal").get((k["tenant_id"], k["principal_id"]))
        j = self._store.table("project").get((k["tenant_id"], k["project_id"]))
        t = self._store.table("tenant").get((k["tenant_id"],))
        if p is None or p.get("disabled_at") or j is None or j.get("deleted_at") or t is None or t.get("deleted_at"):
            return None
        cols = (
            "tenant_id",
            "project_id",
            "principal_id",
            "key_hash",
            "pepper_version",
            "env",
            "scopes",
            "expires_at",
            "revoked_at",
            "ip_allowlist",
        )
        return {**{c: copy.deepcopy(k.get(c)) for c in cols}, "roles": list(p["roles"])}

    async def touch_api_key(self, key_id: str) -> None:
        await anyio.lowlevel.checkpoint()
        k = self._store.table("api_key").get((key_id,))
        if k is not None and (k.get("last_used_at") is None or k["last_used_at"] < self._now - timedelta(minutes=1)):
            k["last_used_at"] = self._now

    async def rehash_api_key(self, key_id: str, key_hash: str, pepper_version: int) -> bool:
        await anyio.lowlevel.checkpoint()
        k = self._store.table("api_key").get((key_id,))
        if k is None or pepper_version <= k["pepper_version"]:
            return False
        k["key_hash"], k["pepper_version"] = key_hash, pepper_version
        return True

    async def claim_jobs(self, kinds: Sequence[str], n: int, worker: str, lock_s: int) -> tuple[Job, ...]:
        await anyio.lowlevel.checkpoint()
        now = self._now
        ready = sorted(
            (
                j
                for j in self._store.table("job").values()
                if j["state"] == "queued" and j["run_after"] <= now and j["kind"] in kinds
            ),
            key=lambda j: (j["priority"], j["run_after"], j["id"]),
        )[:n]
        claimed = []
        for j in ready:
            j.update(
                state="running",
                attempts=j["attempts"] + 1,
                locked_by=worker,
                locked_until=now + timedelta(seconds=lock_s),
                updated_at=now,
            )
            claimed.append(Job(j["id"], j["tenant_id"], j["kind"], copy.deepcopy(j["payload"]), j["attempts"]))
        return tuple(claimed)

    async def finish_job(
        self, job_id: str, worker: str, *, ok: bool, error: str | None, permanent: bool = False
    ) -> FinishedJobState | None:
        await anyio.lowlevel.checkpoint()
        j = self._store.table("job").get((job_id,))
        if j is None or j["state"] != "running" or j.get("locked_by") != worker:
            return None
        now = self._now
        state: FinishedJobState
        if ok:
            state, run_after = "done", j["run_after"]
        elif permanent or j["attempts"] >= j["max_attempts"]:
            state, run_after = "dead", j["run_after"]
        else:
            state, run_after = "queued", now + timedelta(seconds=BACKOFF_BASE_S * 2 ** (j["attempts"] - 1))
        j.update(
            state=state,
            run_after=run_after,
            locked_by=None,
            locked_until=None,
            updated_at=now,
            last_error=None if error is None else error[:200],
        )
        return state

    async def extend_job(self, job_id: str, worker: str, lock_s: int) -> bool:
        await anyio.lowlevel.checkpoint()
        j = self._store.table("job").get((job_id,))
        if j is None or j["state"] != "running" or j.get("locked_by") != worker:
            return False
        j["locked_until"] = self._now + timedelta(seconds=lock_s)
        return True

    async def ensure_periodic(self, kind: str, every_s: int) -> None:
        await anyio.lowlevel.checkpoint()
        jobs = self._store.table("job")
        if live_duplicate(jobs.values(), PLATFORM_TENANT, kind, kind) is not None:
            return
        now = self._now
        bins = (now - _EPOCH_2000) // timedelta(seconds=every_s)
        run_after = _EPOCH_2000 + timedelta(seconds=every_s) * (bins + 1)
        job_id = f"job_periodic_{kind}_{int(run_after.timestamp())}"
        self._store.seed(
            "job",
            {
                "id": job_id,
                "tenant_id": PLATFORM_TENANT,
                "kind": kind,
                "priority": 300,
                "payload": {},
                "state": "queued",
                "attempts": 0,
                "max_attempts": 5,
                "run_after": run_after,
                "locked_by": None,
                "locked_until": None,
                "dedupe_key": kind,
                "last_error": None,
                "created_at": now,
                "updated_at": now,
            },
        )

    # --- outbox (spec-11 §3) ---------------------------------------------------------------------------------------
    def _insert_new(self, table: str, row: Mapping[str, Any]) -> None:
        stored = {k: _dt(v) for k, v in row.items()}
        if table == "usage_line":
            stored["cost_usd"] = Decimal(str(stored["cost_usd"]))
        tbl = self._store.table(table)
        tbl.setdefault(pk_of(table, stored), stored)

    def _apply_finalize(self, record: OutboxRecord) -> str | None:
        p = record.payload
        turn = p["turn"]
        assert isinstance(turn, Mapping)
        turns = self._store.table("turn")
        exists = (record.tenant_id, turn["id"]) in turns
        lease = p.get("lease")
        if isinstance(lease, Mapping):
            session = self._store.table("session").get((record.tenant_id, str(lease["session_id"])))
            if session is None or session["last_seq"] != lease["last_seq"]:
                return None if exists else "seq_conflict"
            seq = int(str(lease["last_seq"])) + 1
            session.update(
                last_seq=seq,
                sticky_spec=copy.deepcopy(p.get("sticky_spec")),
                active_turn_id=None,
                lease_expires_at=None,
            )
            turn = {**turn, "session_id": lease["session_id"], "seq": seq}
        self._insert_new("turn", {"tenant_id": record.tenant_id, **turn})
        for table, key in (("attempt", "attempts"), ("decision", "decisions"), ("turn_file", "turn_files")):
            rows = p.get(key) or []
            assert isinstance(rows, list)
            for row in rows:
                assert isinstance(row, Mapping)
                self._insert_new(table, {"tenant_id": record.tenant_id, **row})
        return None

    def _apply(self, record: OutboxRecord) -> str | None:
        p = record.payload
        if record.kind in ("usage_line", "audit_event", "decision_row"):
            table = {"usage_line": "usage_line", "audit_event": "audit_event", "decision_row": "decision"}[record.kind]
            self._insert_new(table, {"tenant_id": record.tenant_id, "id": record.id, **p})
            return None
        if record.kind == "turn_finalize":
            return self._apply_finalize(record)
        if record.kind == "labeling_offer":
            jobs = self._store.table("job")
            if live_duplicate(jobs.values(), record.tenant_id, "labeling.item", record.id) is None:
                now = self._now
                self._store.seed(
                    "job",
                    {
                        "id": f"job_lab_{record.id}_{len(jobs)}",
                        "tenant_id": record.tenant_id,
                        "kind": "labeling.item",
                        "priority": 200,
                        "payload": copy.deepcopy(dict(p)),
                        "state": "queued",
                        "attempts": 0,
                        "max_attempts": 5,
                        "run_after": now,
                        "locked_by": None,
                        "locked_until": None,
                        "dedupe_key": record.id,
                        "last_error": None,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
            return None
        return "unknown_kind"

    async def outbox_apply(self, records: Sequence[OutboxRecord]) -> tuple[str | None, ...]:
        await anyio.lowlevel.checkpoint()
        return tuple(self._apply(r) for r in records)

    async def outbox_dead(self, record: OutboxRecord, error: str, *, attempts: int = 0) -> None:
        await anyio.lowlevel.checkpoint()
        table = self._store.table("outbox_dead_letter")
        table.setdefault(
            (record.kind, record.id),
            {
                "kind": record.kind,
                "id": record.id,
                "tenant_id": record.tenant_id,
                "payload": copy.deepcopy(dict(record.payload)),
                "error": error,
                "attempts": attempts,
                "created_at": self._now,
            },
        )

    # --- reaper, retention, calibrators, platform reads --------------------------------------------------------------
    async def reap_stuck(self, older_than_s: int) -> int:
        await anyio.lowlevel.checkpoint()
        now = self._now
        requeued = 0
        jobs = self._store.table("job")
        for j in list(jobs.values()):
            if j["state"] == "running" and j["locked_until"] < now - timedelta(seconds=older_than_s):
                state = "dead" if j["attempts"] >= j["max_attempts"] else "queued"
                j.update(state=state, locked_by=None, locked_until=None, updated_at=now)
                requeued += state == "queued"
            elif (j["state"] == "done" and j["updated_at"] < now - timedelta(days=7)) or (
                j["state"] == "dead" and j["updated_at"] < now - timedelta(days=30)
            ):
                jobs.pop((j["id"],))
        return requeued

    async def retention_candidates(self, limit: int) -> tuple[tuple[str, str, str], ...]:
        await anyio.lowlevel.checkpoint()
        now = self._now
        due = [
            (r["expires_at"], r["tenant_id"], table, r["id"])
            for table in RETENTION_TABLES
            for r in self._store.table(table).values()
            if r.get("expires_at") is not None and r["expires_at"] < now
        ]
        return tuple((t, k, i) for _, t, k, i in sorted(due)[:limit])

    async def retention_page(
        self, limit: int, *, after: RetentionCandidate | None = None
    ) -> tuple[RetentionCandidate, ...]:
        await anyio.lowlevel.checkpoint()
        now = self._now
        due = sorted(
            (r["expires_at"], table, r["id"], r["tenant_id"])
            for table in RETENTION_TABLES
            for r in self._store.table(table).values()
            if r.get("expires_at") is not None and r["expires_at"] < now
        )  # (expires_at, kind, id, tenant_id); Python str order = the "C" collation of ops.retention_page
        start = None if after is None else (after[3], after[1], after[2], after[0])
        return tuple((t, k, i, at) for at, k, i, t in due if start is None or (at, k, i, t) > start)[:limit]

    async def calibrator_epochs(self) -> Mapping[str, int]:
        await anyio.lowlevel.checkpoint()
        return {r["scope_key"]: int(r["n"]) for r in self._store.table("calibrator_epoch").values()}

    async def active_calibrators(self, scope_keys: Sequence[str]) -> tuple[Row, ...]:
        await anyio.lowlevel.checkpoint()

        def scope_key(r: Mapping[str, Any]) -> str:
            return "platform" if r["scope"] == "platform" else str(r["tenant_id"])

        rows = [
            r
            for r in self._store.table("calibrator").values()
            if r["status"] == "active" and scope_key(r) in scope_keys
        ]
        hidden = ("acceptance", "reliability")
        return tuple({k: copy.deepcopy(v) for k, v in r.items() if k not in hidden} for r in rows)

    async def active_config(self) -> Row | None:
        await anyio.lowlevel.checkpoint()
        rows = [r for r in self._store.table("config_version").values() if r["status"] == "active"]
        return copy.deepcopy(rows[0]) if rows else None

    async def price_catalog(self, version: int) -> Row | None:
        await anyio.lowlevel.checkpoint()
        row = self._store.table("price_catalog").get((version,))
        return None if row is None else copy.deepcopy(row)

    # --- the noema_app grants of admin.* (labeling drift job, spec-03 §4) --------------------------------------------
    async def set_calibrator_status(self, actor: str, calibrator_id: str, status: str, *, reason: str | None) -> None:
        """As noema_app: only active -> downgraded (reason drift_ece | drift_ci); bumps the scope's epoch."""
        await anyio.lowlevel.checkpoint()
        _system_actor(actor)
        row = self._store.table("calibrator").get((calibrator_id,))
        if row is None:
            raise AdminError("not_found", "not_found: calibrator")
        if not (row["status"] == "active" and status == "downgraded"):
            raise AdminError("forbidden", "forbidden: noema_app may only downgrade an active calibrator")
        if reason not in ("drift_ece", "drift_ci"):
            raise AdminError("invalid_argument", "invalid_argument: a downgrade reason is drift_ece or drift_ci")
        row.update(status="downgraded", downgraded_at=self._now, downgrade_reason=reason)
        self._bump("platform" if row["scope"] == "platform" else str(row["tenant_id"]))

    async def bump_epoch(self, actor: str, scope_key: str) -> int:
        """As noema_app: increments the epoch of 'platform' or a live tenant; the new n."""
        await anyio.lowlevel.checkpoint()
        _system_actor(actor)
        tenant = self._store.table("tenant").get((scope_key,))
        if scope_key != "platform" and (tenant is None or tenant.get("deleted_at") is not None):
            raise AdminError("not_found", "not_found: scope")
        return self._bump(scope_key)

    def _bump(self, scope_key: str) -> int:
        epochs = self._store.table("calibrator_epoch")
        row = epochs.setdefault((scope_key,), {"scope_key": scope_key, "n": 0})
        row["n"] = int(row["n"]) + 1
        return int(row["n"])


def _system_actor(actor: str) -> None:
    if not actor.startswith("system:"):
        raise AdminError("invalid_argument", "invalid_argument: a noema_app actor is 'system:<purpose>'")


def seed_identity(
    store: FakeStore,
    *,
    tenant_id: str,
    project_id: str,
    principal_id: str,
    roles: Sequence[str],
    aws_account_id: str = "111122223333",
) -> None:
    """Seed a tenant, project and principal (fixtures for authn, sessions and the definer calls)."""
    now = store.now()
    store.seed("tenant", {"id": tenant_id, "aws_account_id": aws_account_id, "created_at": now, "deleted_at": None})
    store.seed(
        "project",
        {
            "tenant_id": tenant_id,
            "id": project_id,
            "name": project_id,
            "default_data_class": "internal",
            "created_at": now,
            "deleted_at": None,
        },
    )
    store.seed(
        "principal",
        {
            "tenant_id": tenant_id,
            "project_id": project_id,
            "id": principal_id,
            "subject": principal_id,
            "roles": list(roles),
            "created_at": now,
            "disabled_at": None,
        },
    )
