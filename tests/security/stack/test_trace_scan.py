"""SE12, the traces part: the compose processes export no spans (composition passes no OTLP endpoint), so the span
scan runs on an in-process api built by the REAL composition (`NOEMA_ENV=test`: spans go to the in-memory exporter)
against the security stack's Postgres, Valkey, moto and wire mock (the bootstrap's host environment). It serves a
streamed and a buffered turn, a decision, the facade, an upload and 401s with forged and real-id keys; then every
finished span (name, attributes, events, status) holds 0 spec-09 §4 pattern matches and 0 known secret values. Every
response is contract-validated (`CheckedClient` over the ASGI transport)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import anyio
import httpx
import orjson
import pytest
from asgi_lifespan import LifespanManager
from tests.e2e.conftest import AGHOO_VERSION, CheckedClient, Stack
from tests.e2e.suite.kit import tag
from tests.security.artifacts import write_jsonl
from tests.security.secret_patterns import scan
from tests.security.stack.capture import moto_secrets, secret_values
from tests.security.stack.world import decision_body, text_turn
from tests.unit.media_pipeline import media_fixtures as fx

from noema import composition
from noema.api.app import create_app
from noema.api.middleware.routing import OperationTable
from noema.contracts.bundle import default_bundle
from noema.contracts.validate import ContractValidator
from noema.core.values import JsonValue

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(240)]


@pytest.fixture
async def inproc(stack: Stack, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[Any, httpx.AsyncClient]]:
    for name, value in dict(stack.state["host_env"]).items():
        monkeypatch.setenv(name, str(value))
    monkeypatch.setenv("NOEMA_ENV", "test")
    services = composition.build("test")
    await composition.start(services, "api")
    try:
        async with LifespanManager(create_app(services=services)) as manager:
            transport = httpx.ASGITransport(app=manager.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://noema.test", timeout=60) as http:
                for _ in range(150):  # the config loads in start(); the watcher retries every 5 s
                    if (await http.get("/readyz")).status_code == 200:
                        break
                    await anyio.sleep(0.2)
                else:
                    pytest.fail("the in-process api is not ready after 30 s")
                yield services, http
    finally:
        await composition.stop(services)


def span_text(span: Any) -> str:
    doc = {
        "name": span.name,
        "attributes": dict(span.attributes or {}),
        "events": [{"name": e.name, "attributes": dict(e.attributes or {})} for e in span.events],
        "status": span.status.description,
    }
    return orjson.dumps(doc, default=str).decode()


async def test_spans_hold_no_secret(stack: Stack, inproc: tuple[Any, httpx.AsyncClient]) -> None:
    services, http = inproc
    key = str(stack.principal("t_acme", "main", "developer")["key"])
    http.headers.update({"authorization": f"Bearer {key}", "aghoo-version": AGHOO_VERSION})
    api = CheckedClient(http, ContractValidator(), OperationTable(default_bundle().operations()))
    await api.request("POST", "/v1/turns", json=text_turn(), expect=200)
    await api.sse("/v1/turns", json={**text_turn(), "stream": True})
    await api.request("POST", "/v1/decisions", json=decision_body(), expect=200)
    facade: JsonValue = {"model": "aghoo/router-default", "input": [{"role": "user", "type": "message",
                         "content": tag()}], "questions": [{"type": "predicate", "name": "x",
                         "instructions": "Is it a question?"}]}  # fmt: skip
    await api.request("POST", "/openai/v1/decisions", json=facade, expect=200)
    files = {"file": ("trace.png", fx.png(16, 16), "image/png")}
    await api.request("POST", "/v1/files", data={"purpose": "input"}, files=files, expect=202)
    key_id = key.removeprefix("ak_test_").split(".")[0]
    for bad in (f"ak_test_{key_id}.{'x' * 43}", key.replace("ak_test_", "ak_live_"), "Basic dXNlcjpwYXNz"):
        await api.request("GET", "/v1/profiles", headers={"authorization": f"Bearer {bad}"}, expect=401)
    spans = services.telemetry.setup.spans.get_finished_spans()
    values = secret_values(stack, await moto_secrets(stack), {})
    findings = [f for span in spans for f in scan(f"span:{span.name}", span_text(span), values)]
    rows: list[JsonValue] = [{"span": f.source, "kind": f.kind, "excerpt": f.excerpt} for f in findings]
    names: list[JsonValue] = [*sorted({str(s.name) for s in spans})]
    write_jsonl("trace-findings.jsonl", [*rows, {"spans": len(spans), "names": names}])
    assert len(spans) >= 20 and "decision" in names and "generation.attempt" in names, names
    assert rows == []
