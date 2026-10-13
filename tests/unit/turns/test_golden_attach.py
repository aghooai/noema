"""G12 / G12b (spec-04 §4.2): an idempotent duplicate of a streamed turn attaches to the original's event log
(R2 in flight: backlog then live; R3 replay inside the resume window; R4 410 after it; R5 409 with another body),
on the same API process (G12) and on another one through the Redis mirror (G12b). Through the real routes and
ingress middleware, with the response validator on."""

from __future__ import annotations

from typing import Any

import anyio
import httpx
import orjson
import pytest
from asgi_lifespan import LifespanManager
from tests.golden.compare import compare, load_fixture

from noema.contracts.validate import ContractValidator

from .harness import TOKEN, TurnHarness, app_for, make_harness, second_pod

pytestmark = pytest.mark.unit


def _headers(key: str = "k1") -> dict[str, str]:
    return {
        "authorization": f"Bearer {TOKEN}",
        "content-type": "application/json",
        "Aghoo-Version": "2026-10-01",
        "Idempotency-Key": key,
    }


async def _post(client: httpx.AsyncClient, body: dict[str, Any], key: str = "k1") -> httpx.Response:
    return await client.post("/v1/turns", content=orjson.dumps(body), headers=_headers(key))


def _payloads(body: bytes) -> list[dict[str, Any]]:
    parsed = ContractValidator().check_sse_stream("createTurn", body)
    return [e.data for e in parsed]  # type: ignore[misc]


async def _scenario(h: TurnHarness, other: TurnHarness | None, fx: dict[str, Any]) -> None:
    body = fx["request"]["body"]
    app1 = app_for(h, ingress_delay_s=0.006)
    app2 = app_for(other, ingress_delay_s=0.006) if other is not None else app1
    results: dict[str, httpx.Response] = {}
    async with LifespanManager(app1) as m1, LifespanManager(app2) as m2:
        c1 = httpx.AsyncClient(transport=httpx.ASGITransport(app=m1.app), base_url="http://t")
        c2 = httpx.AsyncClient(transport=httpx.ASGITransport(app=m2.app), base_url="http://t")
        async with c1, c2, anyio.create_task_group() as tg:

            async def r1() -> None:
                results["R1"] = await _post(c1, body)

            async def r2() -> None:
                await h.clock.sleep(0.8)
                results["R2"] = await _post(c2, body)

            tg.start_soon(r1)
            tg.start_soon(r2)
        c3 = httpx.AsyncClient(transport=httpx.ASGITransport(app=m1.app), base_url="http://t")
        async with c3:
            results["R3"] = await _post(c3, body)
            await h.clock.sleep(h.world.config.current().platform.events.resume_window_s + 1)
            results["R4"] = await _post(c3, body)
            results["R5"] = await _post(c3, {**body, "store": False})
    h.results = results  # type: ignore[attr-defined]
    assert app1.state.contract_violations == [] and app2.state.contract_violations == []


@pytest.mark.parametrize("fixture_id", ["G12", "G12b"])
async def test_idempotent_duplicate_attach(fixture_id: str) -> None:
    fx = load_fixture(fixture_id)
    h = make_harness(scenario=fx["scenario"])
    session = fx["setup"]["session"]
    h.seed_session(session["id"], last_seq=session["last_seq"], sticky_spec=session["sticky_spec"])
    other = second_pod(h) if fixture_id == "G12b" else None
    await h.world.runtime.open()
    if other is not None:
        await other.runtime.open()
    try:
        async with h.clock.autojump():
            await _scenario(h, other, fx)
    finally:
        if other is not None:
            await other.runtime.close()
        await h.world.runtime.close()
    r: dict[str, httpx.Response] = h.results  # type: ignore[attr-defined]
    assert r["R1"].status_code == 200 and r["R1"].headers["content-type"].startswith("text/event-stream")
    r1 = _payloads(r["R1"].content)
    assert compare(r1, fx) == []
    assert r["R2"].status_code == 200 and r["R2"].headers["x-aghoo-idempotent-replay"] == "true"
    assert r["R2"].content == r["R1"].content  # byte-identical frames from id 1 to turn.completed
    assert r["R3"].status_code == 200 and r["R3"].content == r["R1"].content
    assert r["R4"].status_code == 410
    error = r["R4"].json()["error"]
    assert error["code"] == "resume_window_expired" and error["details"]["turn_id"] == r1[0]["turn_id"]
    assert r["R5"].status_code == 409 and r["R5"].json()["error"]["code"] == "idempotency_key_reused"
    assert len(h.world.generation.runs) == 1  # no new turn, no new mock requests
    lines = [line for line in h.world.cost.lines if line.attempt_id]
    assert len(lines) == 1  # no new usage lines
