"""The turn's one watcher task (E3; four watcher tasks before): cancel/abandon, shutdown, the deadline and the
session-lease renewal. Renewals stay due RENEW_EVERY_S apart, and one in flight (a Postgres round trip, possibly a
stalled one) never delays a stop trigger."""

from __future__ import annotations

from typing import Any

import pytest
from tests.golden.sse_scenarios import document, router_rules, stream

from noema.turns import session_order
from noema.turns.runner import TurnRunner

from .harness import STICKY_SOL, TurnHarness, first, make_harness, run_turn, running, types_of

pytestmark = pytest.mark.unit
SOL = "us.openai.gpt-6.1-sol"
OLD_WATCHERS = ("_cancel_watch", "_deadline_watch", "_shutdown_watch", "_renew_watch")


def _h() -> TurnHarness:
    sol = stream(SOL, first_event_ms=100, first_text_ms=300, chunks=["a "] * 12, step_ms=2000)  # a ~24 s answer
    h = make_harness(scenario=document("watch", [sol], router_rules()))
    h.seed_session("ses_1", last_seq=3, sticky_spec=STICKY_SOL)
    return h


def _record_watchers(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The names of the watcher tasks a turn starts (every one runs under `_guarded`)."""
    names: list[str] = []
    guarded = TurnRunner._guarded

    async def recording(self: TurnRunner, watch: Any) -> None:
        names.append(watch.__name__)
        await guarded(self, watch)

    monkeypatch.setattr(TurnRunner, "_guarded", recording)
    return names


async def test_one_watcher_task_renews_the_lease_every_renew_every_s(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _h()
    real = session_order.renew
    renewed_at: list[float] = []

    async def renew(*args: Any) -> bool:
        renewed_at.append(h.clock.monotonic())
        return await real(*args)

    monkeypatch.setattr(session_order, "renew", renew)
    watchers = _record_watchers(monkeypatch)
    async with running(h):
        runner = await run_turn(h)
    assert watchers == ["_stop_watch", "_renew", "_renew"]  # one long-lived watcher; a short task per renewal
    assert not set(watchers) & set(OLD_WATCHERS)
    t_in = runner.p.t_in
    assert [round(t - t_in, 3) for t in renewed_at] == [10.0, 20.0]
    assert runner.result is not None and runner.result.status == "completed" and not runner.lease_lost


async def test_a_cancel_during_a_stalled_renewal_is_handled_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _h()
    real = session_order.renew

    async def stalled(*args: Any) -> bool:
        await h.clock.sleep(6.0)  # a slow Postgres
        return await real(*args)

    monkeypatch.setattr(session_order, "renew", stalled)
    asked: list[float] = []

    async def cancel(runner: Any) -> None:
        await h.clock.sleep(runner.p.t_in + 11.0 - h.clock.monotonic())  # 1 s into the renewal
        asked.append(h.clock.monotonic())
        runner.request_cancel("cancelled")

    async with running(h):
        runner = await run_turn(h, during=cancel)
    at, frame = first(h, runner, "generation.cancelled")
    assert frame["reason"] == "client_abort"
    t_d0 = runner.t_d0
    assert t_d0 is not None and t_d0 + at / 1000 == pytest.approx(asked[0])  # not after the renewal returned
    assert "generation.completed" not in types_of(h, runner)
    assert runner.result is not None and runner.result.status == "cancelled"
