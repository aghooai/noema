"""The real adapters over real sockets against the qa-tools wire mock (`tools/mockbedrock`, spec-10 §2), which was
written independently from spec-10 §2.1a: wire parsing (Converse JSON, eventstream CRCs, Mantle JSON/SSE),
classification of its error responses, SigV4 with the broker's key (SEC-STS-15 on the wire), exception events,
reasoning-only stops and malformed frames. Wall clock, zero scenario latency. Skips while the mock is absent."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any

import pytest
from tests.fakes.config import FakeConfigRegistry, patched_example, snapshot_from
from tests.fakes.credentials import FakeBroker
from tests.unit.adapters.support import DEEPSEEK, GLM, HAIKU, SOL, TIMEOUTS

from noema.adapters.converse import ConverseAdapter
from noema.adapters.mantle import MantleAdapter
from noema.adapters.registry import AdapterRegistryImpl
from noema.adapters.resilience.retry_budget import SlidingRetryBudget
from noema.core.clock import Deadline, SystemClock
from noema.core.errors import ErrorKind
from noema.core.principal import TenantRef
from noema.core.protocols.adapters import (
    AdapterError,
    ErrorEvent,
    MessageStart,
    ModelRequest,
    Stop,
    StreamEvent,
    StreamTimeouts,
    TextDelta,
    UsageEvent,
)
from noema.core.values import Message, TextBlock

mockbedrock = pytest.importorskip("tools.mockbedrock")
pytestmark = pytest.mark.integration
AQ = (
    '<app_question>{"instructions": "Intent?", "options": [{"letter": "A", "value": "chitchat", "description": null}, '
    '{"letter": "B", "value": "coding", "description": null}, {"letter": "C", "value": "factual_qa", '
    '"description": null}]}</app_question>\n'
)


@dataclass
class Wired:
    mock: Any
    registry: AdapterRegistryImpl
    broker: FakeBroker
    tenant: TenantRef
    clock: SystemClock
    budget: SlidingRetryBudget

    def use(self, *rules: dict[str, Any]) -> None:
        doc = {"scenario": "adapters-vs-mock", "seed": 1, "mantle_auth": "sigv4", "rules": list(rules)}
        self.mock.load_scenario({**doc, "defaults": {"latency": {"fixed_ms": 0}}})

    def deadline(self, s: float = 20.0) -> Deadline:
        return Deadline(self.clock.monotonic() + s, self.clock)


@asynccontextmanager
async def wired(config: FakeConfigRegistry | None = None) -> AsyncIterator[Wired]:
    clock = SystemClock()
    config = config or FakeConfigRegistry()
    broker = FakeBroker(clock, config, mantle_mode="sigv4")
    budget = SlidingRetryBudget(clock)
    async with mockbedrock.MockBedrock() as mock:
        overrides = {"bedrock-runtime": mock.endpoint_url, "mantle": mock.mantle_url}
        common: dict[str, Any] = {
            "config": config,
            "broker": broker,
            "clock": clock,
            "retry_budget": budget,
            "endpoint_overrides": overrides,
        }
        converse, mantle = ConverseAdapter(**common), MantleAdapter(**common)
        registry = AdapterRegistryImpl(config, {"bedrock-converse": converse, "bedrock-mantle-chat": mantle})
        try:
            yield Wired(mock, registry, broker, config.current().tenant_ref("t_acme"), clock, budget)
        finally:
            await converse.aclose()
            await mantle.aclose()


def req(alias: str, text: str = "Hi", **kw: Any) -> ModelRequest:
    kind = kw.pop("kind", "generation")
    return ModelRequest(alias, ("You classify.",), (Message("user", (TextBlock(text),)),), 64, kind, "none", **kw)


async def stream(w: Wired, alias: str, request: ModelRequest | None = None) -> list[StreamEvent]:
    adapter = w.registry.for_model(alias)
    cm = adapter.stream(request or req(alias), tenant=w.tenant, timeouts=TIMEOUTS, deadline=w.deadline())
    async with cm as events:
        return [ev async for ev in events]


async def test_converse_complete_and_sigv4_with_the_broker_key() -> None:
    async with wired() as w:
        w.use({"match": {"model": HAIKU}, "answer": {"dist": {"chitchat": 0.1, "coding": 0.7, "factual_qa": 0.2}}})
        resp = await w.registry.for_model("haiku-5.5").complete(
            req("haiku-5.5", AQ, kind="decision_member", effort="low"), tenant=w.tenant, deadline=w.deadline()
        )
        akid = (await w.broker.get("t_acme")).aws.access_key_id
        records = w.mock.requests()
    assert '"B"' in resp.text and resp.stop_reason == "end_turn" and resp.usage.input_tokens > 0
    assert records[-1]["akid"] == akid and records[-1]["auth"]["scheme"].lower().startswith("sigv4")


async def test_a_versioned_model_id_is_sent_as_signed() -> None:
    """Wave E: a model id with ':' (every `-v1:0` id, every ARN) reaches the mock percent-encoded as botocore signs
    it; the lean path re-quoted it to ':' (403 InvalidSignatureException on Bedrock, FATAL on the shared breaker)."""
    versioned = f"{HAIKU}-v1:0"
    config = FakeConfigRegistry(snapshot_from(patched_example({"models": {"haiku-5.5": {"id": versioned}}})))
    async with wired(config) as w:
        w.use({"match": {"model": versioned}, "answer": {"raw_text": "ok"}, "stream": {"chunks": ["o", "k"]}})
        resp = await w.registry.for_model("haiku-5.5").complete(
            req("haiku-5.5"), tenant=w.tenant, deadline=w.deadline()
        )
        events = await stream(w, "haiku-5.5")
        records = w.mock.requests()
    assert resp.text == "ok" and "".join(e.text for e in events if isinstance(e, TextDelta)) == "ok"
    assert [(r["model"], r["status"]) for r in records] == [(versioned, 200), (versioned, 200)]


async def test_converse_stream_order() -> None:
    async with wired() as w:
        w.use({"match": {"model": SOL}, "stream": {"chunks": ["Su", "re."]}})
        events = await stream(w, "sol-6.1", req("sol-6.1", effort="low"))
    assert isinstance(events[0], MessageStart)
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Sure."
    assert [type(e) for e in events[-2:]] == [Stop, UsageEvent]


async def test_mantle_logprobs_and_stream() -> None:
    async with wired() as w:
        w.use(
            {"match": {"model": DEEPSEEK, "op": "chat"}, "answer": {"dist": {"chitchat": 0.1, "coding": 0.7}}},
            {"match": {"model": DEEPSEEK, "op": "chat-stream"}, "stream": {"chunks": ["one ", "two"]}},
        )
        lp = req("deepseek-3.2", AQ, kind="decision_member", logprobs=True, top_logprobs=5)
        resp = await w.registry.for_model("deepseek-3.2").complete(lp, tenant=w.tenant, deadline=w.deadline())
        events = await stream(w, "deepseek-3.2")
    assert resp.logprobs is not None and resp.text.strip() == "B"
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["one ", "two"]
    assert isinstance(events[-1], UsageEvent)


@pytest.mark.parametrize(
    ("fault", "kind"),
    [
        ({"kind": "throttling"}, ErrorKind.THROTTLED),
        ({"kind": "access_denied"}, ErrorKind.TENANT_ACCESS),
        ({"kind": "validation", "message": "bad"}, ErrorKind.INVALID_REQUEST),
        ({"kind": "http_5xx", "status": 503}, ErrorKind.RETRYABLE),
        ({"kind": "model_timeout"}, ErrorKind.RETRYABLE),
    ],
)
@pytest.mark.parametrize("alias", ["haiku-5.5", "deepseek-3.2"])
async def test_error_responses_classify(fault: dict[str, Any], kind: ErrorKind, alias: str) -> None:
    async with wired() as w:
        w.use({"match": {}, "fault": fault})
        with pytest.raises(AdapterError) as err:
            await w.registry.for_model(alias).complete(req(alias), tenant=w.tenant, deadline=w.deadline())
    assert err.value.error.kind is kind


@pytest.mark.parametrize(
    ("rule", "check"),
    [
        (
            {"stream": {"chunks": ["a", "b", "c"]}, "fault": {"kind": "exception_event", "after": 2}},
            "exception",
        ),
        ({"fault": {"kind": "reasoning_only"}, "stream": {"reasoning": {"deltas": 3}}}, "reasoning_only"),
        ({"stream": {"chunks": ["a", "b"]}, "fault": {"kind": "malformed", "variant": "bad_crc"}}, "malformed"),
    ],
)
async def test_stream_faults(rule: dict[str, Any], check: str) -> None:
    async with wired() as w:
        w.use({"match": {"model": GLM}, **rule})
        events = await stream(w, "glm-5.3", req("glm-5.3", effort="low"))  # `none`: rejected (Wave E2)
    if check == "reasoning_only":
        assert not any(isinstance(e, TextDelta) for e in events)
        assert [e.reason for e in events if isinstance(e, Stop)] == ["max_tokens"]
    else:
        last = events[-1]
        assert isinstance(last, ErrorEvent) and last.error.kind is ErrorKind.RETRYABLE
        assert sum(isinstance(e, ErrorEvent) for e in events) == 1


# connect_s also bounds the credentials and a cold client creation (~50-80 ms for aiobotocore): keep it roomy.
FAST = StreamTimeouts(connect_s=0.5, first_event_s=0.5, first_answer_s=1.0, idle_s=0.5)


@pytest.mark.parametrize(
    ("rule", "code"),
    [
        ({"fault": {"kind": "hang_before_first_byte"}}, "timeout:first_event"),
        ({"stream": {"chunks": ["a", "b"]}, "fault": {"kind": "stall_after", "n": 1}}, "timeout:idle"),
    ],
)
@pytest.mark.parametrize("alias", ["sol-6.1", "deepseek-3.2"])
async def test_stream_timeouts_over_sockets(rule: dict[str, Any], code: str, alias: str) -> None:
    async with wired() as w:
        w.use({"match": {}, **rule})
        adapter = w.registry.for_model(alias)
        async with adapter.stream(req(alias), tenant=w.tenant, timeouts=FAST, deadline=w.deadline()) as events:
            got = [ev async for ev in events]
    last = got[-1]
    assert isinstance(last, ErrorEvent) and last.error.provider_code == code


@pytest.mark.parametrize("alias", ["sol-6.1", "deepseek-3.2"])
@pytest.mark.parametrize("first_sends", [10, 0])
async def test_connect_reset_is_retried_once_for_default(alias: str, first_sends: int) -> None:
    # the mock's reset follows the request write (possibly delivered): ruling R8 budgets that retry, with no floor
    async with wired() as w:
        model_id = SOL if alias == "sol-6.1" else DEEPSEEK
        for _ in range(first_sends):
            w.budget.note_request(w.tenant.aws_account_id, model_id)
        w.use({"match": {"nth": 1}, "fault": {"kind": "connect_reset"}}, {"match": {}, "stream": {"chunks": ["ok"]}})
        request = replace(req(alias), retry="default")
        if first_sends:
            events = await stream(w, alias, request)
        else:
            with pytest.raises(AdapterError) as err:
                await stream(w, alias, request)
            assert err.value.error.kind is ErrorKind.RETRYABLE
        outcomes = [r["outcome"] for r in w.mock.requests()]
    if first_sends:
        assert any(isinstance(e, TextDelta) for e in events)
    assert outcomes == (["reset", "ok"] if first_sends else ["reset"])
