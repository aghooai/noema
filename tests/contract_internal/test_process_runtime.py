"""ProcessRuntime conformance (spec-01 §3, spec-11 §7): detached tasks that never take the process down, drain
hooks with the grace period, the draining flag behind /readyz, and per-runtime local state."""

from __future__ import annotations

import anyio
import anyio.lowlevel
import pytest

from .kits import RuntimeKit
from .registry import kit_fixture

kit = kit_fixture("process_runtime")


async def _settle() -> None:
    for _ in range(20):
        await anyio.lowlevel.checkpoint()


async def test_spawned_tasks_outlive_the_caller_and_failures_stay_contained(kit: RuntimeKit) -> None:
    with pytest.raises(RuntimeError):
        kit.runtime.spawn("before-open", anyio.lowlevel.checkpoint)
    await kit.open()
    ran: list[str] = []

    async def fails() -> None:
        raise ValueError("contained")

    async def records() -> None:
        ran.append("ok")

    kit.runtime.spawn("fails", fails)
    kit.runtime.spawn("records", records)
    await _settle()
    assert ran == ["ok"]
    await kit.close()
    with pytest.raises(RuntimeError):
        kit.runtime.spawn("after-close", anyio.lowlevel.checkpoint)


async def test_draining_starts_the_hooks_with_the_grace(kit: RuntimeKit) -> None:
    await kit.open()
    graces: list[float] = []

    async def hook(grace_s: float) -> None:
        graces.append(grace_s)

    kit.runtime.on_drain("orchestrator.cancel-turns", hook)
    assert (kit.runtime.draining, graces) == (False, [])
    kit.begin_drain()
    await _settle()
    assert (kit.runtime.draining, graces) == (True, [30.0])
    kit.begin_drain()  # once
    await _settle()
    assert graces == [30.0]
    await kit.close()


async def test_local_state_is_created_once(kit: RuntimeKit) -> None:
    first = kit.runtime.local("turns.registry", lambda: {"turns": 0})
    assert kit.runtime.local("turns.registry", dict) is first
