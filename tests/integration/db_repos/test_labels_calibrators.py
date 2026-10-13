"""LabelRepo and CalibratorRepo (spec-02 §7.1, spec-03 §2-§3, ADR-003): runs, the one-open-run rule, idempotent
items, filtered id DESC pages, calibrator visibility (own + platform) and the write guard."""

from __future__ import annotations

from datetime import timedelta

import asyncpg
import pytest
from tests.fixtures.db import SeededTenant, seed_tenant
from tests.integration.db_repos._rows import NOW, Conn, admin_insert, calibrator_row, item_row, run_row

from noema.core.errors import ErrorCode, NoemaError
from noema.db.repos import PgUnitOfWork
from noema.db.uow import PgDatabase

pytestmark = pytest.mark.integration
DB = PgDatabase[PgUnitOfWork]


async def test_runs_open_run_and_update(repo_db: DB, tenant: SeededTenant) -> None:
    async with repo_db.tenant_uow(tenant.tenant_id) as u:
        run = await u.labels.create_run(run_row())
        assert run["tenant_id"] == tenant.tenant_id and run["completed_at"] is None
        assert await u.labels.open_run("lmd", "1", "q1") == run
        assert await u.labels.open_run("lmd", "1", "q2") is None
        done = await u.labels.update_run(str(run["id"]), status="completed", completed_at=NOW, counts={"kept": 3})
        assert (done["status"], done["counts"], done["completed_at"]) == ("completed", {"kept": 3}, NOW)
        assert await u.labels.open_run("lmd", "1", "q1") is None
        assert await u.labels.update_run(str(run["id"])) == done
        with pytest.raises(NoemaError) as err:
            await u.labels.update_run("lrun_absent", status="failed")
        assert err.value.code is ErrorCode.NOT_FOUND
        with pytest.raises(ValueError, match="tenant_id"):
            await u.labels.update_run(str(run["id"]), tenant_id="t_other")

    async def second_open_run() -> None:
        async with repo_db.tenant_uow(tenant.tenant_id) as u:
            await u.labels.create_run(run_row(question_name="q9"))
            await u.labels.create_run(run_row(question_name="q9"))

    with pytest.raises(asyncpg.UniqueViolationError):  # labeling_run_open: one running run per question definition
        await second_open_run()


async def test_one_running_run_per_definition_not_per_name(repo_db: DB, tenant: SeededTenant) -> None:
    """labeling request 9: definitions sharing a name (facade q0, reused tenant names) each hold a running run."""
    async with repo_db.tenant_uow(tenant.tenant_id) as u:
        a = await u.labels.create_run(run_row(question_name="q0", question_signature="sig_a"))
        b = await u.labels.create_run(run_row(question_name="q0", question_signature="sig_b"))
        assert await u.labels.open_run("lmd", "1", "q0", "sig_a") == a
        assert await u.labels.open_run("lmd", "1", "q0", "sig_b") == b
        assert await u.labels.open_run("lmd", "1", "q0") == max(a, b, key=lambda r: str(r["id"]))  # newest
        assert await u.labels.open_run("lmd", "1", "q0", "sig_c") is None

    async def second_run_of_a_definition() -> None:
        async with repo_db.tenant_uow(tenant.tenant_id) as u:
            await u.labels.create_run(run_row(question_name="q0", question_signature="sig_a"))

    with pytest.raises(asyncpg.UniqueViolationError, match="labeling_run_open"):
        await second_run_of_a_definition()


async def test_items_for_decision_span_runs_and_kept_items_read_one_definition(
    repo_db: DB, tenant: SeededTenant
) -> None:
    """labeling request 4: a retry finds the decision's items in any run; kept_items reads the kept labels of one
    (profile, version, signature) since a time, created_at order, across that definition's runs only."""
    since = NOW - timedelta(days=7)
    async with repo_db.tenant_uow(tenant.tenant_id) as u:
        old = str((await u.labels.create_run(run_row(question_signature="sig_k", status="completed")))["id"])
        new = str((await u.labels.create_run(run_row(question_signature="sig_k")))["id"])
        foreign = str((await u.labels.create_run(run_row(question_signature="sig_x")))["id"])
        rows = [
            item_row(old, decision_id="dec_k1", created_at=NOW - timedelta(days=1)),
            item_row(new, decision_id="dec_k1", question_name="q2"),
            item_row(new, decision_id="dec_k2", created_at=NOW - timedelta(days=2), sample_reason="uncertainty"),
            item_row(new, decision_id="dec_k3", kept=False),
            item_row(old, decision_id="dec_k4", created_at=NOW - timedelta(days=30)),
            item_row(foreign, decision_id="dec_k5"),
        ]
        for row in rows:
            await u.labels.insert_item(row)
        assert [i["id"] for i in await u.labels.items_for_decision("dec_k1")] == sorted(str(r["id"]) for r in rows[:2])
        uniform = await u.labels.kept_items("lmd", "1", "sig_k", since)
        assert [i["id"] for i in uniform] == [rows[0]["id"], rows[1]["id"]]
        every = await u.labels.kept_items("lmd", "1", "sig_k", since, sample_reason=None)
        assert [i["id"] for i in every] == [rows[2]["id"], rows[0]["id"], rows[1]["id"]]
        assert every[0]["labeler_answers"] == rows[2]["labeler_answers"]
        assert await u.labels.kept_items("lmd", "2", "sig_k", since) == ()


async def test_items_are_idempotent_and_filtered(repo_db: DB, tenant: SeededTenant) -> None:
    async with repo_db.tenant_uow(tenant.tenant_id) as u:
        run_id = str((await u.labels.create_run(run_row(question_name="qi")))["id"])
        kept = item_row(run_id)
        dropped = item_row(run_id, kept=False)
        for item in (kept, dropped, {**kept, "id": "litem_retry"}):  # the retry repeats (run, decision, question)
            await u.labels.insert_item(item)
        assert [i["id"] for i in await u.labels.items(run_id)] == [kept["id"], dropped["id"]]
        assert [i["id"] for i in await u.labels.items(run_id, kept=True)] == [kept["id"]]
        assert [i["id"] for i in await u.labels.items(run_id, kept=False)] == [dropped["id"]]
        assert (await u.labels.items(run_id))[0]["labeler_answers"] == kept["labeler_answers"]


async def test_list_runs_and_dropped_page_id_desc_with_filters(repo_db: DB, tenant: SeededTenant) -> None:
    async with repo_db.tenant_uow(tenant.tenant_id) as u:
        runs = [await u.labels.create_run(run_row(question_name=f"ql{i}", status="completed")) for i in range(5)]
        drops = [item_row(str(runs[0]["id"]), kept=False, drop_reason=r) for r in ("refused", "disagreed", "refused")]
        for item in drops:
            await u.labels.insert_item(item)
        await u.labels.insert_item(item_row(str(runs[0]["id"])))
        ids: list[object] = []
        after = None
        while True:
            page = await u.labels.list_runs(
                profile_id="lmd", question_name=None, status="completed", after=after, limit=2
            )
            ids += [r["id"] for r in page.data]
            if not page.has_more:
                break
            after = page.next_cursor
        assert ids == [r["id"] for r in reversed(runs)]
        one = await u.labels.list_runs(profile_id=None, question_name="ql3", status=None, after=None, limit=10)
        assert [r["id"] for r in one.data] == [runs[3]["id"]]
        assert (
            await u.labels.list_runs(profile_id="x", question_name=None, status=None, after=None, limit=5)
        ).data == ()
        refused = await u.labels.list_dropped(run_id=str(runs[0]["id"]), question_name="q1", reason="refused",
                                              after=None, limit=1)  # fmt: skip
        assert [r["id"] for r in refused.data] == [drops[2]["id"]] and refused.has_more
        rest = await u.labels.list_dropped(run_id=None, question_name=None, reason="refused", after=refused.next_cursor,
                                           limit=1)  # fmt: skip
        assert [r["id"] for r in rest.data] == [drops[0]["id"]] and not rest.has_more
        every = await u.labels.list_dropped(run_id=None, question_name=None, reason=None, after=None, limit=200)
        assert [r["id"] for r in every.data] == [d["id"] for d in reversed(drops)]


async def test_labels_are_tenant_scoped(repo_db: DB, tenant: SeededTenant, pg_admin: Conn) -> None:
    other = await seed_tenant(pg_admin)
    async with repo_db.tenant_uow(tenant.tenant_id) as u:
        run = await u.labels.create_run(run_row(question_name="qs"))
    async with repo_db.tenant_uow(other.tenant_id) as u:
        assert await u.labels.open_run("lmd", "1", "qs") is None
        assert await u.labels.items(str(run["id"])) == ()
        with pytest.raises(NoemaError):
            await u.labels.update_run(str(run["id"]), status="failed")


async def test_calibrators_visibility_and_write_guard(repo_db: DB, tenant: SeededTenant, pg_admin: Conn) -> None:
    other = await seed_tenant(pg_admin)
    platform = {**calibrator_row(), "scope": "platform", "tenant_id": None, "status": "accepted", "fitted_at": NOW}
    foreign = {
        **calibrator_row(),
        "scope": "tenant",
        "tenant_id": other.tenant_id,
        "status": "fitted",
        "fitted_at": NOW,
    }
    await admin_insert(pg_admin, "noema.calibrator", platform)
    await admin_insert(pg_admin, "noema.calibrator", foreign)
    own = calibrator_row()
    async with repo_db.tenant_uow(tenant.tenant_id) as u:
        await u.calibrators.insert(own)
        stored = await u.calibrators.get(str(own["id"]))
        assert stored is not None
        assert (stored["scope"], stored["status"], stored["tenant_id"]) == ("tenant", "fitted", tenant.tenant_id)
        assert stored["fitted_at"] is not None and stored["member_subset"] == ["m1"]
        assert await u.calibrators.get(str(platform["id"])) is not None
        assert await u.calibrators.get(str(foreign["id"])) is None
        listed = await u.calibrators.list(profile_id="lmd", question_name="q1", status=None, after=None, limit=200)
        assert {r["id"] for r in listed.data} >= {own["id"], platform["id"]}
        assert foreign["id"] not in {r["id"] for r in listed.data}
        assert [r["id"] for r in listed.data] == sorted((str(r["id"]) for r in listed.data), reverse=True)
        accepted = await u.calibrators.update(
            str(own["id"]), status="accepted", acceptance={"a1": True}, accepted_at=NOW
        )
        assert (accepted["status"], accepted["acceptance"]) == ("accepted", {"a1": True})
        for target in (str(own["id"]), str(platform["id"]), str(foreign["id"]), "cal_absent"):  # not fitted any more,
            with pytest.raises(NoemaError) as err:  # platform, foreign, missing
                await u.calibrators.update(target, status="rejected")
            assert err.value.code is ErrorCode.NOT_FOUND
        with pytest.raises(ValueError, match="activated_by"):
            await u.calibrators.update(str(own["id"]), activated_by=["a", "b"])
        page = await u.calibrators.list(profile_id=None, question_name=None, status="accepted", after=None, limit=1)
        assert len(page.data) == 1 and page.data[0]["status"] == "accepted"

    async def insert_active() -> None:
        async with repo_db.tenant_uow(tenant.tenant_id) as u:
            await u.calibrators.insert({**calibrator_row(), "status": "active"})

    with pytest.raises(asyncpg.InsufficientPrivilegeError):  # the lifecycle guard: only fitted rows are inserted
        await insert_active()
