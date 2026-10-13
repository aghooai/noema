"""JobQueue + Worker conformance (spec-02 §7, spec-11 §4): dedupe, priority order, done/retry/dead, and the
cancellation grace of `Worker.run`."""

from __future__ import annotations

import anyio

from noema.core.protocols.storage import Job, PermanentJobError

from .kits import JobQueueKit
from .registry import kit_fixture

kit = kit_fixture("job_queue")


async def test_dedupe_returns_the_live_job(kit: JobQueueKit) -> None:
    a = await kit.queue.enqueue("session.delete", "t_acme", {"session_id": "ses_1"}, dedupe_key="ses_1")
    b = await kit.queue.enqueue("session.delete", "t_acme", {"session_id": "ses_1"}, dedupe_key="ses_1")
    c = await kit.queue.enqueue("session.delete", "t_other", {"session_id": "ses_1"}, dedupe_key="ses_1")
    assert a == b and c != a
    assert await kit.job_state(a) == "queued"


async def test_worker_runs_jobs_by_priority(kit: JobQueueKit) -> None:
    order: list[str] = []
    for prio, name in ((200, "low"), (10, "high"), (100, "mid")):
        await kit.queue.enqueue("media.process", "t_acme", {"name": name}, priority=prio)

    async def handler(job: Job) -> None:
        order.append(str(job.payload["name"]))
        assert job.attempt == 1 and job.tenant_id == "t_acme"

    async with kit.clock.autojump(), anyio.create_task_group() as tg:
        tg.start_soon(kit.queue.worker(["media.process"], handler, concurrency=1).run)
        await kit.clock.sleep(1)
        tg.cancel_scope.cancel()
    assert order == ["high", "mid", "low"]


async def test_failures_retry_with_backoff_then_permanent_is_dead(kit: JobQueueKit) -> None:
    flaky = await kit.queue.enqueue("labeling.item", "t_acme", {"kind": "flaky"})
    fatal = await kit.queue.enqueue("labeling.item", "t_acme", {"kind": "fatal"})
    attempts: dict[str, list[int]] = {"flaky": [], "fatal": []}

    async def handler(job: Job) -> None:
        kind = str(job.payload["kind"])
        attempts[kind].append(job.attempt)
        if kind == "fatal":
            raise PermanentJobError("bad payload")
        if job.attempt < 3:
            raise RuntimeError("transient")

    async with kit.clock.autojump(), anyio.create_task_group() as tg:
        tg.start_soon(kit.queue.worker(["labeling.item"], handler, concurrency=2).run)
        await kit.clock.sleep(200)  # backoff 30 s, then 60 s
        tg.cancel_scope.cancel()
    assert attempts == {"flaky": [1, 2, 3], "fatal": [1]}
    assert await kit.job_state(flaky) == "done"
    assert await kit.job_state(fatal) == "dead"


async def test_max_attempts_bounds_the_retries(kit: JobQueueKit) -> None:
    """spec-07 §3.1: media.process is enqueued with max_attempts = 2 (dead after the second failure)."""
    job_id = await kit.queue.enqueue("media.process", "t_acme", {"file_id": "file_1"}, max_attempts=2)
    attempts: list[int] = []

    async def handler(job: Job) -> None:
        attempts.append(job.attempt)
        raise RuntimeError("extractor crashed")

    async with kit.clock.autojump(), anyio.create_task_group() as tg:
        tg.start_soon(kit.queue.worker(["media.process"], handler, concurrency=1).run)
        await kit.clock.sleep(600)
        tg.cancel_scope.cancel()
    assert attempts == [1, 2]
    assert await kit.job_state(job_id) == "dead"


async def test_cancelling_run_gives_running_handlers_a_grace_period(kit: JobQueueKit) -> None:
    done: list[str] = []
    await kit.queue.enqueue("retention.sweep", "t_acme", {})

    async def handler(job: Job) -> None:
        await kit.clock.sleep(5)
        done.append(job.id)

    async with kit.clock.autojump(), anyio.create_task_group() as tg:
        tg.start_soon(kit.queue.worker(["retention.sweep"], handler, concurrency=1).run)
        await kit.clock.sleep(1)
        tg.cancel_scope.cancel()
    assert len(done) == 1  # finished inside the 10 s grace
