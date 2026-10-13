"""`start()` freezes the startup heap outside test processes (C2b perf smoke: a full GC of the api scanned every
startup object, 110-125 ms per collection, each one a loop stall; reports/perf/c2b-perf-smoke.md)."""

from __future__ import annotations

import gc
from typing import Final

import pytest

import noema.composition as composition
from noema.composition import SERVICE_FIELDS, build, start, stop
from noema.core.settings import Env

pytestmark = [pytest.mark.unit]
ALL_FAKES: Final = frozenset(SERVICE_FIELDS) - {"redis", "runtime"}


@pytest.mark.parametrize(("env", "frozen"), [("dev", 1), ("test", 0)])
async def test_start_freezes_the_startup_heap_except_in_test(
    monkeypatch: pytest.MonkeyPatch, env: Env, frozen: int
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(composition, "_freeze_startup_heap", lambda: calls.append("freeze"))
    services = build(env, fakes=ALL_FAKES, environ={})
    await start(services, "jobs")
    try:
        assert len(calls) == frozen
    finally:
        await stop(services)


def test_the_freeze_collects_first_then_moves_the_survivors_to_the_permanent_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def collect(*_: object) -> int:
        calls.append("collect")
        return 0

    monkeypatch.setattr(gc, "collect", collect)
    monkeypatch.setattr(gc, "freeze", lambda: calls.append("freeze"))
    monkeypatch.setattr(gc, "get_threshold", lambda: composition.PYTHON_GC_THRESHOLDS)
    monkeypatch.setattr(gc, "set_threshold", lambda *t: calls.append(f"threshold{t}"))
    composition._freeze_startup_heap()
    assert calls == ["collect", "freeze", "threshold(700, 10, 100)"]


def test_thresholds_changed_before_startup_are_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, ...]] = []
    monkeypatch.setattr(gc, "collect", lambda *_: 0)
    monkeypatch.setattr(gc, "freeze", lambda: None)
    monkeypatch.setattr(gc, "get_threshold", lambda: (2000, 10, 10))  # e.g. tests/load/serve.py --gc-threshold
    monkeypatch.setattr(gc, "set_threshold", lambda *t: calls.append(t))
    composition._freeze_startup_heap()
    assert calls == []
