"""T-RET-1 (spec-11 §8, spec-03 §5, IMPL-085) on an emptied Postgres and moto S3: rows past `expires_at` are deleted
per tenant only (RLS), sessions cascade (turns, attempts, decisions, files, then the row), label excerpts are
nulled with their decision, idempotency objects are deleted with (before) their record, an S3 failure leaves the
row for the next run, and each tenant gets one audit event (applied through ops.outbox_apply). The keyset pages of
ops.retention_page: failing rows at the head never starve newer ones (more than a page of them), a row that
survives its delete is tried once per run, failures past max_remembered never stop a run, and rows failing in two
consecutive runs log an ERROR."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import anyio
import pytest
from tests.fakes.clock import FakeIdGen
from tests.fixtures.db import seed_tenant
from tests.integration.db_repos._rows import (
    admin_insert,
    attempt_row,
    decision_row,
    file_row,
    item_row,
    run_row,
    session_row,
    turn_row,
)

from noema.core.protocols.media import FileService
from noema.core.protocols.storage import Database, OutboxRecord
from noema.ops.idempotency import BodyStore
from noema.ops.outbox import apply_records
from noema.ops.retention import RetentionSweeper
from noema.ops.retention.sweeper import SweepReport

from .ledger_harness import CountingDatabase, LedgerClock

pytestmark = pytest.mark.integration
PAST = datetime.now(UTC) - timedelta(days=1)
FUTURE = datetime.now(UTC) + timedelta(days=30)


class RowFiles:
    """`FileService.purge` for the test: the file row (derivatives cascade); media owns the S3 layout."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self.purged: list[tuple[str, str]] = []

    async def purge(self, tenant_id: str, file_id: str) -> None:
        async with self._db.tenant_uow(tenant_id) as uow:
            await uow.files.delete(file_id)
        self.purged.append((tenant_id, file_id))


class ListOutbox:
    def __init__(self) -> None:
        self.records: list[OutboxRecord] = []

    def put(self, record: OutboxRecord) -> None:
        self.records.append(record)

    async def flush(self, timeout_s: float) -> None:
        return None


async def count(admin: Any, table: str, tenant: str) -> int:
    return int(await admin.fetchval(f"SELECT count(*) FROM {table} WHERE tenant_id = $1", tenant))


async def idem(admin: Any, tenant: str, expires_at: datetime) -> str:
    row_id = f"idem_{tenant[-6:]}_{int(expires_at.timestamp())}"
    await admin_insert(
        admin,
        "noema.idempotency_record",
        {
            "tenant_id": tenant,
            "project_id": "prj_a",
            "id": row_id,
            "scope_hash": "s",
            "key": row_id,
            "fingerprint": "f",
            "state": "completed",
            "owner": "o",
            "lease_expires_at": PAST,
            "response_status": 200,
            "response_body_key": f"{tenant}/idempotency/{row_id}",
            "expires_at": expires_at,
        },
    )
    return row_id


async def test_expired_rows_are_deleted_per_tenant(
    emptied: tuple[Any, CountingDatabase, LedgerClock], s3_bucket: tuple[Any, str]
) -> None:
    admin, db, clock = emptied
    client, bucket = s3_bucket
    a = (await seed_tenant(admin, projects=("prj_a",))).tenant_id
    b = (await seed_tenant(admin, projects=("prj_a",))).tenant_id
    base = {"project_id": "prj_a"}
    old_session = {**session_row(), **base, "tenant_id": a, "expires_at": PAST}
    await admin_insert(admin, "noema.session", old_session)
    in_session = {**turn_row(), **base, "tenant_id": a, "session_id": old_session["id"], "seq": 1, "expires_at": FUTURE}
    await admin_insert(admin, "noema.turn", in_session)
    await admin_insert(admin, "noema.attempt", {**attempt_row(), "tenant_id": a, "turn_id": in_session["id"]})
    session_decision = {**decision_row(), **base, "tenant_id": a, "session_id": old_session["id"]}
    await admin_insert(admin, "noema.decision", session_decision)
    session_file = {**file_row(), **base, "tenant_id": a, "session_id": old_session["id"]}
    await admin_insert(admin, "noema.file", session_file)
    old_turn = {**turn_row(), **base, "tenant_id": a, "expires_at": PAST}
    live_turn = {**turn_row(), **base, "tenant_id": a, "expires_at": FUTURE}
    other_tenant_turn = {**turn_row(), **base, "tenant_id": b, "expires_at": PAST}
    for row in (old_turn, live_turn, other_tenant_turn):
        await admin_insert(admin, "noema.turn", row)
    old_decision = {**decision_row(), **base, "tenant_id": a, "expires_at": PAST}
    await admin_insert(admin, "noema.decision", old_decision)
    run = {**run_row(), "tenant_id": a}
    await admin_insert(admin, "noema.labeling_run", run)
    label = {**item_row(run["id"]), "tenant_id": a, "decision_id": old_decision["id"]}
    await admin_insert(admin, "noema.label_item", label)
    old_file = {**file_row(), **base, "tenant_id": b, "expires_at": PAST}
    await admin_insert(admin, "noema.file", old_file)
    old_idem, live_idem = await idem(admin, a, PAST), await idem(admin, a, FUTURE)
    for row_id in (old_idem, live_idem):
        await client.put_object(Bucket=bucket, Key=f"{a}/idempotency/{row_id}", Body=b"body")

    files, outbox = RowFiles(db), ListOutbox()
    file_service = cast(FileService, files)
    sweeper = RetentionSweeper(db, file_service, BodyStore(client, bucket=bucket, env="test"), outbox, *_ids(clock))
    report = await sweeper.sweep()

    assert dict(report.deleted[a]) == {"session": 1, "turn": 1, "decision": 1, "idempotency_record": 1}
    assert dict(report.deleted[b]) == {"turn": 1, "file": 1} and not report.failed
    assert await count(admin, "noema.session", a) == 0 and await count(admin, "noema.attempt", a) == 0
    turns = await admin.fetch("SELECT id FROM noema.turn WHERE tenant_id = ANY($1)", [a, b])
    assert [r["id"] for r in turns] == [live_turn["id"]]
    assert await count(admin, "noema.decision", a) == 0 and await count(admin, "noema.file", a) == 0
    assert sorted(files.purged) == sorted([(a, session_file["id"]), (b, old_file["id"])])
    excerpt = await admin.fetchval("SELECT content_excerpt FROM noema.label_item WHERE id = $1", label["id"])
    assert excerpt is None  # labels are kept for calibration, their content is not
    left = await admin.fetch("SELECT id FROM noema.idempotency_record WHERE tenant_id = $1", a)
    assert [r["id"] for r in left] == [live_idem]
    listed = await client.list_objects_v2(Bucket=bucket)
    assert [o["Key"] for o in listed.get("Contents", [])] == [f"{a}/idempotency/{live_idem}"]
    audits = {r.tenant_id: r.payload for r in outbox.records if r.kind == "audit_event"}
    assert set(audits) == {a, b} and audits[b]["details"] == {"turn": 1, "file": 1, "failed": 0}
    assert audits[a]["action"] == "retention.sweep" and audits[a]["actor_type"] == "system"
    assert (await sweeper.sweep()).total == 0  # nothing left


async def test_an_s3_failure_keeps_the_record_for_the_next_run(
    emptied: tuple[Any, CountingDatabase, LedgerClock], s3_bucket: tuple[Any, str]
) -> None:
    admin, db, clock = emptied
    client, bucket = s3_bucket
    a = (await seed_tenant(admin, projects=("prj_a",))).tenant_id
    row_id = await idem(admin, a, PAST)

    class BrokenBodies(BodyStore):
        async def delete(self, tenant_id: str, record_id: str) -> None:
            raise OSError("s3 down")

    sweeper = RetentionSweeper(
        db, cast(FileService, RowFiles(db)), BrokenBodies(client, bucket=bucket, env="test"), ListOutbox(), *_ids(clock)
    )
    report = await sweeper.sweep()
    assert report.failed[a] == 1 and report.total == 0
    assert await admin.fetchval("SELECT id FROM noema.idempotency_record WHERE id = $1", row_id) == row_id
    sweeper = RetentionSweeper(
        db, cast(FileService, RowFiles(db)), BodyStore(client, bucket=bucket, env="test"), ListOutbox(), *_ids(clock)
    )
    assert (await sweeper.sweep()).deleted[a]["idempotency_record"] == 1


class FailingFiles(RowFiles):
    """`FileService.purge` that fails for `broken` ids (e.g. S3 refuses) and is a silent no-op for `noop` ids."""

    def __init__(self, db: Database, *, broken: set[str] = frozenset(), noop: set[str] = frozenset()) -> None:  # type: ignore[assignment]
        super().__init__(db)
        self.broken, self.noop = broken, noop

    async def purge(self, tenant_id: str, file_id: str) -> None:
        if file_id in self.broken:
            raise OSError("s3 refused the delete")
        if file_id not in self.noop:
            await super().purge(tenant_id, file_id)


async def expired_files(admin: Any, tenant: str, n: int, *, days_ago: int = 3) -> list[str]:
    ids = []
    for i in range(n):
        row = {
            **file_row(),
            "project_id": "prj_a",
            "tenant_id": tenant,
            "expires_at": PAST - timedelta(days=days_ago, seconds=i),
        }
        await admin_insert(admin, "noema.file", row)
        ids.append(row["id"])
    return ids


async def expired_turns(admin: Any, tenant: str, n: int) -> list[str]:
    ids = []
    for _ in range(n):
        row = {**turn_row(), "project_id": "prj_a", "tenant_id": tenant, "expires_at": PAST}  # newer than the files
        await admin_insert(admin, "noema.turn", row)
        ids.append(row["id"])
    return ids


async def test_failing_oldest_rows_never_starve_newer_ones(
    emptied: tuple[Any, CountingDatabase, LedgerClock], s3_bucket: tuple[Any, str], caplog: pytest.LogCaptureFixture
) -> None:
    admin, db, clock = emptied
    client, bucket = s3_bucket
    a = (await seed_tenant(admin, projects=("prj_a",))).tenant_id
    broken = await expired_files(admin, a, 3)  # the 3 oldest candidates fail every time: more than a page
    turns = await expired_turns(admin, a, 2)
    files = FailingFiles(db, broken=set(broken))
    bodies = BodyStore(client, bucket=bucket, env="test")
    sweeper = RetentionSweeper(db, cast(FileService, files), bodies, ListOutbox(), *_ids(clock), page=2)
    report = await sweeper.sweep()
    assert dict(report.deleted[a]) == {"turn": 2} and report.failed[a] == 3
    assert await admin.fetchval("SELECT count(*) FROM noema.turn WHERE id = ANY($1)", turns) == 0
    assert not [r for r in caplog.records if r.levelname == "ERROR"]  # one failed run is not yet an alert
    await expired_turns(admin, a, 1)
    report = await sweeper.sweep()  # the same rows fail again: ERROR (alert); the new turn is still swept
    assert dict(report.deleted[a]) == {"turn": 1} and report.failed[a] == 3
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1 and "3 expired rows failed in two consecutive runs" in errors[0]


async def test_a_row_that_survives_its_delete_is_tried_once_per_run(
    emptied: tuple[Any, CountingDatabase, LedgerClock], s3_bucket: tuple[Any, str]
) -> None:
    admin, db, clock = emptied
    client, bucket = s3_bucket
    a = (await seed_tenant(admin, projects=("prj_a",))).tenant_id
    [ghost] = await expired_files(admin, a, 1)
    turns = await expired_turns(admin, a, 3)
    files = FailingFiles(db, noop={ghost})  # "succeeds" without deleting anything
    bodies = BodyStore(client, bucket=bucket, env="test")
    sweeper = RetentionSweeper(db, cast(FileService, files), bodies, ListOutbox(), *_ids(clock), page=2)
    with anyio.fail_after(10):
        report = await sweeper.sweep()  # the cursor moves past it: never fetched twice in a run
    assert dict(report.deleted[a]) == {"file": 1, "turn": 3} and not report.failed  # purge reported success
    assert await admin.fetchval("SELECT count(*) FROM noema.turn WHERE id = ANY($1)", turns) == 0
    with anyio.fail_after(10):
        assert dict((await sweeper.sweep()).deleted[a]) == {"file": 1}  # the next run tries it again, once
    assert await count(admin, "noema.file", a) == 1


async def test_failures_past_max_remembered_never_stop_a_run(
    emptied: tuple[Any, CountingDatabase, LedgerClock], s3_bucket: tuple[Any, str], caplog: pytest.LogCaptureFixture
) -> None:
    admin, db, clock = emptied
    client, bucket = s3_bucket
    a = (await seed_tenant(admin, projects=("prj_a",))).tenant_id
    broken = await expired_files(admin, a, 4)
    turns = await expired_turns(admin, a, 1)  # newer than every failing file
    files = FailingFiles(db, broken=set(broken))
    bodies = BodyStore(client, bucket=bucket, env="test")
    sweeper = RetentionSweeper(
        db, cast(FileService, files), bodies, ListOutbox(), *_ids(clock), page=1, max_remembered=2
    )
    report = await sweeper.sweep()
    assert report.failed[a] == 4 and dict(report.deleted[a]) == {"turn": 1} and len(files.purged) == 0
    assert await admin.fetchval("SELECT count(*) FROM noema.turn WHERE id = ANY($1)", turns) == 0
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    assert (await sweeper.sweep()).failed[a] == 4  # bounded memory: only the 2 remembered keys are compared
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1 and "2 expired rows failed in two consecutive runs" in errors[0]


async def test_the_audit_record_applies_through_the_outbox(
    emptied: tuple[Any, CountingDatabase, LedgerClock], s3_bucket: tuple[Any, str]
) -> None:
    admin, db, clock = emptied
    client, bucket = s3_bucket
    a = (await seed_tenant(admin, projects=("prj_a",))).tenant_id
    await expired_turns(admin, a, 2)
    outbox = ListOutbox()
    bodies = BodyStore(client, bucket=bucket, env="test")
    await RetentionSweeper(db, cast(FileService, RowFiles(db)), bodies, outbox, *_ids(clock)).sweep()
    [record] = outbox.records
    assert await apply_records(db, [record]) == [None]  # ops.outbox_apply accepts the payload as built
    assert await apply_records(db, [record]) == [None]  # and a replay is a no-op
    rows = await admin.fetch("SELECT * FROM noema.audit_event WHERE tenant_id = $1", a)
    assert [
        (r["id"], r["actor_type"], r["actor_id"], r["action"], r["resource_kind"], r["resource_id"]) for r in rows
    ] == [(record.id, "system", "system:retention", "retention.sweep", "tenant", a)]
    assert rows[0]["details"] == {"turn": 2, "failed": 0}


def _ids(clock: LedgerClock) -> tuple[FakeIdGen, LedgerClock]:
    return FakeIdGen(clock), clock


async def test_a_record_taken_over_after_the_page_read_keeps_its_row_and_object(
    emptied: tuple[Any, CountingDatabase, LedgerClock], s3_bucket: tuple[Any, str]
) -> None:
    """Wave E edge fix 6: a new request with the same key takes an expired record over (same id, same object key)
    between the sweeper's page read and its delete: the live record and the new owner's body stay."""
    admin, db, clock = emptied
    client, bucket = s3_bucket
    a = (await seed_tenant(admin, projects=("prj_a",))).tenant_id
    row_id = await idem(admin, a, PAST)
    key = f"{a}/idempotency/{row_id}"
    async with db.system_uow() as calls:
        page = await calls.retention_page(1000, after=None)  # the sweeper's earlier page read
    assert [(c[1], c[2]) for c in page] == [("idempotency_record", row_id)]
    await admin.execute(  # what the take-over UPDATE does (repos/idempotency.py _TAKE_OVER)
        "UPDATE noema.idempotency_record SET state = 'in_flight', owner = 'new', response_body_key = NULL,"
        " expires_at = now() + interval '1 day' WHERE id = $1",
        row_id,
    )
    await client.put_object(Bucket=bucket, Key=key, Body=b"the new owner's body")
    sweeper = RetentionSweeper(
        db, cast(FileService, RowFiles(db)), BodyStore(client, bucket=bucket, env="test"), ListOutbox(), *_ids(clock)
    )
    report = SweepReport()
    assert await sweeper._sweep_tenant(a, [("idempotency_record", row_id)], report) == []
    assert report.total == 0 and report.failed[a] == 0
    assert await admin.fetchval("SELECT owner FROM noema.idempotency_record WHERE id = $1", row_id) == "new"
    assert (await client.get_object(Bucket=bucket, Key=key))["ContentLength"] == len(b"the new owner's body")
