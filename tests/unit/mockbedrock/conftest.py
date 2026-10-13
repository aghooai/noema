"""Fixtures: an in-process wire mock on 127.0.0.1:0 driven by a `VirtualClock` (planned latencies are asserted
exactly and never waited for), and real clients pointed at it."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from tests.unit.mockbedrock.helpers import bedrock_client
from tools.mockbedrock import MockBedrock, VirtualClock
from types_aiobotocore_bedrock_runtime import BedrockRuntimeClient


@pytest.fixture
def clock() -> VirtualClock:
    return VirtualClock()


@pytest.fixture
async def mock(clock: VirtualClock) -> AsyncIterator[MockBedrock]:
    async with MockBedrock(clock=clock) as server:
        yield server


@pytest.fixture
async def bedrock(mock: MockBedrock) -> AsyncIterator[BedrockRuntimeClient]:
    async with bedrock_client(mock.endpoint_url) as client:
        yield client


@pytest.fixture
async def http(mock: MockBedrock) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=mock.endpoint_url, timeout=5.0) as client:
        yield client
