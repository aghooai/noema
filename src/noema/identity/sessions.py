"""Sessions (spec-08 §9, spec-09 §3, spec-03 §2/§5): create, list, get, update, delete, history.

Authorization follows spec-09 §3: the scope/role steps first (`Authorizer.check(who, action)`), then the session is
loaded under `uow(principal)` (RLS) and checked as a resource (owner; tenant_admin and auditor tenant-wide).
`listSessions` filters by `Authorizer.list_scope` ("none" = an empty page). Retention: `expires_at` = `created_at` +
`platform.data_classes.<class>.retain_days` for `default`, + 24 h for `ephemeral`; `custom` is 422 in Phase 1.
Deletion: the `session.delete` job is enqueued first (dedupe on the session id), then the session is marked deleted,
so a failure between the two still deletes the data (`jobs.delete_session_data`).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Final, cast

from noema.core.clock import Clock
from noema.core.errors import ErrorCode, NoemaError
from noema.core.ids import IdGen
from noema.core.principal import Principal
from noema.core.protocols.auth import Action, Authorizer, ResourceRef
from noema.core.protocols.config import ConfigSnapshot
from noema.core.protocols.storage import Database, JobQueue, Row, UnitOfWork
from noema.core.values import DataClass, JsonValue
from noema.identity.history import history_page

DELETE_JOB_KIND: Final = "session.delete"
EPHEMERAL_RETAIN: Final = timedelta(hours=24)
DATA_CLASSES: Final = frozenset({"public", "internal", "confidential", "restricted"})
# Session metadata bounds (Wave E; the contract leaves them open, the 50 MiB body cap was the only one).
METADATA_MAX_PAIRS: Final = 16
METADATA_MAX_KEY_CHARS: Final = 64
METADATA_MAX_VALUE_CHARS: Final = 512


def _epoch(value: object) -> int | None:
    return int(value.timestamp()) if isinstance(value, datetime) else None


def session_wire(row: Row) -> dict[str, JsonValue]:
    """The contract `Session` of a session row (times in Unix seconds)."""
    metadata = row.get("metadata")
    return {
        "id": str(row["id"]),
        "created_at": _epoch(row.get("created_at")) or 0,
        "last_seq": last_seq if isinstance(last_seq := row.get("last_seq"), int) else 0,
        "owner": str(row["owner_principal_id"]),
        "data_class": str(row["data_class"]),
        "retention": {"policy": str(row.get("retention") or "default"), "expires_at": _epoch(row.get("expires_at"))},
        "metadata": {str(k): str(v) for k, v in metadata.items()} if isinstance(metadata, Mapping) else {},
    }


def expires_at(created_at: datetime, data_class: str, retention: str, snapshot: ConfigSnapshot) -> datetime:
    """The session's expiry for its retention policy and data class."""
    if retention == "ephemeral":
        return created_at + EPHEMERAL_RETAIN
    if data_class not in DATA_CLASSES:
        raise ValueError(f"unknown data class {data_class!r}")
    days = snapshot.platform.data_classes.for_class(cast(DataClass, data_class)).retain_days
    return created_at + timedelta(days=days)


def _check_retention(body: Mapping[str, JsonValue]) -> str | None:
    retention = body.get("retention")
    if retention == "custom":
        raise NoemaError(
            ErrorCode.FEATURE_NOT_AVAILABLE, "Custom retention is not available in this phase.", param="retention"
        )
    return retention if isinstance(retention, str) else None


def _check_metadata(body: Mapping[str, JsonValue]) -> Mapping[str, JsonValue] | None:
    """The body's metadata within the bounds (400 `schema_violation`), None when absent."""
    metadata = body.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    if len(metadata) > METADATA_MAX_PAIRS or any(len(k) > METADATA_MAX_KEY_CHARS for k in metadata):
        raise NoemaError(
            ErrorCode.SCHEMA_VIOLATION,
            f"metadata takes at most {METADATA_MAX_PAIRS} pairs with keys of at most {METADATA_MAX_KEY_CHARS}"
            " characters.",
            param="metadata",
        )
    for k, v in metadata.items():
        if isinstance(v, str) and len(v) > METADATA_MAX_VALUE_CHARS:
            raise NoemaError(
                ErrorCode.SCHEMA_VIOLATION,
                f"metadata values are at most {METADATA_MAX_VALUE_CHARS} characters.",
                param=f"metadata.{k}",
            )
    return metadata


def _ref(row: Row) -> ResourceRef:
    return ResourceRef("session", str(row["id"]), str(row["project_id"]), str(row["owner_principal_id"]))


class SessionService:
    """The session operations for one request (cheap to build; holds no state)."""

    def __init__(self, *, db: Database, jobs: JobQueue, authz: Authorizer, ids: IdGen, clock: Clock) -> None:
        self._db = db
        self._jobs = jobs
        self._authz = authz
        self._ids = ids
        self._clock = clock

    async def create(
        self, who: Principal, body: Mapping[str, JsonValue], *, session_id: str, snapshot: ConfigSnapshot
    ) -> dict[str, JsonValue]:
        """createSession: owner := caller, project := caller's; data_class defaults to the project's."""
        self._authz.check(who, "session.create")
        retention = _check_retention(body) or "default"
        metadata = _check_metadata(body)
        now = self._clock.now()
        async with self._db.uow(who) as uow:
            data_class = body.get("data_class")
            if not isinstance(data_class, str):
                project = await uow.projects.get(who.project_id)
                default = None if project is None else project.get("default_data_class")
                data_class = default if isinstance(default, str) and default in DATA_CLASSES else "internal"
            row = await uow.sessions.create(
                {
                    "id": session_id,
                    "project_id": who.project_id,
                    "owner_principal_id": who.principal_id,
                    "data_class": data_class,
                    "retention": retention,
                    "expires_at": expires_at(now, data_class, retention, snapshot),
                    "metadata": dict(metadata) if isinstance(metadata, Mapping) else {},
                    "created_at": now,
                }
            )
        return session_wire(row)

    async def list(self, who: Principal, *, after: str | None, limit: int) -> dict[str, JsonValue]:
        """listSessions, newest first: own sessions, or the tenant's for tenant_admin/auditor."""
        scope = self._authz.list_scope(who, "session.list")
        if scope == "none":
            return {"data": [], "has_more": False, "next_cursor": None}
        owner = who.principal_id if scope == "own" else None
        async with self._db.uow(who) as uow:
            page = await uow.sessions.list(owner_principal_id=owner, after=after, limit=limit)
        return {
            "data": [session_wire(r) for r in page.data],
            "has_more": page.has_more,
            "next_cursor": page.next_cursor,
        }

    async def get(self, who: Principal, session_id: str) -> dict[str, JsonValue]:
        """getSession."""
        self._authz.check(who, "session.read")
        async with self._db.uow(who) as uow:
            row = await self._load(uow, who, "session.read", session_id)
        return session_wire(row)

    async def update(
        self, who: Principal, session_id: str, body: Mapping[str, JsonValue], *, snapshot: ConfigSnapshot
    ) -> dict[str, JsonValue]:
        """updateSession: present fields replace; a data_class change writes an audit_event."""
        self._authz.check(who, "session.update")
        retention = _check_retention(body)
        metadata = _check_metadata(body)
        async with self._db.uow(who) as uow:
            row = await self._load(uow, who, "session.update", session_id)
            changes: dict[str, object] = {}
            if metadata is not None:
                changes["metadata"] = dict(metadata)
            new_retention = retention or str(row["retention"])
            data_class = body.get("data_class")
            new_class = data_class if isinstance(data_class, str) else str(row["data_class"])
            if retention is not None:
                changes["retention"] = retention
            if isinstance(data_class, str):
                changes["data_class"] = data_class
            if retention is not None or isinstance(data_class, str):
                created = row.get("created_at")
                start = created if isinstance(created, datetime) else self._clock.now()
                changes["expires_at"] = expires_at(start, new_class, new_retention, snapshot)
            if isinstance(data_class, str) and data_class != row["data_class"]:
                await uow.audit.insert(
                    {
                        "id": self._ids.new("aud"),
                        "actor_type": "principal",
                        "actor_id": who.principal_id,
                        "action": "session.data_class.update",
                        "resource_kind": "session",
                        "resource_id": session_id,
                        "details": {"from": str(row["data_class"]), "to": data_class},
                        "request_id": who.request_id or None,
                    }
                )
            updated = await uow.sessions.update(session_id, **changes)
        return session_wire(updated)

    async def delete(self, who: Principal, session_id: str) -> dict[str, JsonValue]:
        """deleteSession: 202 with the deletion job id (the job deletes turns, decisions, files, then the row)."""
        self._authz.check(who, "session.delete")
        async with self._db.uow(who) as uow:
            await self._load(uow, who, "session.delete", session_id)
        job_id = await self._jobs.enqueue(
            DELETE_JOB_KIND, who.tenant_id, {"session_id": session_id}, dedupe_key=session_id
        )
        async with self._db.uow(who) as uow:
            await uow.sessions.mark_deleted(session_id)
        return {"id": session_id, "deleted": True, "deletion_job_id": job_id}

    async def list_turns(
        self, who: Principal, session_id: str, *, after: str | None, limit: int
    ) -> dict[str, JsonValue]:
        """listSessionTurns: committed turns oldest first, each as its user then its assistant message; `limit`
        counts messages (`history.history_page`)."""
        self._authz.check(who, "session.read")
        async with self._db.uow(who) as uow:
            await self._load(uow, who, "session.read", session_id)
            return await history_page(uow.turns, session_id, after=after, limit=limit)

    async def _load(self, uow: UnitOfWork, who: Principal, action: Action, session_id: str) -> Row:
        row = await uow.sessions.get(session_id)
        if row is None:
            raise NoemaError(ErrorCode.SESSION_NOT_FOUND, "Session not found.")
        self._authz.check(who, action, _ref(row))
        return row
