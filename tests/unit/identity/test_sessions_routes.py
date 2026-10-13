"""Session routes through the real middleware and contract validator (spec-08 §9, spec-09 §3): create, get, list,
update, delete, history, idempotent create, pagination, the authz cells of the six operations."""

from __future__ import annotations

from datetime import timedelta

import pytest
from tests.unit.identity.sessions_rig import (
    ADMIN,
    AUD,
    DEV,
    DEV_P2,
    EU,
    NO_SESSIONS_SCOPE,
    OTHER,
    PEER,
    REV,
    T_A,
    Caller,
    client,
    seed_turn,
    sessions_world,
)

JSON = {"content-type": "application/json"}


async def create(http: object, who: Caller, body: str = "{}", **headers: str) -> dict[str, object]:
    response = await http.post("/v1/sessions", content=body, headers=who.headers(**JSON, **headers))  # type: ignore[attr-defined]
    assert response.status_code == 201, response.text
    data: dict[str, object] = response.json()
    return data


async def test_create_defaults_and_retention() -> None:
    world = sessions_world()
    async with client(world) as http:
        s = await create(http, DEV, '{"metadata": {"app": "support"}}')
        assert s["owner"] == "prn_dev" and s["last_seq"] == 0 and s["metadata"] == {"app": "support"}
        assert s["data_class"] == "internal" and s["retention"] == {
            "policy": "default",
            "expires_at": s["created_at"] + 90 * 86400,  # type: ignore[operator]
        }
        e = await create(http, EU, '{"retention": "ephemeral", "data_class": "restricted"}')
        assert e["retention"]["expires_at"] == e["created_at"] + 86400  # type: ignore[index,operator]
        r = await create(http, DEV, '{"data_class": "confidential"}')
        assert r["retention"]["expires_at"] == r["created_at"] + 30 * 86400  # type: ignore[index,operator]
        custom = await http.post("/v1/sessions", content='{"retention": "custom"}', headers=DEV.headers(**JSON))
        assert custom.status_code == 422
        assert custom.json()["error"]["code"] == "feature_not_available"
        assert custom.json()["error"]["param"] == "retention"
        empty = await http.post("/v1/sessions", headers=DEV.headers())  # no body at all
        assert empty.status_code == 201
    row = world.db.store.table("session")[(T_A, s["id"])]
    assert row["project_id"] == "p1" and row["expires_at"] - row["created_at"] == timedelta(days=90)


async def test_create_role_and_scope_failures_are_403() -> None:
    world = sessions_world()
    async with client(world) as http:
        for who in (AUD, REV, NO_SESSIONS_SCOPE):
            response = await http.post("/v1/sessions", content="{}", headers=who.headers(**JSON))
            assert response.status_code == 403 and response.json()["error"]["code"] == "permission_denied"
    assert world.db.store.rows("session") == []


async def test_create_is_idempotent() -> None:
    world = sessions_world()
    async with client(world) as http:
        first = await http.post("/v1/sessions", content="{}", headers=DEV.headers(**JSON, **{"idempotency-key": "k1"}))
        again = await http.post("/v1/sessions", content="{}", headers=DEV.headers(**JSON, **{"idempotency-key": "k1"}))
        other = await http.post(
            "/v1/sessions", content='{"metadata": {"a": "b"}}', headers=DEV.headers(**JSON, **{"idempotency-key": "k1"})
        )
    assert first.status_code == again.status_code == 201
    assert again.json()["id"] == first.json()["id"] and again.headers["x-aghoo-idempotent-replay"] == "true"
    assert other.status_code == 409 and other.json()["error"]["code"] == "idempotency_key_reused"
    assert len(world.db.store.rows("session")) == 1


@pytest.mark.parametrize(
    ("who", "status"),
    [
        (DEV, 200),
        (PEER, 404),
        (EU, 404),
        (REV, 404),
        (ADMIN, 200),
        (AUD, 200),
        (DEV_P2, 404),
        (OTHER, 404),
        (NO_SESSIONS_SCOPE, 404),
    ],
)
async def test_get_visibility(who: Caller, status: int) -> None:
    world = sessions_world()
    async with client(world) as http:
        s = await create(http, DEV)
        response = await http.get(f"/v1/sessions/{s['id']}", headers=who.headers())
        assert response.status_code == status
        if status == 404:
            assert response.json()["error"]["code"] == "session_not_found"
        malformed = await http.get("/v1/sessions/not-a-session", headers=DEV.headers())
        assert malformed.status_code == 404


async def test_list_scopes_and_pagination() -> None:
    world = sessions_world()
    async with client(world) as http:
        mine = [await create(http, DEV) for _ in range(3)]
        await create(http, PEER)
        await create(http, DEV_P2)
        dev = (await http.get("/v1/sessions", headers=DEV.headers())).json()
        assert [s["id"] for s in dev["data"]] == [s["id"] for s in reversed(mine)]  # newest first
        assert dev["has_more"] is False and dev["next_cursor"] is None
        assert len((await http.get("/v1/sessions", headers=ADMIN.headers())).json()["data"]) == 5
        assert len((await http.get("/v1/sessions", headers=AUD.headers())).json()["data"]) == 5
        for who in (REV, NO_SESSIONS_SCOPE):
            empty = await http.get("/v1/sessions", headers=who.headers())
            assert empty.status_code == 200 and empty.json() == {"data": [], "has_more": False, "next_cursor": None}
        assert (await http.get("/v1/sessions", headers=OTHER.headers())).json()["data"] == []
        seen: list[str] = []
        cursor = None
        while True:
            url = "/v1/sessions?limit=2" + (f"&after={cursor}" if cursor else "")
            page = (await http.get(url, headers=DEV.headers())).json()
            seen += [s["id"] for s in page["data"]]
            cursor = page["next_cursor"]
            if not page["has_more"]:
                break
        assert seen == [s["id"] for s in reversed(mine)]
        bad = await http.get("/v1/sessions?after=%%%", headers=DEV.headers())
        assert bad.status_code == 400 and bad.json()["error"]["code"] == "schema_violation"
        too_big = await http.get("/v1/sessions?limit=201", headers=DEV.headers())
        assert too_big.status_code == 400


async def test_update() -> None:
    world = sessions_world()
    async with client(world) as http:
        s = await create(http, DEV, '{"metadata": {"a": "1", "b": "2"}}')
        url = f"/v1/sessions/{s['id']}"
        meta = await http.patch(url, content='{"metadata": {"c": "3"}}', headers=DEV.headers(**JSON))
        assert meta.status_code == 200 and meta.json()["metadata"] == {"c": "3"}  # replace, not merge
        assert meta.json()["retention"] == s["retention"]
        dc = await http.patch(url, content='{"data_class": "restricted"}', headers=ADMIN.headers(**JSON))
        assert dc.status_code == 200 and dc.json()["data_class"] == "restricted"
        assert dc.json()["retention"]["expires_at"] == s["created_at"] + 7 * 86400  # type: ignore[operator]
        eph = await http.patch(url, content='{"retention": "ephemeral"}', headers=DEV.headers(**JSON))
        assert eph.json()["retention"] == {"policy": "ephemeral", "expires_at": s["created_at"] + 86400}  # type: ignore[operator]
        cleared = await http.patch(url, content='{"metadata": {}}', headers=DEV.headers(**JSON))
        assert cleared.json()["metadata"] == {}
        for who, status, code in (
            (PEER, 404, "session_not_found"),
            (AUD, 403, "permission_denied"),
            (REV, 403, "permission_denied"),
            (OTHER, 404, "session_not_found"),
        ):
            response = await http.patch(url, content='{"metadata": {}}', headers=who.headers(**JSON))
            assert (response.status_code, response.json()["error"]["code"]) == (status, code), who
        custom = await http.patch(url, content='{"retention": "custom"}', headers=DEV.headers(**JSON))
        assert custom.status_code == 422
        empty = await http.patch(url, content="{}", headers=DEV.headers(**JSON))
        assert empty.status_code == 400
    audits = [a for a in world.db.store.rows("audit_event") if a["action"] == "session.data_class.update"]
    assert len(audits) == 1
    assert audits[0]["actor_id"] == "prn_admin" and audits[0]["details"] == {"from": "internal", "to": "restricted"}
    assert audits[0]["resource_id"] == s["id"] and audits[0]["tenant_id"] == T_A


async def test_delete_then_the_job_purges_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.fakes.db_repos import FakeIdempotencyRepo

    world = sessions_world()
    purged: list[tuple[str, str]] = []
    expired: list[str] = []  # the Postgres repo's expire_for_session (Wave E2) is beyond the frozen fake

    async def expire_for_session(self: object, session_id: str) -> int:
        expired.append(session_id)
        return 0

    monkeypatch.setattr(FakeIdempotencyRepo, "expire_for_session", expire_for_session, raising=False)

    async def purge(tenant_id: str, file_id: str) -> None:
        purged.append((tenant_id, file_id))
        world.db.store.table("file").pop((tenant_id, file_id), None)

    world.files.purge = purge  # type: ignore[method-assign]
    async with client(world) as http:
        s = await create(http, DEV)
        keep = await create(http, DEV)
        seed_turn(world, str(s["id"]), 1)
        seed_turn(world, str(keep["id"]), 1)
        world.db.store.seed("file", {"tenant_id": T_A, "project_id": "p1", "id": "file_1", "session_id": s["id"]})
        world.db.store.seed(
            "decision", {"tenant_id": T_A, "project_id": "p1", "id": "dec_1", "session_id": s["id"], "turn_id": None}
        )
        url = f"/v1/sessions/{s['id']}"
        for who, status in ((PEER, 404), (AUD, 403), (REV, 403)):
            assert (await http.delete(url, headers=who.headers())).status_code == status
        deleted = await http.delete(url, headers=DEV.headers())
        assert deleted.status_code == 202
        body = deleted.json()
        assert body["id"] == s["id"] and body["deleted"] is True
        assert (await http.get(url, headers=DEV.headers())).status_code == 404
        assert (await http.delete(url, headers=DEV.headers())).status_code == 404
        listed = (await http.get("/v1/sessions", headers=DEV.headers())).json()["data"]
        assert [x["id"] for x in listed] == [keep["id"]]
    (job,) = world.db.store.rows("job")
    assert job["id"] == body["deletion_job_id"] and job["kind"] == "session.delete"
    assert job["payload"] == {"session_id": s["id"]} and job["dedupe_key"] == s["id"]
    from noema.core.protocols.storage import Job
    from noema.identity.jobs import make_handlers

    handler = make_handlers(world)["session.delete"]  # type: ignore[arg-type]
    await handler(Job(id=str(job["id"]), tenant_id=T_A, kind="session.delete", payload=job["payload"], attempt=1))
    await handler(Job(id=str(job["id"]), tenant_id=T_A, kind="session.delete", payload=job["payload"], attempt=2))
    assert purged == [(T_A, "file_1")]
    assert expired == [s["id"]] * 4  # before the turns and decisions go, and again for late finalizes; per run
    assert (T_A, s["id"]) not in world.db.store.table("session")
    assert [t["session_id"] for t in world.db.store.rows("turn")] == [keep["id"]]
    assert world.db.store.rows("decision") == []
    assert (T_A, keep["id"]) in world.db.store.table("session")


async def test_job_rejects_a_bad_payload() -> None:
    from noema.core.protocols.storage import Job, PermanentJobError
    from noema.identity.jobs import make_handlers

    handler = make_handlers(sessions_world())["session.delete"]  # type: ignore[arg-type]
    with pytest.raises(PermanentJobError):
        await handler(Job(id="job_1", tenant_id=T_A, kind="session.delete", payload={}, attempt=1))


async def test_history_messages_and_pagination() -> None:
    world = sessions_world()
    async with client(world) as http:
        s = await create(http, DEV)
        sid = str(s["id"])
        seed_turn(world, sid, 1)
        seed_turn(
            world,
            sid,
            2,
            output_text="mail me at [REDACTED:pii_email]",
            redactions={"user": [], "assistant": [{"start": 11, "end": 31, "reason": "pii_email"}]},
        )
        seed_turn(world, sid, 3, content_policy="metadata_only", input_parts=None, output_text=None, status="failed")
        url = f"/v1/sessions/{sid}/turns"
        page = (await http.get(url, headers=DEV.headers())).json()
        assert [(m["seq"], m["role"]) for m in page["data"]] == [
            (1, "user"),
            (1, "assistant"),
            (2, "user"),
            (2, "assistant"),
            (3, "user"),
            (3, "assistant"),
        ]
        assert page["data"][0]["text"] == "question 1" and page["data"][1]["text"] == "answer 1"
        assert page["data"][3]["text"] == "mail me at [redacted]"
        assert page["data"][3]["redactions"] == [{"start": 11, "end": 21, "reason": "pii_email"}]
        assert page["data"][4]["text"] == "[redacted]" and page["data"][4]["redactions"][0]["reason"] == "metadata_only"
        assert page["data"][1]["created_at"] >= page["data"][0]["created_at"]
        first = (await http.get(f"{url}?limit=4", headers=DEV.headers())).json()
        assert [m["seq"] for m in first["data"]] == [1, 1, 2, 2] and first["has_more"] is True
        rest = (await http.get(f"{url}?limit=4&after={first['next_cursor']}", headers=DEV.headers())).json()
        assert [m["seq"] for m in rest["data"]] == [3, 3] and rest["has_more"] is False
        for who, status in ((PEER, 404), (REV, 404), (AUD, 200), (ADMIN, 200), (OTHER, 404)):
            assert (await http.get(url, headers=who.headers())).status_code == status, who


async def walk(http: object, url: str, limit: int) -> list[list[tuple[int, str]]]:
    pages: list[list[tuple[int, str]]] = []
    after = ""
    while True:
        response = await http.get(f"{url}?limit={limit}{after}", headers=DEV.headers())  # type: ignore[attr-defined]
        assert response.status_code == 200, response.text
        body = response.json()
        pages.append([(m["seq"], m["role"]) for m in body["data"]])
        assert len(body["data"]) <= limit and body["has_more"] is (body["next_cursor"] is not None)
        if not body["has_more"]:
            return pages
        after = f"&after={body['next_cursor']}"


async def test_history_limit_counts_messages() -> None:
    """`limit` caps the messages of a page (a page may end between a turn's user and assistant messages)."""
    world = sessions_world()
    async with client(world) as http:
        sid = str((await create(http, DEV))["id"])
        for seq in (1, 2, 3):
            seed_turn(world, sid, seq)
        url = f"/v1/sessions/{sid}/turns"
        everything = [(seq, role) for seq in (1, 2, 3) for role in ("user", "assistant")]
        for limit in (1, 2, 3, 4, 5, 6, 7, 200):
            pages = await walk(http, url, limit)
            assert [m for page in pages for m in page] == everything, limit
            assert all(len(page) == limit for page in pages[:-1]), limit
        assert (await walk(http, url, 3))[0] == [(1, "user"), (1, "assistant"), (2, "user")]
        empty = (await http.get(f"/v1/sessions/{(await create(http, DEV))['id']}/turns", headers=DEV.headers())).json()
        assert empty == {"data": [], "has_more": False, "next_cursor": None}


async def test_history_cursor_survives_a_deleted_turn_and_rejects_tampering() -> None:
    world = sessions_world()
    async with client(world) as http:
        sid = str((await create(http, DEV))["id"])
        for seq in (1, 2, 3):
            seed_turn(world, sid, seq)
        url = f"/v1/sessions/{sid}/turns"
        first = (await http.get(f"{url}?limit=3", headers=DEV.headers())).json()
        assert [(m["seq"], m["role"]) for m in first["data"]][-1] == (2, "user")
        turns = world.db.store.table("turn")
        del turns[next(k for k, r in turns.items() if r["seq"] == 2)]  # the half-delivered turn is gone (retention)
        rest = (await http.get(f"{url}?limit=3&after={first['next_cursor']}", headers=DEV.headers())).json()
        assert [(m["seq"], m["role"]) for m in rest["data"]] == [(3, "user"), (3, "assistant")]
        for bad in ("not-a-cursor!", "WzEsMl0", "WyJ4IiwtMV0", "WyJ4Iix0cnVlXQ"):  # [1,2], ["x",-1], ["x",true]
            response = await http.get(f"{url}?after={bad}", headers=DEV.headers())
            assert response.status_code == 400 and response.json()["error"]["param"] == "after", bad


async def test_metadata_is_bounded_on_create_and_update() -> None:
    """Wave E edge fix 13: session metadata is at most 16 pairs, keys of at most 64 and values of at most 512
    characters (400 `schema_violation`), so one key cannot store megabytes per session and inflate listSessions."""
    import json

    world = sessions_world()
    at_limit = {f"k{i:02d}".ljust(64, "k"): "v" * 512 for i in range(16)}
    too_many = {**at_limit, "one-more": "v"}
    async with client(world) as http:
        s = await create(http, DEV, json.dumps({"metadata": at_limit}))
        assert s["metadata"] == at_limit
        url = f"/v1/sessions/{s['id']}"
        for metadata, param in (
            (too_many, "metadata"),
            ({"k" * 65: "v"}, "metadata"),
            ({"k": "v" * 513}, "metadata.k"),
        ):
            body = json.dumps({"metadata": metadata})
            for response in (
                await http.post("/v1/sessions", content=body, headers=DEV.headers(**JSON)),
                await http.patch(url, content=body, headers=DEV.headers(**JSON)),
            ):
                assert response.status_code == 400, response.text
                error = response.json()["error"]
                assert (error["code"], error["param"]) == ("schema_violation", param)
    assert len(world.db.store.rows("session")) == 1
