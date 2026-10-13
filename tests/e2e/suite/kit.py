"""Helpers of the full e2e suite: live SSE turns, polling, the mock's request log, noema-admin and /metrics.

Every Noema response still goes through the `CheckedClient`'s ContractValidator: a live stream validates each frame
as it arrives and the whole body (ids 1, 2, ...) when it ends.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any, Final

import anyio
import httpx
import orjson
from tests.e2e.conftest import REPO, CheckedClient, Mock, Stack

from noema.contracts.validate import SseEvent
from noema.core.values import JsonValue

Json = dict[str, Any]
CREATE_TURN: Final = "createTurn"


def tag(label: str = "e2e") -> str:
    """A unique marker for a test's input text (no L2 decision-cache hit across tests)."""
    return f"{label}-{uuid.uuid4().hex[:10]}"


def turn_body(text: str, **extra: Any) -> Json:
    return {"input": [{"role": "user", "content": [{"type": "input_text", "text": text}]}], "stream": True, **extra}


def data(event: SseEvent) -> Json:
    assert isinstance(event.data, dict), event
    return event.data


def kinds(events: Sequence[SseEvent]) -> list[str]:
    return [e.event for e in events]


def in_order(events: Sequence[SseEvent], *expected: str) -> bool:
    """The first occurrences of `expected` appear in this order (other events may come in between)."""
    order = kinds(events)
    return all(k in order for k in expected) and [order.index(k) for k in expected] == sorted(
        order.index(k) for k in expected
    )


def first(events: Sequence[SseEvent], kind: str) -> Json:
    found = [data(e) for e in events if e.event == kind]
    assert found, f"no {kind} in {kinds(events)}"
    return found[0]


def deltas_text(events: Sequence[SseEvent], attempt_id: str | None = None) -> str:
    return "".join(
        str(data(e)["text"])
        for e in events
        if e.event == "generation.delta" and (attempt_id is None or data(e)["attempt_id"] == attempt_id)
    )


class LiveTurn:
    """One createTurn SSE response read frame by frame (`next`, `until`, `rest`); `events` is everything so far."""

    def __init__(self, client: CheckedClient, response: httpx.Response) -> None:
        self.client, self.response = client, response
        self.events: list[SseEvent] = []
        self.at_ms: list[float] = []  # arrival of each event, ms after the response headers (diagnostics)
        self._t0 = time.monotonic()
        self.raw = bytearray()
        self._buffer = b""
        self._pending: deque[SseEvent] = deque()
        self._chunks = response.aiter_bytes()
        self._ended = False

    @property
    def turn_id(self) -> str:
        return str(data(self.events[0])["turn_id"])

    async def next(self) -> SseEvent | None:
        while not self._pending:
            if self._ended:
                return None
            try:
                chunk = await anext(self._chunks)
            except StopAsyncIteration:
                self._ended = True
                continue
            self.raw += chunk
            self._buffer = (self._buffer + chunk).replace(b"\r\n", b"\n")
            *blocks, self._buffer = self._buffer.split(b"\n\n")
            for block in blocks:
                for frame_id, name, payload in self.client.validator.parse_sse(block + b"\n\n"):
                    assert name is not None
                    value = self.client.validator.check_sse_frame(CREATE_TURN, frame_id, name, payload)
                    self._pending.append(SseEvent(id=frame_id, event=name, data=value))
        event = self._pending.popleft()
        self.events.append(event)
        self.at_ms.append(round((time.monotonic() - self._t0) * 1000, 1))
        return event

    def timeline(self) -> list[tuple[float, str, str]]:
        """(arrival ms, type, attempt_id or reason) per event: a readable assertion message."""
        return [
            (at, e.event, str(data(e).get("attempt_id") or data(e).get("reason") or data(e).get("code") or ""))
            for at, e in zip(self.at_ms, self.events, strict=True)
        ]

    async def until(self, done: Callable[[SseEvent], bool], *, timeout_s: float = 30.0) -> SseEvent:
        with anyio.fail_after(timeout_s):
            while (event := await self.next()) is not None:
                if done(event):
                    return event
        raise AssertionError(f"stream ended without the awaited event: {kinds(self.events)}")

    async def rest(self, *, timeout_s: float = 45.0) -> list[SseEvent]:
        with anyio.fail_after(timeout_s):
            while await self.next() is not None:
                pass
        return self.events

    def validate(self) -> None:
        """The whole body: every frame again plus the frame-id order (as `CheckedClient.sse` does)."""
        op = self.client.operation_id(self.response)
        status, headers = self.response.status_code, dict(self.response.headers)
        self.client.validator.check_response(op, status, headers, bytes(self.raw))
        self.client.checked.append((op, self.response.status_code))


@asynccontextmanager
async def live_turn(
    client: CheckedClient, body: JsonValue, *, headers: Mapping[str, str] | None = None
) -> AsyncIterator[LiveTurn]:
    """POST /v1/turns as a stream; a non-200 is read, validated and asserted. The body is validated at exit (also
    when the caller stopped reading early, for what was received)."""
    request_headers = {"content-type": "application/json", "accept": "text/event-stream", **(headers or {})}
    async with client.http.stream("POST", "/v1/turns", content=orjson.dumps(body), headers=request_headers) as resp:
        if resp.status_code != 200:
            await resp.aread()
            client.check(resp, body)
            raise AssertionError(f"createTurn answered {resp.status_code}: {resp.text[:500]}")
        live = LiveTurn(client, resp)
        yield live
        live.validate()


async def eventually(
    client: CheckedClient,
    path: str,
    done: Callable[[Json], bool],
    *,
    timeout_s: float = 30.0,
    params: Mapping[str, Any] | None = None,
) -> Json:
    """GET `path` until a 200 whose body satisfies `done` (404 while not written yet is retried)."""
    with anyio.fail_after(timeout_s):
        while True:
            response = await client.request("GET", path, params=params)
            assert response.status_code in (200, 404), (response.status_code, response.text[:300])
            body: Json = response.json()
            if response.status_code == 200 and done(body):
                return body
            await anyio.sleep(0.25)


async def mock_seq(mock: Mock) -> int:
    return int((await mock.requests(limit=0))["last_seq"])


async def mock_calls(mock: Mock, after: int) -> list[Json]:
    """Every request record the mock received after `after` (paged)."""
    out: list[Json] = []
    while True:
        page = (await mock.requests(after=after, limit=200))["requests"]
        out.extend(page)
        if len(page) < 200:
            return out
        after = int(page[-1]["seq"])


async def report(client: CheckedClient, group_by: str, **params: Any) -> list[Json]:
    """Every row of one getUsage report (paged with `after`)."""
    rows: list[Json] = []
    query: dict[str, Any] = {"group_by": group_by, "limit": 200, **params}
    while True:
        page = (await client.request("GET", "/v1/usage", params=query, expect=200)).json()
        assert page["group_by"] == group_by
        rows.extend(page["data"])
        if not page["has_more"]:
            return rows
        query["after"] = page["next_cursor"]


# --- noema-admin (operator CLI) against the stack ---------------------------------------------------------------------
class AdminRefused(Exception):
    """noema-admin exited non-zero; `.code` and `.stderr` (`{"error", "message"}`)."""

    def __init__(self, code: int, stderr: str) -> None:
        super().__init__(f"noema-admin exit {code}: {stderr.strip()[:300]}")
        self.code, self.stderr = code, stderr


async def admin(stack: Stack, argv: Sequence[str], *, operator: str = "noema-bootstrap-a") -> Json:
    """One `python -m noema.identity.cli` run in a subprocess with the stack's dev environment (the pytest process
    keeps its scrubbed AWS environment); its JSON stdout."""
    pg = stack.state["endpoints"]["postgres"]
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "NOEMA_ENV": str(stack.state.get("env", "dev")),
        "NOEMA_CLI_DSN": f"postgresql://noema_cli:noema-cli-dev-only@{pg}/noema",
        "NOEMA_AWS_ENDPOINT_OVERRIDES": orjson.dumps({"secretsmanager": stack.state["endpoints"]["moto"]}).decode(),
        "AWS_ACCESS_KEY_ID": "noema-compose-fake",
        "AWS_SECRET_ACCESS_KEY": "noema-compose-fake",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        "AWS_EC2_METADATA_DISABLED": "true",
        "NOEMA_OPERATOR_KEY": str(stack.state["operators"][operator]["key"]),
    }
    done = await anyio.run_process([sys.executable, "-m", "noema.identity.cli", *argv], env=env, cwd=REPO, check=False)
    if done.returncode != 0:
        raise AdminRefused(done.returncode, done.stderr.decode())
    result: Json = orjson.loads(done.stdout)
    return result


# --- /metrics (Prometheus text) of one api replica: every worker process of the pod -----------------------------------
def metrics_urls(stack: Stack, replica: int) -> list[str]:
    """The /metrics endpoints of every worker of api replica `replica` (tools/bootstrap_dev.py records
    NOEMA_METRICS_PORT .. + WEB_CONCURRENCY - 1 per replica; a state file written before that holds one URL)."""
    entry = stack.state["endpoints"]["metrics"][replica]
    return [entry] if isinstance(entry, str) else [str(url) for url in entry]


async def metric_samples(stack: Stack, replica: int, name: str) -> list[tuple[dict[str, str], float]]:
    """Every sample of metric `name` on api replica `replica` as (labels, value), worker by worker: summing them gives
    the replica's counter total; the first sample of a label set is the first worker's (gauges)."""
    out: list[tuple[dict[str, str], float]] = []
    async with httpx.AsyncClient(timeout=10.0) as http:
        for url in metrics_urls(stack, replica):
            out += _samples((await http.get(f"{url}/metrics")).text, name)
    return out


def _samples(text: str, name: str) -> list[tuple[dict[str, str], float]]:
    out: list[tuple[dict[str, str], float]] = []
    for line in text.splitlines():
        if not line.startswith(name) or line.startswith("#"):
            continue
        head, _, value = line.rpartition(" ")
        metric, _, raw_labels = head.partition("{")
        if metric != name:
            continue
        labels: dict[str, str] = {}
        for pair in raw_labels.rstrip("}").split('",'):
            key, sep, val = pair.partition('="')
            if sep:
                labels[key.strip(",")] = val.rstrip('"')
        out.append((labels, float(value)))
    return out


# --- the stack's bucket (moto) ----------------------------------------------------------------------------------------
async def s3_keys(stack: Stack, prefix: str) -> list[str]:
    """The keys under `prefix` in the stack's bucket (moto on its published port, the compose fake keys)."""
    from aiobotocore.config import AioConfig
    from aiobotocore.session import get_session

    config = AioConfig(s3={"addressing_style": "path"}, retries={"max_attempts": 1})
    async with get_session().create_client(
        "s3",
        region_name="us-east-1",
        endpoint_url=str(stack.state["endpoints"]["moto"]),
        aws_access_key_id="noema-compose-fake",
        aws_secret_access_key="noema-compose-fake",  # moto accepts any key (the compose default)
        config=config,
    ) as s3:
        listed = await s3.list_objects_v2(Bucket=str(stack.state["bucket"]), Prefix=prefix)
    return [str(item["Key"]) for item in listed.get("Contents", [])]
