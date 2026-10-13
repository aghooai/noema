"""Files always reach a terminal state (C1 review): the per-file watchdog (`pipeline.arm_watchdog`, a delayed
`media.process` job with payload `watchdog`) re-queues a file whose job ended without settling it (a crash between
the INSERT and the enqueue, a worker killed on its last attempt) and settles it `failed` at the last round; an
unreachable sandboxd (`SandboxUnavailable`) is retried and never fails a file by itself."""

from __future__ import annotations

from collections.abc import AsyncIterator, Collection
from pathlib import Path
from typing import Any

import pytest
from tests.contract_internal.kits import principal

from noema.core.protocols.sandbox import ExtractResult, SandboxJob, SandboxLimits
from noema.core.protocols.storage import Job, PermanentJobError
from noema.media.pipeline import MEDIA_PROCESS, WATCHDOG_DELAY_S, WATCHDOG_ROUNDS
from noema.media.sandbox.remote import SandboxUnavailable

from . import media_fixtures as fx
from .support import InlineRunner, MediaEnv, md_script, media_env

pytestmark = pytest.mark.unit


class Outage:
    """sandboxd is unreachable for the `kinds` while `down` (nothing dispatched: `SandboxUnavailable`)."""

    def __init__(self, inner: InlineRunner, kinds: Collection[str] | None = None) -> None:
        self.inner = inner
        self.kinds = kinds
        self.down = True

    async def run(self, job: SandboxJob, limits: SandboxLimits) -> ExtractResult:
        if self.down and (self.kinds is None or job.kind in self.kinds):
            raise SandboxUnavailable("sandboxd is unreachable (FileNotFoundError)")
        return await self.inner.run(job, limits)


async def upload(env: MediaEnv, data: bytes, declared: str = "image/png", name: str = "a.png") -> str:
    async def body() -> AsyncIterator[bytes]:
        yield data

    ref = await env.files.upload(principal(), body(), declared_type=declared, filename=name, session_id=None)
    return ref.file_id


def media_jobs(env: MediaEnv) -> list[dict[str, Any]]:
    return [r for r in env.world.db.store.rows("job") if r["kind"] == MEDIA_PROCESS]


async def status(env: MediaEnv, file_id: str) -> str:
    return (await env.files.get(principal(), file_id)).status


async def test_every_file_arms_a_delayed_watchdog(tmp_path: Path) -> None:
    env = media_env(tmp_path)
    file_id = await upload(env, fx.png())
    (check,) = (r for r in media_jobs(env) if "watchdog" in r["payload"])
    assert check["payload"] == {"file_id": file_id, "watchdog": 1} and check["dedupe_key"] == f"{file_id}:watchdog:1"
    assert (check["run_after"] - env.clock.now()).total_seconds() == WATCHDOG_DELAY_S
    await env.drain()
    assert await status(env, file_id) == "ready" and check["state"] == "queued"  # not due yet
    await env.clock.advance(WATCHDOG_DELAY_S)
    ran = await env.drain()
    assert [j.payload.get("watchdog") for j in ran] == [1] and len(media_jobs(env)) == 2  # terminal: nothing new


async def test_a_file_whose_job_ended_unsettled_is_requeued(tmp_path: Path) -> None:
    """The state a crash between the INSERT and the enqueue (or a lost run) leaves: no live job, file quarantined."""
    env = media_env(tmp_path)
    file_id = await upload(env, fx.png())
    async with env.world.db.system_uow() as sys:
        (job,) = await sys.claim_jobs((MEDIA_PROCESS,), 10, "w", 60)
        await sys.finish_job(job.id, "w", ok=True, error=None)
    assert await status(env, file_id) == "quarantined" and await env.drain() == []
    await env.clock.advance(WATCHDOG_DELAY_S)
    ran = await env.drain()
    assert [j.payload.get("watchdog") for j in ran] == [1, None]  # the check, then the fresh run
    assert await status(env, file_id) == "ready"


async def test_a_live_run_is_not_duplicated(tmp_path: Path) -> None:
    env = media_env(tmp_path)
    file_id = await upload(env, fx.png())
    await env.processor.process(Job("job_check", "t_acme", MEDIA_PROCESS, {"file_id": file_id, "watchdog": 1}, 1))
    runs = [r for r in media_jobs(env) if "watchdog" not in r["payload"]]
    assert len(runs) == 1 and runs[0]["state"] == "queued"  # dedupe file_id: the queued run is kept
    assert {r["payload"].get("watchdog") for r in media_jobs(env)} == {None, 1, 2}


async def test_a_sandbox_outage_never_fails_the_file_and_the_watchdog_recovers_it(tmp_path: Path) -> None:
    env = media_env(tmp_path)
    outage = Outage(env.runner)
    env.processor._runner = outage
    file_id = await upload(env, fx.png())
    await env.drain()
    await env.clock.advance(31)  # the 30 s backoff, then the last attempt
    await env.drain()
    run = next(r for r in media_jobs(env) if "watchdog" not in r["payload"])
    assert run["state"] == "dead" and await status(env, file_id) == "processing"  # not settled: nothing ran
    outage.down = False
    await env.clock.advance(WATCHDOG_DELAY_S)
    await env.drain()
    assert await status(env, file_id) == "ready"


async def test_the_last_round_settles_a_file_that_never_finishes(tmp_path: Path) -> None:
    env = media_env(tmp_path)
    env.processor._runner = Outage(env.runner)
    file_id = await upload(env, fx.png())
    for _ in range(WATCHDOG_ROUNDS):
        for _ in range(2):  # both attempts of the current run
            await env.drain()
            await env.clock.advance(31)
        assert await status(env, file_id) == "processing"
        await env.clock.advance(WATCHDOG_DELAY_S)
    await env.drain()
    ref = await env.files.get(principal(), file_id)
    assert ref.status == "failed" and (ref.failure or {}).get("code") == "extraction_failed"
    assert all(d.status == "failed" for d in ref.derivatives)
    assert not [r for r in media_jobs(env) if r["state"] == "queued"]  # no further round


async def test_an_outage_in_a_pdf_sibling_is_retried_unwrapped(tmp_path: Path) -> None:
    """`pdf.render` ∥ `pdf.markdown`: the sibling's `SandboxUnavailable` reaches the handler as such (no
    ExceptionGroup), so the last attempt does not settle the file; the retry skips what finished."""
    env = media_env(tmp_path)
    env.runner.script["pdf.markdown"] = md_script(["Invoice number 4471"])
    outage = Outage(env.runner, kinds={"pdf.render"})
    env.processor._runner = outage
    file_id = await upload(env, fx.text_pdf("Invoice number 4471"), "application/pdf", "a.pdf")
    await env.drain()
    await env.clock.advance(31)
    await env.drain()
    ref = await env.files.get(principal(), file_id)
    kinds = {d.kind: d.status for d in ref.derivatives}
    assert ref.status == "partial" and kinds["markdown"] == "ready" and kinds["page_images"] == "pending"
    outage.down = False
    await env.clock.advance(WATCHDOG_DELAY_S)
    await env.drain()
    assert await status(env, file_id) == "ready"
    assert [j.kind for j in env.runner.jobs].count("pdf.markdown") == 1  # not re-run


async def test_watchdog_of_a_purged_file_and_a_bad_payload(tmp_path: Path) -> None:
    env = media_env(tmp_path)
    file_id = await upload(env, fx.png())
    await env.files.purge("t_acme", file_id)
    await env.processor.process(Job("job_check", "t_acme", MEDIA_PROCESS, {"file_id": file_id, "watchdog": 1}, 1))
    assert {r["payload"].get("watchdog") for r in media_jobs(env)} == {None, 1}  # nothing re-queued or re-armed
    with pytest.raises(PermanentJobError):
        await env.processor.process(Job("job_bad", "t_acme", MEDIA_PROCESS, {"file_id": file_id, "watchdog": 0}, 1))
