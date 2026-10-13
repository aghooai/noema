"""The frozen wire mapping against the golden requests (spec-02 §4.2, `tests/golden/{decision,generation}_requests`),
both from the pure builders and through the real adapters' captured wire; the `AdapterRegistry`; capabilities
from config; the pinned snapshot; and the probe-verified capability matrix."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Final

import pytest
from tests.fakes.config import example_snapshot, patched_example, snapshot_from
from tests.golden.wire import check_adapter_against_goldens, load_wire_goldens, request_from
from tests.unit.adapters.emulator_wire import golden_json
from tests.unit.adapters.support import SOL, request, rig

from noema.adapters.capabilities import PROBED, capabilities_of, probe_discrepancies, probe_unverified
from noema.adapters.wire import check_request, converse_params, mantle_body
from noema.core.errors import NoemaError
from noema.core.protocols.adapters import AdapterError

GOLDENS = load_wire_goldens()


@pytest.mark.parametrize("golden", GOLDENS, ids=[g["name"] for g in GOLDENS])
def test_builders_match_the_goldens(golden: dict[str, Any]) -> None:
    req = request_from(golden["request"])
    model = example_snapshot().model(req.model_alias)
    if "error" in golden["expected"]:
        with pytest.raises(AdapterError):
            check_request(req, model, model.adapter)
        return
    check_request(req, model, model.adapter)
    if model.adapter == "bedrock-converse":
        body = converse_params(req, model)
    else:
        body = mantle_body(req, model, stream=golden["op"] == "chat-stream")
    assert golden_json(body) == golden["expected"]["body"]


async def test_adapters_put_the_goldens_on_the_wire() -> None:
    async with rig() as r:

        class Kit:
            adapters, clock, tenant = r.registry, r.clock, r.tenant
            use_scenario = staticmethod(r.emulator.use_scenario)

            @staticmethod
            def wire_requests() -> list[dict[str, Any]]:
                return r.emulator.wire

        async with r.clock.autojump():
            await check_adapter_against_goldens(Kit())
        urls = {q.model for q in r.emulator.requests if q.op.startswith("chat")}
    assert urls == {"deepseek.v3.2"}


def test_registry_routes_by_adapter_kind() -> None:
    snapshot = example_snapshot()
    kinds = {alias: snapshot.model(alias).adapter for alias in ("haiku-5.5", "deepseek-3.2", "glm-5.3")}
    assert kinds == {
        "haiku-5.5": "bedrock-converse",
        "deepseek-3.2": "bedrock-mantle-chat",
        "glm-5.3": "bedrock-converse",
    }


async def test_registry_lookup_and_unknown_alias() -> None:
    async with rig() as r:
        assert r.registry.for_model("opus-5.5") is r.converse
        assert r.registry.for_model("deepseek-3.2") is r.mantle
        with pytest.raises(NoemaError):
            r.registry.for_model("nope")


def test_capabilities_come_from_config() -> None:
    snapshot = example_snapshot()
    glm = capabilities_of(snapshot.model("glm-5.3"))
    assert glm.temperature and glm.max_output_tokens_cap == 4096 and glm.modalities == frozenset({"text"})
    assert glm.context_window == 128_000 and glm.chars_per_token == 4.0
    assert "xhigh" in glm.effort and "none" not in glm.effort  # Bedrock rejects `none` (Wave E live smoke)
    sonnet = capabilities_of(snapshot.model("sonnet-5.5"))
    assert sonnet.effort == frozenset() and not sonnet.temperature and "document" in sonnet.modalities


async def test_the_pinned_snapshot_wins_over_a_later_activation() -> None:
    async with rig() as r:
        pinned = r.config.current()
        r.config.activate(snapshot_from(patched_example({"models": {"sol-6.1": {"id": "us.openai.gpt-6.1-sol-v2"}}})))
        r.scenario({"match": {}, "answer": {"raw_text": "ok"}})
        async with r.clock.autojump():
            await r.converse.complete(
                replace(request("sol-6.1"), snapshot=pinned), tenant=r.tenant, deadline=r.deadline()
            )
            await r.converse.complete(request("sol-6.1"), tenant=r.tenant, deadline=r.deadline())
    assert [q.model for q in r.emulator.requests] == [SOL, "us.openai.gpt-6.1-sol-v2"]


# Wave E live smoke (2026-10-11): Bedrock rejects GLM-5.3 effort `none`; the shipped config dropped it in Wave E2
# (GLM effort low..max incl. xhigh, race-sol-glm's GLM member at `low`; io-fixer change request item 1).
KNOWN_DRIFT: Final[list[str]] = []


def test_the_example_config_agrees_with_the_probes() -> None:
    snapshot = example_snapshot()
    assert probe_discrepancies(snapshot) == KNOWN_DRIFT
    unverified = probe_unverified(snapshot)
    assert any("sonnet-5.5" in n and "'image'" in n for n in unverified)
    assert all("sonnet-5.5" in n for n in unverified)


def test_refuted_config_claims_are_reported() -> None:
    patch = {
        "models": {
            "opus-5.5": {"controls": {"temperature": True}},
            "sol-6.1": {"controls": {"effort": ["none", "low", "medium", "high", "max"]}},
            "glm-5.3": {"modalities": ["text", "image"]},
        }
    }
    problems = probe_discrepancies(snapshot_from(patched_example(patch)))
    assert len(problems) == 3 + len(KNOWN_DRIFT) and set(KNOWN_DRIFT) <= set(problems)
    assert any("opus-5.5" in p and "temperature" in p for p in problems)
    assert any("sol-6.1" in p and "'none'" in p for p in problems)
    assert any("glm-5.3" in p and "'image'" in p for p in problems)


def test_probe_matrix_covers_every_configured_model() -> None:
    ids = {example_snapshot().model(a).id for a in ("haiku-5.5", "luna-6", "deepseek-3.2", "sol-6.1", "opus-5.5")}
    assert ids <= set(PROBED) and PROBED["deepseek.v3.2"].logprobs is True


def test_logprobs_without_top_logprobs_never_sends_null() -> None:
    # adapters review: spec-02 §4.2 sends `"top_logprobs": n`; with no n the key is omitted, never null
    model = example_snapshot().model("deepseek-3.2")
    req = request("deepseek-3.2", kind="decision_member", retry="none", logprobs=True)
    check_request(req, model, "bedrock-mantle-chat")
    body = mantle_body(req, model, stream=False)
    assert body["logprobs"] is True and "top_logprobs" not in body
    assert mantle_body(replace(req, top_logprobs=5), model, stream=False)["top_logprobs"] == 5
