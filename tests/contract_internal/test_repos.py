"""Repository conformance (spec-02 §7.1): every repo method, keyset pagination order, `get` -> None for foreign
rows, malformed cursors, and the SystemCalls job functions."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.storage import OutboxRecord, SessionLease, StoredResponse
from noema.db.repos import IDEM_INLINE_MAX_BYTES
from noema.db.system import AdminError

from .kits import DatabaseKit, principal
from .registry import kit_fixture
from .rows import T0, attempt, decision, file, seed_tenancy, session, turn, usage_line

kit = kit_fixture("database")
ADMIN = principal(roles=("tenant_admin",), principal_id="prn_admin")
ACTIVATED: dict[str, Any] = {"approved_by": "op_a", "activated_by": ["op_a", "op_b"], "activated_at": T0}  # 2-person


async def test_sessions_list_update_delete(kit: DatabaseKit) -> None:
    await seed_tenancy(kit)
    async with kit.db.uow(principal()) as uow:
        for i in range(5):
            await uow.sessions.create(session(f"ses_{i}", owner="prn_dev" if i % 2 == 0 else "prn_peer"))
        page = await uow.sessions.list(owner_principal_id="prn_dev", after=None, limit=2)
        assert [r["id"] for r in page.data] == ["ses_4", "ses_2"] and page.has_more
        rest = await uow.sessions.list(owner_principal_id="prn_dev", after=page.next_cursor, limit=2)
        assert [r["id"] for r in rest.data] == ["ses_0"] and not rest.has_more and rest.next_cursor is None
        updated = await uow.sessions.update("ses_1", metadata={"a": "b"}, data_class="confidential")
        assert updated["metadata"] == {"a": "b"} and updated["data_class"] == "confidential"
        await uow.sessions.mark_deleted("ses_1")
        assert await uow.sessions.get("ses_1") is None
        with pytest.raises(NoemaError) as err:
            await uow.sessions.update("ses_missing", metadata={})
        assert err.value.code is ErrorCode.SESSION_NOT_FOUND


async def test_malformed_cursor(kit: DatabaseKit) -> None:
    await seed_tenancy(kit)
    async with kit.db.uow(principal()) as uow:
        with pytest.raises(NoemaError) as err:
            await uow.sessions.list(owner_principal_id=None, after="!!not-a-cursor!!", limit=10)
    assert err.value.code is ErrorCode.SCHEMA_VIOLATION and err.value.param == "after"


async def test_turns_attempts_history_and_cascade(kit: DatabaseKit) -> None:
    await seed_tenancy(kit)
    async with kit.db.uow(principal()) as uow:
        await uow.sessions.create(session("ses_1"))
        for seq in (1, 2, 3):
            lease = await uow.sessions.acquire("ses_1", f"turn_{seq}", expected_seq=seq - 1, lease_s=30)
            assert isinstance(lease, SessionLease)
            await uow.sessions.finalize_turn(
                lease,
                turn(f"turn_{seq}"),
                [attempt(f"att_{seq}b", started_at=T0 + timedelta(seconds=2)), attempt(f"att_{seq}a")],
                [],
                [],
                None,
            )
        await uow.turns.insert(turn("turn_x"), [], [], [])  # store: true without a session
        page = await uow.turns.list_for_session("ses_1", after=None, limit=2)
        assert [r["seq"] for r in page.data] == [1, 2] and page.has_more
        tail = await uow.turns.list_for_session("ses_1", after=page.next_cursor, limit=2)
        assert [r["seq"] for r in tail.data] == [3]
        assert [r["seq"] for r in await uow.turns.recent("ses_1", max_seq=2, limit=5)] == [2, 1]
        assert [a["id"] for a in await uow.attempts.list_for_turn("turn_1")] == ["att_1a", "att_1b"]
        stray = await uow.turns.get("turn_x")
        assert stray is not None and stray["session_id"] is None and stray["seq"] is None
        await uow.turns.delete("turn_1")
        assert await uow.turns.get("turn_1") is None and await uow.attempts.list_for_turn("turn_1") == ()
        assert await uow.turns.delete_for_session("ses_1") == 2


async def test_decisions_and_foreign_rows(kit: DatabaseKit) -> None:
    await seed_tenancy(kit)
    async with kit.db.uow(principal()) as uow:
        await uow.decisions.insert(decision("dec_1"))
        await uow.decisions.insert(decision("dec_2", session_id="ses_9"))
        assert (await uow.decisions.get("dec_1") or {})["profile_id"] == "lightweight-model-decide"
        assert await uow.decisions.delete_for_session("ses_9") == 1
    async with kit.db.uow(principal(project_id="p2")) as uow:
        assert await uow.decisions.get("dec_1") is None
    async with kit.db.uow(principal(tenant_id="t_other")) as uow:
        assert await uow.decisions.get("dec_1") is None
    async with kit.db.uow(ADMIN) as uow:
        await uow.decisions.delete("dec_1")
        assert await uow.decisions.get("dec_1") is None


async def test_files_and_derivatives(kit: DatabaseKit) -> None:
    await seed_tenancy(kit)
    async with kit.db.uow(principal()) as uow:
        await uow.files.create(file("file_1", session_id="ses_1"))
        await uow.files.update("file_1", status="ready", media_type="image/png")
        assert (await uow.files.get("file_1") or {})["status"] == "ready"
        base = {"file_id": "file_1", "status": "ready", "extractor_version": "1", "sha256": "5" * 64, "bytes": 1}
        await uow.derivatives.upsert({**base, "id": "der_1", "kind": "raster"})
        await uow.derivatives.upsert({**base, "id": "der_1", "kind": "raster", "bytes": 2})  # same (file, kind)
        await uow.derivatives.upsert({**base, "id": "der_2", "kind": "thumbnail"})
        kinds = {(d["kind"], d["bytes"]) for d in await uow.derivatives.list_for_file("file_1")}
        assert kinds == {("raster", 2), ("thumbnail", 1)}
        assert [f["id"] for f in await uow.files.list_for_session("ses_1")] == ["file_1"]
        await uow.files.delete("file_1")
        assert await uow.files.get("file_1") is None and await uow.derivatives.list_for_file("file_1") == ()


async def test_usage_report_grouping_and_pages(kit: DatabaseKit) -> None:
    await seed_tenancy(kit)
    lines = [
        usage_line(f"u{i:024d}", line=line, model_id=model)
        for i, (line, model) in enumerate(
            [
                ("decision", "deepseek.v3.2"),
                ("decision", "us.openai.gpt-6-luna"),
                ("generation", "us.openai.gpt-6.1-sol"),
                ("waste", "deepseek.v3.2"),
            ]
        )
    ]
    lines.append(usage_line("u_p2_000000000000000000000", project_id="p2"))
    lines.append(usage_line("u_old_00000000000000000000", at=T0 - timedelta(days=40)))
    for row in lines:
        await kit.seed("usage_line", row)
    async with kit.db.uow(principal()) as uow:
        page = await uow.usage.report(
            group_by="line", day_from=date(2026, 10, 1), day_to=date(2026, 10, 31), after=None, limit=2
        )
        assert [(r["key"], r["line"]) for r in page.data] == [("decision", "decision"), ("generation", "generation")]
        assert page.data[0]["input_tokens"] == 20 and page.has_more
        rest = await uow.usage.report(
            group_by="line", day_from=date(2026, 10, 1), day_to=date(2026, 10, 31), after=page.next_cursor, limit=2
        )
        assert [r["key"] for r in rest.data] == ["waste"]
    async with kit.db.uow(ADMIN) as uow:
        by_project = await uow.usage.report(
            group_by="project", day_from=date(2026, 10, 1), day_to=date(2026, 10, 31), after=None, limit=50
        )
        assert {r["key"] for r in by_project.data} == {"p1", "p2"}


async def test_jobs_dedupe_and_definer_calls(kit: DatabaseKit) -> None:
    await seed_tenancy(kit)
    job = {"kind": "session.delete", "payload": {"session_id": "ses_1"}, "dedupe_key": "ses_1", "priority": 100}
    async with kit.db.tenant_uow("t_acme") as uow:
        first = await uow.jobs.insert({**job, "id": "job_1"})
        assert await uow.jobs.insert({**job, "id": "job_2"}) == first
    async with kit.db.system_uow() as sys:
        [claimed] = await sys.claim_jobs(["session.delete"], 5, "w1", 60)
        assert (claimed.id, claimed.attempt) == (first, 1)
        assert await sys.extend_job(first, "w1", 60) and not await sys.extend_job(first, "w2", 60)
        await sys.finish_job(first, "w2", ok=True, error=None)  # stale worker: no change
        await sys.finish_job(first, "w1", ok=False, error="boom")
        assert await sys.claim_jobs(["session.delete"], 5, "w1", 60) == ()  # backoff 30 s
        await kit.clock.advance(31)
        [again] = await sys.claim_jobs(["session.delete"], 5, "w1", 60)
        assert again.attempt == 2
        await sys.finish_job(first, "w1", ok=True, error=None)


async def test_finish_job_returns_the_new_state(kit: DatabaseKit) -> None:
    """ops-ledger request 1: done / queued / dead (per-job max_attempts, or permanent); None = not held."""
    await seed_tenancy(kit)
    async with kit.db.tenant_uow("t_acme") as uow:
        for job_id, max_attempts in (("job_ok", 5), ("job_retry", 5), ("job_last", 1), ("job_perm", 5)):
            await uow.jobs.insert({"id": job_id, "kind": "test.finish", "payload": {}, "max_attempts": max_attempts})
    async with kit.db.system_uow() as sys:
        assert len(await sys.claim_jobs(["test.finish"], 10, "w1", 60)) == 4
        assert await sys.finish_job("job_ok", "w2", ok=True, error=None) is None  # another worker: unchanged
        assert await sys.finish_job("job_ok", "w1", ok=True, error=None) == "done"
        assert await sys.finish_job("job_ok", "w1", ok=True, error=None) is None  # no longer running
        assert await sys.finish_job("job_retry", "w1", ok=False, error="boom") == "queued"
        assert await sys.finish_job("job_last", "w1", ok=False, error="boom") == "dead"  # max_attempts 1
        assert await sys.finish_job("job_perm", "w1", ok=False, error="bad", permanent=True) == "dead"
        assert await sys.finish_job("job_missing", "w1", ok=True, error=None) is None


async def test_retention_page_is_a_keyset_over_expired_rows(kit: DatabaseKit) -> None:
    """ops-ledger request 5: (expires_at, kind, id, tenant_id) order, strictly after the previous page's last
    candidate (equal expiries and equal ids in two tenants included); rows not yet expired never appear."""
    await seed_tenancy(kit)
    await seed_tenancy(kit, "t_other")
    later = FAR_PAST + timedelta(days=1)
    async with kit.db.uow(principal()) as uow:
        for sid, expires in (("ses_b", FAR_PAST), ("ses_a", FAR_PAST), ("ses_live", FAR_FUTURE)):
            await uow.sessions.create({**session(sid), "expires_at": expires})
        await uow.files.create({**file("file_a"), "expires_at": later})
    async with kit.db.uow(principal(tenant_id="t_other", principal_id="prn_p1")) as uow:
        await uow.sessions.create({**session("ses_a", tenant_id="t_other"), "expires_at": FAR_PAST})
    expected = [
        ("t_acme", "session", "ses_a", FAR_PAST),
        ("t_other", "session", "ses_a", FAR_PAST),
        ("t_acme", "session", "ses_b", FAR_PAST),
        ("t_acme", "file", "file_a", later),
    ]
    expected.sort(key=lambda c: (c[3], c[1], c[2], c[0]))
    async with kit.db.system_uow() as sys:
        assert list(await sys.retention_page(10)) == expected
        walked: list[Any] = []
        after = None
        while page := await sys.retention_page(1, after=after):
            walked.extend(page)
            after = page[-1]
        assert walked == expected
        assert list(await sys.retention_page(2, after=expected[1])) == expected[2:]
        assert await sys.retention_page(5, after=expected[-1]) == ()


async def test_idempotency_audit_project_rows(kit: DatabaseKit) -> None:
    await seed_tenancy(kit)
    row = {
        "id": "idem_1",
        "scope_hash": "h",
        "key": "k",
        "fingerprint": "f",
        "state": "in_flight",
        "owner": "o",
        "lease_expires_at": T0 + timedelta(seconds=30),
        "resource_id": "turn_1",
        "expires_at": T0 + timedelta(days=1),
    }
    async with kit.db.uow(principal()) as uow:
        assert await uow.idempotency.insert_in_flight(row)
        assert not await uow.idempotency.insert_in_flight({**row, "id": "idem_2"})
        assert (await uow.idempotency.get_by_key("h", "k") or {})["id"] == "idem_1"
        assert await uow.idempotency.renew("idem_1", owner="o", lease_s=30)
        assert not await uow.idempotency.renew("idem_1", owner="intruder", lease_s=30)
        await uow.audit.insert(
            {
                "id": "aud_1",
                "actor_type": "principal",
                "actor_id": "prn_dev",
                "action": "session.update",
                "resource_kind": "session",
                "resource_id": "ses_1",
                "details": {},
                "request_id": "req_1",
            }
        )
        assert (await uow.projects.get("p1") or {})["name"] == "p1"
        assert await uow.projects.get("p_missing") is None


async def test_labeling_runs_items_and_calibrator_visibility(kit: DatabaseKit) -> None:
    await seed_tenancy(kit)
    run: dict[str, Any] = {
        "id": "lrun_1",
        "profile_id": "router-default",
        "profile_version": "1",
        "question_name": "intent",
        "question_signature": "sha256:" + "6" * 64,
        "status": "running",
        "labelers": ["sol-6.1", "sonnet-5.5"],
        "rule": "unanimous",
        "counts": {},
        "consensus_rate": None,
        "consensus_rate_by_class": None,
        "n_by_class": None,
        "usage": {},
        "started_at": T0,
    }
    item = {
        "run_id": "lrun_1",
        "decision_id": "dec_1",
        "question_name": "intent",
        "sample_reason": "uniform",
        "labeler_answers": [],
        "ensemble": {},
        "content_excerpt": "x",
        "redacted": False,
        "input_view_signature": "sha256:" + "7" * 64,
        "member_subset": ["m1"],
    }
    cal = {
        "profile_id": "router-default",
        "profile_version": "1",
        "question_name": "intent",
        "question_signature": "sha256:" + "6" * 64,
        "member_subset": ["m1", "m2"],
        "input_view_signature": "sha256:" + "7" * 64,
        "choice_method": "flat",
        "method": "temperature",
        "member_t": {},
        "pooled": {},
        "epsilon": 0.0001,
        "holdout": {},
        "acceptance": {},
        "reliability": {},
        "fitted_at": T0,
    }
    await kit.seed(
        "calibrator",
        {**cal, **ACTIVATED, "id": "cal_platform", "scope": "platform", "tenant_id": None, "status": "active"},
    )
    await kit.seed(
        "calibrator",
        {**cal, **ACTIVATED, "id": "cal_other", "scope": "tenant", "tenant_id": "t_other", "status": "active"},
    )
    async with kit.db.tenant_uow("t_acme") as uow:
        await uow.labels.create_run(run)
        assert (await uow.labels.open_run("router-default", "1", "intent") or {})["id"] == "lrun_1"
        await uow.labels.insert_item({**item, "id": "lab_1", "kept": True, "value": "coding", "drop_reason": None})
        await uow.labels.insert_item(
            {**item, "id": "lab_2", "decision_id": "dec_2", "kept": False, "value": None, "drop_reason": "disagreed"}
        )
        assert [i["id"] for i in await uow.labels.items("lrun_1", kept=True)] == ["lab_1"]
        dropped = await uow.labels.list_dropped(run_id="lrun_1", question_name=None, reason=None, after=None, limit=10)
        assert [i["id"] for i in dropped.data] == ["lab_2"]
        await uow.labels.update_run("lrun_1", status="completed")
        assert await uow.labels.open_run("router-default", "1", "intent") is None
        runs = await uow.labels.list_runs(
            profile_id="router-default", question_name=None, status=None, after=None, limit=10
        )
        assert [r["id"] for r in runs.data] == ["lrun_1"]
        await uow.calibrators.insert({**cal, "id": "cal_mine", "scope": "tenant", "tenant_id": "t_acme"})
        listed = await uow.calibrators.list(profile_id=None, question_name=None, status=None, after=None, limit=10)
        assert {c["id"] for c in listed.data} == {"cal_mine", "cal_platform"}
        assert await uow.calibrators.get("cal_other") is None
        assert (await uow.calibrators.update("cal_mine", status="accepted"))["status"] == "accepted"


def _run(run_id: str, signature: str) -> dict[str, Any]:
    return {
        "id": run_id,
        "profile_id": "lmd",
        "profile_version": "1",
        "question_name": "q0",
        "question_signature": signature,
        "status": "running",
        "labelers": ["sol-6.1", "sonnet-5.5"],
        "rule": "unanimous",
        "counts": {},
        "usage": {},
        "started_at": T0,
    }


def _item(item_id: str, run_id: str, decision_id: str, *, kept: bool = True, **extra: Any) -> dict[str, Any]:
    return {
        "id": item_id,
        "run_id": run_id,
        "decision_id": decision_id,
        "question_name": "q0",
        "sample_reason": "uniform",
        "labeler_answers": [],
        "kept": kept,
        "value": "yes" if kept else None,
        "drop_reason": None if kept else "disagreed",
        "ensemble": {},
        "redacted": False,
        "input_view_signature": "sha256:" + "7" * 64,
        "member_subset": ["m1"],
        "created_at": T0,
        **extra,
    }


async def test_label_runs_are_per_definition_and_items_read_across_runs(kit: DatabaseKit) -> None:
    """labeling requests 9 and 4: two definitions sharing a name run side by side (open_run by signature;
    None = the newest of the name); items_for_decision spans runs; kept_items reads one definition's kept labels
    since a time in created_at order."""
    await seed_tenancy(kit)
    sig_a, sig_b = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    async with kit.db.tenant_uow("t_acme") as uow:
        labels = uow.labels
        await labels.create_run(_run("lrun_a", sig_a))
        await labels.create_run(_run("lrun_b", sig_b))
        assert (await labels.open_run("lmd", "1", "q0", sig_a) or {})["id"] == "lrun_a"
        assert (await labels.open_run("lmd", "1", "q0", question_signature=sig_b) or {})["id"] == "lrun_b"
        assert (await labels.open_run("lmd", "1", "q0") or {})["id"] == "lrun_b"
        assert await labels.open_run("lmd", "1", "q0", "sha256:" + "c" * 64) is None
        hour = timedelta(hours=1)
        for row in (
            _item("lab_1", "lrun_a", "dec_1"),
            _item("lab_2", "lrun_b", "dec_1"),
            _item("lab_3", "lrun_a", "dec_2", sample_reason="uncertainty", created_at=T0 - hour),
            _item("lab_4", "lrun_a", "dec_3", kept=False),
            _item("lab_5", "lrun_a", "dec_4", created_at=T0 - 48 * hour),
            _item("lab_0", "lrun_a", "dec_5"),
        ):
            await labels.insert_item(row)
        assert [i["id"] for i in await labels.items_for_decision("dec_1")] == ["lab_1", "lab_2"]
        assert await labels.items_for_decision("dec_none") == ()
        since = T0 - 2 * hour
        assert [i["id"] for i in await labels.kept_items("lmd", "1", sig_a, since)] == ["lab_0", "lab_1"]
        both = await labels.kept_items("lmd", "1", sig_a, since, sample_reason=None)
        assert [i["id"] for i in both] == ["lab_3", "lab_0", "lab_1"]
        assert [i["id"] for i in await labels.kept_items("lmd", "1", sig_a, T0 - 72 * hour)] == [
            "lab_5",
            "lab_0",
            "lab_1",
        ]
        assert [i["id"] for i in await labels.kept_items("lmd", "1", sig_b, since)] == ["lab_2"]
        assert await labels.kept_items("lmd", "2", sig_a, since) == ()
        await labels.update_run("lrun_b", status="completed")
        assert (await labels.open_run("lmd", "1", "q0") or {})["id"] == "lrun_a"
    async with kit.db.tenant_uow("t_other") as uow:
        assert await uow.labels.items_for_decision("dec_1") == ()
        assert await uow.labels.kept_items("lmd", "1", sig_a, since) == ()


FAR_PAST = T0 - timedelta(days=3650)  # before any clock the kits run on (fake store time or Postgres now())
FAR_FUTURE = T0 + timedelta(days=3650)


async def test_session_hard_delete(kit: DatabaseKit) -> None:
    """SessionRepo.delete (spec-03 §5): turns first, then the row, deleted or not; absent is a no-op."""
    await seed_tenancy(kit)
    async with kit.db.uow(principal()) as uow:
        await uow.sessions.create(session("ses_1"))
        lease = await uow.sessions.acquire("ses_1", "turn_1", expected_seq=0, lease_s=30)
        assert isinstance(lease, SessionLease)
        await uow.sessions.finalize_turn(lease, turn("turn_1"), [attempt("att_1")], [], [], None)
        await uow.sessions.mark_deleted("ses_1")
    async with kit.db.tenant_uow("t_acme") as uow:
        assert await uow.turns.delete_for_session("ses_1") == 1
        await uow.sessions.delete("ses_1")
        await uow.sessions.delete("ses_1")  # absent: no-op
        await uow.sessions.delete("ses_missing")
    async with kit.db.uow(principal()) as uow:
        assert (await uow.sessions.create(session("ses_1")))["last_seq"] == 0  # the id is free again


def _idem(rid: str, *, lease: object = FAR_FUTURE, owner: str = "o1") -> dict[str, Any]:
    return {
        "id": rid,
        "scope_hash": "h",
        "key": f"k_{rid}",
        "fingerprint": "f",
        "state": "in_flight",
        "owner": owner,
        "lease_expires_at": lease,
        "resource_id": "turn_old",
        "expires_at": FAR_FUTURE,
    }


async def test_idempotency_take_over_is_conditional_and_finish_is_fenced(kit: DatabaseKit) -> None:
    """spec-11 §5: take_over only while expired / not_stored / lease lapsed (one racing taker wins) and sets the new
    resource_id; finish only by the owner of an in_flight record; bodies over 1 MiB are stored by key."""
    await seed_tenancy(kit)
    ok = StoredResponse(200, (("content-type", "application/json"),), b"{}", None)
    async with kit.db.uow(principal()) as uow:
        repo = uow.idempotency
        for rid, lease in (("idem_live", FAR_FUTURE), ("idem_lapsed", FAR_PAST), ("idem_ns", FAR_FUTURE)):
            assert await repo.insert_in_flight(_idem(rid, lease=lease))
        assert not await repo.take_over("idem_live", owner="o2", fingerprint="f2", lease_s=30, ttl_s=60)
        await repo.finish("idem_live", owner="o2", response=ok)  # not the owner: no change
        assert (await repo.get_by_key("h", "k_idem_live") or {})["state"] == "in_flight"
        await repo.finish("idem_live", owner="o1", response=ok)
        await repo.finish("idem_live", owner="o1", response=None)  # late: never downgrades a completed record
        live = await repo.get_by_key("h", "k_idem_live") or {}
        assert (live["state"], live["response_status"], live["response_body"]) == ("completed", 200, b"{}")
        assert not await repo.take_over("idem_live", owner="o2", fingerprint="f2", lease_s=30, ttl_s=60)
        assert await repo.take_over(
            "idem_lapsed", owner="o2", fingerprint="f2", lease_s=30, ttl_s=60, resource_id="turn_new"
        )
        taken = await repo.get_by_key("h", "k_idem_lapsed") or {}
        assert (taken["state"], taken["owner"], taken["resource_id"]) == ("in_flight", "o2", "turn_new")
        assert not await repo.take_over("idem_lapsed", owner="o3", fingerprint="f3", lease_s=30, ttl_s=60)
        await repo.finish("idem_ns", owner="o1", response=None)
        assert await repo.take_over("idem_ns", owner="o2", fingerprint="f2", lease_s=30, ttl_s=60)
        assert (await repo.get_by_key("h", "k_idem_ns") or {})["resource_id"] is None  # None clears the old id
        assert await repo.insert_in_flight(_idem("idem_big"))
        big = StoredResponse(200, (), b"x" * (IDEM_INLINE_MAX_BYTES + 1), None)
        await repo.finish("idem_big", owner="o1", response=big)
        stored = await repo.get_by_key("h", "k_idem_big") or {}
        assert (stored["response_body"], stored["response_body_key"]) == (None, "t_acme/idempotency/idem_big")


async def test_attempts_and_derivatives_follow_their_parent_scope(kit: DatabaseKit) -> None:
    await seed_tenancy(kit)
    async with kit.db.uow(principal()) as uow:
        await uow.turns.insert(turn("turn_p1"), [attempt("att_p1")], [], [])
        await uow.files.create(file("file_p1"))
        await uow.derivatives.upsert(
            {"file_id": "file_p1", "kind": "raster", "id": "der_p1", "status": "ready", "extractor_version": "1"}
        )
    async with kit.db.uow(principal(project_id="p2", principal_id="prn_p2")) as uow:
        assert await uow.attempts.list_for_turn("turn_p1") == ()
        assert await uow.derivatives.list_for_file("file_p1") == ()
    async with kit.db.uow(principal()) as uow:
        assert [a["id"] for a in await uow.attempts.list_for_turn("turn_p1")] == ["att_p1"]
        assert [d["id"] for d in await uow.derivatives.list_for_file("file_p1")] == ["der_p1"]


async def test_derivative_upserts_merge_partial_rows(kit: DatabaseKit) -> None:
    await seed_tenancy(kit)
    async with kit.db.uow(principal()) as uow:
        await uow.files.create(file("file_1"))
        full = {
            "file_id": "file_1",
            "kind": "page_images",
            "id": "der_pi",
            "status": "pending",
            "extractor_version": "1",
        }
        await uow.derivatives.upsert(full)
        await uow.derivatives.upsert({"file_id": "file_1", "kind": "page_images", "status": "ready", "pages": 3})
        await uow.derivatives.upsert({"file_id": "file_1", "kind": "page_images", "text_layer_pages": [1, 3]})
        (row,) = await uow.derivatives.list_for_file("file_1")
        assert (row["id"], row["status"], row["pages"], row["extractor_version"]) == ("der_pi", "ready", 3, "1")
        assert list(row["text_layer_pages"]) == [1, 3]  # type: ignore[call-overload]
        assert row["facts"] is None
        facts = {"active_content": ["javascript", "launch"]}  # media-pipeline request 2
        sanitized = {"file_id": "file_1", "kind": "pdf_sanitized", "id": "der_ps", "status": "pending"}
        await uow.derivatives.upsert({**sanitized, "extractor_version": "1"})
        await uow.derivatives.upsert({"file_id": "file_1", "kind": "pdf_sanitized", "status": "ready", "facts": facts})
        by_kind = {d["kind"]: d for d in await uow.derivatives.list_for_file("file_1")}
        assert (by_kind["pdf_sanitized"]["status"], by_kind["pdf_sanitized"]["facts"]) == ("ready", facts)
    with pytest.raises(Exception):  # noqa: B017, PT011 (a NOT NULL violation; the error type is the backend's)
        async with kit.db.uow(principal()) as uow:
            await uow.derivatives.upsert({"file_id": "file_1", "kind": "ocr_text", "status": "ready"})


def _calibrator(cid: str, *, tenant_id: str | None, status: str = "active") -> dict[str, Any]:
    return {
        "id": cid,
        "scope": "platform" if tenant_id is None else "tenant",
        "tenant_id": tenant_id,
        "status": status,
        "profile_id": "router-default",
        "profile_version": "1",
        "question_name": "intent",
        "question_signature": "sha256:" + "6" * 64,
        "member_subset": ["m1"],
        "input_view_signature": "sha256:" + "7" * 64,
        "choice_method": "flat",
        "method": "temperature",
        "member_t": {},
        "pooled": {},
        "epsilon": 0.0001,
        "holdout": {},
        "acceptance": {},
        "reliability": {},
        "fitted_at": T0,
        "approved_by": "op_a",
        "activated_by": ["op_a", "op_b"],
        "activated_at": T0,
    }


async def test_labeling_grants_and_dead_letters(kit: DatabaseKit) -> None:
    """The noema_app grants of spec-03 §4 (labeling drift job): active -> downgraded only, epoch bumps; and the
    outbox dead letter with its delivery count."""
    await seed_tenancy(kit)
    await kit.seed("calibrator", _calibrator("cal_drift", tenant_id="t_acme"))
    await kit.seed("calibrator", _calibrator("cal_fitted", tenant_id="t_acme", status="fitted"))
    actor = "system:labeling.drift"
    async with kit.db.system_uow() as sys:
        before = (await sys.calibrator_epochs()).get("t_acme", 0)
        for cid, status, reason, why in (
            ("cal_fitted", "downgraded", "drift_ece", "forbidden"),  # not active
            ("cal_drift", "retired", "manual", "forbidden"),  # noema_app may only downgrade
            ("cal_drift", "downgraded", "other", "invalid_argument"),
            ("cal_missing", "downgraded", "drift_ece", "not_found"),
        ):
            with pytest.raises(AdminError) as refused:
                await sys.set_calibrator_status(actor, cid, status, reason=reason)
            assert refused.value.reason == why, cid
        with pytest.raises(AdminError) as not_system:
            await sys.set_calibrator_status("op_a", "cal_drift", "downgraded", reason="drift_ece")
        assert not_system.value.reason == "invalid_argument"
        await sys.set_calibrator_status(actor, "cal_drift", "downgraded", reason="drift_ci")
        assert (await sys.calibrator_epochs())["t_acme"] == before + 1
        assert await sys.bump_epoch(actor, "t_acme") == before + 2
        platform = (await sys.calibrator_epochs()).get("platform", 0)
        assert await sys.bump_epoch(actor, "platform") == platform + 1
        with pytest.raises(AdminError) as unknown:
            await sys.bump_epoch(actor, "t_unknown")
        assert unknown.value.reason == "not_found"
        record = OutboxRecord("audit_event", "aud_dead", "t_acme", {"action": "x"})
        await sys.outbox_dead(record, "sqlstate_23503:fk", attempts=10)
        await sys.outbox_dead(record, "again", attempts=11)  # idempotent per (kind, id)
    async with kit.db.tenant_uow("t_acme") as uow:
        downgraded = await uow.calibrators.get("cal_drift") or {}
        assert (downgraded["status"], downgraded["downgrade_reason"]) == ("downgraded", "drift_ci")
