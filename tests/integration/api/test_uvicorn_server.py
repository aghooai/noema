"""The API over real sockets: uvicorn with uvloop and httptools on 127.0.0.1, port 0 (spec-01 §4)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from tests.integration.api.uvicorn_app import SNAPSHOTS
from tests.unit.api.apifakes import auth

from noema.contracts.validate import ContractValidator

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CAP = 52_428_800  # platform.limits.max_json_body_bytes default (IMPL-064)
_LISTENING = re.compile(r"Uvicorn running on http://127\.0\.0\.1:(\d+)")


@pytest.fixture(scope="module")
def server() -> Iterator[tuple[str, list[str]]]:
    env = {**os.environ, "PYTHONPATH": str(ROOT), "NOEMA_ENV": "test", "NOEMA_CONTRACT_VALIDATE": "1"}
    cmd = [sys.executable, "-m", "uvicorn", "tests.integration.api.uvicorn_app:create", "--factory"]
    cmd += ["--loop", "uvloop", "--http", "httptools", "--host", "127.0.0.1", "--port", "0", "--lifespan", "on"]
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
    lines: list[str] = []
    port: list[str] = []
    ready = threading.Event()

    def drain() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            lines.append(line)
            m = _LISTENING.search(line)
            if m:
                port.append(m.group(1))
                ready.set()
        ready.set()

    threading.Thread(target=drain, daemon=True).start()
    try:
        assert ready.wait(30) and port, "uvicorn did not start:\n" + "".join(lines)
        yield f"http://127.0.0.1:{port[0]}", lines
    finally:
        proc.terminate()
        proc.wait(10)
    log = "".join(lines)
    assert "Traceback" not in log, log
    assert "contract violation" not in log, log


def test_request_ids_stubs_and_unknown_routes(server: tuple[str, list[str]]) -> None:
    base, _ = server
    with httpx.Client(base_url=base) as client:
        unauth = client.get("/v1/turns/turn_1", headers={"x-request-id": "edge-1"})
        stub = client.get("/v1/turns/turn_1", headers=auth())
        unknown = client.get("/nope")
        live = client.get("/healthz")
    assert (unauth.status_code, unauth.headers["x-request-id"], unauth.json()["error"]["request_id"]) == (
        401,
        "edge-1",
        "edge-1",
    )
    assert unauth.headers["www-authenticate"] == "Bearer"
    assert (stub.status_code, stub.json()["error"]["code"]) == (501, "not_implemented")
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "not_found")
    assert live.status_code == 200


def test_sec_edge_01_a_50_mib_plus_one_byte_body_is_413_before_parsing(server: tuple[str, list[str]]) -> None:
    base, _ = server
    body = b"{" + b" " * (DEFAULT_CAP - 1)  # exactly one byte over with the closing brace below
    body += b"}"
    assert len(body) == DEFAULT_CAP + 1
    started = time.monotonic()
    with httpx.Client(base_url=base, timeout=30) as client:
        resp = client.post("/v1/turns", content=body, headers={"content-type": "application/json"})
    assert (resp.status_code, resp.json()["error"]["code"]) == (413, "request_too_large")
    assert time.monotonic() - started < 20


def test_sse_frames_stream_over_http_1_1(server: tuple[str, list[str]]) -> None:
    base, _ = server
    with httpx.Client(base_url=base) as client, client.stream("GET", "/v1/files/file_1/events", headers=auth()) as r:
        assert r.status_code == 200
        assert r.headers["content-type"] == "text/event-stream; charset=utf-8"
        assert r.headers["x-request-id"].startswith("req_")
        body = r.read()
    events = ContractValidator().check_sse_stream("watchFile", body)
    assert [(e.id, e.event, e.data) for e in events] == [
        ("1", "file.updated", SNAPSHOTS[0]),
        ("2", "file.updated", SNAPSHOTS[1]),
    ]


def test_a_chunked_body_is_cut_off_at_the_cap(server: tuple[str, list[str]]) -> None:
    base, _ = server

    def chunks() -> Iterator[bytes]:
        sent = 0
        while sent <= DEFAULT_CAP:
            yield b" " * 1_048_576
            sent += 1_048_576

    with httpx.Client(base_url=base, timeout=30) as client:  # authenticated: the body is read after step 4 (Wave E)
        resp = client.post("/v1/turns", content=chunks(), headers=auth(**{"content-type": "application/json"}))
    assert "content-length" not in resp.request.headers  # chunked transfer encoding
    assert (resp.status_code, resp.json()["error"]["code"]) == (413, "request_too_large")
