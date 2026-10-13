"""`deadline_scope` on `SystemClock` is a native anyio deadline (no watcher task, E3 / sprint B F1), with the same
outcomes as the clock-driven watcher variant that every other clock keeps."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from typing import Any

import anyio
import anyio.lowlevel
import pytest

from noema.core import clock as clock_mod
from noema.core.clock import Deadline, SystemClock, deadline_scope

pytestmark = pytest.mark.unit

Scope = Callable[[Any, float], contextlib.AbstractAsyncContextManager[anyio.CancelScope]]
VARIANTS: dict[str, Scope] = {"loop": deadline_scope, "watcher": clock_mod._watched_scope}


async def test_a_system_clock_scope_starts_no_task_and_is_its_own_cancel_scope() -> None:
    clock = SystemClock()
    before = len(asyncio.all_tasks())
    async with deadline_scope(clock, clock.monotonic() + 60) as scope:
        assert len(asyncio.all_tasks()) == before
        assert isinstance(scope, anyio.CancelScope)
        assert scope.deadline == pytest.approx(anyio.current_time() + 60, abs=0.5)
        await anyio.lowlevel.checkpoint()
    assert not scope.cancelled_caught
    assert len(asyncio.all_tasks()) == before


async def test_the_loop_deadline_fires_at_at_not_before() -> None:
    clock = SystemClock()
    t0 = time.monotonic()
    async with Deadline(t0 + 0.03, clock).scope() as scope:
        await anyio.sleep(5)
    elapsed = time.monotonic() - t0
    assert scope.cancelled_caught
    assert 0.029 <= elapsed < 1.0  # the loop clock's resolution is 1 ms (uvloop); the stdlib loop's is exact


async def test_a_scope_that_ended_leaves_no_timer_behind() -> None:
    clock = SystemClock()
    async with deadline_scope(clock, clock.monotonic() + 0.01) as scope:
        await anyio.lowlevel.checkpoint()
    with anyio.CancelScope() as later:
        await anyio.sleep(0.03)  # past the scope's deadline: nothing cancels this task
    assert not scope.cancelled_caught and not later.cancelled_caught


async def _scenario(variant: Scope, name: str) -> tuple[object, ...]:
    """One scenario through one variant: (scope cancelled_caught, outer cancelled_caught, exception, progress)."""
    clock = SystemClock()
    now = clock.monotonic()
    progress: list[str] = []
    error: str | None = None
    inner: anyio.CancelScope | None = None
    with anyio.CancelScope() as outer:
        try:
            if name == "expired":
                async with variant(clock, now - 1) as inner:
                    progress.append("entered")
                    await anyio.lowlevel.checkpoint()
                    progress.append("after checkpoint")
            elif name == "fires":
                async with variant(clock, now + 0.02) as inner:
                    await anyio.sleep(5)
                    progress.append("slept through")
            elif name == "body_error":
                async with variant(clock, now + 60) as inner:
                    raise KeyError("body")
            elif name == "error_after_expiry":
                async with variant(clock, now + 0.01) as inner:
                    time.sleep(0.02)  # noqa: ASYNC251 (expired without a checkpoint, then the body fails)
                    raise KeyError("body")
            elif name == "outer_cancel":
                outer.cancel()
                async with variant(clock, now + 60) as inner:
                    await anyio.sleep(5)
                    progress.append("slept through")
            elif name == "outer_deadline_first":
                outer.deadline = anyio.current_time() + 0.01
                async with variant(clock, now + 60) as inner:
                    await anyio.sleep(5)
            elif name == "explicit_cancel":
                async with variant(clock, now + 60) as inner:
                    inner.cancel()
                    await anyio.lowlevel.checkpoint()
                    progress.append("after checkpoint")
            elif name == "shielded_body":
                async with variant(clock, now + 0.01) as inner:
                    with anyio.CancelScope(shield=True):
                        await anyio.sleep(0.03)
                        progress.append("shielded done")
                    await anyio.lowlevel.checkpoint()
                    progress.append("after shield")
            else:
                async with variant(clock, now + 60) as inner:
                    await anyio.lowlevel.checkpoint()
                    progress.append("done")
        except KeyError as exc:
            error = f"KeyError {exc}"
    assert inner is not None
    return inner.cancelled_caught, outer.cancelled_caught, error, tuple(progress)


@pytest.mark.parametrize(
    "name",
    [
        "expired",
        "fires",
        "body_error",
        "error_after_expiry",
        "outer_cancel",
        "outer_deadline_first",
        "explicit_cancel",
        "shielded_body",
        "plain",
    ],
)
async def test_the_loop_deadline_has_the_watcher_variants_outcomes(name: str) -> None:
    outcomes = {key: await _scenario(variant, name) for key, variant in VARIANTS.items()}
    assert outcomes["loop"] == outcomes["watcher"], outcomes
