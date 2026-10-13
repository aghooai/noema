"""Smoke e2e against the compose stack (make e2e-smoke): both replicas healthy and ready, one turn over SSE, one
`/v1/decisions` native and one Jev, one file upload through the media worker and sandbox to its derivatives, and
`GET /v1/usage`; every response is contract-validated by the `stack` clients (tests/e2e/conftest.py). The stack runs
its default mock scenario (deploy/compose/mockbedrock/default.yaml): no test here changes it."""

from __future__ import annotations

import struct
import uuid
import zlib
from typing import Any, Final

import anyio
import orjson
import pytest
from tests.e2e.conftest import DEFAULT_SCENARIO, REPO, CheckedClient, Mock, Stack

from noema.core.values import JsonValue

pytestmark = [pytest.mark.e2e]

EXAMPLES: Final = REPO / "src" / "noema" / "contracts" / "_vendor" / "examples"


def _turn(text: str) -> JsonValue:
    return {"input": [{"role": "user", "content": [{"type": "input_text", "text": text}]}], "stream": True}


TERMINAL_FILE: Final = frozenset({"ready", "partial", "rejected", "failed"})


def _png(width: int = 64, height: int = 32) -> bytes:
    """A small valid RGB PNG (a dark bar on white), built without image libraries."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(
        b"\x00" + b"".join(b"\x20\x20\x20" if 8 <= y < 24 and 8 <= x < 56 else b"\xff\xff\xff" for x in range(width))
        for y in range(height)
    )
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


async def _eventually(client: CheckedClient, path: str, done: Any, *, timeout_s: float = 30.0) -> dict[str, Any]:
    """GET `path` until a 200 whose body satisfies `done(body)` (a 404 while the row is not written yet is retried);
    every response is validated."""
    with anyio.fail_after(timeout_s):
        while True:
            response = await client.request("GET", path)
            assert response.status_code in (200, 404), (response.status_code, response.text[:300])
            body: dict[str, Any] = response.json()
            if response.status_code == 200 and done(body):
                return body
            await anyio.sleep(0.25)


@pytest.fixture(autouse=True)
async def default_scenario(mock: Mock) -> None:
    """The stack's default scenario, whatever an earlier run loaded (the mock reads its file only at start)."""
    await mock.load(DEFAULT_SCENARIO.read_text())


@pytest.mark.parametrize("replica", [0, 1])
async def test_healthz_and_readyz_on_both_replicas(stack: Stack, replica: int) -> None:
    client = stack.client(replica, role=None)
    await client.request("GET", "/healthz", expect=200)
    await client.request("GET", "/readyz", expect=200)


async def test_one_turn_over_sse_then_read_on_the_other_replica(stack: Stack, mock: Mock) -> None:
    before = (await mock.requests(limit=0))["last_seq"]
    question = f"What is the capital of France? (smoke {uuid.uuid4().hex[:8]})"  # unique: no L2 decision cache hit
    events = await stack.client(0).sse("/v1/turns", json=_turn(question))
    kinds = [e.event for e in events]
    assert kinds[0] == "turn.created" and kinds[-1] == "turn.completed", kinds
    assert {"preflight.completed", "route.selected", "decision.completed", "generation.completed"} <= set(kinds)
    text = "".join(str(e.data["text"]) for e in events if e.event == "generation.delta" and isinstance(e.data, dict))
    assert text == "Paris is the capital of France. It is also its largest city."
    created = events[0].data
    assert isinstance(created, dict)
    turn_id = str(created["turn_id"])
    calls = (await mock.requests(after=before, limit=100))["requests"]
    assert {r["op"] for r in calls} >= {"converse", "chat", "converse-stream"}  # router members + generation
    turn = await _eventually(stack.client(1), f"/v1/turns/{turn_id}", lambda b: True)  # api-2 reads the stored turn
    assert turn["turn_id"] == turn_id and turn["text"] == text
    other_tenant = stack.client(1, tenant="t_beta")
    await other_tenant.request("GET", f"/v1/turns/{turn_id}", expect=404)  # tenant isolation


async def test_one_native_decision(stack: Stack) -> None:
    body = orjson.loads((EXAMPLES / "decision-request.valid-default-profile.json").read_bytes())
    body |= {"profile": "router-default", "escalation": "off"}
    decision = (await stack.client(0).request("POST", "/v1/decisions", json=body, expect=200)).json()
    answers = {a["name"]: a for a in decision["answers"]}
    assert set(answers) == {q["name"] for q in body["questions"]}
    assert answers["department"]["candidates"][0]["value"] == "billing"
    stored = await stack.client(1).request("GET", f"/v1/decisions/{decision['id']}", expect=200)
    assert stored.json()["id"] == decision["id"]


async def test_one_jev_decision(stack: Stack) -> None:
    body = orjson.loads((EXAMPLES / "decision-request.valid-jev.json").read_bytes())
    jev = (await stack.client(1).request("POST", "/v1/decisions", json=body, expect=200)).json()
    answers = jev["answers"]
    assert answers["department"]["choice"] == "billing" and answers["urgency"]["score"] > 1.0, answers
    assert answers["needs_human"]["type"] == "noul" and jev["decision_id"].startswith("dec_")


async def test_one_file_upload_to_its_derivatives(stack: Stack) -> None:
    client = stack.client(0)
    files = {"file": ("bar.png", _png(), "image/png")}
    uploaded = (await client.request("POST", "/v1/files", data={"purpose": "input"}, files=files, expect=202)).json()
    assert uploaded["status"] == "quarantined"
    file = await _eventually(stack.client(1), f"/v1/files/{uploaded['id']}", lambda b: b["status"] in TERMINAL_FILE)
    assert file["status"] in ("ready", "partial"), file
    kinds = {d["kind"]: d["status"] for d in file["derivatives"]}
    assert kinds.get("raster") == "ready" and kinds.get("thumbnail") == "ready", kinds
    raster = await client.request("GET", f"/v1/files/{uploaded['id']}/derivatives/raster", expect=200)
    assert raster.headers["content-type"].startswith("image/png") and raster.content.startswith(b"\x89PNG")


@pytest.mark.parametrize("role", ["developer", "tenant_admin"])
async def test_usage_report(stack: Stack, role: str) -> None:
    await stack.client(0).request("POST", "/v1/decisions", json=_one_question(), expect=200)
    report = await _eventually(
        stack.client(1, role=role), "/v1/usage?group_by=line", lambda b: any(r["line"] == "decision" for r in b["data"])
    )
    assert all(r["usage"]["input_tokens"] >= 0 for r in report["data"])


def _one_question() -> dict[str, Any]:
    return {
        "profile": "router-default",
        "escalation": "off",
        "input": "Please call me back.",
        "questions": [{"type": "predicate", "name": "callback", "instructions": "The user asks for a call back."}],
    }
