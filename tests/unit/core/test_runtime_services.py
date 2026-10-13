"""`TaskGroupRuntime` (core/runtime.py), the job-handler helpers (core/protocols/services.py), the object-storage
conventions (core/objects.py) and `Degradation.to_wire`."""

from __future__ import annotations

import logging

import anyio
import anyio.lowlevel
import pytest
from tests.fakes.clock import FakeClock

from noema.core.objects import default_bucket, sse_args, tenant_kms_key_alias
from noema.core.protocols.runtime import ProcessRuntime
from noema.core.protocols.services import expand_kinds, merge_handlers
from noema.core.protocols.storage import Job
from noema.core.runtime import TaskGroupRuntime
from noema.core.values import Degradation


async def test_spawn_runs_detached_and_logs_failures(caplog: pytest.LogCaptureFixture) -> None:
    runtime = TaskGroupRuntime(FakeClock())
    view: ProcessRuntime = runtime
    with pytest.raises(RuntimeError):
        view.spawn("early", anyio.lowlevel.checkpoint)
    await runtime.open()
    done = anyio.Event()

    async def boom() -> None:
        raise KeyError("secret-free")

    with caplog.at_level(logging.ERROR, logger="noema.runtime"):
        view.spawn("boom", boom)
        view.spawn("ok", lambda: _set(done))
        await done.wait()
        for _ in range(10):
            await anyio.lowlevel.checkpoint()
    assert any("runtime task boom failed: KeyError" in r.getMessage() for r in caplog.records)
    await runtime.close()
    await runtime.close()  # idempotent
    with pytest.raises(RuntimeError):
        view.spawn("late", anyio.lowlevel.checkpoint)


async def _set(event: anyio.Event) -> None:
    event.set()


async def test_close_cancels_spawned_tasks() -> None:
    clock = FakeClock()
    runtime = TaskGroupRuntime(clock)
    await runtime.open()
    cancelled = []

    async def forever() -> None:
        try:
            await clock.sleep(10_000)
        except anyio.get_cancelled_exc_class():
            cancelled.append(True)
            raise

    runtime.spawn("forever", forever)
    await anyio.lowlevel.checkpoint()
    await runtime.close()
    assert cancelled == [True]


async def test_drain_hooks_get_the_grace_and_drain_is_bounded_by_the_clock() -> None:
    clock = FakeClock()
    runtime = TaskGroupRuntime(clock, grace_s=30)
    await runtime.open()
    seen: list[tuple[str, float]] = []

    async def quick(grace_s: float) -> None:
        seen.append(("quick", grace_s))

    async def slow(grace_s: float) -> None:
        seen.append(("slow", grace_s))
        await clock.sleep(grace_s * 10)

    runtime.on_drain("quick", quick)
    runtime.on_drain("slow", slow)
    assert not runtime.draining
    result: list[bool] = []

    async def drain() -> None:
        result.append(await runtime.drain())

    async with anyio.create_task_group() as tg:
        tg.start_soon(drain)
        await clock.advance(29)
        assert (runtime.draining, result) == (True, [])
        await clock.advance(1)
    assert result == [False] and sorted(seen) == [("quick", 30), ("slow", 30)]
    late: list[float] = []

    async def after(grace_s: float) -> None:
        late.append(grace_s)

    runtime.on_drain("after", after)  # already draining: runs at once
    for _ in range(5):
        await anyio.lowlevel.checkpoint()
    assert late == [30]
    await runtime.close()


async def test_drain_without_hooks_returns_at_once_and_local_is_per_runtime() -> None:
    runtime = TaskGroupRuntime(FakeClock())
    other = TaskGroupRuntime(FakeClock())
    registry: object = runtime.local("turns.registry", dict)
    again: object = runtime.local("turns.registry", list)  # the factory runs once per runtime
    assert again is registry and other.local("turns.registry", dict) is not registry
    runtime.begin_drain()  # not open: only the flag
    assert runtime.draining and await runtime.drain(0) is True


async def _noop(job: Job) -> None:
    await anyio.lowlevel.checkpoint()


def test_job_handler_helpers() -> None:
    merged = merge_handlers({"labeling.item": _noop, "labeling.drift": _noop}, {"session.delete": _noop})
    assert set(merged) == {"labeling.item", "labeling.drift", "session.delete"}
    with pytest.raises(ValueError, match="claimed twice"):
        merge_handlers({"session.delete": _noop}, {"session.delete": _noop})
    assert expand_kinds(["labeling.*", "session.delete"], merged) == (
        "labeling.drift",
        "labeling.item",
        "session.delete",
    )
    with pytest.raises(ValueError, match="matches no job handler"):
        expand_kinds(["retention.sweep"], merged)
    assert expand_kinds(["Labeling.*", "labeling.*"], {"labeling.x": _noop, "Labeling.y": _noop}) == (
        "Labeling.y",
        "labeling.x",
    )


def test_object_storage_conventions() -> None:
    assert default_bucket("prod") == "noema-prod"
    assert tenant_kms_key_alias("prod", "t_acme") == "alias/noema-prod-t_acme"
    assert sse_args("prod", "t_acme") == {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": "alias/noema-prod-t_acme"}
    assert sse_args("test", "t_acme") == {"ServerSideEncryption": "AES256"}
    with pytest.raises(ValueError, match="tenant_id"):
        tenant_kms_key_alias("prod", "")


def test_degradation_to_wire_omits_unset_fields() -> None:
    d = Degradation("image", "ocr_text", "model_capability", part_index=0, model="us.zai.glm-5.3")
    assert d.to_wire() == {
        "from": "image",
        "to": "ocr_text",
        "reason": "model_capability",
        "part_index": 0,
        "model": "us.zai.glm-5.3",
    }
    assert list(d.to_wire()) == ["from", "to", "reason", "part_index", "model"]
    assert Degradation("pdf", "markdown", "media_budget").to_wire() == {
        "from": "pdf",
        "to": "markdown",
        "reason": "media_budget",
    }
