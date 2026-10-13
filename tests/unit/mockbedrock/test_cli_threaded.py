"""The CLI contract the compose `mockbedrock` service relies on (`python -m tools.mockbedrock --host --port`,
`GET /_mock/requests` -> 200, clean SIGTERM) and `ThreadedMockBedrock` with synchronous clients."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import botocore.session
import httpx
import openai
import orjson
import pytest
from botocore.config import Config
from tests.unit.mockbedrock.helpers import (
    AKID,
    DEEPSEEK,
    SECRET,
    SOL,
    TOKEN,
    USER_HI,
    scenario,
    short_term_token,
    sigv4_headers,
)
from tools.mockbedrock import ThreadedMockBedrock
from tools.mockbedrock.__main__ import CPUS_ENV, cpu_list

REPO = Path(__file__).resolve().parents[3]


def raw_converse(url: str) -> bytes:
    """Send one Converse request on a raw socket and read until EOF (ConnectionResetError on a TCP RST)."""
    parts = urlsplit(url)
    body = orjson.dumps({"messages": USER_HI})
    head = "".join(f"{k}: {v}\r\n" for k, v in sigv4_headers().items())
    request = (
        f"POST /model/{SOL}/converse HTTP/1.1\r\nhost: x\r\nconnection: close\r\n"
        f"content-type: application/json\r\n{head}content-length: {len(body)}\r\n\r\n"
    ).encode() + body
    with socket.create_connection((parts.hostname or "127.0.0.1", parts.port or 80), timeout=5) as sock:
        sock.sendall(request)
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
        return data


def test_cli_serves_and_stops_on_sigterm(tmp_path: Path) -> None:
    doc = tmp_path / "scenario.json"
    rules = (
        {"match": {"model": SOL, "nth": 1}, "fault": {"kind": "connect_reset"}},
        {"match": {"model": SOL}, "answer": {"raw_text": "cli"}},
    )
    doc.write_bytes(orjson.dumps(scenario(*rules)))
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    proc = subprocess.Popen(
        [
            *(
                sys.executable,
                "-m",
                "tools.mockbedrock",
                "--port",
                "0",
                "--scenario",
                str(doc),
                "--log-level",
                "warning",
            ),
            *("--record-limit", "100", "--record-bytes", "1000000", "--region", "us-east-1"),
        ],
        cwd=REPO,
        env=env,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline().strip()
        assert line.startswith("mockbedrock listening on http://127.0.0.1:")
        url = line.rsplit(" ", 1)[1]
        assert httpx.get(f"{url}/_mock/requests", timeout=5).status_code == 200
        health = httpx.get(f"{url}/_mock/requests?limit=0", timeout=5)  # the O(1) healthcheck form
        assert (health.status_code, health.json()["requests"]) == (200, [])
        assert httpx.get(f"{url}/_mock/scenario", timeout=5).json()["scenario"]["scenario"] == "unit"
        with pytest.raises(ConnectionResetError):  # uvloop: SO_LINGER 0 + abort is a real RST, not a FIN
            raw_converse(url)
        answer = raw_converse(url)
        assert answer.startswith(b"HTTP/1.1 200 OK\r\n")
        assert b'"content":[{"text":"cli"}]' in answer
    finally:
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0


def test_the_cpu_list_syntax() -> None:
    assert cpu_list("44-45,47") == {44, 45, 47}
    assert cpu_list("3") == {3}
    with pytest.raises(ValueError, match="invalid literal"):
        cpu_list("4-x")


def test_cli_pins_itself_to_the_cpus_of_its_environment() -> None:
    cpu = min(os.sched_getaffinity(0))
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")} | {CPUS_ENV: str(cpu)}
    cmd = [sys.executable, "-m", "tools.mockbedrock", "--port", "0", "--log-level", "warning"]
    proc = subprocess.Popen(cmd, cwd=REPO, env=env, stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().startswith("mockbedrock listening on ")
        assert os.sched_getaffinity(proc.pid) == {cpu}
    finally:
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0


def test_threaded_mock_with_sync_clients() -> None:
    with ThreadedMockBedrock(scenario=scenario({"match": {}, "answer": {"raw_text": "sync"}})) as mock:
        client: Any = botocore.session.get_session().create_client(
            "bedrock-runtime",
            region_name="us-east-1",
            endpoint_url=mock.endpoint_url,
            aws_access_key_id=AKID,
            aws_secret_access_key=SECRET,
            aws_session_token=TOKEN,
            config=Config(retries={"total_max_attempts": 1}),
        )
        resp = client.converse(modelId=SOL, messages=USER_HI)
        assert resp["output"]["message"]["content"] == [{"text": "sync"}]
        with openai.OpenAI(base_url=mock.mantle_url, api_key=short_term_token(), max_retries=0) as chat:
            answer = chat.chat.completions.create(model=DEEPSEEK, messages=[{"role": "user", "content": "q"}])
        assert answer.choices[0].message.content == "sync"
        assert [r["op"] for r in mock.requests()] == ["converse", "chat"]
        mock.release("unused")
        mock.set_quotas([{"model": SOL, "rpm": 1}])
        assert mock.unmatched() == []
        mock.reset()
        assert mock.requests() == []
        assert mock.load_scenario(scenario({"match": {}, "answer": {"raw_text": "x"}})).name == "unit"
        assert mock.port > 0


def test_connect_reset_is_a_tcp_rst_on_asyncio() -> None:
    doc = scenario(
        {"match": {"model": SOL, "nth": 1}, "fault": {"kind": "connect_reset"}},
        {"match": {"model": SOL}, "answer": {"raw_text": "after"}},
    )
    with ThreadedMockBedrock(scenario=doc) as mock:
        with pytest.raises(ConnectionResetError):
            raw_converse(mock.endpoint_url)
        assert b'"content":[{"text":"after"}]' in raw_converse(mock.endpoint_url)
        assert [r["outcome"] for r in mock.requests()] == ["reset", "ok"]
