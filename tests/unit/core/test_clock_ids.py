"""Clock, Deadline and IdGen (spec-02 §1)."""

from __future__ import annotations

import re
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import anyio
import anyio.lowlevel
import pytest

from noema.core.clock import Clock, Deadline, SystemClock, deadline_scope
from noema.core.ids import CROCKFORD32, IdGen, UlidIdGen, encode_ulid

pytestmark = pytest.mark.unit


class StepClock:
    """A minimal manual clock for these tests (the shared FakeClock lives in tests/fakes)."""

    def __init__(self, mono: float = 100.0, wall: datetime | None = None) -> None:
        self.mono = mono
        self.wall = wall or datetime(2026, 10, 10, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    async def sleep(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += timedelta(seconds=seconds)


def test_deadline_remaining_and_expiry() -> None:
    clock = StepClock()
    d = Deadline(at=clock.monotonic() + 1.5, clock=clock)
    assert d.remaining_ms() == 1500
    assert not d.expired()
    clock.mono += 1.4999
    assert d.remaining_ms() == 0  # floor
    clock.mono += 0.0001
    assert d.expired()
    clock.mono += 10
    assert d.remaining_ms() == 0


def test_deadline_child_and_earliest() -> None:
    clock = StepClock()
    d = Deadline(at=clock.monotonic() + 2, clock=clock)
    assert d.child(500).at == pytest.approx(clock.monotonic() + 0.5)
    assert d.child(5000).at == d.at  # capped by the parent
    other = Deadline(at=d.at - 1, clock=clock)
    assert d.earliest(other) is other
    assert other.earliest(d) is other
    assert d.earliest(None) is d
    with pytest.raises(FrozenInstanceError):
        d.at = 0  # type: ignore[misc]


async def test_deadline_scope_expires_on_the_event_loop() -> None:
    clock = SystemClock()
    async with Deadline(at=clock.monotonic() + 0.01, clock=clock).scope() as scope:
        await anyio.sleep(5)
    assert scope.cancelled_caught


async def test_deadline_scope_of_an_expired_deadline_cancels_at_once() -> None:
    clock = StepClock()
    async with Deadline(at=clock.monotonic() - 1, clock=clock).scope() as scope:
        await anyio.sleep(5)
    assert scope.cancelled_caught


async def test_deadline_scope_passes_body_errors_through_unwrapped() -> None:
    clock = SystemClock()
    with pytest.raises(KeyError):
        async with deadline_scope(clock, clock.monotonic() + 60):
            raise KeyError("body")
    async with deadline_scope(clock, clock.monotonic() + 60) as scope:
        await anyio.lowlevel.checkpoint()
    assert not scope.cancelled_caught


async def test_system_clock() -> None:
    clock: Clock = SystemClock()
    assert clock.now().tzinfo is UTC
    before = clock.monotonic()
    await clock.sleep(0)
    assert clock.monotonic() >= before


def test_ulid_format_and_prefix() -> None:
    gen: IdGen = UlidIdGen(StepClock())
    value = gen.new("turn")
    assert re.fullmatch(r"turn_[0-9A-HJKMNP-TV-Z]{26}", value)
    assert value[5] in "01234567"  # 128 bits: the first character carries 3 bits


def test_ulid_encodes_the_millisecond_timestamp() -> None:
    clock = StepClock(wall=datetime(2026, 10, 10, tzinfo=UTC))
    gen = UlidIdGen(clock, randbytes=lambda n: b"\x00" * n)
    ms = int(clock.wall.timestamp() * 1000)
    assert gen.ulid() == encode_ulid(ms << 80)


def test_ulids_are_monotonic_within_one_millisecond_and_across_clock_steps_back() -> None:
    clock = StepClock()
    gen = UlidIdGen(clock, randbytes=lambda n: b"\xff" * (n - 1) + b"\xfe")
    a, b = gen.ulid(), gen.ulid()  # same millisecond: the random part is incremented
    clock.wall -= timedelta(seconds=5)  # wall clock steps back; the random part overflows to the next ms
    c = gen.ulid()
    assert a < b < c
    ids = [gen.new("dec") for _ in range(1000)]
    assert ids == sorted(ids)
    assert len(set(ids)) == 1000


def test_encode_ulid_bounds() -> None:
    assert encode_ulid(0) == "0" * 26
    assert encode_ulid((1 << 128) - 1) == "7" + "Z" * 25
    assert set(encode_ulid(123456789)) <= set(CROCKFORD32)
    with pytest.raises(ValueError, match="128-bit"):
        encode_ulid(1 << 128)
