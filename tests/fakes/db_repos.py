"""The spec-02 §7.1 repositories over `FakeStore` (one class per repo Protocol; scoping per spec-03 §3)."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any, Final, Literal

from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.storage import Busy, LeaseLost, Page, Row, SessionLease, StoredResponse
from noema.core.values import JsonValue
from noema.db.repos.idempotency import IDEM_INLINE_MAX_BYTES, idempotency_body_key

from .db_store import PROJECT_TABLES, Scope, Tx, check_limit, decode_cursor, encode_cursor, paginate, pk_of

SESSION_UPDATABLE: Final = frozenset({"metadata", "data_class", "retention", "expires_at"})
FILE_UPDATABLE: Final = frozenset({"status", "media_type", "rejection", "failure", "pages", "deleted_at"})


class _Repo:
    def __init__(self, tx: Tx, scope: Scope) -> None:
        self._tx = tx
        self._scope = scope

    @property
    def _now(self) -> datetime:
        return self._tx.store.now()

    def _all(self, table: str) -> list[dict[str, Any]]:
        return [r for r in self._tx.store.table(table).values() if self._scope.sees(table, r)]

    def _find(self, table: str, **eq: Any) -> list[dict[str, Any]]:
        return [r for r in self._all(table) if all(r.get(k) == v for k, v in eq.items())]

    def _one(self, table: str, row_id: str) -> dict[str, Any] | None:
        key = (row_id,) if table in ("job", "calibrator") else (self._scope.tenant_id, row_id)
        row = self._tx.store.table(table).get(key)
        return row if row is not None and self._scope.sees(table, row) else None

    def _insert(self, table: str, row: Row) -> dict[str, Any]:
        stored = dict(row)
        stored.setdefault("tenant_id", self._scope.tenant_id)
        if table in PROJECT_TABLES and self._scope.project_id:
            stored.setdefault("project_id", self._scope.project_id)
        self._scope.check_write(table, stored)
        return self._tx.put(table, stored)

    def _update(self, table: str, row: Mapping[str, Any], changes: Mapping[str, object]) -> dict[str, Any]:
        new = {**row, **changes}
        self._scope.check_write(table, new)
        return self._tx.put(table, new)


def _dc(row: Mapping[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(dict(row))


class FakeSessionRepo(_Repo):
    async def create(self, row: Row) -> Row:
        defaults = {
            "retention": "default",
            "metadata": {},
            "last_seq": 0,
            "sticky_spec": None,
            "active_turn_id": None,
            "lease_expires_at": None,
            "created_at": self._now,
            "deleted_at": None,
            "expires_at": None,
        }
        return _dc(self._insert("session", {**defaults, **row}))

    def _live(self, session_id: str) -> dict[str, Any] | None:
        row = self._one("session", session_id)
        return row if row is not None and row.get("deleted_at") is None else None

    async def get(self, session_id: str) -> Row | None:
        row = self._live(session_id)
        return None if row is None else _dc(row)

    async def update(self, session_id: str, **changes: object) -> Row:
        row = self._live(session_id)
        if row is None:
            raise NoemaError(ErrorCode.SESSION_NOT_FOUND, "Session not found.")
        if unknown := set(changes) - SESSION_UPDATABLE:
            raise ValueError(f"session columns not updatable: {sorted(unknown)}")
        return _dc(self._update("session", row, changes))

    async def list(self, *, owner_principal_id: str | None, after: str | None, limit: int) -> Page[Row]:
        rows = [r for r in self._all("session") if r.get("deleted_at") is None]
        if owner_principal_id is not None:
            rows = [r for r in rows if r["owner_principal_id"] == owner_principal_id]
        return paginate(rows, lambda r: (r["id"],), descending=True, after=after, limit=limit)

    async def mark_deleted(self, session_id: str) -> None:
        row = self._live(session_id)
        if row is not None:
            self._update("session", row, {"deleted_at": self._now})

    async def delete(self, session_id: str) -> None:
        row = self._one("session", session_id)
        if row is None:
            return
        if self._find("turn", session_id=session_id):  # the turn foreign key (spec-03 §2)
            raise RuntimeError("session has turns: delete them first (TurnRepo.delete_for_session)")
        self._tx.delete("session", pk_of("session", row))

    async def acquire(
        self, session_id: str, turn_id: str, *, expected_seq: int | None, lease_s: int
    ) -> SessionLease | Busy:
        row = self._live(session_id)
        if row is None:
            raise NoemaError(ErrorCode.SESSION_NOT_FOUND, "Session not found.")
        if expected_seq is not None and expected_seq != row["last_seq"]:
            raise NoemaError(
                ErrorCode.SESSION_CONFLICT, "The session has moved on.", details={"last_seq": row["last_seq"]}
            )
        held = row.get("active_turn_id") is not None and row["lease_expires_at"] > self._now
        if held and row["active_turn_id"] != turn_id:
            return Busy()
        self._update(
            "session", row, {"active_turn_id": turn_id, "lease_expires_at": self._now + timedelta(seconds=lease_s)}
        )
        return SessionLease(session_id, turn_id, row["last_seq"], copy.deepcopy(row.get("sticky_spec")))

    async def renew(self, lease: SessionLease, lease_s: int) -> bool:
        row = self._live(lease.session_id)
        if row is None or row.get("active_turn_id") != lease.turn_id:
            return False
        self._update("session", row, {"lease_expires_at": self._now + timedelta(seconds=lease_s)})
        return True

    async def finalize_turn(
        self,
        lease: SessionLease,
        turn: Row,
        attempts: Sequence[Row],
        decisions: Sequence[Row],
        turn_files: Sequence[Row],
        sticky_spec: Mapping[str, JsonValue] | None,
    ) -> int:
        row = self._live(lease.session_id)
        if row is None or row.get("active_turn_id") != lease.turn_id or row["last_seq"] != lease.last_seq:
            raise LeaseLost(lease.session_id)
        seq = lease.last_seq + 1
        _insert_turn(self, {**turn, "session_id": lease.session_id, "seq": seq}, attempts, decisions, turn_files)
        self._update(
            "session",
            row,
            {
                "last_seq": seq,
                "sticky_spec": copy.deepcopy(sticky_spec),
                "active_turn_id": None,
                "lease_expires_at": None,
            },
        )
        return seq

    async def release(self, lease: SessionLease) -> None:
        row = self._live(lease.session_id)
        if row is not None and row.get("active_turn_id") == lease.turn_id:
            self._update("session", row, {"active_turn_id": None, "lease_expires_at": None})


def _insert_turn(
    repo: _Repo, turn: Row, attempts: Sequence[Row], decisions: Sequence[Row], turn_files: Sequence[Row]
) -> None:
    stored = repo._insert("turn", turn)
    for a in attempts:
        repo._insert("attempt", {**a, "turn_id": stored["id"]})
    for d in decisions:
        repo._insert("decision", d)
    for f in turn_files:
        repo._insert("turn_file", {**f, "turn_id": stored["id"]})


class FakeTurnRepo(_Repo):
    async def get(self, turn_id: str) -> Row | None:
        row = self._one("turn", turn_id)
        return None if row is None else _dc(row)

    async def insert(
        self, turn: Row, attempts: Sequence[Row], decisions: Sequence[Row], turn_files: Sequence[Row]
    ) -> None:
        _insert_turn(self, {**turn, "session_id": None, "seq": None}, attempts, decisions, turn_files)

    async def list_for_session(self, session_id: str, *, after: str | None, limit: int) -> Page[Row]:
        rows = self._find("turn", session_id=session_id)
        return paginate(rows, lambda r: (r["seq"],), descending=False, after=after, limit=limit)

    async def recent(self, session_id: str, *, max_seq: int, limit: int) -> tuple[Row, ...]:
        rows = [r for r in self._find("turn", session_id=session_id) if r["seq"] <= max_seq]
        return tuple(_dc(r) for r in sorted(rows, key=lambda r: r["seq"], reverse=True)[:limit])

    async def delete(self, turn_id: str) -> None:
        row = self._one("turn", turn_id)
        if row is None:
            return
        for table in ("attempt", "turn_file"):
            for child in self._find(table, turn_id=turn_id):
                self._tx.delete(table, pk_of(table, child))
        self._tx.delete("turn", pk_of("turn", row))

    async def delete_for_session(self, session_id: str) -> int:
        rows = self._find("turn", session_id=session_id)
        for r in rows:
            await self.delete(r["id"])
        return len(rows)


class FakeAttemptRepo(_Repo):
    async def list_for_turn(self, turn_id: str) -> tuple[Row, ...]:
        if self._one("turn", turn_id) is None:  # read through the turn row: its project scope applies
            return ()
        return tuple(_dc(r) for r in sorted(self._find("attempt", turn_id=turn_id), key=lambda r: r["started_at"]))


class FakeDecisionRepo(_Repo):
    async def insert(self, row: Row) -> None:
        self._insert("decision", row)

    async def get(self, decision_id: str) -> Row | None:
        row = self._one("decision", decision_id)
        return None if row is None else _dc(row)

    async def delete(self, decision_id: str) -> None:
        row = self._one("decision", decision_id)
        if row is None:
            return
        for item in self._find("label_item", decision_id=decision_id):  # labels stay, content goes (spec-03 §5)
            self._update("label_item", item, {"content_excerpt": None})
        self._tx.delete("decision", pk_of("decision", row))

    async def delete_for_session(self, session_id: str) -> int:
        rows = self._find("decision", session_id=session_id)
        for r in rows:
            await self.delete(r["id"])
        return len(rows)


class FakeFileRepo(_Repo):
    async def create(self, row: Row) -> Row:
        defaults = {
            "created_at": self._now,
            "deleted_at": None,
            "expires_at": None,
            "rejection": None,
            "failure": None,
            "pages": None,
            "media_type": None,
            "session_id": None,
        }
        return _dc(self._insert("file", {**defaults, **row}))

    async def get(self, file_id: str) -> Row | None:
        row = self._one("file", file_id)
        return None if row is None or row.get("deleted_at") is not None else _dc(row)

    async def update(self, file_id: str, **changes: object) -> Row:
        row = self._one("file", file_id)
        if row is None:
            raise NoemaError(ErrorCode.NOT_FOUND, "File not found.")
        if unknown := set(changes) - FILE_UPDATABLE:
            raise ValueError(f"file columns not updatable: {sorted(unknown)}")
        return _dc(self._update("file", row, changes))

    async def list_for_session(self, session_id: str) -> tuple[Row, ...]:
        rows = [r for r in self._find("file", session_id=session_id) if r.get("deleted_at") is None]
        return tuple(_dc(r) for r in sorted(rows, key=lambda r: r["id"]))

    async def delete(self, file_id: str) -> None:
        row = self._one("file", file_id)
        if row is None:
            return
        for child in self._find("derivative", file_id=file_id):
            self._tx.delete("derivative", pk_of("derivative", child))
        self._tx.delete("file", pk_of("file", row))


_DERIVATIVE_REQUIRED: Final = ("id", "status", "extractor_version")  # NOT NULL without a default
_DERIVATIVE_DEFAULTS: Final[Mapping[str, object]] = dict.fromkeys(
    (
        "sha256",
        "bytes",
        "pages",
        "width",
        "height",
        "text_layer_pages",
        "facts",
        "storage_key",
        "error_code",
        "completed_at",
    )
)


class FakeDerivativeRepo(_Repo):
    async def upsert(self, row: Row) -> None:
        if "file_id" not in row or "kind" not in row:
            raise ValueError("a derivative upsert needs file_id and kind")
        existing = self._find("derivative", file_id=row["file_id"], kind=row["kind"])
        if not existing:
            if missing := [c for c in _DERIVATIVE_REQUIRED if row.get(c) is None]:
                raise ValueError(f"a new derivative row needs {missing} (NOT NULL)")
            self._insert("derivative", {"created_at": self._now, **_DERIVATIVE_DEFAULTS, **row})
            return
        base = existing[0]
        if "id" in row and row["id"] != base["id"]:
            self._tx.delete("derivative", pk_of("derivative", base))
        self._insert("derivative", {**base, **row})

    async def list_for_file(self, file_id: str) -> tuple[Row, ...]:
        if self._one("file", file_id) is None:  # read through the file row (deleted files included)
            return ()
        return tuple(_dc(r) for r in sorted(self._find("derivative", file_id=file_id), key=lambda r: r["kind"]))


_USAGE_SUMS: Final = (
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "reasoning_tokens",
    "cost_usd",
)


def _usage_key(row: Mapping[str, Any], group_by: str) -> str | None:
    if group_by == "day":
        at = row["at"]
        return str((at if isinstance(at, datetime) else datetime.fromisoformat(str(at))).date())
    column = {"project": "project_id", "session": "session_id", "model": "model_id", "line": "line"}[group_by]
    value = row.get(column)
    return None if value is None else str(value)


class FakeUsageRepo(_Repo):
    async def report(
        self,
        *,
        group_by: Literal["project", "session", "model", "day", "line"],
        day_from: date,
        day_to: date,
        after: str | None,
        limit: int,
    ) -> Page[Row]:
        check_limit(limit)
        groups: dict[tuple[str | None, str], dict[str, Any]] = {}
        for r in self._all("usage_line"):
            at = r["at"] if isinstance(r["at"], datetime) else datetime.fromisoformat(str(r["at"]))
            if not day_from <= at.date() <= day_to:
                continue
            key = (_usage_key(r, group_by), r["line"])
            g = groups.setdefault(
                key,
                {
                    "key": key[0],
                    "line": key[1],
                    **dict.fromkeys(_USAGE_SUMS, 0),
                    "cost_estimated": False,
                    "tokens_estimated": False,
                },
            )
            for col in _USAGE_SUMS:
                g[col] = g[col] + r[col]
            g["cost_estimated"] = g["cost_estimated"] or bool(r["cost_estimated"])
            g["tokens_estimated"] = g["tokens_estimated"] or bool(r["tokens_estimated"])

        def order(k: tuple[str | None, str]) -> tuple[int, str, str]:
            return (1 if k[0] is None else 0, k[0] or "", k[1])

        keys = sorted(groups, key=order)
        if after is not None:
            mark = decode_cursor(after)
            mark_key = (mark[0], mark[1]) if len(mark) == 2 else None
            if mark_key is None:
                raise NoemaError(ErrorCode.SCHEMA_VIOLATION, "Malformed pagination cursor.", param="after")
            keys = [k for k in keys if order(k) > order((mark_key[0], str(mark_key[1])))]
        page = keys[:limit]
        has_more = len(keys) > limit
        return Page(
            data=tuple(groups[k] for k in page),
            has_more=has_more,
            next_cursor=encode_cursor(list(page[-1])) if has_more and page else None,
        )


class FakeLabelRepo(_Repo):
    async def create_run(self, row: Row) -> Row:
        return _dc(self._insert("labeling_run", {"completed_at": None, **row}))

    async def update_run(self, run_id: str, **changes: object) -> Row:
        row = self._one("labeling_run", run_id)
        if row is None:
            raise NoemaError(ErrorCode.NOT_FOUND, "Labeling run not found.")
        return _dc(self._update("labeling_run", row, changes))

    async def open_run(
        self, profile_id: str, profile_version: str, question_name: str, question_signature: str | None = None
    ) -> Row | None:
        rows = [
            r
            for r in self._find(
                "labeling_run",
                profile_id=profile_id,
                profile_version=profile_version,
                question_name=question_name,
                status="running",
            )
            if question_signature is None or r["question_signature"] == question_signature
        ]
        return _dc(max(rows, key=lambda r: r["id"])) if rows else None

    async def insert_item(self, row: Row) -> None:
        if not self._find(
            "label_item", run_id=row["run_id"], decision_id=row["decision_id"], question_name=row["question_name"]
        ):
            self._insert("label_item", {"created_at": self._now, **row})

    async def items(self, run_id: str, *, kept: bool | None = None) -> tuple[Row, ...]:
        rows = [r for r in self._find("label_item", run_id=run_id) if kept is None or r["kept"] == kept]
        return tuple(_dc(r) for r in sorted(rows, key=lambda r: r["id"]))

    async def items_for_decision(self, decision_id: str) -> tuple[Row, ...]:
        return tuple(_dc(r) for r in sorted(self._find("label_item", decision_id=decision_id), key=lambda r: r["id"]))

    async def kept_items(
        self,
        profile_id: str,
        profile_version: str,
        question_signature: str,
        since: datetime,
        sample_reason: Literal["uniform", "uncertainty"] | None = "uniform",
    ) -> tuple[Row, ...]:
        found = self._find(
            "labeling_run",
            profile_id=profile_id,
            profile_version=profile_version,
            question_signature=question_signature,
        )
        runs = {r["id"] for r in found}
        rows = [
            r
            for r in self._all("label_item")
            if r["run_id"] in runs
            and r["kept"]
            and r["created_at"] >= since
            and (sample_reason is None or r["sample_reason"] == sample_reason)
        ]
        return tuple(_dc(r) for r in sorted(rows, key=lambda r: (r["created_at"], r["id"])))

    async def list_runs(
        self, *, profile_id: str | None, question_name: str | None, status: str | None, after: str | None, limit: int
    ) -> Page[Row]:
        rows = [
            r
            for r in self._all("labeling_run")
            if _filters(r, profile_id=profile_id, question_name=question_name, status=status)
        ]
        return paginate(rows, lambda r: (r["id"],), descending=True, after=after, limit=limit)

    async def list_dropped(
        self, *, run_id: str | None, question_name: str | None, reason: str | None, after: str | None, limit: int
    ) -> Page[Row]:
        rows = [
            r
            for r in self._all("label_item")
            if not r["kept"] and _filters(r, run_id=run_id, question_name=question_name, drop_reason=reason)
        ]
        return paginate(rows, lambda r: (r["id"],), descending=True, after=after, limit=limit)


def _filters(row: Mapping[str, Any], **eq: object) -> bool:
    return all(v is None or row.get(k) == v for k, v in eq.items())


class FakeCalibratorRepo(_Repo):
    async def insert(self, row: Row) -> None:
        self._insert("calibrator", {"scope": "tenant", "status": "fitted", "fitted_at": self._now, **row})

    async def get(self, calibrator_id: str) -> Row | None:
        row = self._one("calibrator", calibrator_id)
        return None if row is None else _dc(row)

    async def update(self, calibrator_id: str, **changes: object) -> Row:
        row = self._one("calibrator", calibrator_id)
        if row is None:
            raise NoemaError(ErrorCode.NOT_FOUND, "Calibrator not found.")
        return _dc(self._update("calibrator", row, changes))

    async def list(
        self, *, profile_id: str | None, question_name: str | None, status: str | None, after: str | None, limit: int
    ) -> Page[Row]:
        rows = [
            r
            for r in self._all("calibrator")
            if _filters(r, profile_id=profile_id, question_name=question_name, status=status)
        ]
        return paginate(rows, lambda r: (r["id"],), descending=True, after=after, limit=limit)


class FakeIdempotencyRepo(_Repo):
    def _by_key(self, scope_hash: str, key: str) -> dict[str, Any] | None:
        rows = self._find("idempotency_record", scope_hash=scope_hash, key=key)
        return rows[0] if rows else None

    async def insert_in_flight(self, row: Row) -> bool:
        project = row.get("project_id", self._scope.project_id)
        clash = [
            r
            for r in self._tx.store.table("idempotency_record").values()
            if r["tenant_id"] == self._scope.tenant_id
            and r["project_id"] == project
            and r["scope_hash"] == row["scope_hash"]
            and r["key"] == row["key"]
        ]
        if clash:
            return False
        defaults = {
            "state": "in_flight",
            "created_at": self._now,
            "response_status": None,
            "response_headers": None,
            "response_body": None,
            "sse_turn_id": None,
            "response_body_key": None,
        }
        self._insert("idempotency_record", {**defaults, **row})
        return True

    async def get_by_key(self, scope_hash: str, key: str) -> Row | None:
        row = self._by_key(scope_hash, key)
        return None if row is None else _dc(row)

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
        row = self._one("idempotency_record", record_id)
        if row is None:
            return False
        now = self._now
        free = (
            row["expires_at"] < now
            or row["state"] == "not_stored"
            or (row["state"] == "in_flight" and row["lease_expires_at"] < now)
        )
        if not free:  # the conditional UPDATE of spec-11 §5: a racing taker already won
            return False
        self._update(
            "idempotency_record",
            row,
            {
                "state": "in_flight",
                "owner": owner,
                "fingerprint": fingerprint,
                "resource_id": resource_id,
                "created_at": now,
                "lease_expires_at": now + timedelta(seconds=lease_s),
                "expires_at": now + timedelta(seconds=ttl_s),
                "response_status": None,
                "response_headers": None,
                "response_body": None,
                "sse_turn_id": None,
                "response_body_key": None,
            },
        )
        return True

    async def renew(self, record_id: str, *, owner: str, lease_s: int) -> bool:
        row = self._one("idempotency_record", record_id)
        if row is None or row["owner"] != owner or row["state"] != "in_flight":
            return False
        self._update("idempotency_record", row, {"lease_expires_at": self._now + timedelta(seconds=lease_s)})
        return True

    async def finish(self, record_id: str, *, owner: str, response: StoredResponse | None) -> None:
        row = self._one("idempotency_record", record_id)
        if row is None or row["owner"] != owner or row["state"] != "in_flight":
            return  # fenced: a late finish never changes a completed or taken-over record
        if response is None:
            self._update("idempotency_record", row, {"state": "not_stored"})
            return
        body, body_key = response.body, None
        if body is not None and len(body) > IDEM_INLINE_MAX_BYTES:
            body, body_key = None, idempotency_body_key(row["tenant_id"], record_id)
        self._update(
            "idempotency_record",
            row,
            {
                "state": "completed",
                "response_status": response.status,
                "response_headers": [list(h) for h in response.headers],
                "response_body": body,
                "response_body_key": body_key,
                "sse_turn_id": response.sse_turn_id,
            },
        )

    async def delete(self, record_id: str) -> None:
        row = self._one("idempotency_record", record_id)
        if row is not None:
            self._tx.delete("idempotency_record", pk_of("idempotency_record", row))


class FakeAuditRepo(_Repo):
    async def insert(self, row: Row) -> None:
        self._insert("audit_event", {"at": self._now, "details": {}, **row})


class FakeProjectRepo(_Repo):
    async def get(self, project_id: str) -> Row | None:
        row = self._one("project", project_id)
        return None if row is None or row.get("deleted_at") is not None else _dc(row)


def live_duplicate(rows: Any, tenant_id: str, kind: str, dedupe_key: str | None) -> dict[str, Any] | None:
    """The queued or running job with the same (tenant, kind, dedupe_key), if any (the job_dedupe index)."""
    if dedupe_key is None:
        return None
    for r in rows:
        if (r["tenant_id"], r["kind"], r.get("dedupe_key")) == (tenant_id, kind, dedupe_key) and r["state"] in (
            "queued",
            "running",
        ):
            return dict(r)
    return None


class FakeJobRepo(_Repo):
    async def insert(self, row: Row) -> str:
        dedupe = row.get("dedupe_key")
        dup = live_duplicate(
            self._tx.store.table("job").values(),
            self._scope.tenant_id,
            str(row["kind"]),
            None if dedupe is None else str(dedupe),
        )
        if dup is not None:
            return str(dup["id"])
        defaults = {
            "priority": 100,
            "state": "queued",
            "attempts": 0,
            "max_attempts": 5,
            "run_after": self._now,
            "locked_by": None,
            "locked_until": None,
            "dedupe_key": None,
            "last_error": None,
            "created_at": self._now,
            "updated_at": self._now,
        }
        stored = self._insert("job", {**defaults, **row})
        return str(stored["id"])
