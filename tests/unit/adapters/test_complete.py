"""`Adapter.complete` on both adapters (spec-02 §4.1): answers, refusals, stop reasons, usage, latency and region;
the deadline (`timeout:deadline`), `retry="none"` vs `"default"` on connect errors and throttling, the RetryBudget
limit, and the pre-wire rules (nothing reaches the wire)."""

from __future__ import annotations

import json
import logging
from typing import Any

import anyio
import anyio.lowlevel
import httpx
import pytest
from tests.fakes.clock import FakeClock
from tests.unit.adapters.support import DEEPSEEK, HAIKU, SOL, Rig, Unary, json_reply, request, rig

from noema.adapters.resilience.retry_budget import RetryBudgetPolicy, SlidingRetryBudget
from noema.core.errors import ErrorKind
from noema.core.protocols.adapters import AdapterError, ModelRequest
from noema.core.values import DocumentBlock, ImageBlock, Message, TextBlock

AQ = (
    '<app_question>{"instructions": "Intent?", "options": [{"letter": "A", "value": "chitchat", "description": null}, '
    '{"letter": "B", "value": "coding", "description": null}]}</app_question>\n'
)


async def complete(r: Rig, req: ModelRequest, deadline_s: float = 60.0) -> Any:
    async with r.clock.autojump():
        return await r.registry.for_model(req.model_alias).complete(
            req, tenant=r.tenant, deadline=r.deadline(deadline_s)
        )


async def test_converse_answer_usage_latency_region() -> None:
    async with rig() as r:
        r.scenario(
            {"match": {"model": HAIKU}, "answer": {"dist": {"chitchat": 0.25, "coding": 0.75}}, "latency_ms": 640}
        )
        resp = await complete(r, request("haiku-5.5", AQ, kind="decision_member", retry="none", effort="low"))
    assert json.loads(resp.text) == pytest.approx({"A": 0.25, "B": 0.75})
    assert resp.stop_reason == "end_turn" and not resp.refused and resp.logprobs is None
    assert resp.latency_ms == 640 and resp.region == "us-east-1"
    assert resp.usage.input_tokens > 0 and resp.usage.output_tokens > 0
    assert r.emulator.requests[-1].body["outputConfig"] == {"effort": "low"}


async def test_converse_refusal_is_provider_signalled_only() -> None:
    async with rig() as r:
        r.scenario({"match": {"model": HAIKU}, "fault": {"kind": "refusal"}})
        refused = await complete(r, request("haiku-5.5", AQ, kind="decision_member", retry="none"))
        r.scenario({"match": {"model": HAIKU}, "answer": {"raw_text": "I cannot help with that."}})
        prose = await complete(r, request("haiku-5.5", AQ, kind="decision_member", retry="none"))
    assert refused.refused and refused.stop_reason == "content_filter" and refused.text == ""
    assert not prose.refused and prose.stop_reason == "end_turn"  # refusal prose is the parser's problem (IMPL-053)


async def test_mantle_logprobs_map_to_token_logprobs() -> None:
    async with rig() as r:
        r.scenario({"match": {"model": DEEPSEEK, "op": "chat"}, "answer": {"dist": {"chitchat": 0.2, "coding": 0.8}}})
        req = request("deepseek-3.2", AQ, kind="decision_member", retry="none", logprobs=True, top_logprobs=5)
        resp = await complete(r, req)
        body = r.emulator.requests[-1].body
    assert body["logprobs"] is True and body["top_logprobs"] == 5 and body["stream"] is False
    assert resp.logprobs is not None and resp.logprobs[0].token == "B" == resp.text
    tops = dict(resp.logprobs[0].top)
    assert tops["B"] == pytest.approx(-0.2231, abs=1e-3) and tops["A"] == pytest.approx(-1.6094, abs=1e-3)


async def test_complete_deadline_cuts_a_hung_call() -> None:
    async with rig() as r:
        r.scenario({"match": {"model": DEEPSEEK}, "fault": {"kind": "hang_before_first_byte"}})
        with pytest.raises(AdapterError) as err:
            await complete(r, request("deepseek-3.2", kind="decision_member", retry="none"), deadline_s=1.0)
        assert r.clock.elapsed_ms() == 1000
    assert err.value.error.kind is ErrorKind.RETRYABLE and err.value.error.provider_code == "timeout:deadline"


@pytest.mark.parametrize("alias", ["sol-6.1", "deepseek-3.2"])
@pytest.mark.parametrize(("retry", "calls"), [("default", 2), ("none", 1)])
async def test_connect_error_retried_once_only_for_default(alias: str, retry: str, calls: int) -> None:
    model = SOL if alias == "sol-6.1" else DEEPSEEK
    async with rig() as r:
        r.scenario(
            {"match": {"model": model, "nth": 1}, "fault": {"kind": "connect_reset"}},
            {"match": {"model": model}, "answer": {"raw_text": "ok"}},
        )
        req = request(alias, retry=retry)  # type: ignore[arg-type]
        if calls == 2:
            assert (await complete(r, req)).text == "ok"
        else:
            with pytest.raises(AdapterError) as err:
                await complete(r, req)
            assert err.value.error.kind is ErrorKind.RETRYABLE
        assert len(r.emulator.requests) == calls


@pytest.mark.parametrize(("failure", "sent"), [(httpx.RemoteProtocolError, 1), (httpx.ConnectError, 2)])
async def test_connect_retry_needs_the_retry_budget_unless_nothing_was_sent(
    failure: type[httpx.TransportError], sent: int
) -> None:
    # owner ruling R8: a retry after a possible delivery spends the budget (none on a quiet key: no floor); a
    # failed connect never reached the upstream and is retried once without it
    requests: list[httpx.Request] = []
    answer = {"choices": [{"index": 0, "message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}}

    async def handle(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        if len(requests) == 1:
            raise failure("connection failed", request=req)
        return httpx.Response(200, json=answer)

    async with rig(mantle_transport=httpx.MockTransport(handle)) as r:
        if sent == 1:
            with pytest.raises(AdapterError):
                await complete(r, request("deepseek-3.2", retry="default"))
        else:
            assert (await complete(r, request("deepseek-3.2", retry="default"))).text == "ok"
    assert len(requests) == sent


async def test_throttling_is_never_retried_in_the_adapter() -> None:
    async with rig() as r:
        r.scenario({"match": {"model": SOL}, "fault": {"kind": "throttling", "retry_after_s": 2}})
        with pytest.raises(AdapterError) as err:
            await complete(r, request("sol-6.1", retry="default"))
        assert len(r.emulator.requests) == 1
        assert r.metric("aghoo_bedrock_throttles_total", tenant="t_acme", model=SOL) == 1
    assert err.value.error.kind is ErrorKind.THROTTLED and err.value.error.retry_after_s == 2.0


async def test_every_wire_request_is_counted_in_the_budget() -> None:
    clock = FakeClock()
    budget = SlidingRetryBudget(clock, policy=RetryBudgetPolicy(min_retries=0))
    async with rig(clock=clock, budget=budget) as r:
        for _ in range(20):
            await complete(r, request("sol-6.1"))
    account = r.tenant.aws_account_id
    granted = 0
    while budget.try_spend(account, SOL):  # each granted retry is then sent (and counted) as a request
        granted += 1
        budget.note_request(account, SOL)
    assert granted == 2  # 10 % of the 20 first attempts


PNG = b"\x89PNG\r\n\x1a\n" + bytes(16)


@pytest.mark.parametrize(
    "req",
    [
        request("haiku-5.5", logprobs=True, top_logprobs=5),  # logprobs on Converse
        request("opus-5.5", temperature=0.2),  # controls.temperature false
        request("sol-6.1", effort="none"),  # Sol rejects effort none
        request("deepseek-3.2", effort="low"),  # DeepSeek has no effort
        request("deepseek-3.2", logprobs=True, top_logprobs=21),  # above top_logprobs_max
        request("deepseek-3.2", top_logprobs=3),  # top_logprobs without logprobs
        ModelRequest(
            "glm-5.3", (), (Message("user", (TextBlock("x"), ImageBlock("png", PNG, 1, 1))),), 8, "generation", "none"
        ),
        ModelRequest(
            "sol-6.1", (), (Message("user", (DocumentBlock("pdf", "doc-1", b"%PDF"),)),), 8, "generation", "none"
        ),
        ModelRequest("deepseek-3.2", (), (Message("user", (ImageBlock("png", PNG, 1, 1),)),), 8, "generation", "none"),
        ModelRequest("glm-5.3", (), (Message("user", (TextBlock("x"),)),), 4097, "generation", "none"),  # above cap
        ModelRequest("sol-6.1", (), (), 8, "generation", "none"),
        ModelRequest("unknown-alias", (), (Message("user", (TextBlock("x"),)),), 8, "generation", "none"),
    ],
    ids=lambda r: r.model_alias,
)
async def test_pre_wire_rules_reject_before_credentials_or_bytes(req: ModelRequest) -> None:
    async with rig() as r:
        adapter = r.converse if req.model_alias in ("unknown-alias",) else r.registry.for_model(req.model_alias)
        with pytest.raises(AdapterError) as err:
            await adapter.complete(req, tenant=r.tenant, deadline=r.deadline())
        assert err.value.error.kind is ErrorKind.INVALID_REQUEST
        assert r.emulator.requests == [] and r.broker.gets["t_acme"] == 0


def answering(body: dict[str, Any]) -> Unary:
    """A unary (Converse) transport answering every call with `body` (and counting the clients)."""
    return Unary(lambda *_: json_reply(body))


def converse_reply(stop: str, content: list[dict[str, Any]]) -> dict[str, Any]:
    usage = {"inputTokens": 5, "outputTokens": 40, "totalTokens": 45, "cacheReadInputTokens": 2}
    return {"output": {"message": {"role": "assistant", "content": content}}, "stopReason": stop, "usage": usage}


@pytest.mark.parametrize(
    ("stop", "reason", "refused"),
    [
        ("end_turn", "end_turn", False),
        ("stop_sequence", "stop_sequence", False),
        ("max_tokens", "max_tokens", False),
        ("guardrail_intervened", "content_filter", True),
        ("content_filtered", "content_filter", True),
        ("model_context_window_exceeded", "max_tokens", False),
        ("tool_use", "other", False),
        ("malformed_model_output", "other", False),
    ],
)
async def test_converse_stop_reasons_and_reasoning_blocks(stop: str, reason: str, refused: bool) -> None:
    content: list[dict[str, Any]] = [
        {"reasoningContent": {"reasoningText": {"text": "x" * 64, "signature": "s"}}},
        {"text": "A"},
        {"text": "B"},
    ]
    unary = answering(converse_reply(stop, content))
    async with rig(converse_unary=unary) as r:
        resp = await complete(r, request("sol-6.1"))
    assert resp.text == "AB" and resp.stop_reason == reason and resp.refused is refused
    assert (resp.usage.input_tokens, resp.usage.cached_input_tokens, resp.usage.output_tokens) == (7, 2, 40)
    assert resp.usage.reasoning_tokens == 16


@pytest.mark.parametrize(
    ("hidden", "text", "reasoning"),
    [
        ({"reasoningText": {"text": "", "signature": "sig"}}, "", 40),  # live Haiku: every output token reasoned
        ({"redactedContent": "AAEC"}, "x" * 40, 30),  # 40 - ceil(40 / 4) text tokens
    ],
    ids=["signature_only", "redacted"],
)
async def test_hidden_reasoning_is_estimated_from_the_output_left_after_the_text(
    hidden: dict[str, Any], text: str, reasoning: int
) -> None:
    """Wave E (live cassette 35-haiku-5.5-complete): Bedrock hid the reasoning (an empty text with a signature, or
    redactedContent), so no reasoning character was counted and reasoning_tokens was 0 of output 88."""
    content: list[dict[str, Any]] = [{"reasoningContent": hidden}] + ([{"text": text}] if text else [])
    async with rig(converse_unary=answering(converse_reply("max_tokens", content))) as r:
        resp = await complete(r, request("sol-6.1"))
    assert resp.text == text and resp.usage.output_tokens == 40 and resp.usage.reasoning_tokens == reasoning


async def test_concurrent_calls_share_one_client_per_tenant() -> None:
    unary = answering(converse_reply("end_turn", [{"text": "ok"}]))
    async with rig(converse_unary=unary) as r, r.clock.autojump(), anyio.create_task_group() as tg:
        for _ in range(50):
            tg.start_soon(lambda: r.converse.complete(request("sol-6.1"), tenant=r.tenant, deadline=r.deadline()))
    assert len(unary.clients) == 1 and len(unary.calls) == 50


@pytest.mark.parametrize(
    ("failure", "level", "delivered"),
    [
        (httpx.ConnectError, logging.INFO, False),
        (httpx.RemoteProtocolError, logging.WARNING, True),  # "Server disconnected without sending a response"
    ],
)
async def test_a_retry_after_a_possible_delivery_is_flagged(
    failure: type[httpx.TransportError], level: int, delivered: bool, caplog: pytest.LogCaptureFixture
) -> None:
    # adapters review (IMPL-016): both are retried; only the second may have reached (and been billed by) Bedrock
    caplog.set_level(logging.INFO, logger="noema.adapters")
    sent: list[httpx.Request] = []
    answer = {"choices": [{"index": 0, "message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}}

    async def handle(req: httpx.Request) -> httpx.Response:
        sent.append(req)
        if len(sent) == 1:
            raise failure("connection failed", request=req)
        return httpx.Response(200, json=answer)

    async with rig(mantle_transport=httpx.MockTransport(handle)) as r:
        for _ in range(10):  # R8: 10 first sends in the window allow the one budgeted re-send
            r.budget.note_request(r.tenant.aws_account_id, DEEPSEEK)
        resp = await complete(r, request("deepseek-3.2", retry="default"))
    assert resp.text == "ok" and len(sent) == 2
    (record,) = [rec for rec in caplog.records if rec.getMessage().startswith("connect retry")]
    assert record.levelno == level and getattr(record, "possibly_delivered", False) is delivered


@pytest.mark.parametrize(
    "body",
    [
        {"output": {"message": {"content": [{"text": 5}]}}, "stopReason": "end_turn"},  # read fields of other types
        {"output": {"message": {"content": "not a list"}}},
    ],
)
async def test_a_converse_answer_of_the_wrong_shape_is_retryable_malformed(body: dict[str, Any]) -> None:
    # E sprint A (adapters, F8 for Converse): never a raw TypeError out of complete() or a fatal
    async with rig(converse_unary=answering(body)) as r:
        with pytest.raises(AdapterError) as err:
            await complete(r, request("sol-6.1"))
    error = err.value.error
    assert (error.kind, error.provider_code) == (ErrorKind.RETRYABLE, "malformed:response")
