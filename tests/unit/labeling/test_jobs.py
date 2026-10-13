"""`make_handlers` (spec-01 §1, spec-11 §4): the three labeling job kinds behind `--kinds labeling.*`."""

from __future__ import annotations

from typing import Any, cast

import pytest
from tests.fakes.world import FakeWorld, build_world
from tests.unit.labeling.jobkit import CounterRedis

from noema.core.protocols.services import ServiceSet, expand_kinds, merge_handlers
from noema.labeling import DRIFT_PERIOD_S, make_handlers
from noema.labeling.drift import DriftJob
from noema.labeling.items import ItemJob
from noema.labeling.runs import RunJob

pytestmark = pytest.mark.unit


class WithRedis:
    """A fake world plus the process Redis client (`ServiceSet.redis`; the world has no Redis fake)."""

    def __init__(self, world: FakeWorld) -> None:
        self.world = world
        self.redis: Any = CounterRedis()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.world, name)


def test_handlers_cover_the_labeling_kinds() -> None:
    services = WithRedis(build_world())
    handlers = make_handlers(cast(ServiceSet, services))
    assert expand_kinds(["labeling.*"], merge_handlers(handlers)) == ("labeling.drift", "labeling.item", "labeling.run")
    assert isinstance(handlers["labeling.item"], ItemJob)
    assert isinstance(handlers["labeling.run"], RunJob)
    assert isinstance(handlers["labeling.drift"], DriftJob)
    assert DRIFT_PERIOD_S == 604_800
    item = handlers["labeling.item"]
    assert isinstance(item, ItemJob)
    assert item._d.redis is services.redis and item._d.keys is services.world.keys  # the exact daily cap
