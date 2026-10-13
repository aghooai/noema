"""Fixtures of the fault suite (tests/fault, spec-10 §4; `make qa-fault`) on the fault-routed compose stack.

The stack is `make e2e-up` with the app containers reaching postgres, valkey and moto through toxiproxy (see
make/qa-fault.mk). Every test runs only with NOEMA_FAULT=1 (the make target), so a plain `pytest` never drives
another agent's e2e stack. Session: the fault config (tenant limits lifted, spec-05 §1.2) active on both replicas.
Each test starts with every proxy enabled and no toxic, the fault suite's default scenario loaded and every service
circuit closed on both replicas (the breakers outlive a test and are shared through breaker_events).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import orjson
import pytest
from tests.e2e.conftest import Mock, Stack, mock, stack
from tests.fault import kit
from tests.fault.stackctl import Toxiproxy, ensure_fault_config, heal

__all__ = ["base_version", "fault_test", "mock", "stack", "toxi"]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("NOEMA_FAULT") == "1":
        return
    skip = pytest.mark.skip(reason="fault suite: run it with `make qa-fault` (NOEMA_FAULT=1, a fault-routed stack)")
    for item in items:
        if Path(str(item.fspath)).is_relative_to(Path(__file__).parent):
            item.add_marker(skip)


DEFAULT = kit.scenario("fault-default", [kit.gen(kit.SOL), kit.gen(kit.OPUS), kit.gen(kit.GLM)], kit.members())


@pytest.fixture(scope="session")
async def base_version(stack: Stack) -> int:
    """The fault config version (activated once per session, every proxy enabled first)."""
    async with Toxiproxy(str(stack.state["endpoints"]["toxiproxy"])) as toxi:
        await toxi.reset()
    async with httpx.AsyncClient(base_url=stack.mock_url, timeout=30.0) as http:
        await Mock(http).load(DEFAULT)
    return await ensure_fault_config(stack)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """The measured numbers of every fault row that ran (NOEMA_FAULT_RESULTS, merged into an existing file)."""
    target = os.environ.get("NOEMA_FAULT_RESULTS")
    if not target or not kit.RESULTS:
        return
    path = Path(target)
    merged = orjson.loads(path.read_bytes()) if path.exists() else {}
    merged.update(kit.RESULTS)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(orjson.dumps(merged, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))


@pytest.fixture
async def toxi(stack: Stack) -> AsyncIterator[Toxiproxy]:
    async with Toxiproxy(str(stack.state["endpoints"]["toxiproxy"])) as handle:
        yield handle


@pytest.fixture(autouse=True)
async def fault_test(stack: Stack, mock: Mock, toxi: Toxiproxy, base_version: int) -> AsyncIterator[None]:
    """Every proxy enabled, the default scenario, every circuit closed; the proxies restored afterwards too."""
    await toxi.reset()
    await mock.load(DEFAULT)
    await heal(stack)
    yield
    await toxi.reset()
