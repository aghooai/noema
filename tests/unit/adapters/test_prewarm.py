"""Client creation keeps blocking work off the event loop (adapters review, performance): Converse parses botocore's
service model, endpoint rules and partitions once, in a worker thread, into the loader and the lean path's model,
and builds one TLS context in a worker thread that every pooled transport shares; Mantle builds one TLS context (CA
bundle) in a worker thread and every pooled client shares it. `prewarm()` is idempotent and is what the first
client calls."""

from __future__ import annotations

import threading
from typing import Any, cast

import httpx
import pytest
from tests.unit.adapters.support import rig

import noema.adapters._converse_wire as wire_module
import noema.adapters._unary as unary_module
import noema.adapters.converse as converse_module
from noema.adapters.converse import ConverseAdapter
from noema.adapters.mantle import MantleAdapter


def on_worker(calls: list[str], name: str, fn: Any) -> Any:
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        calls.append(f"{name}:{'main' if threading.current_thread() is threading.main_thread() else 'worker'}")
        return fn(*args, **kwargs)

    return wrapped


async def test_converse_parses_botocore_data_once_in_a_worker_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    # E sprint A: no aiobotocore session per client any more; the loader, the lean path's ConverseModel and the TLS
    # context are each built once, off the event loop, and shared by every pooled transport
    calls: list[str] = []
    monkeypatch.setattr(
        converse_module, "_loaded_data_loader", on_worker(calls, "loader", converse_module._loaded_data_loader)
    )
    monkeypatch.setattr(converse_module, "ConverseModel", on_worker(calls, "model", wire_module.ConverseModel))
    monkeypatch.setattr(converse_module, "tls_context", on_worker(calls, "tls", unary_module.tls_context))
    async with rig(two_tenants=True) as r:
        adapter = ConverseAdapter(config=r.config, broker=r.broker, clock=r.clock, retry_budget=r.budget)
        try:
            await adapter.prewarm()
            await adapter.prewarm()
            contexts = []
            for tenant in ("t_acme", "t_beta"):
                transport, close = await adapter._make_client(await r.broker.get(tenant))
                contexts.append(cast(Any, transport)._session.connector._ssl)
                await close()
            loader, wire = adapter._loader, adapter._wire
        finally:
            await adapter.aclose()
    assert calls == ["loader:worker", "model:worker", "tls:worker"]
    assert loader is not None and wire is not None and wire.endpoint("us-east-1").endswith(".amazonaws.com")
    assert loader.load_service_model("bedrock-runtime", "service-2")["metadata"]["serviceId"] == "Bedrock Runtime"
    assert contexts[0] is contexts[1] is adapter._tls


async def test_mantle_clients_share_one_tls_context_built_in_a_worker_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(httpx, "create_ssl_context", on_worker(calls, "tls", httpx.create_ssl_context))
    async with rig() as r:
        adapter = MantleAdapter(config=r.config, broker=r.broker, clock=r.clock, retry_budget=r.budget)
        creds = await r.broker.get("t_acme")
        try:
            await adapter.prewarm()
            first, close_first = await adapter._make_client(creds)
            second, close_second = await adapter._make_client(creds)
            contexts = [cast(Any, c._transport)._pool._ssl_context for c in (first, second)]
            await close_first()
            await close_second()
        finally:
            await adapter.aclose()
        await r.mantle.prewarm()  # an injected transport never builds a TLS context
        injected = r.mantle._ssl
    assert calls == ["tls:worker"]
    assert contexts[0] is contexts[1] is adapter._ssl and injected is None
