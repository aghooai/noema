"""The shared wire goldens (tests/golden/{decision,generation}_requests, IMPL-050) replayed against the mock: every
request the adapters are specified to send must pass the mock's §2.2 validation and get a 200. Converse goldens go
through the real aiobotocore client (the golden body is its call parameters), Mantle goldens through httpx."""

from __future__ import annotations

import base64
from typing import Any

import httpx
import pytest
from tests.golden.wire import load_wire_goldens
from tests.unit.mockbedrock.helpers import bearer_headers, scenario
from tools.mockbedrock import MockBedrock
from types_aiobotocore_bedrock_runtime import BedrockRuntimeClient

WIRE_GOLDENS = [g for g in load_wire_goldens() if "body" in g["expected"]]  # the rest are adapter-side rejects


def unbytes(node: Any) -> Any:
    """The golden JSON form (`{"$bytes_b64": ...}`) back to botocore call parameters (raw bytes)."""
    if isinstance(node, dict):
        if set(node) == {"$bytes_b64"}:
            return base64.b64decode(node["$bytes_b64"])
        return {k: unbytes(v) for k, v in node.items()}
    if isinstance(node, list):
        return [unbytes(v) for v in node]
    return node


def test_the_goldens_cover_every_api() -> None:
    assert len(WIRE_GOLDENS) >= 12
    assert {g["expected"]["api"] for g in WIRE_GOLDENS} == {"Converse", "ConverseStream", "chat/completions"}


@pytest.mark.parametrize("golden", WIRE_GOLDENS, ids=[g["name"] for g in WIRE_GOLDENS])
async def test_golden_wire_request_is_accepted(
    mock: MockBedrock, bedrock: BedrockRuntimeClient, http: httpx.AsyncClient, golden: dict[str, Any]
) -> None:
    mock.load_scenario(scenario({"match": {}, "answer": {"raw_text": "A"}, "stream": {"chunks": ["ok"]}}))
    expected = golden["expected"]
    api = expected["api"]
    if api == "chat/completions":
        assert expected["url"].endswith("/v1/chat/completions")
        resp = await http.post("/v1/chat/completions", json=expected["body"], headers=bearer_headers())
        assert resp.status_code == 200, resp.text
        assert resp.content
    elif api == "ConverseStream":
        stream = await bedrock.converse_stream(**unbytes(expected["body"]))
        events = [e async for e in stream["stream"]]
        assert "messageStop" in events[-2]
    else:
        out = await bedrock.converse(**unbytes(expected["body"]))
        assert out["stopReason"] == "end_turn"
    (rec,) = mock.requests()
    assert (rec["status"], rec["outcome"], rec["op"]) == (200, "ok", golden["op"])
    assert mock.unmatched() == []
