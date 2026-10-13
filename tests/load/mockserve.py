"""The wire mock for load runs: `tools.mockbedrock.MockBedrock` in-process (same server as the CLI) plus

- a record drain: every `DRAIN_EVERY_S` the records older than `--drain-after-s` (complete by then) are reduced to
  their timing fields and appended to `--out` as JSON lines, in-process, so the run never pages
  `GET /_mock/requests` over HTTP (an O(page) serialization on the mock's loop) and the ring stays small;
- an event-loop lag sampler (`--lag-out`; spec-10 §5: a run is invalid if the mock's lag p99 exceeds 2 ms).

The request's nonce is the LAST `«nonce:<id>»` in the request's last message: with session history in the prompt the
first marker belongs to an older turn (the mock's own `nonce` field is the first one).

    taskset -c 40-41 python -m tests.load.mockserve --port 28781 --scenario tests/load/scenarios/overhead.yaml \
        --out /tmp/run/mock.jsonl --lag-out /tmp/run/mock.lag
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import gc
import re
import signal
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import IO, Any, Final

import orjson
from tests.load.lag import LagSampler
from tools.mockbedrock import MockBedrock

DRAIN_EVERY_S: Final = 0.5
PAGE: Final = 5000
NONCE: Final = re.compile(rb"\xc2\xabnonce:([^\xc2]{1,128})\xc2\xbb")  # «nonce:<id>» in UTF-8
FIELDS: Final = (
    "seq",
    "op",
    "model",
    "rule",
    "status",
    "outcome",
    "t_req_recv_ns",
    "t_first_byte_ns",
    "t_first_text_sent_ns",
    "t_done_ns",
    "akid",  # C1: the mock's quota windows are per (AKID, model)
)


def last_nonce(body: Any) -> str | None:
    """The last nonce marker of the request's last message (the whole body when it has no `messages`)."""
    node = body
    if isinstance(body, Mapping) and isinstance(body.get("messages"), list) and body["messages"]:
        node = body["messages"][-1]
    found = NONCE.findall(orjson.dumps(node))
    return found[-1].decode() if found else None


def compact(record: Mapping[str, Any]) -> dict[str, Any]:
    out = {k: record.get(k) for k in FIELDS}
    out["nonce"] = last_nonce(record.get("body"))
    return out


class Drain:
    """Moves complete records from the mock's ring to the output file."""

    def __init__(self, mock: MockBedrock, out: IO[bytes], drain_after_s: float) -> None:
        self.mock = mock
        self.out = out
        self.after_ns = int(drain_after_s * 1e9)
        self.cursor = 0
        self.written = 0
        self.lost = 0

    def step(self, *, everything: bool = False) -> None:
        horizon = time.monotonic_ns() - (0 if everything else self.after_ns)
        while True:
            page = self.mock.requests(self.cursor, PAGE)
            if not page:
                return
            for record in page:
                if record["t_req_recv_ns"] > horizon:
                    return
                self.lost += record["seq"] - self.cursor - 1
                self.cursor = record["seq"]
                self.out.write(orjson.dumps(compact(record)) + b"\n")
                self.written += 1
            self.out.flush()

    async def run(self) -> None:
        while True:
            await asyncio.sleep(DRAIN_EVERY_S)
            self.step()


async def serve(args: argparse.Namespace) -> None:
    mock = MockBedrock(
        scenario=args.scenario.read_text(), record_limit=args.record_limit, record_bytes=args.record_bytes
    )
    if args.quotas:  # C1: per-(AKID, model) Bedrock quotas, e.g. '[{"model": "us.openai.gpt-6.1-sol", "rpm": 600}]'
        mock.set_quotas(orjson.loads(args.quotas))
    await mock.start(args.host, args.port, reuse_port=args.reuse_port)
    print(f"mockbedrock listening on http://{args.host}:{mock.port}", flush=True)  # the runner waits for this line
    gc.freeze()  # startup objects (scenario, modules) are never scanned again
    lag = LagSampler(args.lag_out)
    lag.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    with args.out.open("wb") as out:
        drain = Drain(mock, out, args.drain_after_s)
        task = loop.create_task(drain.run())
        try:
            await stop.wait()
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await lag.stop()
            await mock.stop()
            drain.step(everything=True)
            out.write(orjson.dumps({"summary": {"written": drain.written, "lost": drain.lost}}) + b"\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.load.mockserve", description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--lag-out", type=Path, required=True)
    parser.add_argument("--drain-after-s", type=float, default=20.0)
    parser.add_argument("--reuse-port", action="store_true", help="SO_REUSEPORT: several mocks share --port")
    parser.add_argument("--quotas", help="JSON list of quotas (tools/mockbedrock set_quotas: model, rpm, tpm, akid)")
    # the drain empties the ring after --drain-after-s, so it only has to hold that long; a big ring of prompt-sized
    # records made the mock's full GCs stall its loop for up to ~0.9 s at 200 streams (C2b perf smoke)
    parser.add_argument("--record-limit", type=int, default=30_000)
    parser.add_argument("--record-bytes", type=int, default=512 << 20)
    args = parser.parse_args(argv)
    import uvloop

    uvloop.run(serve(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
