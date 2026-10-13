"""One Noema api process ("pod") for the load harness: the image CMD (uvicorn, uvloop, httptools, no access log,
35 s graceful shutdown) run programmatically, plus the measurement hooks the CLI cannot add:

- an event-loop lag sampler on the server's loop (`--lag-out`, see tests/load/lag.py);
- a GC pause log (`--gc-out`: int64 triples t_start_ns, duration_ns, generation, from `gc.callbacks`);
- an on-demand profiler: SIGUSR1 starts it, SIGUSR2 stops it and writes `--profile-out` (`--profiler cpu`: SIGPROF
  every 1 ms of CPU, the stack of the loop thread, low overhead and CPU-proportional; `sample`: a wall-clock sampler
  thread, biased toward GIL-releasing calls; `cprofile`: stdlib cProfile, exact call counts but about 2x slower).
  The profiled runs are never the reported numbers.

    set -a; . ~/.noema/stacks/noema-perf.env; set +a
    taskset -c 44-45 python -m tests.load.serve --port 28790 --lag-out /tmp/run/api.lag

The process environment is the stack's host env (NOEMA_ENV, endpoint overrides, DSN secret ref); SIGTERM/SIGINT
stop it the uvicorn way (drain, lifespan shutdown) and the samples are flushed before exit.
"""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import gc
import io
import pstats
import signal
import socket
import sys
import threading
import time
from array import array
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from types import CodeType, FrameType
from typing import Any, Final

import uvicorn
from tests.load.lag import LagSampler

SAMPLE_INTERVAL_S: Final = 0.002
APP: Final = "noema.api.app:create_app"


class GcLog:
    """Every collection's start, duration and generation (int64 triples), appended to `path` once a second."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._pending = array("q")
        self._started = 0
        self._last_flush = time.monotonic_ns()
        path.write_bytes(b"")

    def callback(self, phase: str, info: dict[str, Any]) -> None:
        now = time.monotonic_ns()
        if phase == "start":
            self._started = now
            return
        self._pending.extend((self._started, now - self._started, int(info.get("generation", -1))))
        if now - self._last_flush > 1_000_000_000:
            self.flush()

    def flush(self) -> None:
        self._last_flush = time.monotonic_ns()
        if self._pending:
            with self.path.open("ab") as out:
                self._pending.tofile(out)
            self._pending = array("q")


class StackSampler:
    """Wall-clock samples of the main thread's Python stack every `interval_s`; `dump()` writes collapsed stacks
    (flamegraph input) plus self/inclusive tables of the busy samples (a stack deeper than the idle loop's)."""

    def __init__(self, interval_s: float = SAMPLE_INTERVAL_S) -> None:
        self.interval_s = interval_s
        self.stacks: Counter[tuple[CodeType, ...]] = Counter()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._main = threading.main_thread().ident
        self._switch = sys.getswitchinterval()
        self._previous: Any = None

    def start(self) -> None:
        # the sampler can only look while the loop thread drops the GIL: without a short switch interval it would
        # see mostly the GIL-releasing syscalls (urandom, hashing, poll) instead of where the time goes
        self._switch = sys.getswitchinterval()
        sys.setswitchinterval(self.interval_s / 10)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="load.stack_sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        sys.setswitchinterval(self._switch)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            frame: FrameType | None = sys._current_frames().get(self._main or 0)
            codes: list[CodeType] = []
            while frame is not None:
                codes.append(frame.f_code)
                frame = frame.f_back
            self.stacks[tuple(reversed(codes))] += 1

    def dump(self, path: Path) -> None:
        def name(code: CodeType) -> str:
            return f"{code.co_filename.rsplit('site-packages/', 1)[-1]}:{code.co_qualname}"

        total = sum(self.stacks.values())
        idle_depth = -1 if isinstance(self, CpuSampler) else min((len(s) for s in self.stacks), default=0)
        busy = {s: n for s, n in self.stacks.items() if len(s) > idle_depth}
        n_busy = sum(busy.values())
        own: Counter[str] = Counter()
        incl: Counter[str] = Counter()
        for stack, n in busy.items():
            own[name(stack[-1])] += n
            for key in {name(c) for c in stack}:
                incl[key] += n
        lines = [f"samples {total}, busy {n_busy} ({100.0 * n_busy / max(1, total):.1f}%), interval {self.interval_s}s"]
        lines.append("\n== self (share of busy samples) ==")
        lines += [f"{100.0 * n / max(1, n_busy):6.2f}%  {k}" for k, n in own.most_common(60)]
        lines.append("\n== inclusive (share of busy samples) ==")
        lines += [f"{100.0 * n / max(1, n_busy):6.2f}%  {k}" for k, n in incl.most_common(120)]
        path.write_text("\n".join(lines) + "\n")
        collapsed = path.with_suffix(".collapsed")
        collapsed.write_text("".join(f"{';'.join(name(c) for c in s)} {n}\n" for s, n in busy.items()))


class CpuSampler(StackSampler):
    """CPU-time samples: SIGPROF every `interval_s` of process CPU (setitimer ITIMER_PROF); the handler runs on the
    loop thread at the next bytecode boundary, so C time (GC, hashing, orjson) is charged to its Python caller and an
    idle loop is never sampled. Every sample is busy (`dump` drops none)."""

    def start(self) -> None:
        self._previous = signal.signal(signal.SIGPROF, self._on_signal)
        signal.setitimer(signal.ITIMER_PROF, self.interval_s, self.interval_s)

    def stop(self) -> None:
        signal.setitimer(signal.ITIMER_PROF, 0, 0)
        signal.signal(signal.SIGPROF, self._previous)

    def _on_signal(self, signum: int, frame: FrameType | None) -> None:
        codes: list[CodeType] = []
        while frame is not None:
            codes.append(frame.f_code)
            frame = frame.f_back
        self.stacks[tuple(reversed(codes))] += 1


class Profiling:
    """SIGUSR1 starts the chosen profiler, SIGUSR2 stops it and writes the report (repeatable: `-<n>` suffix)."""

    def __init__(self, kind: str, out: Path) -> None:
        self.kind = kind
        self.out = out
        self.round = 0
        self._sampler: StackSampler | None = None
        self._cprofile: cProfile.Profile | None = None

    def start(self) -> None:
        if self.kind == "cprofile":
            self._cprofile = cProfile.Profile()
            self._cprofile.enable()
        else:
            self._sampler = CpuSampler(0.001) if self.kind == "cpu" else StackSampler()
            self._sampler.start()

    def stop(self) -> None:
        self.round += 1
        path = self.out.with_name(f"{self.out.stem}-{self.round}{self.out.suffix}")
        if self._cprofile is not None:
            self._cprofile.disable()
            text = io.StringIO()
            stats = pstats.Stats(self._cprofile, stream=text)
            stats.sort_stats("tottime").print_stats(80)
            stats.sort_stats("cumulative").print_stats(120)
            path.write_text(text.getvalue())
            self._cprofile = None
        if self._sampler is not None:
            self._sampler.stop()
            self._sampler.dump(path)
            self._sampler = None


def census(path: Path) -> None:
    """The GC-tracked heap by type (SIGRTMIN): what a full collection has to scan, frozen objects excluded; then
    the cyclic garbage a full collection finds now (what only the GC can free), by type."""
    counts = Counter(type(o).__qualname__ for o in gc.get_objects())
    lines = [f"tracked {sum(counts.values())}, frozen {gc.get_freeze_count()}, counts {gc.get_count()}"]
    lines += [f"{n:9d}  {name}" for name, n in counts.most_common(60)]
    gc.set_debug(gc.DEBUG_SAVEALL)
    try:
        gc.collect()
        garbage = Counter(f"{type(o).__module__}.{type(o).__qualname__}" for o in gc.garbage)
        gc.garbage.clear()
    finally:
        gc.set_debug(0)
    lines.append(f"-- cyclic garbage: {sum(garbage.values())} objects")
    lines += [f"{n:9d}  {name}" for name, n in garbage.most_common(60)]
    with path.open("a") as out:
        out.write("\n".join(lines) + "\n\n")


async def serve(args: argparse.Namespace) -> None:
    config = uvicorn.Config(
        APP,
        factory=True,
        host=args.host,
        port=args.port,
        loop="uvloop",
        http="httptools",
        access_log=False,
        timeout_graceful_shutdown=35,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    lag = LagSampler(args.lag_out) if args.lag_out else None
    gc_log = GcLog(args.gc_out) if args.gc_out else None
    if gc_log is not None:
        gc.callbacks.append(gc_log.callback)
    if args.profile_out:
        profiling = Profiling(args.profiler, args.profile_out)
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGUSR1, profiling.start)
        loop.add_signal_handler(signal.SIGUSR2, profiling.stop)
    if args.gc_threshold:
        gc.set_threshold(*(int(x) for x in args.gc_threshold.split(",")))
    if args.dispatch_yields is not None:  # A/B of the decision dispatch yields (C2b perf smoke fix 5)
        from noema.decision import coordinator

        setattr(coordinator, "DISPATCH_YIELDS", args.dispatch_yields)  # noqa: B010 (a Final, read per call)
    if args.trace_out:
        from tests.load import trace

        trace.install()
    if args.census_out:
        asyncio.get_running_loop().add_signal_handler(signal.SIGRTMIN, census, args.census_out)
    probe = None
    if args.sse_probe_out:  # C3: the SSE writer's queue high-water marks and client_too_slow timing
        from tests.load.sseprobe import Probe

        probe = Probe()
        probe.install()
    if lag is not None:
        lag.start()
    try:
        # --fd: a listening socket inherited from tests.load.run --api-procs N, shared by N api processes like the
        # workers of `uvicorn --workers N` (WEB_CONCURRENCY) share the parent's socket
        await server.serve(sockets=[socket.socket(fileno=args.fd)] if args.fd is not None else None)
    finally:
        if lag is not None:
            await lag.stop()
        if gc_log is not None:
            gc_log.flush()
        if args.trace_out:
            trace.dump(args.trace_out)
        if probe is not None:
            probe.dump(args.sse_probe_out)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.load.serve", description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--fd", type=int, help="serve on this inherited listening socket (several api processes)")
    parser.add_argument("--lag-out", type=Path)
    parser.add_argument("--gc-out", type=Path)
    parser.add_argument("--profile-out", type=Path, help="report path; SIGUSR1 starts, SIGUSR2 stops and writes")
    parser.add_argument("--profiler", choices=("cpu", "sample", "cprofile"), default="cpu")
    parser.add_argument("--census-out", type=Path, help="SIGRTMIN appends a GC heap census by type here")
    parser.add_argument("--trace-out", type=Path, help="wall-clock createTurn timelines (tests/load/trace.py)")
    parser.add_argument("--gc-threshold", help="gc.set_threshold(a, b, c) before the app starts (experiments)")
    parser.add_argument("--dispatch-yields", type=int, help="override noema.decision.coordinator.DISPATCH_YIELDS")
    parser.add_argument("--sse-probe-out", type=Path, help="SSE writer high-water / too-slow probe (C3)")
    args = parser.parse_args(argv)
    # uvicorn re-raises the signal that stopped it once serve() returns; keep that from killing us before the flush
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: None)
    factory = uvicorn.Config(APP, loop="uvloop").get_loop_factory()
    with asyncio.Runner(loop_factory=factory) as runner:
        runner.run(serve(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
