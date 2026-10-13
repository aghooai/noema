"""W4 read-only admin routes (IMPL-042, spec-09 §3) through the real middleware with the test-mode contract
validator: roles and scopes, tenant + platform calibrators, newest first, keyset pages, filters, and the closed
`CalibratorInfo` / `LabelingRun` / `LabelingDroppedItem` shapes."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tests.fakes.auth import FakeAuthorizer
from tests.fakes.clock import FakeClock
from tests.fakes.config import example_snapshot
from tests.fakes.db import FakeDatabase
from tests.unit.api.apifakes import FakeAuthenticator, FakeServices, asgi_client, make_app, principal
from tests.unit.labeling.jobkit import SIG, VIEW, dropped_row, item_rows, run_row
from tests.unit.labeling.synth import LABELERS, labeled_set, sampled_items

from noema.api.routes import admin_readonly
from noema.core.principal import ALL_SCOPES
from noema.core.protocols.labeling import CalibratorBindingKey
from noema.labeling.acceptance import SubsetEvidence, Thresholds, evaluate_gate
from noema.labeling.aggregate import aggregate
from noema.labeling.artifact import artifact_row
from noema.labeling.evaluation import fit_and_evaluate

pytestmark = pytest.mark.unit
KEYS = {
    role: f"ak_test_01J0000000000000000000{role[:3].upper()}0." + role[0] * 43
    for role in ("tenant_admin", "reviewer", "auditor", "developer")
}
T = datetime(2026, 10, 1, tzinfo=UTC)


def calibrator(cid: str, *, scope: str = "tenant", status: str = "accepted", tenant: str = "t_acme") -> dict[str, Any]:
    ls = labeled_set(700, temp=0.4, seed=3, members=("m1", "m2", "m3"))
    ev = fit_and_evaluate(
        ls,
        sampled_items(ls),
        dataset_id="lrun_0001",
        labelers=LABELERS,
        lift_threshold=0.1,
        class_n_min=30,
        view_signature=VIEW,
        evaluated_at=T,
        epsilon=1e-4,
    )
    gate = evaluate_gate(
        ev.holdout, ls.options, Thresholds.from_config(example_snapshot().labeling), SubsetEvidence(True, 1.0)
    )
    key = CalibratorBindingKey("router-default", "1", SIG, ("m1", "m2", "m3"), VIEW)
    row = artifact_row(
        calibrator_id=cid,
        key=key,
        question_name="intent",
        params=ev.params,
        holdout=ev.holdout,
        acceptance=gate.wire(),
        reliability=ev.reliability,
        fitted_at=T,
    )
    row.update(scope=scope, tenant_id=tenant if scope == "tenant" else None, status=status)
    if status == "active":
        row.update(approved_by="op_a", activated_by=["op_a", "op_b"], activated_at=T + timedelta(days=1))
    return row


def services() -> tuple[FakeServices, FakeDatabase]:
    roles: dict[str, frozenset[Any]] = {r: frozenset({r}) for r in KEYS}
    svc = FakeServices(authn=FakeAuthenticator({k: principal(k, roles=roles[r]) for r, k in KEYS.items()}))
    db = FakeDatabase(FakeClock())
    svc.db = db  # type: ignore[attr-defined]
    svc.authz = FakeAuthorizer()  # type: ignore[attr-defined]
    for row in (
        calibrator("cal_01A"),
        calibrator("cal_01B", scope="platform", status="active"),
        calibrator("cal_01C", tenant="t_other"),
    ):
        db.store.seed("calibrator", row)
    ls = labeled_set(5, temp=0.4, seed=1)
    items = item_rows(ls, "lrun_0001") + [
        dropped_row("lrun_0001", i, reason=("disagreed", "refused")[i % 2]) for i in range(3)
    ]
    run = run_row("lrun_0001", T)
    run.update(aggregate(items).changes())
    db.store.seed("labeling_run", run)
    db.store.seed(
        "labeling_run",
        {**run_row("lrun_0002", T), "question_name": "complexity", "status": "completed", "completed_at": T},
    )
    for item in items:
        db.store.seed("label_item", {**item, "created_at": T})
    return svc, db


def auth(role: str) -> dict[str, str]:
    return {"authorization": f"Bearer {KEYS[role]}"}


async def get(role: str, path: str, **params: str) -> tuple[int, Any]:
    svc, _ = services()
    app, _ = make_app(svc, routers=[admin_readonly.router])
    async with asgi_client(app) as client:
        resp = await client.get(path, params=params, headers=auth(role))
    return resp.status_code, resp.json()


async def test_calibrators_show_tenant_and_platform_rows_newest_first() -> None:
    status, body = await get("auditor", "/v1/admin/calibrators")
    assert status == 200 and [c["id"] for c in body["data"]] == ["cal_01B", "cal_01A"] and body["has_more"] is False
    platform, tenant = body["data"]
    assert (
        platform["scope"] == "platform"
        and platform["activated_by"] == ["op_a", "op_b"]
        and platform["approved_by"] == "op_a"
    )
    assert (
        tenant["status"] == "accepted"
        and tenant["activated_by"] == []
        and tenant["acceptance"]["passed"] in (True, False)
    )
    assert tenant["binding"] == {
        "profile_id": "router-default",
        "profile_version": "1",
        "question_signature": SIG,
        "input_view_signature": VIEW,
        "choice_method": "flat",
    }
    assert set(tenant["parameters"]) == {"pooled", "member_T", "epsilon"} and tenant["holdout"]["labelers"] == list(
        LABELERS
    )
    assert tenant["created_at"] == int(T.timestamp()) and len(tenant["reliability"]["bins"]) == 15


async def test_pagination_and_filters() -> None:
    svc, _ = services()
    app, _ = make_app(svc, routers=[admin_readonly.router])
    async with asgi_client(app) as client:
        first = (await client.get("/v1/admin/calibrators", params={"limit": "1"}, headers=auth("tenant_admin"))).json()
        assert [c["id"] for c in first["data"]] == ["cal_01B"] and first["has_more"] is True
        second = (
            await client.get(
                "/v1/admin/calibrators",
                params={"limit": "1", "after": first["next_cursor"]},
                headers=auth("tenant_admin"),
            )
        ).json()
        assert [c["id"] for c in second["data"]] == ["cal_01A"] and second["has_more"] is False
        active = (
            await client.get("/v1/admin/calibrators", params={"status": "active"}, headers=auth("reviewer"))
        ).json()
        assert [c["id"] for c in active["data"]] == ["cal_01B"]
        bad = await client.get("/v1/admin/calibrators", params={"after": "not-a-cursor"}, headers=auth("reviewer"))
        assert bad.status_code == 400 and bad.json()["error"]["param"] == "after"
        runs = (
            await client.get("/v1/admin/labeling/runs", params={"question_name": "complexity"}, headers=auth("auditor"))
        ).json()
        assert [r["id"] for r in runs["data"]] == ["lrun_0002"] and runs["data"][0]["completed_at"] == int(
            T.timestamp()
        )


async def test_runs_carry_counts_and_consensus_rates() -> None:
    status, body = await get("reviewer", "/v1/admin/labeling/runs", status="running")
    [run] = body["data"]
    assert status == 200 and run["counts"]["sampled"] == 8 and run["counts"]["kept"] == 5
    assert run["counts"]["dropped_disagreed"] == 2 and run["counts"]["dropped_refused"] == 1
    assert (
        run["consensus_rate"] == pytest.approx(5 / 8)
        and run["labelers"] == list(LABELERS)
        and run["rule"] == "unanimous"
    )
    assert sum(run["n_by_class"].values()) == 5 and run["usage"]["input_tokens"] == 100


async def test_dropped_items_show_both_answers_and_the_redacted_excerpt() -> None:
    status, body = await get("reviewer", "/v1/admin/labeling/dropped", reason="disagreed")
    assert status == 200 and [d["id"] for d in body["data"]] == ["ldrop_000100000002", "ldrop_000100000000"]
    item = body["data"][0]
    assert (
        item["question_signature"] == SIG and item["reason"] == "disagreed" and item["sample_reason"] == "uncertainty"
    )
    assert item["labeler_answers"] == [
        {"model": LABELERS[0], "status": "ok", "value": "opt0"},
        {"model": LABELERS[1], "status": "ok", "value": "opt1"},
    ]
    assert item["ensemble"] == {
        "value": "opt0",
        "probability": 0.4,
        "band": "low",
        "calibration_status": "uncalibrated",
    }
    assert item["content"] == {"excerpt": "text", "redacted": False} and item["sampled_at"] == int(T.timestamp())


@pytest.mark.parametrize(
    ("role", "path", "expected"),
    [
        ("developer", "/v1/admin/calibrators", 403),
        ("developer", "/v1/admin/labeling/runs", 403),
        ("auditor", "/v1/admin/labeling/dropped", 403),
        ("auditor", "/v1/admin/labeling/runs", 200),
        ("tenant_admin", "/v1/admin/labeling/dropped", 200),
    ],
)
async def test_roles(role: str, path: str, expected: int) -> None:
    status, body = await get(role, path)
    assert status == expected
    if expected == 403:
        assert body["error"]["code"] == "permission_denied"


async def test_the_admin_read_scope_is_required() -> None:
    svc, _ = services()
    no_scope = replace(
        principal(KEYS["tenant_admin"], roles=frozenset({"tenant_admin"})), scopes=ALL_SCOPES - {"admin.read"}
    )
    svc.authn = FakeAuthenticator({KEYS["tenant_admin"]: no_scope})
    app, _ = make_app(svc, routers=[admin_readonly.router])
    async with asgi_client(app) as client:
        resp = await client.get("/v1/admin/calibrators", headers=auth("tenant_admin"))
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "permission_denied"
