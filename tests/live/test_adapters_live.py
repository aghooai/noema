"""Live smoke, adapter level (spec-10 §6): L1 non-streaming Converse (GLM, effort none), L2 ConverseStream (Opus low
with reasoning first, GLM none), L3 the Mantle auth probes on DeepSeek (short-term bearer from the ambient
credentials, then SigV4; ADR-001 §6), L4 Sonnet media (gap 43, IMPL-043), the capability matrix (temperature and
effort: pre-wire refusals for the 7 models, no call; three provider probes), and (Wave E3) one call on a ":" model
id through the lean SigV4 path. 11 calls. The direct `Adapter.stream()` calls of L8 run interleaved with the pinned
turns in `test_api_live.py`. Reports: reports/live/{mantle-auth,sonnet-media,capability-probes,colon-model-id}.json."""

from __future__ import annotations

import os
import struct
import time
from pathlib import Path
from typing import Any, Final

import orjson
import pytest
from tests.live.conftest import MOCK, Live
from tests.live.livecfg import live_snapshot

from noema.adapters.capabilities import PROBED, probe_discrepancies
from noema.core.protocols.adapters import (
    AdapterError,
    MessageStart,
    ModelRequest,
    ReasoningDelta,
    Stop,
    StreamEvent,
    TextDelta,
    UsageEvent,
)
from noema.core.values import CallKind, ContentBlock, DocumentBlock, Effort, ImageBlock, Message, TextBlock

pytestmark = [pytest.mark.live, pytest.mark.enable_socket]
FIXTURES: Final = Path("/codes/aghooai/probes/fixtures")  # number.png and invoice.pdf both show 4471 (03-env)
DIGITS_PROMPT: Final = "What number is shown? Answer with the digits only."
TOPICS: Final = ("TCP slow start", "B-tree indexes", "CRDTs", "consistent hashing", "the CAP theorem", "Bloom filters",
                  "write-ahead logging", "vector clocks", "Raft leader election", "copy-on-write")  # fmt: skip
L8_PAIRS: Final = min(int(os.environ.get("NOEMA_LIVE_L8_PAIRS", "5")), len(TOPICS))  # the most pairs (budget bounds)
ALIASES: Final = ("haiku-5.5", "luna-6", "deepseek-3.2", "sol-6.1", "opus-5.5", "glm-5.3", "sonnet-5.5")
EFFORTS: Final[tuple[Effort, ...]] = ("none", "low", "medium", "high", "xhigh", "max")


def req(
    alias: str,
    *blocks: ContentBlock,
    effort: Effort | None = None,
    max_tokens: int = 128,
    kind: CallKind = "generation",
    temperature: float | None = None,
    snapshot: Any = None,
) -> ModelRequest:
    content = blocks or (TextBlock("What is 17 * 23? Answer with the number only."),)
    return ModelRequest(
        alias, ("You are terse.",), (Message("user", content),), max_tokens, kind, "none", effort=effort,
        temperature=temperature, snapshot=snapshot,
    )  # fmt: skip


def png_size(data: bytes) -> tuple[int, int]:
    """(width, height) from the IHDR chunk (the estimator's image tokens need them, K1)."""
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


async def stream_all(live: Live, request: ModelRequest) -> tuple[list[StreamEvent], dict[str, int]]:
    timeouts = live.config.current().route_timeouts(request.model_alias, request.effort)
    marks: dict[str, int] = {}
    t0 = time.monotonic()
    out: list[StreamEvent] = []
    adapter = live.adapters.for_model(request.model_alias)
    async with adapter.stream(request, tenant=live.tenant, timeouts=timeouts, deadline=live.deadline()) as events:
        async for ev in events:
            if isinstance(ev, TextDelta) and "first_text" not in marks:
                marks["first_text"] = round((time.monotonic() - t0) * 1000)
            out.append(ev)
    marks["e2e"] = round((time.monotonic() - t0) * 1000)
    return out, marks


def assert_well_formed(events: list[StreamEvent]) -> None:
    assert isinstance(events[0], MessageStart)
    assert any(isinstance(e, TextDelta) for e in events)
    stops = [i for i, e in enumerate(events) if isinstance(e, Stop)]
    assert len(stops) == 1 and all(isinstance(e, UsageEvent) for e in events[stops[0] + 1 :])


# GLM-5.3 effort: spec-10 L1/L2 say `none` (probed 2026-10-09), but Bedrock rejected `none` for us.zai.glm-5.3 on
# 2026-10-11 ("Supported values are: 'low', 'medium', 'high', 'xhigh', and 'max'", cassettes live-20261011T073404
# 01 and 03; reports/qa/live-smoke.md): the GLM items run at the lowest accepted effort, with room for its reasoning.
GLM_EFFORT: Final[Effort] = "low"


async def test_l1_converse_complete_glm(live: Live) -> None:
    live.ledger.need(1)
    resp = await live.adapters.for_model("glm-5.3").complete(
        req("glm-5.3", effort=GLM_EFFORT, max_tokens=512), tenant=live.tenant, deadline=live.deadline(60)
    )
    assert "391" in resp.text and resp.stop_reason == "end_turn" and resp.region == "us-east-1"
    assert resp.usage.input_tokens > 0 and resp.usage.output_tokens > 0


@pytest.mark.parametrize(("alias", "effort"), [("opus-5.5", "low"), ("glm-5.3", GLM_EFFORT)])
async def test_l2_converse_stream(live: Live, alias: str, effort: Effort) -> None:
    live.ledger.need(1)
    events, marks = await stream_all(live, req(alias, effort=effort, max_tokens=256 if alias == "opus-5.5" else 512))
    call = live.ledger.calls[-1]
    wire = call["wire"]
    path = live.ledger.report_path("lean-stream.json")
    previous: dict[str, Any] = orjson.loads(path.read_bytes()) if path.exists() else {}
    row = {"effort": effort, "marks": marks, "events": call["counts"], "stop": call.get("stop"),
           "usage": call.get("usage"), "decoder": wire.get("decoder"), "signer": wire.get("signer")}  # fmt: skip
    live.ledger.write_report("lean-stream.json", {**previous, alias: row})
    assert_well_formed(events)
    # Wave E3: the lean ConverseStream path's decoder and signer against botocore on these very bytes and request
    decoder, signer = wire["decoder"], wire["signer"]
    assert decoder["messages"] == decoder["equal"] > 0 and not decoder["fallback"], decoder
    assert decoder["botocore_extra"] == decoder["botocore_errors"] == 0, decoder
    assert signer["equal_to_botocore"], signer
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    first_text = next(i for i, e in enumerate(events) if isinstance(e, TextDelta))
    reasoning = [i for i, e in enumerate(events) if isinstance(e, ReasoningDelta)]
    assert "391" in text and next(e for e in events if isinstance(e, Stop)).reason == "end_turn"
    if reasoning:  # Opus and GLM stream reasoning before the answer (probes); counted, never text
        assert reasoning[0] < first_text


async def test_l3_mantle_auth_probes(live: Live) -> None:
    live.ledger.need(2)
    results: dict[str, Any] = {}
    for mode in ("bearer", "sigv4"):
        live.broker.mantle_mode = mode
        try:
            resp = await live.adapters.for_model("deepseek-3.2").complete(
                req("deepseek-3.2", kind="decision_member"), tenant=live.tenant, deadline=live.deadline(30)
            )
            results[mode] = {"ok": True, "stop": resp.stop_reason, "latency_ms": resp.latency_ms,
                             "answer_391": "391" in resp.text}  # fmt: skip
        except AdapterError as exc:
            results[mode] = {"ok": False, "kind": exc.error.kind.value, "provider_code": exc.error.provider_code}
    live.broker.mantle_mode = "bearer"
    live.ledger.write_report("mantle-auth.json", {"mode": "ambient IAM user", "results": results})
    # the shipped `platform.credentials.mantle_auth` (bearer_short_term) must work; SigV4 is reported (ADR-001 §6)
    assert results["bearer"]["ok"] and results["bearer"]["answer_391"], results


@pytest.mark.parametrize("kind", ["image", "pdf"])
async def test_l4_sonnet_media(live: Live, kind: str) -> None:
    """Gap 43 / IMPL-043: the config lists image and document for Sonnet 5.5; both fixtures show 4471."""
    live.ledger.need(1)
    if kind == "image":
        data = (FIXTURES / "number.png").read_bytes()
        block: ContentBlock = ImageBlock("png", data, *png_size(data))
    else:
        block = DocumentBlock("pdf", "invoice", (FIXTURES / "invoice.pdf").read_bytes(), 1)
    outcome: dict[str, Any]
    try:
        resp = await live.adapters.for_model("sonnet-5.5").complete(
            req("sonnet-5.5", block, TextBlock(DIGITS_PROMPT), kind="labeling"),
            tenant=live.tenant,
            deadline=live.deadline(60),
        )
        outcome = {"accepted": True, "digits_found": "4471" in resp.text, "stop": resp.stop_reason}
    except AdapterError as exc:
        outcome = {"accepted": False, "kind": exc.error.kind.value, "provider_code": exc.error.provider_code}
    path = live.ledger.report_path("sonnet-media.json")
    previous: dict[str, Any] = orjson.loads(path.read_bytes()) if path.exists() else {}
    live.ledger.write_report("sonnet-media.json", {**previous, kind: outcome})
    assert outcome["accepted"] and outcome["digits_found"], outcome


# Wave E3 item 2: a ":" model id. The configured ids have none (no `-v1:0` id among the 7 models), so the Luna
# inference profile by its ARN (`aws bedrock list-inference-profiles --region us-east-1`, 2026-10-12: ACTIVE): its
# model path carries %3A and %2F, which botocore's canonical path encodes again (`_sigv4`).
COLON_MODEL_ID: Final = "arn:aws:bedrock:us-east-1:{account}:inference-profile/us.openai.gpt-6-luna"


async def test_colon_model_id_through_lean_sigv4(live: Live) -> None:
    """One Converse call on an ARN model id: Bedrock accepts the lean signer's signature (no 403), the signed headers
    equal SigV4Auth's on the same request (wire tap), and Luna answers. The wire mock knows no ARN ids, so the
    rehearsal checks the signer only."""
    live.ledger.need(1)
    assert live.broker.caller_account is not None
    arn = COLON_MODEL_ID.format(account=live.broker.caller_account)
    snapshot = live_snapshot(live.broker.caller_account, extra={"models": {"luna-6": {"id": arn}}})
    request = req("luna-6", effort="none", max_tokens=64, kind="decision_member", snapshot=snapshot)
    outcome: dict[str, Any]
    try:
        resp = await live.adapters.for_model("luna-6").complete(request, tenant=live.tenant, deadline=live.deadline(60))
        outcome = {"ok": True, "answer_391": "391" in resp.text, "stop": resp.stop_reason, "region": resp.region,
                   "latency_ms": resp.latency_ms}  # fmt: skip
    except AdapterError as exc:
        outcome = {"ok": False, "kind": exc.error.kind.value, "provider_code": exc.error.provider_code}
    call = live.ledger.calls[-1]
    signer = call["wire"].get("signer") or {}
    live.ledger.write_report("colon-model-id.json", {"model_id": call["model_id"], "signer": signer, **outcome})
    assert call["model_id"] == arn and signer["model_path_colon_encoded"] and signer["equal_to_botocore"], call
    if not MOCK:
        assert outcome["ok"] and outcome["answer_391"] and outcome["region"] == "us-east-1", outcome


async def test_capability_matrix_prewire_refusals(live: Live) -> None:
    """Temperature and effort for each of the 7 configured models: everything the probe matrix (`PROBED`, 03-env)
    rejects is refused by the adapter before any byte (`invalid_request`, no provider code), and the shipped config
    claims nothing the probes refuted. No model call: the inner adapters are called, not the counting registry."""
    snapshot = live.config.current()
    assert probe_discrepancies(snapshot) == []
    rows: dict[str, Any] = {}
    for alias in ALIASES:
        model = snapshot.model(alias)
        probe = PROBED[model.id]
        adapter = live.converse if model.adapter == "bedrock-converse" else live.mantle
        cases: list[tuple[str, ModelRequest]] = [(f"effort={e}", req(alias, effort=e)) for e in EFFORTS]
        cases.append(("temperature", req(alias, temperature=0.0)))
        refused = []
        for name, request in cases:
            allowed = model.controls.temperature if name == "temperature" else request.effort in model.controls.effort
            if allowed:
                continue  # accepted by the config: sending it would be a model call (the probes below cover some)
            with pytest.raises(AdapterError) as info:
                await adapter.complete(request, tenant=live.tenant, deadline=live.deadline(5))
            assert info.value.error.kind.value == "invalid_request" and info.value.error.provider_code is None
            refused.append(name)
        assert {f"effort={e}" for e in probe.effort_rejected if e in EFFORTS} <= set(refused), alias
        assert probe.temperature is not False or "temperature" in refused, alias
        rows[alias] = {"model_id": model.id, "refused_pre_wire": refused, "config_effort": list(model.controls.effort),
                       "config_temperature": model.controls.temperature}  # fmt: skip
    live.ledger.write_report("capability-prewire.json", {"calls": 0, "models": rows})


PROVIDER_PROBES: Final[tuple[tuple[str, Effort | None, float | None, str], ...]] = (
    ("sonnet-5.5", "low", None, "unprobed (gap 43): config lists no effort for Sonnet 5.5"),
    ("sol-6.1", "none", None, "rejected: PROBED effort_rejected = {none}"),
    ("haiku-5.5", None, 0.0, "rejected: PROBED temperature = False"),
)


async def test_capability_provider_probes(live: Live) -> None:
    """Three requests the shipped config refuses pre-wire, sent with relaxed controls (a probe snapshot) so Bedrock
    answers: Sol effort `none` and Haiku `temperature` must still be rejected (ValidationException, classified
    `invalid_request`), Sonnet effort `low` is gap 43 and only reported."""
    live.ledger.need(len(PROVIDER_PROBES))
    assert live.broker.caller_account is not None
    relaxed = live_snapshot(live.broker.caller_account, extra={"models": {
        "sonnet-5.5": {"controls": {"effort": ["low"]}},
        "sol-6.1": {"controls": {"effort": ["none", "low", "medium", "high", "max"]}},
        "haiku-5.5": {"controls": {"temperature": True}},
    }})  # fmt: skip
    results: dict[str, Any] = {}
    for alias, effort, temperature, expectation in PROVIDER_PROBES:
        request = req(alias, effort=effort, temperature=temperature, snapshot=relaxed)
        try:
            resp = await live.adapters.for_model(alias).complete(
                request, tenant=live.tenant, deadline=live.deadline(60)
            )
            outcome: dict[str, Any] = {"accepted": True, "stop": resp.stop_reason, "answer_391": "391" in resp.text}
        except AdapterError as exc:
            wire = live.ledger.calls[-1]["wire"]["error"] or {}
            outcome = {"accepted": False, "kind": exc.error.kind.value, "provider_code": exc.error.provider_code,
                       "message": wire.get("message")}  # fmt: skip
        results[f"{alias} effort={effort} temperature={temperature}"] = {"expected": expectation, **outcome}
    live.ledger.write_report("capability-probes.json", {"mock": MOCK, "results": results})
    sol, haiku = (results[k] for k in list(results)[1:])
    for rejected in (sol, haiku):
        assert not rejected["accepted"] and rejected["kind"] == "invalid_request", results
        assert rejected["provider_code"] == "ValidationException", results
