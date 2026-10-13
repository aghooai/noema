"""Heavy-stream clients for the C3 slow-client row (06 §5, spec-10 §3 C3), started by `run.py --side slow|fast:...`.

Each of `--clients` clients loops one long turn at a time, each on a fresh session (the user text carries `«c3»`,
which scenarios/slow.yaml answers with its heavy stream; a fresh session keeps the multi-MB answer out of the next
prompt): createTurn over a raw socket straight to the api (`tests/fault/slowclient.py`, h11), then
- slow (`--rate-bps` > 0): a 16 KiB receive buffer and paced reads at `--rate-bps`, until EOF or `--turn-max-s`; the
  connection is then closed and the next turn starts;
- fast (`--rate-bps 0`): the same turns read as fast as they arrive (the C3 baseline: the same load, no slow reader).
One JSON line per turn to `--out`: `t_send`/`t_end` (CLOCK_MONOTONIC ns), `eof_after_s`, `bytes`, `status`,
`turn_id`, `frames` (complete frames, each contract-validated, ids strictly increasing), `coalesced` (delta frames
carrying more than one mock chunk), `text_chars`, `errors` (the error frames' codes) and `violation` (a contract
assertion, if any).
"""

from __future__ import annotations

import argparse
import asyncio
import socket
import sys
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import IO, Any, Final

import anyio
import h11
import httpx
import orjson
from tests.fault.slowclient import SlowClient
from tests.load.driver import AGHOO_VERSION, PINNED

from noema.contracts.validate import ContractValidator

CHUNK_CHARS: Final = 4000  # scenarios/slow.yaml: chunk_tokens 1000 x 4 characters per token
TAG: Final = "«c3»"
# 16 KiB: with 4 KiB the receive window let only ~2 KB arrive per 0.25 s tick (8.2 of the asked 12.6 KB/s)
RCVBUF: Final = 16_384


class FastClient(SlowClient):
    """The same request, read as fast as it arrives (default receive buffer)."""

    async def run(self, stop: anyio.Event, max_s: float) -> None:
        loop = asyncio.get_running_loop()
        request, payload = self.request()
        wire = h11.Connection(h11.CLIENT)
        data = b"".join(wire.send(e) or b"" for e in (request, h11.Data(data=payload), h11.EndOfMessage()))
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        t0 = time.monotonic()
        try:
            await loop.sock_connect(sock, (self.host, self.port))
            await loop.sock_sendall(sock, data)
            while not stop.is_set() and time.monotonic() - t0 < max_s:
                with anyio.move_on_after(1.0):
                    chunk = await loop.sock_recv(sock, 1 << 18)
                    if not chunk:
                        self.eof_after_s = time.monotonic() - t0
                        return
                    self.raw += chunk
        finally:
            sock.close()


def body(session_id: str, n: int) -> dict[str, Any]:
    text = f"C3 heavy turn {n}: {TAG} «nonce:{uuid.uuid4().hex}»"
    content = [{"type": "input_text", "text": text}]
    return {"session_id": session_id, "input": [{"role": "user", "content": content}], "stream": True,
            "store": True, "generation": PINNED, "conditioning": "off"}  # fmt: skip


def response_of(client: SlowClient) -> tuple[int, dict[str, str], bytes]:
    """(status, headers, body) of the raw bytes received so far (h11; the body may be incomplete)."""
    request, payload = client.request()
    wire = h11.Connection(h11.CLIENT)
    for sent in (request, h11.Data(data=payload), h11.EndOfMessage()):
        wire.send(sent)
    wire.receive_data(bytes(client.raw))
    status, headers, body = 0, dict[str, str](), bytearray()
    while True:
        event = wire.next_event()
        if isinstance(event, h11.Response):
            status = event.status_code
            headers = {k.decode(): v.decode() for k, v in event.headers}
        elif isinstance(event, h11.Data):
            body += event.data
        else:
            return status, headers, bytes(body)


def obj(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def record(client: SlowClient, validator: ContractValidator, t_send: int) -> dict[str, Any]:
    out: dict[str, Any] = {"t_send": t_send, "t_end": time.monotonic_ns(), "eof_after_s": client.eof_after_s,
                           "bytes": len(client.raw), "violation": None}  # fmt: skip
    try:
        status, headers, raw = response_of(client)
        if status != 200:  # a shed or rejected turn: its JSON error envelope
            validator.check_response("createTurn", status, headers, raw, {})
            return out | {"status": status, "errors": [obj(obj(orjson.loads(raw)).get("error")).get("code")]}
        status, events = client.parse(validator)
    except AssertionError as exc:
        out["violation"] = str(exc)[:400]
        return out
    deltas = [obj(e.data) for e in events if e.event == "generation.delta"]
    created = [obj(e.data) for e in events if e.event == "turn.created"]
    out |= {
        "status": status,
        "turn_id": created[0].get("turn_id") if created else None,
        "frames": len(events),
        "coalesced": sum(1 for d in deltas if len(str(d.get("text", ""))) > CHUNK_CHARS),
        "text_chars": sum(len(str(d.get("text", ""))) for d in deltas),
        "errors": [obj(e.data).get("code") for e in events if e.event == "error"],
        "tail_bytes": client.tail_bytes,
    }
    return out


async def loop_turns(args: argparse.Namespace, index: int, key: str, out: IO[bytes], stop_ns: int) -> None:
    validator = ContractValidator()
    headers = {"authorization": f"Bearer {key}", "aghoo-version": AGHOO_VERSION, "content-type": "application/json"}
    await anyio.sleep(index * args.stagger_s / max(1, args.clients))
    n = 0
    async with httpx.AsyncClient(base_url=f"http://{args.host}:{args.port}", headers=headers, timeout=30) as http:
        while time.monotonic_ns() < stop_ns:
            response = await http.post("/v1/sessions", content=b"{}")
            try:
                validator.check_response(
                    "createSession", response.status_code, dict(response.headers), response.content, {}
                )
            except AssertionError as exc:
                out.write(orjson.dumps({"client": index, "kind": args.kind, "violation": str(exc)[:400]}) + b"\n")
            if response.status_code != 201:
                await anyio.sleep(1.0)
                continue
            kind = SlowClient if args.rate_bps > 0 else FastClient
            client = kind(
                args.host, args.port, key, body(str(response.json()["id"]), n), rate_bps=args.rate_bps, rcvbuf=RCVBUF
            )
            t_send = time.monotonic_ns()
            await client.run(anyio.Event(), args.turn_max_s)
            row = record(client, validator, t_send) | {"client": index, "n": n, "kind": args.kind}
            out.write(orjson.dumps(row) + b"\n")
            out.flush()
            n += 1
            if row.get("status") != 200:
                await anyio.sleep(1.0)  # retry-after of a shed turn (429 queue_timeout)


async def run(args: argparse.Namespace, key: str) -> None:
    stop_ns = time.monotonic_ns() + int(args.duration_s * 1e9)
    with args.out.open("wb") as out:
        async with anyio.create_task_group() as tg:
            for i in range(args.clients):
                tg.start_soon(loop_turns, args, i, key, out, stop_ns)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.load.slow", description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--clients", type=int, default=20)
    parser.add_argument("--rate-bps", type=float, required=True, help="read rate per client; 0 = as fast as possible")
    parser.add_argument("--kind", default="slow", help="recorded in every line (slow | fast)")
    parser.add_argument("--duration-s", type=float, required=True, help="no new turn starts after this")
    parser.add_argument("--turn-max-s", type=float, default=110.0, help="a turn's connection is closed after this")
    parser.add_argument("--stagger-s", type=float, default=10.0)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    import uvloop

    uvloop.run(run(args, Path(args.key_file).read_text().strip()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
