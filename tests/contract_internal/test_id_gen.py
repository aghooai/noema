"""IdGen conformance (spec-02 §1): `f"{prefix}_{ULID}"`, 26 Crockford characters, unique and monotonic."""

from __future__ import annotations

import re
from typing import get_args

from noema.core.ids import IdPrefix

from .kits import IdGenKit
from .registry import kit_fixture

kit = kit_fixture("id_gen")
ULID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


async def test_prefix_and_shape(kit: IdGenKit) -> None:
    for prefix in get_args(IdPrefix):
        value = kit.ids.new(prefix)
        head, _, tail = value.partition("_")
        assert head == prefix and ULID.fullmatch(tail), value


async def test_unique_and_monotonic_within_one_millisecond(kit: IdGenKit) -> None:
    ids = [kit.ids.new("turn") for _ in range(1000)]
    assert len(set(ids)) == len(ids)
    assert ids == sorted(ids)


async def test_monotonic_across_time(kit: IdGenKit) -> None:
    first = kit.ids.new("dec")
    await kit.clock.advance(1.0)
    second = kit.ids.new("dec")
    assert second > first
    assert second[4:14] > first[4:14]  # the 48-bit time part moved
