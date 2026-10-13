"""Wave E edge fix 10 on the decision routes: the idempotency record keeps no decision result of a project whose
data class stores content redacted or not at all (it is completed as not_stored; a retry with the same key runs
again), while an `internal` project's result is replayed as before."""

from __future__ import annotations

import json

import pytest
from tests.unit.decision.app import client, headers, make
from tests.unit.decision.test_routes import example, scripted

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("data_class", "replayed"), [("restricted", False), ("confidential", False), ("internal", True)]
)
async def test_a_keyed_decision_is_replayed_only_when_its_content_may_be_kept(data_class: str, replayed: bool) -> None:
    app, _, ew = make()
    now = ew.world.db.store.now()
    ew.world.db.store.seed(
        "project",
        {"tenant_id": "t_acme", "id": "p1", "name": "p1", "default_data_class": data_class, "created_at": now},
    )
    ew.use(scripted() + scripted())
    body = example("decision-request.valid.json")
    keyed = headers(**{"idempotency-key": "dec-key-1"})
    async with client(app) as http, ew.clock.autojump():
        first = await http.post("/v1/decisions", content=json.dumps(body), headers=keyed)
        second = await http.post("/v1/decisions", content=json.dumps(body), headers=keyed)
    assert (first.status_code, second.status_code) == (200, 200), second.text
    assert (second.headers.get("x-aghoo-idempotent-replay") == "true") is replayed
    assert (second.json()["id"] == first.json()["id"]) is replayed
    (record,) = ew.world.idempotency._records.values()
    assert record.state == ("completed" if replayed else "not_stored")


async def test_another_principals_key_is_a_conflict_never_a_replay() -> None:
    """Wave E edge fix 14: the stored fingerprint is bound to the principal, so another principal of the project
    who reuses a key with the same body gets 409 `idempotency_key_reused`, never the first principal's result
    (spec-09 §3: decisions are visible to their creator only); the creator's own retry still replays."""
    from tests.unit.decision.app import OTHER_KEY

    app, _, ew = make()
    ew.use(scripted() + scripted())
    body = json.dumps(example("decision-request.valid.json"))
    key = {"idempotency-key": "order-42"}
    async with client(app) as http, ew.clock.autojump():
        alice = await http.post("/v1/decisions", content=body, headers=headers(**key))
        bob = await http.post("/v1/decisions", content=body, headers=headers(OTHER_KEY, **key))
        again = await http.post("/v1/decisions", content=body, headers=headers(**key))
    assert alice.status_code == 200
    assert bob.status_code == 409 and bob.json()["error"]["code"] == "idempotency_key_reused"
    assert alice.json()["id"] not in bob.text
    assert again.headers["x-aghoo-idempotent-replay"] == "true" and again.json()["id"] == alice.json()["id"]


async def test_the_file_checks_run_under_the_request_deadline() -> None:
    """Wave E2 (edge #8 rest): createDecision's file checks (`prepare_native` -> `check_files`, before any
    dispatch) run under `Aghoo-Deadline-Ms`; a file lookup that never answers ends in 504 `deadline_exceeded`
    (IMPL-032: before dispatch, no row) instead of holding the request and its slot."""
    import anyio
    from tests.unit.decision.test_inputs import _image_body

    app, svc, ew = make()

    async def hung(who: object, file_id: str) -> object:
        await anyio.sleep_forever()
        raise AssertionError("unreachable")

    svc.files.get = hung
    body = json.dumps(_image_body(["file_01J0000000000000000000HUNG"]))
    with anyio.fail_after(10):  # real time: a safety net only (before the fix the request never answered)
        async with client(app) as http, ew.clock.autojump():
            resp = await http.post("/v1/decisions", content=body, headers=headers(**{"aghoo-deadline-ms": "2000"}))
    assert (resp.status_code, resp.json()["error"]["code"]) == (504, "deadline_exceeded")
