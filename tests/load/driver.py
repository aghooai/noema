"""Load driver (spec-10 §5): anyio + httpx SSE clients; one process runs `--streams` concurrent streams, the runner
starts several processes (50 streams each) on their own cores.

Each stream loops turns on its own session (createSession, untimed; `store: true`; a new session every
`--turns-per-session` turns, so the history stays bounded) with every pre-commit key pinned (`generation:
{profile: sol-fast, effort: low, max_output_tokens: 600}`, `conditioning: "off"`): the decided spec equals spec0 and
the router's decision fan-out still runs. The user text carries `«nonce:<uuid>»` (unique: no L2 decision cache hit;
the mock records join on it).

Per turn one JSON line goes to `--out` (CLOCK_MONOTONIC ns, shared with the mock and the api on this host):
`t_send` (before the request is written), `t_headers`, `t_first_delta` (the chunk carrying the first
generation.delta), `stamp0` (its first «ts:» marker = the mock's send time), `t_done` (turn.completed or the end of
the body), `holdback_ms`/`decision_ms`/`decision_lead_ms`/`ttft_ms`/`e2e_ms` (the server's timings), `reason`
(generation.committed), `deltas`, `egress_max_us` (max over the other deltas of receive time - stamp), `status`,
`error`, `validated`.

Contract checks (IMPL-041) are kept off the timed path: the first turn of every stream and every non-200 response
are validated with the ContractValidator after the turn has been timed (`validated: true` in its record).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Final

import anyio
import httpx
import orjson
from tests.load.lag import LagSampler

from noema.contracts.validate import ContractValidator

AGHOO_VERSION: Final = "2026-10-01"
STAMP: Final = re.compile(rb"\xc2\xabts:(\d+)\xc2\xbb")  # «ts:<ns>» in UTF-8
PINNED: Final = {"profile": "sol-fast", "effort": "low", "max_output_tokens": 600}
TIMED_EVENTS: Final = (b"generation.delta", b"generation.committed", b"turn.created", b"turn.completed", b"error")
# Idle pooled connections expire before uvicorn closes them (timeout_keep_alive 5 s): equal expiries let a request
# reuse a connection the server is closing (transport ReadError, e3-perf-cpu-shed finding b).
KEEPALIVE_EXPIRY_S: Final = 4.0


def turn_body(nonce: str, n: int, session_id: str) -> dict[str, Any]:
    text = f"Load turn {n}: what is the capital of France? «nonce:{nonce}»"
    return {
        "session_id": session_id,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": text}]}],
        "stream": True,
        "store": True,
        "generation": PINNED,
        "conditioning": "off",
    }


def split_frames(buffer: bytes) -> tuple[list[bytes], bytes]:
    """Complete SSE blocks (separated by a blank line) and the incomplete rest."""
    parts = buffer.split(b"\n\n")
    return parts[:-1], parts[-1]


def frame_event(block: bytes) -> tuple[bytes | None, bytes | None]:
    """(event name, data) of one SSE block; comments and unknown lines are skipped."""
    event = data = None
    for line in block.split(b"\n"):
        if line.startswith(b"event: "):
            event = line[7:]
        elif line.startswith(b"data: "):
            data = line[6:]
    return event, data


@dataclass
class TurnRecord:
    stream: int
    n: int
    nonce: str
    t_send: int
    t_headers: int | None = None
    t_first_delta: int | None = None
    stamp0: int | None = None
    t_done: int | None = None
    status: int | None = None
    error: str | None = None
    turn_id: str | None = None
    session_id: str | None = None
    reason: str | None = None
    holdback_ms: int | None = None
    decision_ms: int | None = None
    decision_lead_ms: int | None = None
    ttft_ms: int | None = None
    e2e_ms: int | None = None
    deltas: int = 0
    egress_max_us: int | None = None
    retry_after_s: float | None = None  # a 429/503's retry-after (the stream waits that long, keeps its session)
    group: str = "main"  # the runner's driver group (`run.py --side flood:...`: "flood"); stats keep "main"
    validated: bool = False
    raw: bytearray = field(default_factory=bytearray, repr=False)

    def as_json(self) -> bytes:
        out = {k: v for k, v in self.__dict__.items() if k != "raw"}
        return orjson.dumps(out)


def on_frame(rec: TurnRecord, event: bytes, data: bytes, t: int) -> None:
    """Fold one received event into the turn record (`t` = when its chunk arrived)."""
    if event == b"generation.delta":
        rec.deltas += 1
        stamps = STAMP.findall(data)
        if rec.t_first_delta is None:
            rec.t_first_delta = t
            rec.stamp0 = int(stamps[0]) if stamps else None
        elif stamps:
            egress = (t - int(stamps[0])) // 1000
            rec.egress_max_us = egress if rec.egress_max_us is None else max(rec.egress_max_us, egress)
        return
    value = orjson.loads(data)
    if event == b"turn.created":
        rec.turn_id, rec.session_id = value.get("turn_id"), value.get("session_id")
    elif event == b"generation.committed":
        rec.reason = value.get("reason")
    elif event == b"turn.completed":
        timings = value.get("timings") or {}
        rec.holdback_ms, rec.ttft_ms, rec.e2e_ms = (
            timings.get("holdback_ms"),
            timings.get("ttft_ms"),
            timings.get("e2e_ms"),
        )
        rec.decision_ms, rec.decision_lead_ms = timings.get("decision_ms"), timings.get("decision_lead_ms")
    elif event == b"error":
        rec.error = str(value.get("code"))


class Driver:
    def __init__(self, args: argparse.Namespace, out: IO[bytes]) -> None:
        self.args = args
        self.out = out
        self.validator = ContractValidator()
        self.violations = 0
        key = os.environ.get("NOEMA_LOAD_KEY") or Path(args.key_file).read_text().strip()
        self.headers = {"authorization": f"Bearer {key}", "aghoo-version": AGHOO_VERSION}

    def validate(self, rec: TurnRecord, headers: httpx.Headers, request: dict[str, Any]) -> None:
        try:
            self.validator.check_response("createTurn", rec.status or 0, dict(headers), bytes(rec.raw), request)
            if rec.status == 200:
                self.validator.check_sse_stream("createTurn", bytes(rec.raw))
        except AssertionError as exc:
            self.violations += 1
            rec.error = rec.error or "contract_violation"
            sys.stderr.write(f"contract violation (stream {rec.stream} turn {rec.n}): {str(exc)[:400]}\n")
        rec.validated = True

    async def turn(self, http: httpx.AsyncClient, rec: TurnRecord, body: dict[str, Any], check: bool) -> None:
        content = orjson.dumps(body)
        headers = {"content-type": "application/json", "accept": "text/event-stream"}
        rec.t_send = time.monotonic_ns()
        async with http.stream("POST", "/v1/turns", content=content, headers=headers) as response:
            rec.t_headers = time.monotonic_ns()
            rec.status = response.status_code
            if rec.status in (429, 503) and (after := response.headers.get("retry-after", "")).isdigit():
                rec.retry_after_s = float(after)
            rest = b""
            async for chunk in response.aiter_raw():
                t = time.monotonic_ns()
                if check or rec.status != 200:
                    rec.raw += chunk
                blocks, rest = split_frames(rest + chunk)
                for block in blocks:
                    event, data = frame_event(block)
                    if event in TIMED_EVENTS and data is not None:
                        on_frame(rec, event, data, t)
            rec.t_done = time.monotonic_ns()
            if rec.status != 200:
                rec.error = rec.error or str(orjson.loads(bytes(rec.raw)).get("error", {}).get("code"))
            if check or rec.status != 200:
                self.validate(rec, response.headers, body)

    async def new_session(self, http: httpx.AsyncClient, check: bool) -> str | None:
        """createSession (untimed); None when it failed (the stream then backs off and retries)."""
        try:
            response = await http.post("/v1/sessions", content=b"{}", headers={"content-type": "application/json"})
        except httpx.HTTPError as exc:
            sys.stderr.write(f"createSession: {type(exc).__name__}\n")
            return None
        if check or response.status_code != 201:
            try:
                self.validator.check_response(
                    "createSession", response.status_code, dict(response.headers), response.content, {}
                )
            except AssertionError as exc:
                self.violations += 1
                sys.stderr.write(f"contract violation (createSession): {str(exc)[:400]}\n")
        return str(response.json()["id"]) if response.status_code == 201 else None

    async def stream(self, http: httpx.AsyncClient, index: int, start_ns: int, stop_ns: int) -> None:
        await anyio.sleep(max(0.0, (start_ns - time.monotonic_ns()) / 1e9))
        session_id: str | None = None
        n = 0
        while time.monotonic_ns() < stop_ns:
            if session_id is None or n % self.args.turns_per_session == 0:
                session_id = await self.new_session(http, check=n == 0)
                if session_id is None:
                    await anyio.sleep(0.5)
                    continue
            rec = TurnRecord(stream=index, n=n, nonce=uuid.uuid4().hex, t_send=0, group=self.args.group)
            try:
                await self.turn(http, rec, turn_body(rec.nonce, n, session_id), check=n == 0)
            except (httpx.HTTPError, orjson.JSONDecodeError) as exc:
                rec.error = rec.error or f"transport:{type(exc).__name__}"
                rec.t_done = time.monotonic_ns()
            if rec.error is not None and rec.retry_after_s is None:
                session_id = None  # a shed turn (429 + retry-after) never reached its session: keep it
            self.out.write(rec.as_json() + b"\n")
            n += 1
            if rec.error is not None:  # back off on errors instead of spinning; honor retry-after like an SDK
                await anyio.sleep(rec.retry_after_s or 0.5)

    async def run(self) -> None:
        a = self.args
        start_ns = a.start_ns or time.monotonic_ns()
        stop_ns = start_ns + int(a.duration_s * 1e9)
        limits = httpx.Limits(
            max_connections=a.streams + 10,
            max_keepalive_connections=a.streams + 10,
            keepalive_expiry=KEEPALIVE_EXPIRY_S,
        )
        timeout = httpx.Timeout(60.0, connect=10.0)
        lag = LagSampler(a.lag_out)
        lag.start()
        client = httpx.AsyncClient(base_url=a.base_url, headers=self.headers, limits=limits, timeout=timeout)
        async with client as http, anyio.create_task_group() as tg:
            for i in range(a.streams):
                offset = int(i * a.stagger_s * 1e9 / max(1, a.streams))
                tg.start_soon(self.stream, http, a.stream_base + i, start_ns + offset, stop_ns)
        await lag.stop()
        self.out.write(orjson.dumps({"summary": {"violations": self.violations}}) + b"\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.load.driver", description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--key-file", help="file with the API key (or NOEMA_LOAD_KEY)")
    parser.add_argument("--streams", type=int, default=50)
    parser.add_argument("--stream-base", type=int, default=0, help="index of this process's first stream")
    parser.add_argument("--start-ns", type=int, default=0, help="CLOCK_MONOTONIC start (default now)")
    parser.add_argument("--stagger-s", type=float, default=5.0, help="spread the stream starts over this window")
    parser.add_argument("--duration-s", type=float, required=True, help="no new turn starts after start + duration")
    parser.add_argument("--turns-per-session", type=int, default=10)
    parser.add_argument("--group", default="main", help="recorded in every turn line (run.py side groups)")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--lag-out", type=Path, required=True)
    args = parser.parse_args(argv)
    import uvloop

    with args.out.open("wb") as out:
        uvloop.run(Driver(args, out).run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
