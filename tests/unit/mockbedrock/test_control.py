"""The control API (spec-10 §2.1): requests (paging, bounded log), scenario load (YAML/JSON, errors), barriers,
reset, quotas (C1: per-(tenant, model) throttling above Q) and the no-rule / no-scenario failures."""

from __future__ import annotations

import asyncio
import base64

import httpx
import orjson
import pytest
from botocore.exceptions import ClientError
from tests.unit.mockbedrock.helpers import AKID, AKID_B, HAIKU, SOL, USER_HI, bedrock_client, scenario, sigv4_headers
from tests.unit.mockbedrock.test_parsing import CopySpy
from tools.mockbedrock import MockBedrock, VirtualClock
from tools.mockbedrock.state import DEFAULT_RECORD_LIMIT
from types_aiobotocore_bedrock_runtime import BedrockRuntimeClient

PNG_1X1 = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (1).to_bytes(4, "big") * 2 + b"\x08\x02\x00\x00\x00"
YAML_DOC = """
scenario: control_yaml
seed: 3
rules:
  - match: { model: us.openai.gpt-6.1-sol, op: converse }
    answer: { raw_text: "from yaml" }
"""


async def converse(http: httpx.AsyncClient, model: str = SOL, text: str = "hi") -> httpx.Response:
    body = {"messages": [{"role": "user", "content": [{"text": text}]}], "inferenceConfig": {"maxTokens": 10}}
    return await http.post(f"/model/{model}/converse", json=body, headers=sigv4_headers())


async def test_scenario_load_yaml_and_json_and_errors(mock: MockBedrock, http: httpx.AsyncClient) -> None:
    resp = await http.post("/_mock/scenario", content=YAML_DOC)
    assert resp.json() == {"scenario": "control_yaml", "rules": 1}
    assert (await converse(http)).json()["output"]["message"]["content"] == [{"text": "from yaml"}]
    doc = scenario({"match": {"model": SOL}, "answer": {"raw_text": "from json"}})
    assert (await http.post("/_mock/scenario", content=orjson.dumps(doc))).status_code == 200
    assert (await http.get("/_mock/scenario")).json()["scenario"]["rules"][0]["answer"] == {"raw_text": "from json"}
    bad = await http.post("/_mock/scenario", content=b'{"scenario": "x", "rules": [{"match": {"op": "nope"}}]}')
    assert bad.status_code == 400
    assert bad.json()["errors"]
    assert (await converse(http)).json()["output"]["message"]["content"] == [{"text": "from json"}]  # kept


async def test_requests_paging_and_bounded_log(clock: VirtualClock) -> None:
    async with MockBedrock(clock=clock, record_limit=3) as mock, httpx.AsyncClient(base_url=mock.endpoint_url) as http:
        mock.load_scenario(scenario({"match": {"model": SOL}, "answer": {"raw_text": "ok"}}))
        for i in range(5):
            await converse(http, text=f"q{i}")
        page = (await http.get("/_mock/requests", params={"after": 3})).json()
        assert [r["seq"] for r in page["requests"]] == [4, 5]
        assert page["dropped"] == 2
        assert page["last_seq"] == 5
        assert [r["seq"] for r in (await http.get("/_mock/requests?limit=1")).json()["requests"]] == [3]
        assert (await http.get("/_mock/requests?limit=0")).json() == {"requests": [], "dropped": 2, "last_seq": 5}
        for bad in ("after=x", "limit=-2", "after=-1", "limit=1.5"):  # a negative limit used to drop the newest
            resp = await http.get(f"/_mock/requests?{bad}")
            assert resp.status_code == 400, bad
            assert resp.json() == {"errors": ["after and limit must be integers >= 0"]}


async def test_health_probe_on_a_full_ring_copies_nothing(clock: VirtualClock) -> None:
    """Regression: with a full ring, every `GET /_mock/requests` copied and serialized all of it on the event loop
    (about 0.5 s per compose healthcheck, stalling every stream). `limit=0` and a tail page now cost O(page)."""
    async with MockBedrock(clock=clock) as mock, httpx.AsyncClient(base_url=mock.endpoint_url) as http:
        mock.load_scenario(scenario({"match": {"model": SOL}, "stream": {"chunks": ["a", "b"]}}))
        for _ in range(DEFAULT_RECORD_LIMIT):
            mock.state.log.add(CopySpy(outcome="ok"))
        CopySpy.copies = 0
        probe, stream = await asyncio.gather(
            http.get("/_mock/requests?limit=0"),
            http.post(f"/model/{SOL}/converse-stream", json={"messages": USER_HI}, headers=sigv4_headers()),
        )
        health = probe.json()
        assert health["requests"] == []
        assert health["last_seq"] in (DEFAULT_RECORD_LIMIT, DEFAULT_RECORD_LIMIT + 1)  # either order on the loop
        assert stream.status_code == 200
        assert b"messageStop" in stream.content
        assert CopySpy.copies == 0
        page = (await http.get(f"/_mock/requests?after={DEFAULT_RECORD_LIMIT - 1}")).json()["requests"]
        assert [r["seq"] for r in page] == [DEFAULT_RECORD_LIMIT, DEFAULT_RECORD_LIMIT + 1]
        assert page[-1]["op"] == "converse-stream"
        assert CopySpy.copies == 1


async def test_memory_stays_bounded(clock: VirtualClock) -> None:
    """Regression: the scenario runtime kept every RequestInfo (prompt + canonical wire body with base64 media)
    until the next load, and the ring was bounded by count only."""
    image = {"image": {"format": "png", "source": {"bytes": base64.b64encode(PNG_1X1 * 2_000).decode()}}}
    async with (
        MockBedrock(clock=clock, record_limit=1_000, record_bytes=200_000) as mock,
        httpx.AsyncClient(base_url=mock.endpoint_url) as http,
    ):
        mock.load_scenario(scenario({"match": {"model": HAIKU}, "answer": {"raw_text": "ok"}}))
        for i in range(40):
            body = {"messages": [{"role": "user", "content": [{"text": f"q{i} " + "x" * 20_000}, image]}]}
            resp = await http.post(f"/model/{HAIKU}/converse", json=body, headers=sigv4_headers())
            assert resp.status_code == 200
        assert mock.state.runtime is not None
        assert mock.state.runtime.requests == []
        log = mock.state.log
        assert 0 < log.bytes <= 200_000
        assert log.dropped == 40 - len(log)
        assert mock.requests()[-1]["body"]["messages"][0]["content"][0]["text"].startswith("q39 ")  # text kept whole


async def test_quota_windows_survive_a_scenario_reload(
    mock: MockBedrock, clock: VirtualClock, http: httpx.AsyncClient
) -> None:
    """Regression: quota windows were stamped relative to the last scenario load, so a reload made earlier calls
    look future-dated and kept throttling for (time since the previous load) + 60 s."""
    doc = scenario({"match": {"model": SOL}, "answer": {"raw_text": "ok"}})
    mock.load_scenario(doc)
    mock.set_quotas([{"model": SOL, "rpm": 2}])
    clock.advance_ms(50_000)
    assert [(await converse(http)).status_code for _ in range(3)] == [200, 200, 429]
    clock.advance_ms(50_000)
    mock.load_scenario(doc)  # quotas are kept across loads
    assert (await converse(http)).status_code == 429  # 50 s after the calls: still in the window
    clock.advance_ms(10_001)
    assert (await converse(http)).status_code == 200  # 60 s after the calls


async def test_barrier_holds_until_released(mock: MockBedrock, http: httpx.AsyncClient) -> None:
    mock.load_scenario(scenario({"match": {"model": SOL}, "answer": {"raw_text": "late"}, "barrier": "gate"}))
    task = asyncio.create_task(converse(http))
    for _ in range(5000):  # wait (bounded) until the request is held on the barrier
        if mock.requests() and mock.requests()[-1]["barrier"] == "gate":
            break
        await asyncio.sleep(0.001)
    assert mock.requests()[-1]["barrier"] == "gate"
    assert not task.done()
    assert mock.requests()[-1]["status"] is None
    assert (await http.post("/_mock/barrier/gate/release")).json() == {"released": "gate"}
    resp = await task
    assert resp.json()["output"]["message"]["content"] == [{"text": "late"}]
    assert (await converse(http)).status_code == 200  # released barriers stay open


async def test_reset_clears_scenario_log_and_quotas(mock: MockBedrock, http: httpx.AsyncClient) -> None:
    mock.load_scenario(scenario({"match": {"model": SOL}, "answer": {"raw_text": "ok"}}))
    mock.set_quotas([{"model": SOL, "rpm": 1}])
    await converse(http)
    assert (await http.post("/_mock/reset")).json() == {"reset": True}
    assert (await http.get("/_mock/requests")).json()["requests"] == []
    assert (await http.get("/_mock/scenario")).json() == {"scenario": None}
    resp = await converse(http)
    assert resp.status_code == 500
    assert resp.json() == {"message": "mockbedrock: no scenario is loaded"}
    assert (mock.requests()[-1]["outcome"], mock.requests()[-1]["error"]) == ("unmatched", "InternalServerException")


async def test_unmatched_request_is_a_500_and_listed(mock: MockBedrock, bedrock: BedrockRuntimeClient) -> None:
    mock.load_scenario(scenario({"match": {"model": HAIKU}, "answer": {"raw_text": "ok"}}))
    with pytest.raises(ClientError) as err:
        await bedrock.converse(modelId=SOL, messages=USER_HI)
    assert err.value.response["Error"]["Code"] == "InternalServerException"
    assert "no scenario rule matches us.openai.gpt-6.1-sol converse" in err.value.response["Error"]["Message"]
    assert [r["model"] for r in mock.unmatched()] == [SOL]
    rec = mock.requests()[-1]
    assert (rec["outcome"], rec["status"], rec["rule"]) == ("unmatched", 500, None)


async def test_rpm_quota_per_akid_and_window(mock: MockBedrock, clock: VirtualClock, http: httpx.AsyncClient) -> None:
    mock.load_scenario(scenario({"match": {"model": SOL}, "answer": {"raw_text": "ok"}}))
    resp = await http.post("/_mock/quotas", json={"quotas": [{"model": SOL, "rpm": 2}]})
    assert resp.json() == {"quotas": 1}
    assert [(await converse(http)).status_code for _ in range(3)] == [200, 200, 429]
    assert mock.requests()[-1]["outcome"] == "throttled_quota"
    async with bedrock_client(mock.endpoint_url, akid=AKID_B) as other:
        assert (await other.converse(modelId=SOL, messages=USER_HI))["stopReason"] == "end_turn"  # its own window
    clock.advance_ms(60_001)
    assert (await converse(http)).status_code == 200


async def test_a_shared_quota_is_one_window_for_every_akid(
    mock: MockBedrock, clock: VirtualClock, http: httpx.AsyncClient
) -> None:
    """QL-4 (Wave E2): a Bedrock quota is per account, and two api processes hold two STS sessions (two AKIDs) of
    the same account; `akid: "*"` makes them share one window, so their sum is throttled."""
    mock.load_scenario(scenario({"match": {"model": SOL}, "answer": {"raw_text": "ok"}}))
    mock.set_quotas([{"model": SOL, "rpm": 2, "akid": "*"}])
    assert (await converse(http)).status_code == 200
    async with bedrock_client(mock.endpoint_url, akid=AKID_B) as other:
        assert (await other.converse(modelId=SOL, messages=USER_HI))["stopReason"] == "end_turn"
        with pytest.raises(ClientError, match="ThrottlingException"):
            await other.converse(modelId=SOL, messages=USER_HI)
    assert (await converse(http)).status_code == 429
    assert {r["akid"] for r in mock.requests() if r["outcome"] == "throttled_quota"} == {AKID, AKID_B}
    clock.advance_ms(60_001)
    assert (await converse(http)).status_code == 200


async def test_tpm_quota_counts_input_and_max_tokens(mock: MockBedrock, http: httpx.AsyncClient) -> None:
    mock.load_scenario(scenario({"match": {"model": SOL}, "answer": {"raw_text": "ok"}}))
    mock.set_quotas([{"model": SOL, "tpm": 22, "akid": AKID}])  # each call: ceil(2/4) + 10 = 11 tokens
    assert (await converse(http)).status_code == 200
    assert (await converse(http)).status_code == 200
    resp = await converse(http)
    assert resp.status_code == 429
    assert resp.json()["message"] == "Too many tokens, please wait before trying again."


@pytest.mark.parametrize(
    "body",
    [b"[]", b'{"quotas": [{"model": 1}]}', b'{"quotas": [{"model": "m", "rpm": 0}]}', b'{"quotas": [{"x": 1}]}', b"{"],
)
async def test_bad_quota_payloads(mock: MockBedrock, http: httpx.AsyncClient, body: bytes) -> None:
    assert (await http.post("/_mock/quotas", content=body)).status_code == 400


async def test_unknown_paths(http: httpx.AsyncClient) -> None:
    assert (await http.get("/_mock/nope")).status_code == 404
    resp = await http.get("/model/x/converse")
    assert resp.status_code == 404
    assert resp.headers["x-amzn-errortype"] == "UnknownOperationException"
    assert (await http.post("/model/x/invoke")).status_code == 404


async def test_mocks_share_one_port_only_with_reuse_port() -> None:
    """Load runs (spec-10 §5, 4 mock processes behind the 2 endpoints): with `reuse_port` a second mock listens on
    the first one's port and serves; without it that bind fails."""
    first, second = MockBedrock(clock=VirtualClock()), MockBedrock(clock=VirtualClock())
    await first.start(port=0, reuse_port=True)
    try:
        await second.start(port=first.port, reuse_port=True)
        assert second.port == first.port
        async with httpx.AsyncClient(base_url=second.endpoint_url, timeout=5.0) as http:
            assert (await http.get("/_mock/requests", params={"limit": 0})).status_code == 200
        with pytest.raises(OSError, match="address already in use"):
            await MockBedrock(clock=VirtualClock()).start(port=first.port)
    finally:
        await second.stop()
        await first.stop()
