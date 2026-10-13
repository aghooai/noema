"""The decision routes (spec-06 §12, §15; IMPL-031, IMPL-057): POST/GET /v1/decisions, the facade, /v1/profiles."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from tests.unit.decision.app import OTHER_KEY, client, headers, make
from tests.unit.decision.world import DEEPSEEK, HAIKU, LUNA, answer, fault

import noema.contracts

EXAMPLES = Path(noema.contracts.__file__).parent / "_vendor" / "examples"
DEPT = {"billing": 0.7, "technical": 0.1, "shipping": 0.1, "other": 0.1}


def example(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((EXAMPLES / name).read_text())
    return loaded


def scripted() -> list[dict[str, Any]]:
    return [
        answer(None, "speak with a human", {"true": 0.1, "false": 0.9}, 300),
        answer(None, "How urgent", {"low": 0.2, "medium": 0.6, "high": 0.2}, 300),
        answer(None, "How severe", {"Cosmetic": 0.1, "Workaround available": 0.7, "Fully blocked": 0.2}, 300),
        answer(None, "regression", {"true": 0.72, "false": 0.28}, 300),
        answer(None, None, DEPT, 300),
    ]


async def test_create_and_get_a_native_decision() -> None:
    app, _, ew = make()
    ew.use(scripted())
    body = example("decision-request.valid.json")
    async with client(app) as http, ew.clock.autojump():
        resp = await http.post("/v1/decisions", content=json.dumps(body), headers=headers())
        assert resp.status_code == 200, resp.text
        result = resp.json()
        assert result["object"] == "decision" and result["status"] == "completed"
        assert "members" in result["answers"][0]
        got = await http.get(f"/v1/decisions/{result['id']}", headers=headers())
        assert got.status_code == 200 and got.json()["id"] == result["id"]
        hidden = await http.get(f"/v1/decisions/{result['id']}", headers=headers(OTHER_KEY))
        assert hidden.status_code == 404 and hidden.json()["error"]["code"] == "not_found"
        missing = await http.get("/v1/decisions/dec_01J00000000000000000000000", headers=headers())
        assert missing.status_code == 404


async def test_include_omitted_drops_members_and_jev_format() -> None:
    app, _, ew = make()
    ew.use(scripted())
    body = {k: v for k, v in example("decision-request.valid.json").items() if k != "include"}
    jev_body = example("decision-request.valid-jev.json")
    async with client(app) as http, ew.clock.autojump():
        resp = await http.post("/v1/decisions", content=json.dumps(body), headers=headers())
        assert resp.status_code == 200 and all("members" not in a for a in resp.json()["answers"])
        jev = await http.post("/v1/decisions", content=json.dumps(jev_body), headers=headers())
        assert jev.status_code == 200, jev.text
        assert jev.json()["model"] == "aghoo/lightweight-model-decide@1" and set(jev.json()["answers"]) == {
            "department",
            "urgency",
            "needs_human",
        }


async def test_profile_errors() -> None:
    app, _, ew = make()
    ew.use(scripted())
    base = example("decision-request.valid.json")
    hier = example("decision-request.valid-hierarchical.json")
    cases = [
        ({**base, "profile": "answer-verifier"}, 403, "profile_not_allowed"),
        ({**base, "profile": "nope"}, 403, "profile_not_allowed"),
        ({**base, "profile": "router-default@7"}, 422, "unsupported_parameter"),
        ({**base, "min_quorum": 4}, 422, "unsupported_parameter"),
        ({**hier, "output_format": "jev"}, 422, "unsupported_parameter"),
    ]
    async with client(app) as http, ew.clock.autojump():
        for body, status, code in cases:
            resp = await http.post("/v1/decisions", content=json.dumps(body), headers=headers())
            assert (resp.status_code, resp.json()["error"]["code"]) == (status, code), body
        allowed = await http.post("/v1/decisions", content=json.dumps({**base, "profile": "nope"}), headers=headers())
        assert allowed.json()["error"]["details"]["allowed"] == [
            "lightweight-model-decide",
            "router-default",
            "router-plus",
        ]
    assert ew.world.adapters.calls() == []


async def test_default_profile_and_all_unavailable_is_200_partial() -> None:
    app, _, ew = make()
    ew.use([fault(None, "http_5xx", 50)])
    body = example("decision-request.valid-default-profile.json")
    async with client(app) as http, ew.clock.autojump():
        too_many = await http.post("/v1/decisions", content=json.dumps(body), headers=headers())
        assert too_many.status_code == 422 and too_many.json()["error"]["details"] == {"max": 1}  # Qmax of the default
        body = {k: v for k, v in body.items() if k != "min_quorum"}
        resp = await http.post("/v1/decisions", content=json.dumps(body), headers=headers())
    assert resp.status_code == 200 and resp.json()["status"] == "partial"
    assert resp.json()["provenance"]["profile_id"] == "lightweight-model-decide"


async def test_all_tenant_access_failures_are_503_with_the_decision_id() -> None:
    app, _, ew = make()
    ew.use([fault(None, "access_denied", 50)])
    body = {**example("decision-request.valid.json"), "include": []}
    async with client(app) as http, ew.clock.autojump():
        resp = await http.post("/v1/decisions", content=json.dumps(body), headers=headers())
        assert resp.status_code == 503
        err = resp.json()["error"]
        assert err["code"] == "tenant_access_misconfigured" and err["details"]["decision_id"].startswith("dec_")
        stored = await http.get(f"/v1/decisions/{err['details']['decision_id']}", headers=headers())
        assert stored.status_code == 200


async def test_credentials_and_deadline_failures_store_nothing() -> None:
    app, _, ew = make()
    ew.use(scripted())
    body = example("decision-request.valid.json")
    async with client(app) as http, ew.clock.autojump():
        ew.world.broker.fail("t_acme", "access_denied")
        resp = await http.post("/v1/decisions", content=json.dumps(body), headers=headers())
        assert (resp.status_code, resp.json()["error"]["code"]) == (503, "tenant_access_misconfigured")
        ew.world.broker.fail("t_acme", None)
        late = await http.post(
            "/v1/decisions", content=json.dumps(body), headers=headers(**{"aghoo-deadline-ms": "100"})
        )
        assert late.status_code == 200  # 100 ms still leaves >= 50 ms to dispatch
    assert len(ew.world.db.store.rows("decision")) == 1


async def test_session_and_file_checks() -> None:
    app, _, ew = make()
    ew.use(scripted())
    base = example("decision-request.valid.json")
    img = example("decision-request.valid-image.json")
    async with client(app) as http, ew.clock.autojump():
        resp = await http.post(
            "/v1/decisions",
            content=json.dumps({**base, "session_id": "ses_01J0000000000000000000000X"}),
            headers=headers(),
        )
        assert (resp.status_code, resp.json()["error"]["code"]) == (404, "session_not_found")
        resp = await http.post("/v1/decisions", content=json.dumps(img), headers=headers())
        assert (resp.status_code, resp.json()["error"]["code"]) == (404, "not_found")


async def test_facade_round_trip_ensemble_and_errors() -> None:
    app, _, ew = make()
    ew.use(scripted())
    oa = example("openai-request.valid.json")
    async with client(app) as http, ew.clock.autojump():
        resp = await http.post("/openai/v1/decisions", content=json.dumps(oa), headers=headers())
        assert resp.status_code == 200, resp.text
        assert resp.json()["answers"] == example("openai-response.valid.json")["answers"]
        assert "x_aghoo" not in resp.json()
        ens = await http.post(
            "/openai/v1/decisions", content=json.dumps(oa), headers=headers(**{"x-aghoo-include": "ensemble"})
        )
        x = ens.json()["x_aghoo"]
        assert set(x["calibration"]) == {"q0", "regression"} and x["decision_id"].startswith("dec_")
        stored = await http.get(f"/v1/decisions/{x['decision_id']}", headers=headers())
        assert [a["name"] for a in stored.json()["answers"]] == ["q0", "regression"]
        for model, code in (
            ("router-default", "model_not_available"),
            ("aghoo/router-default@9", "model_not_available"),
            ("aghoo/answer-verifier", "profile_not_allowed"),
        ):
            bad = await http.post("/openai/v1/decisions", content=json.dumps({**oa, "model": model}), headers=headers())
            assert bad.json()["error"]["code"] == code
        many = {
            **oa,
            "questions": [
                {"type": "choice", "instructions": "pick", "choices": [{"value": f"v{i}"} for i in range(30)]}
            ],
        }
        big = await http.post("/openai/v1/decisions", content=json.dumps(many), headers=headers())
        assert (big.status_code, big.json()["error"]["param"]) == (422, "questions[0].choices")


async def test_facade_all_unavailable_is_503() -> None:
    app, _, ew = make()
    ew.use([fault(None, "http_5xx", 50)])
    oa = example("openai-request.valid.json")
    async with client(app) as http, ew.clock.autojump():
        resp = await http.post("/openai/v1/decisions", content=json.dumps(oa), headers=headers())
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "all_routes_unavailable" and resp.json()["error"]["details"]["decision_id"]


async def test_list_profiles() -> None:
    app, _, _ = make()
    async with client(app) as http:
        resp = await http.get("/v1/profiles", headers=headers())
        data = resp.json()["data"]
        decision = [p for p in data if p["kind"] == "decision"]
        assert [p["id"] for p in decision] == ["lightweight-model-decide", "router-default", "router-plus"]
        rd = next(p for p in decision if p["id"] == "router-default")
        assert rd["version"] == "1" and rd["models"] == [DEEPSEEK, LUNA, HAIKU]
        assert rd["modalities"] == ["text", "image", "document"]
        assert rd["description"] == "3 members: deepseek-3.2, luna-6, haiku-5.5; linear_pool; deadline 1000 ms"
        gen = [p["id"] for p in data if p["kind"] == "generation"]
        assert gen == ["glm-reason", "opus-deep", "race-sol-glm", "sol-fast"]  # best_of_n omitted
        only = await http.get("/v1/profiles?kind=decision", headers=headers())
        assert {p["kind"] for p in only.json()["data"]} == {"decision"}


RATE = {f"x-ratelimit-{k}-{u}" for k in ("limit", "remaining", "reset") for u in ("requests", "tokens")}


async def test_native_jev_and_facade_200s_carry_the_rate_limit_headers() -> None:
    """Review finding 5 (IMPL-033, spec-08 §4): every 2xx of createDecision and the facade has the six
    x-ratelimit-* headers of the most constraining buckets, as the reservation reported them after the debit."""
    app, _, ew = make()
    ew.use(scripted())
    native = example("decision-request.valid.json")
    jev = example("decision-request.valid-jev.json")
    oa = example("openai-request.valid.json")
    async with client(app) as http, ew.clock.autojump():
        for path, body in (("/v1/decisions", native), ("/v1/decisions", jev), ("/openai/v1/decisions", oa)):
            resp = await http.post(path, content=json.dumps(body), headers=headers())
            assert resp.status_code == 200, resp.text
            assert set(resp.headers) >= RATE, (path, sorted(resp.headers))
            assert int(resp.headers["x-ratelimit-remaining-requests"]) < int(resp.headers["x-ratelimit-limit-requests"])


class QueuedAdmission:
    """The world's admission behind a queue wait of `wait_s` (virtual); keeps the reservations it handed out."""

    def __init__(self, inner: Any, clock: Any, wait_s: float) -> None:
        self.inner, self.clock, self.wait_s = inner, clock, wait_s
        self.reservations: list[Any] = []

    async def reserve(self, *args: Any, **kw: Any) -> Any:
        await self.clock.sleep(self.wait_s)
        reservation = await self.inner.reserve(*args, **kw)
        self.reservations.append(reservation)
        return reservation


async def test_no_time_left_to_dispatch_is_504_and_a_retry_re_executes() -> None:
    """Review finding 6 (spec-06 §2.3, IMPL-032): a 60 ms admission wait leaves 40 ms of Aghoo-Deadline-Ms 100 at
    dispatch -> 504 `deadline_exceeded`: no call, no row, no usage line, the reservation released unconsumed, and
    nothing stored under the Idempotency-Key, so the same key re-executes."""
    app, svc, ew = make()
    ew.use(scripted())
    queued = QueuedAdmission(ew.world.admission, ew.clock, 0.06)
    cast(Any, svc).admission = queued
    body = example("decision-request.valid.json")
    h = headers(**{"aghoo-deadline-ms": "100", "idempotency-key": "k-504"})
    async with client(app) as http, ew.clock.autojump():
        late = await http.post("/v1/decisions", content=json.dumps(body), headers=h)
        assert (late.status_code, late.json()["error"]["code"]) == (504, "deadline_exceeded")
        assert ew.world.adapters.calls() == [] and ew.world.cost.lines == []
        assert ew.world.db.store.rows("decision") == []
        (reservation,) = queued.reservations
        assert reservation.released and not reservation.consumed
        queued.wait_s = 0.0
        retry = await http.post("/v1/decisions", content=json.dumps(body), headers=h)
    assert retry.status_code == 200, retry.text
    assert "x-aghoo-idempotent-replay" not in retry.headers and ew.world.adapters.calls() != []
    assert [r["id"] for r in ew.world.db.store.rows("decision")] == [retry.json()["id"]]


async def test_the_data_class_fails_closed_when_the_project_cannot_be_read() -> None:
    """spec-05 §1.1, P1-C4: a project read that fails (Postgres down, a pool or statement timeout) never gives a
    laxer data class than the project's own: the decision continues (IMPL-022) as `restricted` (metadata_only, no
    L2, 7 days), not `internal` (Wave E fix)."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from noema.decision.service import data_class_of

    def svc(fail: bool) -> Any:
        async def get(project_id: str) -> dict[str, Any]:
            if fail:
                raise TimeoutError("pool acquire timed out")
            return {"default_data_class": "confidential"}

        @asynccontextmanager
        async def uow(who: Any) -> Any:
            yield SimpleNamespace(projects=SimpleNamespace(get=get))

        return SimpleNamespace(db=SimpleNamespace(uow=uow))

    who = SimpleNamespace(project_id="prj_1", tenant_id="t_acme")
    assert await data_class_of(svc(False), who, None) == "confidential"  # type: ignore[arg-type]
    assert await data_class_of(svc(True), who, None) == "restricted"  # type: ignore[arg-type]
