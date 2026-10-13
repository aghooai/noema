"""Test helpers: shared fixtures, contract examples, golden SSE fixtures and SSE framing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import yaml

REPO = Path(__file__).resolve().parents[3]
BASE = "http://127.0.0.1:4010"
API_KEY = "ak_test_01JA00000000000000000000AA.secret"


def fixture_json(name: str) -> dict[str, Any]:
    """A shared response fixture (sdk/fixtures/<operationId>.<case>.json: status, headers, body or frames)."""
    loaded: dict[str, Any] = json.loads((REPO / "sdk/fixtures" / f"{name}.json").read_text())
    return loaded


def example(name: str) -> Any:
    """A vendored contract example (src/noema/contracts/_vendor/examples)."""
    return json.loads((REPO / "src/noema/contracts/_vendor/examples" / f"{name}.json").read_text())


def goldens() -> dict[str, dict[str, Any]]:
    """The golden SSE fixtures of spec-04 §4.2, keyed by id."""
    out: dict[str, dict[str, Any]] = {}
    for path in sorted((REPO / "tests/golden/sse").glob("G*.yaml")):
        g = yaml.safe_load(path.read_text())
        out[g["id"]] = g
    return out


def sse_frame(id_: object, event: str, data: Any) -> bytes:
    return f"id: {id_}\nevent: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


def sse_body(events: list[dict[str, Any]]) -> bytes:
    """The golden events as one SSE body, with a ping and an unknown event type mixed in."""
    out = b": ping\n\n"
    for ev in events:
        out += sse_frame(ev["id"], ev["type"], ev["data"])
        if ev["id"] == 2:
            out += sse_frame(2, "turn.future_event", {"type": "turn.future_event", "x": 1})
    return out


def respond(name: str) -> httpx.Response:
    """respx side effect returning the named fixture."""
    fx = fixture_json(name)
    return httpx.Response(fx["status"], headers=fx["headers"], json=fx["body"])
