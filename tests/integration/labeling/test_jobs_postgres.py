"""The labeling jobs on the real repositories (noema_app, RLS, spec-03 constraints): `labeling.item` writes runs and
items the schema accepts (kept iff value, drop reasons, jsonb answers), `labeling.run` fits a synthetic run and the
gate moves the fitted tenant artifact through the RLS lifecycle guard, and the W4 views of those rows validate
against the contract."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import asyncpg
import orjson
import pytest
from tests.fakes.config import FakeConfigRegistry, example_body, snapshot_from
from tests.fakes.world import build_world
from tests.fixtures.db import seed_tenant
from tests.unit.labeling.jobkit import (
    INTENT,
    ScriptedAdapter,
    ScriptedRegistry,
    agree_on,
    answer,
    decision_row,
    deps,
    item_rows,
    member,
)
from tests.unit.labeling.synth import labeled_set

from noema.contracts.validate import ContractValidator
from noema.core.principal import Principal, system_principal
from noema.core.protocols.storage import Job
from noema.core.values import JsonValue
from noema.db.engine import Engine
from noema.db.repos import make_unit_of_work
from noema.db.uow import PgDatabase
from noema.labeling import views
from noema.labeling.deps import LabelingDeps
from noema.labeling.items import ItemJob
from noema.labeling.runs import RunJob

pytestmark = pytest.mark.integration
type Conn = asyncpg.Connection[asyncpg.Record]


@pytest.fixture
async def tenant(pg_admin: Conn) -> str:
    return (await seed_tenant(pg_admin, projects=("p1",))).tenant_id


@pytest.fixture
async def db(pg_app_engine: Engine) -> PgDatabase[Any]:
    return PgDatabase(pg_app_engine, make_unit_of_work)


def full_decision(row: dict[str, Any], world: Any) -> dict[str, Any]:
    out = {k: v for k, v in row.items() if k not in ("tenant_id", "project_id")}
    out.update(
        created_at=world.clock.now(),
        origin="native",
        output_format="native",
        profile_id="router-default",
        profile_version="1",
        question_signature="sha256:" + "1" * 64,
        input_view_signature="sha256:" + "5" * 64,
        input_digest="hmac-sha256:" + "0" * 64,
        status="completed",
        cache_hit=False,
    )
    return out


def world_for(tenant: str) -> Any:
    """A fake world whose config maps the fresh test tenant onto t_acme's entry."""
    body = dict(example_body())
    body["tenants"] = {**body["tenants"], tenant: body["tenants"]["t_acme"]}
    return build_world(config=FakeConfigRegistry(snapshot_from(body)))


def pg_deps(world: Any, db: PgDatabase[Any], adapter: ScriptedAdapter) -> LabelingDeps:
    """The world's fakes with the real database."""
    return replace(deps(world, ScriptedRegistry(adapter)), db=db)


def check(operation_id: str, body: Any) -> None:
    headers = {"x-request-id": "req_1", "content-type": "application/json"}
    ContractValidator().check_response(operation_id, 200, headers, orjson.dumps(body))


def wide(tenant: str) -> Principal:
    return system_principal(tenant, purpose="test", request_id="req_1")


async def test_item_and_run_jobs_on_postgres(db: PgDatabase[Any], tenant: str) -> None:
    world = world_for(tenant)
    members = [
        member("m1", {"billing": 0.6, "coding": 0.4}),
        member("m2", {"billing": 0.7, "other": 0.3}),
        member("m3", {"billing": 0.5, "chitchat": 0.5}),
    ]
    d = pg_deps(world, db, ScriptedAdapter(agree_on({"intent": "billing"})))
    async with db.tenant_uow(tenant, project_id="p1") as uow:
        await uow.decisions.insert(full_decision(decision_row("dec_pg1", [answer(INTENT, members)]), world))
    payload: dict[str, JsonValue] = {
        "decision_id": "dec_pg1",
        "turn_id": None,
        "question_names": ["intent"],
        "sample_reason": "uniform",
    }
    await ItemJob(d)(Job("job_pg1", tenant, "labeling.item", payload, 1))
    async with db.tenant_uow(tenant) as uow:
        [run] = (
            await uow.labels.list_runs(profile_id=None, question_name=None, status="running", after=None, limit=10)
        ).data
        [item] = await uow.labels.items(str(run["id"]))
    assert item["kept"] is True and item["value"] == "billing" and item["member_subset"] == ["m1", "m2", "m3"]
    assert [a["status"] for a in item["labeler_answers"]] == ["ok", "ok"]
    # a synthetic month of labels completes the run and fits the binding
    keys = ["chitchat", "coding", "billing", "other"]  # INTENT's declared choices
    ls = labeled_set(1300, temp=0.4, seed=21, members=("m1", "m2", "m3"), options=keys)
    async with db.tenant_uow(tenant) as uow:
        for row in item_rows(ls, str(run["id"])):
            fields = {k: v for k, v in row.items() if k != "tenant_id"}
            await uow.labels.insert_item(fields | {"input_view_signature": item["input_view_signature"]})
    await RunJob(d)(Job("job_pg2", tenant, "labeling.run", {"run_id": str(run["id"])}, 1))
    async with db.uow(wide(tenant)) as uow:
        page = await uow.calibrators.list(
            profile_id="router-default", question_name="intent", status=None, after=None, limit=10
        )
        done = await uow.labels.update_run(str(run["id"]))
    assert done["status"] == "completed" and done["counts"]["sampled"] == 1301
    [cal] = [c for c in page.data if c["tenant_id"] == tenant]
    assert cal["status"] == "accepted" and cal["acceptance"] == {"passed": True, "failed_rules": []}
    assert cal["accepted_at"] is not None and cal["scope"] == "tenant"
    listed = await views.list_calibrators(
        db, wide(tenant), profile_id=None, question_name="intent", status=None, after=None, limit=50
    )
    # the page also holds the platform artifacts other suites store in the session database
    # (db/test_admin_keys_calibrators); this test checks the tenant's own rows
    rows = listed["data"]
    assert isinstance(rows, list)
    own = [r for r in rows if isinstance(r, dict) and r["scope"] == "tenant"]
    assert [r["id"] for r in own] == [cal["id"]]
    check("listCalibrators", {**listed, "data": own})
    check(
        "listLabelingRuns",
        await views.list_runs(db, wide(tenant), profile_id=None, question_name=None, status=None, after=None, limit=50),
    )


async def test_dropped_items_satisfy_the_schema_checks(db: PgDatabase[Any], tenant: str) -> None:
    world = world_for(tenant)
    d = pg_deps(world, db, ScriptedAdapter(lambda alias, aq: None if alias == "sonnet-5.5" else "A"))
    async with db.tenant_uow(tenant, project_id="p1") as uow:
        row = decision_row("dec_pg2", [answer(INTENT, [member("m1", {"billing": 0.6, "coding": 0.4})])])
        await uow.decisions.insert(full_decision(row, world))
    payload: dict[str, JsonValue] = {
        "decision_id": "dec_pg2",
        "turn_id": None,
        "question_names": ["intent"],
        "sample_reason": "uncertainty",
    }
    await ItemJob(d)(Job("job_pg3", tenant, "labeling.item", payload, 1))
    body = await views.list_dropped(
        db, wide(tenant), run_id=None, question_name=None, reason=None, after=None, limit=50
    )
    check("listLabelingDropped", body)
    data = body["data"]
    assert isinstance(data, list) and len(data) == 1 and isinstance(dropped := data[0], dict)
    assert dropped["reason"] == "refused" and str(dropped["id"]).startswith("ldrop_")
    assert dropped["content"] == {"excerpt": "My invoice is wrong", "redacted": False}


async def test_definitions_sharing_a_name_keep_their_own_runs_on_postgres(db: PgDatabase[Any], tenant: str) -> None:
    """The labeling_run_open index of revision 0002 holds one running run per (question name, signature): alternating
    definitions each keep one running run (never a run per item), and a retry finds what it stored with one
    items_for_decision read, so it labels nothing twice."""
    world = world_for(tenant)
    other = {**INTENT, "instructions": "Which department should answer?"}
    other["choices"] = [{"value": v} for v in ("billing", "coding")]
    members = [member("m1", {"billing": 0.6, "coding": 0.4})]
    adapter = ScriptedAdapter(lambda alias, aq: str(aq["options"][0]["letter"]))
    d = pg_deps(world, db, adapter)
    async with db.tenant_uow(tenant, project_id="p1") as uow:
        for i in range(6):
            q = INTENT if i % 2 == 0 else other
            await uow.decisions.insert(full_decision(decision_row(f"dec_alt{i}", [answer(q, members)], [q]), world))

    def payload(i: int) -> dict[str, JsonValue]:
        return {"decision_id": f"dec_alt{i}", "turn_id": None, "question_names": ["intent"], "sample_reason": "uniform"}

    for i in range(6):
        await ItemJob(d)(Job(f"job_alt{i}", tenant, "labeling.item", payload(i), 1))
    asked = len(adapter.requests)
    await ItemJob(d)(Job("job_alt0", tenant, "labeling.item", payload(0), 2))  # a retry of an already stored item
    async with db.tenant_uow(tenant) as uow:
        runs = (
            await uow.labels.list_runs(profile_id=None, question_name="intent", status=None, after=None, limit=10)
        ).data
        sizes = sorted([len(await uow.labels.items(str(r["id"]))) for r in runs])
        stored = await uow.labels.items_for_decision("dec_alt0")
    assert sorted(str(r["status"]) for r in runs) == ["running", "running"] and sizes == [3, 3]
    assert len({r["question_signature"] for r in runs}) == 2 and len(stored) == 1
    assert len(adapter.requests) == asked  # the retry asked no labeler
    check(
        "listLabelingRuns",
        await views.list_runs(db, wide(tenant), profile_id=None, question_name=None, status=None, after=None, limit=50),
    )


async def test_the_drift_slice_reads_kept_items_on_postgres(db: PgDatabase[Any], tenant: str) -> None:
    """`DriftJob._slice` on the real `kept_items` (the label_item_kept index): the kept uniform items of the
    binding's definition since the slice start, one per decision."""
    from datetime import timedelta

    from noema.core.protocols.labeling import Calibrator, CalibratorBindingKey
    from noema.labeling.calibrators.transforms import IdentityTransform
    from noema.labeling.drift import DriftJob

    world = world_for(tenant)
    d = pg_deps(world, db, ScriptedAdapter(agree_on({"intent": "billing"})))
    for i in range(2):
        async with db.tenant_uow(tenant, project_id="p1") as uow:
            row = decision_row(f"dec_drift{i}", [answer(INTENT, [member("m1", {"billing": 0.6, "coding": 0.4})])])
            await uow.decisions.insert(full_decision(row, world))
        payload: dict[str, JsonValue] = {
            "decision_id": f"dec_drift{i}",
            "turn_id": None,
            "question_names": ["intent"],
            "sample_reason": "uniform",
        }
        await ItemJob(d)(Job(f"job_drift{i}", tenant, "labeling.item", payload, 1))
    async with db.tenant_uow(tenant) as uow:
        [run] = (await uow.labels.list_runs(profile_id=None, question_name=None, status=None, after=None, limit=5)).data
    key = CalibratorBindingKey("router-default", "1", str(run["question_signature"]), ("m1",), "sha256:" + "5" * 64)
    ident = IdentityTransform()
    cal = Calibrator("cal_x", "tenant", "temperature", 1e-4, {"m1": 1.0}, ident, lambda m: ident, {}, key)
    since = world.clock.now() - timedelta(days=1)
    items = await DriftJob(d)._slice(tenant, cal, since)
    assert sorted(str(i["decision_id"]) for i in items) == ["dec_drift0", "dec_drift1"]
    assert await DriftJob(d)._slice(tenant, cal, world.clock.now() + timedelta(seconds=1)) == []
