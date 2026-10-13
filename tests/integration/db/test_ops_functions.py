"""ops.retention_candidates, ops.retention_page and ops.calibrator_epochs (spec-03 §4-§5, spec-11 §6; IMPL-072)."""

from __future__ import annotations

from datetime import datetime

import asyncpg
import pytest
from tests.fixtures.db import new_tenant_id
from tests.integration.db._rows import PROJECTS, row_sql, seed_parents

from noema.db.uow import PgDatabase, Tx

pytestmark = pytest.mark.integration

type Conn = asyncpg.Connection[asyncpg.Record]
KINDS = ("session", "turn", "decision", "file", "idempotency_record")


async def test_retention_candidates_are_the_expired_rows_oldest_first(pg_admin: Conn, pg_db: PgDatabase[Tx]) -> None:
    tenant = new_tenant_id()
    await seed_parents(pg_admin, tenant)
    expected: list[tuple[str, str, str]] = []
    for age, kind in enumerate(KINDS):  # 1900-01-01 + age days: older than anything else in the database
        rid = f"{kind[:4]}_old_{tenant}"
        sql, args = row_sql(f"noema.{kind}", tenant, PROJECTS[0], rid)
        await pg_admin.execute(sql, *args)
        await pg_admin.execute(
            f"UPDATE noema.{kind} SET expires_at = timestamptz '1900-01-01' + $3::int * interval '1 day'"
            " WHERE tenant_id = $1 AND id = $2",
            tenant,
            rid,
            age,
        )
        expected.append((tenant, kind, rid))
    sql, args = row_sql("noema.session", tenant, PROJECTS[0], f"ses_future_{tenant}")
    await pg_admin.execute(sql, *args)
    await pg_admin.execute(
        "UPDATE noema.session SET expires_at = now() + interval '1 day' WHERE tenant_id = $1 AND id = $2",
        tenant,
        f"ses_future_{tenant}",
    )
    async with pg_db.system_uow() as calls:
        assert await calls.retention_candidates(len(KINDS)) == tuple(expected)
        assert await calls.retention_candidates(2) == tuple(expected[:2])
        everything = await calls.retention_candidates(100_000)
    assert (tenant, "session", f"ses_future_{tenant}") not in everything


async def test_retention_page_walks_expired_rows_by_keyset(pg_admin: Conn, pg_db: PgDatabase[Tx]) -> None:
    """ops-ledger request 5: (expires_at, kind, id, tenant_id) order, bytewise text; each page starts strictly after
    the previous page's last candidate, so a row that keeps failing never holds the head of a page."""
    tenants = sorted((new_tenant_id(), new_tenant_id()))
    expected: list[tuple[str, str, str, datetime]] = []
    for tenant in tenants:
        await seed_parents(pg_admin, tenant)
        for age, kind in enumerate(KINDS):  # 1800-01-01 + age // 2 days: older than any other test's rows
            for rid in (f"{kind[:4]}_B_shared", f"{kind[:4]}_a_shared"):  # 'B' < 'a' bytewise, not in en_US
                sql, args = row_sql(f"noema.{kind}", tenant, PROJECTS[0], rid)
                await pg_admin.execute(sql, *args)
                expires = await pg_admin.fetchval(
                    f"UPDATE noema.{kind} SET expires_at = timestamptz '1800-01-01' + $3::int * interval '1 day'"
                    " WHERE tenant_id = $1 AND id = $2 RETURNING expires_at",
                    tenant,
                    rid,
                    age // 2,
                )
                expected.append((tenant, kind, rid, expires))
    expected.sort(key=lambda c: (c[3], c[1], c[2], c[0]))
    try:
        async with pg_db.system_uow() as calls:
            assert list(await calls.retention_page(len(expected))) == expected
            walked: list[tuple[str, str, str, datetime]] = []
            after = None
            while len(walked) < len(expected):
                page = await calls.retention_page(3, after=after)
                assert page, walked
                walked.extend(page)
                after = page[-1]
            assert walked[: len(expected)] == expected
            assert list(await calls.retention_page(2, after=expected[4])) == expected[5:7]
    finally:  # the other retention tests expect their own rows to be the oldest of the session database
        for kind in KINDS:
            await pg_admin.execute(
                f"UPDATE noema.{kind} SET expires_at = now() + interval '100 years' WHERE tenant_id = ANY($1)", tenants
            )


async def test_calibrator_epochs_cover_platform_and_every_live_tenant(pg_admin: Conn, pg_db: PgDatabase[Tx]) -> None:
    live, deleted, bumped = new_tenant_id(), new_tenant_id(), new_tenant_id()
    await pg_admin.execute(
        "INSERT INTO noema.tenant (id, aws_account_id, deleted_at) VALUES ($1, '1', NULL), ($2, '1', now()),"
        " ($3, '1', NULL)",
        live,
        deleted,
        bumped,
    )
    await pg_admin.execute("INSERT INTO noema.calibrator_epoch (scope_key, n) VALUES ($1, 7)", bumped)
    async with pg_db.system_uow() as calls:
        epochs = await calls.calibrator_epochs()
        assert await calls.active_calibrators([live, "platform"]) == tuple(
            r for r in await calls.active_calibrators(["platform"])
        )
    assert epochs[live] == 0 and epochs[bumped] == 7 and "platform" in epochs
    assert deleted not in epochs


async def test_deleting_a_decision_clears_its_label_excerpts_only(pg_admin: Conn, pg_db: PgDatabase[Tx]) -> None:
    tenant = new_tenant_id()
    await seed_parents(pg_admin, tenant)
    for decision in ("dec_gone", "dec_kept"):
        sql, args = row_sql("noema.decision", tenant, PROJECTS[0], decision)
        await pg_admin.execute(sql, *args)
        await pg_admin.execute(
            "INSERT INTO noema.label_item (tenant_id, id, run_id, decision_id, question_name, sample_reason,"
            " labeler_answers, kept, value, ensemble, content_excerpt, redacted, input_view_signature, member_subset)"
            " VALUES ($1, $2, 'lrun_x', $3, 'q', 'uniform', '[]', true, '1', '{}', 'the excerpt', false, 'vs', '{m1}')",
            tenant,
            f"lab_{decision}",
            decision,
        )
    async with pg_db.tenant_uow(tenant) as tx:  # as noema_app, under RLS
        assert await tx.conn.execute("DELETE FROM noema.decision WHERE id = 'dec_gone'") == "DELETE 1"
    rows = await pg_admin.fetch(
        "SELECT decision_id, content_excerpt FROM noema.label_item WHERE tenant_id = $1 ORDER BY 1", tenant
    )
    assert [(r["decision_id"], r["content_excerpt"]) for r in rows] == [("dec_gone", None), ("dec_kept", "the excerpt")]
