"""The worker-process runner shared by `noema.entrypoints.jobs` and `.media_worker` (spec-01 §1, §4; spec-11 §4, §7).

`serve()` builds the process `Services` (`composition.build(NOEMA_ENV)`), starts them for the process, merges the
owners' `make_handlers(services)` maps (`merge_handlers`: a kind claimed twice is a startup error), expands the
`--kinds` globs against them (`expand_kinds`: a pattern without a handler is a startup error) and runs one job worker
(`services.jobs.worker`) in the root task group until SIGTERM or SIGINT. Shutdown: `composition.stop` drains (the
worker stops claiming; running handlers get 10 s), flushes the outbox and closes the clients; unfinished jobs return
through the reaper.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
from collections.abc import Awaitable, Callable, Sequence
from typing import Final, cast

import anyio

from noema import composition
from noema.core.errors import ForbiddenInEnvironment
from noema.core.protocols.services import JobHandler, JobHandlers, ServiceSet, expand_kinds, merge_handlers
from noema.core.protocols.storage import Job, PermanentJobError
from noema.core.settings import Env

log: Final = logging.getLogger("noema.entrypoints")
DEFAULT_CONCURRENCY: Final = 8

type HandlerFactory = Callable[[ServiceSet], JobHandlers]


def parse_args(argv: Sequence[str] | None, *, prog: str, default_kinds: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--kinds", default=default_kinds, help=f"comma-separated job kinds or globs (default {default_kinds})"
    )
    parser.add_argument(
        "--concurrency", type=int, default=DEFAULT_CONCURRENCY, help="jobs run at once (default %(default)s)"
    )
    args = parser.parse_args(argv)
    args.kinds = [k.strip() for k in str(args.kinds).split(",") if k.strip()]
    if not args.kinds:
        parser.error("--kinds names no job kind")
    if args.concurrency < 1:
        parser.error("--concurrency must be >= 1")
    return args


def dispatch(handlers: JobHandlers) -> JobHandler:
    """One worker handler over the merged map: a kind without a handler is a permanent failure (dead at once)."""

    async def run(job: Job) -> None:
        handler = handlers.get(job.kind)
        if handler is None:
            raise PermanentJobError(f"no handler for job kind {job.kind!r}")
        await handler(job)

    return run


def resolve_env(environ: dict[str, str] | None = None) -> Env:
    value = (os.environ if environ is None else environ).get("NOEMA_ENV", "")
    if value not in composition.ENVS:
        raise composition.StartupError(f"NOEMA_ENV must be one of {sorted(composition.ENVS)}")
    return cast("Env", value)


async def serve(
    process: composition.Process,
    factories: Sequence[HandlerFactory],
    kinds: Sequence[str],
    *,
    concurrency: int,
    services: composition.Services | None = None,
    stopped: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Run the worker process until `stopped()` returns (default: SIGTERM or SIGINT). `services` (tests) is built
    from `NOEMA_ENV` when None; either way this function starts and stops it."""
    services = services if services is not None else composition.build(resolve_env(), process=process)
    await composition.start(services, process)
    try:
        handlers = merge_handlers(*(factory(services) for factory in factories))
        selected = expand_kinds(kinds, handlers)
        worker = services.jobs.worker(selected, dispatch(handlers), concurrency=concurrency)
        run_worker = composition.supervised("ops.jobs.worker", worker.run, services.clock)  # restarted on a bug
        services.runtime.spawn("ops.jobs.worker", run_worker)
        drain = getattr(worker, "drain", None)  # PgWorker: stop claiming, wait for the running handlers
        if callable(drain):
            services.runtime.on_drain("ops.jobs.worker", drain)
        log.info("%s worker started: kinds=%s concurrency=%d", process, ",".join(selected), concurrency)
        await (stopped or _signalled)()
        log.info("%s worker stopping", process)
    finally:
        await composition.stop(services)


async def _signalled() -> None:
    with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
        async for _signum in signals:
            return


def run(process: composition.Process, factories: Sequence[HandlerFactory], args: argparse.Namespace) -> int:
    """The process main: exit 0 after a graceful stop, 2 for a startup error (invalid settings or kinds)."""
    try:
        anyio.run(
            lambda: serve(process, factories, args.kinds, concurrency=args.concurrency),
            backend="asyncio",
            backend_options={"use_uvloop": True},
        )
    except (ValueError, ForbiddenInEnvironment) as exc:  # StartupError, a kinds/handlers clash, a gated backend
        log.error("%s: startup refused: %s", process, exc)
        return 2
    return 0
