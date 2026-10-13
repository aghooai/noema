"""CLI: `python -m tools.mockbedrock --host 0.0.0.0 --port 8080 [--scenario FILE] [--record-limit N]
[--record-bytes N] [--region R] [--cpus LIST]`.

The compose service `mockbedrock` runs it on the api image (runtime dependencies only, read-only root, uid 10001);
its healthcheck should be `GET /_mock/requests?limit=0` -> 200 (O(1); the unpaged dump costs O(ring)). With
`--port 0` the bound port is printed on the first stdout line (`mockbedrock listening on http://<host>:<port>`).
SIGTERM and SIGINT stop it cleanly (exit 0). `--cpus` (default `$NOEMA_MOCK_CPUS`) pins the process to a taskset-style
CPU list before it starts, so its scripted latencies do not wait for a busy core (spec-10 §5 lateness); empty =
unpinned, a list with no usable CPU fails the start.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from .app import DEFAULT_REGION, MockBedrock
from .state import DEFAULT_RECORD_BYTES, DEFAULT_RECORD_LIMIT

CPUS_ENV = "NOEMA_MOCK_CPUS"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m tools.mockbedrock", description="Noema wire-level Bedrock mock")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="bind port; 0 = ephemeral (default 8080)")
    parser.add_argument("--scenario", type=Path, help="a scenario file (YAML or JSON) to load at start")
    parser.add_argument("--record-limit", type=int, default=DEFAULT_RECORD_LIMIT, help="request records kept")
    parser.add_argument(
        "--record-bytes", type=int, default=DEFAULT_RECORD_BYTES, help="approximate bytes the request log may keep"
    )
    parser.add_argument("--region", default=DEFAULT_REGION, help="the SigV4 region Converse accepts (us-east-1)")
    parser.add_argument("--log-level", default="info", choices=("debug", "info", "warning", "error"))
    parser.add_argument(
        "--cpus", default=os.environ.get(CPUS_ENV, ""), help=f"pin to these CPUs, e.g. 44-45 (default ${CPUS_ENV})"
    )
    return parser.parse_args(argv)


def cpu_list(spec: str) -> set[int]:
    """A taskset-style list (`44-45,47`) as a CPU set; ValueError when malformed."""
    cpus: set[int] = set()
    for part in spec.split(","):
        low, _, high = part.strip().partition("-")
        cpus.update(range(int(low), int(high or low) + 1))
    return cpus


async def serve(args: argparse.Namespace) -> None:
    scenario = args.scenario.read_text() if args.scenario is not None else None
    mock = MockBedrock(
        scenario=scenario, record_limit=args.record_limit, record_bytes=args.record_bytes, region=args.region
    )
    await mock.start(args.host, args.port)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    # Announce readiness only once the handlers are installed, so a stop signal right after the line exits cleanly.
    print(f"mockbedrock listening on http://{args.host}:{mock.port}", flush=True)
    try:
        await stop.wait()
    finally:
        await mock.stop()


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.cpus:
        os.sched_setaffinity(0, cpu_list(args.cpus))
        logging.getLogger("tools.mockbedrock").info("pinned to CPUs %s", sorted(os.sched_getaffinity(0)))
    try:
        import uvloop
    except ImportError:  # the runtime images ship uvloop; a bare interpreter falls back to asyncio
        asyncio.run(serve(args))
    else:
        uvloop.run(serve(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
