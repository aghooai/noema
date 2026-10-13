"""Every failure of `complete()` and `stream().__aenter__` is an `AdapterError` (spec-02 §4.1; adapters review):
unexpected broker exceptions, a call racing `aclose()`, a client that cannot be created, and the deadline expiring
while the pooled client is being created. Both adapters, both call shapes; nothing reaches the wire."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from tests.unit.adapters.support import DEEPSEEK, SOL, TIMEOUTS, Rig, request, rig

from noema.core.errors import ErrorKind, SecretMissing
from noema.core.protocols.adapters import AdapterError

CASES = [(alias, stream) for alias in ("sol-6.1", "deepseek-3.2") for stream in (False, True)]
MODEL = {"sol-6.1": SOL, "deepseek-3.2": DEEPSEEK}


async def call(r: Rig, alias: str, *, stream: bool, deadline_s: float = 60.0) -> None:
    adapter = r.registry.for_model(alias)
    async with r.clock.autojump():
        if not stream:
            await adapter.complete(request(alias), tenant=r.tenant, deadline=r.deadline(deadline_s))
            return
        cm = adapter.stream(request(alias), tenant=r.tenant, timeouts=TIMEOUTS, deadline=r.deadline(deadline_s))
        async with cm as events:
            async for _ in events:
                pass


def pool(adapter: Any, stream: bool) -> Any:
    """The client pool of the call shape: Mantle has the streams' httpx clients and the unary (`complete`)
    transports; Converse one pool of lean transports for both."""
    return adapter._unary if not stream and hasattr(adapter, "_unary") else adapter._pool


def failing_broker(exc: Exception) -> Callable[[str], Awaitable[Any]]:
    async def get(tenant_id: str) -> Any:
        raise exc

    return get


@pytest.mark.parametrize(("alias", "stream"), CASES)
@pytest.mark.parametrize(
    ("exc", "kind", "code"),
    [
        (ConnectionError("broker backend down"), ErrorKind.RETRYABLE, "ConnectionError"),
        (SecretMissing(), ErrorKind.FATAL, "SecretMissing"),  # e.g. the tenant's external-id secret is gone
        (RuntimeError("bug"), ErrorKind.FATAL, "RuntimeError"),
    ],
)
async def test_unexpected_broker_errors_are_classified(
    alias: str, stream: bool, exc: Exception, kind: ErrorKind, code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with rig() as r:
        r.scenario({"match": {}, "answer": {"raw_text": "ok"}, "stream": {"chunks": ["ok"]}})
        monkeypatch.setattr(r.broker, "get", failing_broker(exc))
        with pytest.raises(AdapterError) as err:
            await call(r, alias, stream=stream)
        assert r.emulator.requests == []
    assert err.value.error.kind is kind and err.value.error.provider_code == code
    assert "broker backend down" not in err.value.error.message


@pytest.mark.parametrize(("alias", "stream"), CASES)
async def test_a_call_after_aclose_is_a_retryable_adapter_error(alias: str, stream: bool) -> None:
    async with rig() as r:
        r.scenario({"match": {}, "answer": {"raw_text": "ok"}, "stream": {"chunks": ["ok"]}})
        await r.converse.aclose()
        await r.mantle.aclose()
        with pytest.raises(AdapterError) as err:
            await call(r, alias, stream=stream)
        assert r.emulator.requests == []
    assert err.value.error.kind is ErrorKind.RETRYABLE and err.value.error.provider_code == "adapter_closed"


@pytest.mark.parametrize(("alias", "stream"), CASES)
async def test_a_client_that_cannot_be_created_is_an_adapter_error(alias: str, stream: bool) -> None:
    async with rig() as r:
        adapter: Any = r.registry.for_model(alias)

        async def broken(_: Any) -> Any:
            raise ValueError("unknown service")

        pool(adapter, stream).factory = broken
        with pytest.raises(AdapterError) as err:
            await call(r, alias, stream=stream)
        assert adapter.open_streams == 0
    assert err.value.error.kind is ErrorKind.FATAL and err.value.error.provider_code == "ValueError"


@pytest.mark.parametrize(("alias", "stream"), CASES)
async def test_the_deadline_bounds_client_creation(alias: str, stream: bool) -> None:
    async with rig() as r:
        r.scenario({"match": {"model": MODEL[alias]}, "answer": {"raw_text": "ok"}, "stream": {"chunks": ["ok"]}})
        adapter: Any = r.registry.for_model(alias)
        make = pool(adapter, stream).factory

        async def slow(creds: Any) -> Any:
            await r.clock.sleep(10.0)  # e.g. a cold client on a stalled host
            return await make(creds)

        pool(adapter, stream).factory = slow
        t0 = r.clock.monotonic()
        with pytest.raises(AdapterError) as err:
            await call(r, alias, stream=stream, deadline_s=2.0)
        assert r.clock.monotonic() - t0 == pytest.approx(2.0)  # the deadline (streams: connect_s 3 capped by it)
        assert r.emulator.requests == [] and adapter.open_streams == 0
    assert err.value.error.kind is ErrorKind.RETRYABLE and err.value.error.provider_code == "timeout:deadline"
