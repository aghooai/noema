"""Shared fixtures of the Python SDK tests (respx-mocked httpx; nothing leaves the process).

Run from the repo root with the root venv: `UV_NO_SYNC=1 uv run --frozen --no-sync pytest sdk/python -q`.
"""

from __future__ import annotations

import inspect
import os
from collections.abc import AsyncIterator, Iterator

import pytest
import respx

from aghoo import Aghoo, AsyncAghoo, _retry
from support import API_KEY, BASE


def _isolate_environment() -> None:
    for name in list(os.environ):
        if name.startswith(("AWS_", "AGHOO_")):
            del os.environ[name]
    os.environ["AWS_CONFIG_FILE"] = os.devnull
    os.environ["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull


_isolate_environment()


@pytest.hookimpl(tryfirst=True)
def pytest_pycollect_makeitem(collector: pytest.Collector, name: str, obj: object) -> None:
    """Every `async def` test runs on anyio (asyncio backend) without a marker."""
    if collector.istestfunction(obj, name) and inspect.iscoroutinefunction(obj):  # type: ignore[attr-defined]
        pytest.mark.anyio(obj)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("NOEMA_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="live test: set NOEMA_LIVE=1")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=False, assert_all_mocked=True) as router:
        yield router


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Retry sleeps are recorded instead of slept; full jitter is pinned at random() = 0.5."""
    recorded: list[float] = []

    def sleep(s: float, stop: object) -> None:
        recorded.append(s)

    async def async_sleep(s: float, stop: object) -> None:
        recorded.append(s)

    monkeypatch.setattr(_retry, "sleep", sleep)
    monkeypatch.setattr(_retry, "async_sleep", async_sleep)
    monkeypatch.setattr(_retry, "_random", lambda: 0.5)
    return recorded


@pytest.fixture
def client() -> Iterator[Aghoo]:
    with Aghoo(api_key=API_KEY, base_url=BASE) as c:
        yield c


@pytest.fixture
async def aclient() -> AsyncIterator[AsyncAghoo]:
    async with AsyncAghoo(api_key=API_KEY, base_url=BASE) as c:
        yield c
