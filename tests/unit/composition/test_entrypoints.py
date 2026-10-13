"""The worker entrypoints (spec-01 §4): `--kinds` parsing, the HANDLERS merge per process (jobs never claims the
media-worker's kinds), the dispatch of an unknown kind, and `serve()` starting and stopping the process services."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Final

import pytest

from noema.composition import SERVICE_FIELDS, Services, build
from noema.core.protocols.storage import Job, PermanentJobError
from noema.entrypoints import jobs, media_worker
from noema.entrypoints._worker import dispatch, parse_args, serve

pytestmark = [pytest.mark.unit]
FAKES: Final = frozenset(SERVICE_FIELDS) - {"redis", "runtime", "clock"}  # the fake workers poll on the real clock
JOBS_KINDS: Final = ("labeling.drift", "labeling.item", "labeling.run", "retention.sweep", "session.delete")


def test_the_default_kinds() -> None:
    assert parse_args([], prog="jobs", default_kinds=jobs.DEFAULT_KINDS).kinds == [
        "labeling.*",
        "session.delete",
        "retention.sweep",
    ]
    args = parse_args(["--concurrency", "3"], prog="mw", default_kinds=media_worker.DEFAULT_KINDS)
    assert (args.kinds, args.concurrency) == (["media.process", "file.purge"], 3)
    for argv in (["--kinds", " , "], ["--concurrency", "0"]):
        with pytest.raises(SystemExit):
            parse_args(argv, prog="jobs", default_kinds=jobs.DEFAULT_KINDS)


async def test_dispatch_routes_by_kind_and_refuses_unknown_kinds() -> None:
    seen: list[str] = []

    async def handler(job: Job) -> None:
        seen.append(job.id)

    run = dispatch({"session.delete": handler})
    job = Job(id="job_1", tenant_id="t_acme", kind="session.delete", payload={}, attempt=1)
    await run(job)
    assert seen == ["job_1"]
    with pytest.raises(PermanentJobError):
        await run(Job(id="job_2", tenant_id="t_acme", kind="nope", payload={}, attempt=1))


def _recording(services: Services) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []
    original = services.jobs.worker

    def worker(kinds: Sequence[str], handler: Callable[[Job], Awaitable[None]], *, concurrency: int) -> Any:
        calls.append(tuple(kinds))
        return original(kinds, handler, concurrency=concurrency)

    services.jobs.worker = worker  # type: ignore[method-assign]
    return calls


async def _now() -> None:
    return None


@pytest.mark.parametrize(
    ("module", "process", "kinds", "expected"),
    [
        (jobs, "jobs", ["labeling.*", "session.delete", "retention.sweep"], JOBS_KINDS),
        (media_worker, "media-worker", ["media.process", "file.purge"], ("file.purge", "media.process")),
    ],
)
async def test_serve_merges_the_owners_handlers(
    module: Any, process: Any, kinds: list[str], expected: tuple[str, ...]
) -> None:
    services = build("test", fakes=FAKES, environ={})
    calls = _recording(services)
    await serve(process, module.factories(), kinds, concurrency=2, services=services, stopped=_now)
    assert calls == [expected]
    assert services._lifecycle.stopped and not services.runtime.is_open


async def test_jobs_never_claims_the_media_workers_kinds() -> None:
    services = build("test", fakes=FAKES, environ={})
    with pytest.raises(ValueError, match="matches no job handler"):
        await serve("jobs", jobs.factories(), ["media.process"], concurrency=1, services=services, stopped=_now)
    assert services._lifecycle.stopped  # a startup refusal still stops what start() opened
