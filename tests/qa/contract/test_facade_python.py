"""Gate Q2 (06 §2.2, §11): the official `openai` Python client (unmodified) against the stack's facade, acceptance
and parsing. Retries on 429/503 are in test_facade_retries.py, the npm client in test_facade_typescript.py.

Pass criterion: every case passes (100%): the SDK accepts each contract request example and parses each answer
into its typed models; the answers equal the contract response examples on the mock's scripted distributions;
`unavailable` maps to `refusal` with and without `x-aghoo-include: ensemble` (with it, the reason is in
`x_aghoo.mapped_unavailable`); typed errors surface as the SDK's exception classes with the contract code. Every
response is also contract-validated (facadekit.facade: ContractValidator incl. `oa_invariants`).
"""

from __future__ import annotations

from typing import Any

import openai
import pytest
from tests.e2e.conftest import Mock, Stack
from tests.qa.contract.facadekit import DOWN, example, facade, scenario, unique

pytestmark = [pytest.mark.e2e]
OK = "openai-response.valid.json"


@pytest.fixture(autouse=True)
async def facade_scenario(default_scenario: None, mock: Mock) -> None:
    await mock.load(scenario())


async def test_request_example_is_accepted_and_parsed(stack: Stack) -> None:
    sdk = facade(stack)
    decision = await sdk.client.decisions.create(**unique(example("openai-request.valid.json")))
    assert decision.model == "aghoo/router-default@1"
    score, choice = decision.answers
    assert score.type == "score" and choice.type == "choice"
    assert [p.label for p in score.probabilities] == ["Cosmetic", "Workaround available", "Fully blocked"]
    assert choice.choice is True and [p.value for p in choice.probabilities] == [True, False]
    assert [a.model_dump() for a in decision.answers] == example(OK)["answers"]
    assert decision.usage.total_tokens == decision.usage.input_tokens + decision.usage.output_tokens
    assert "x_aghoo" not in (decision.model_extra or {}) and sdk.seen == [(200, None)]


async def test_non_pattern_names_round_trip(stack: Stack) -> None:
    sdk = facade(stack, replica=1)
    body = unique(example("openai-request.valid-non-pattern-name.json"))
    decision = await sdk.client.decisions.create(**body, extra_headers={"x-aghoo-include": "ensemble"})
    assert [a.model_dump() for a in decision.answers] == example("openai-response.valid-non-pattern-name.json")[
        "answers"
    ]
    assert set((decision.model_extra or {})["x_aghoo"]["calibration"]) == {"Severity Level", "regression"}


async def test_ensemble_header_links_the_native_decision(stack: Stack) -> None:
    sdk = facade(stack)
    decision = await sdk.client.decisions.create(
        **unique(example("openai-request.valid.json")), extra_headers={"x-aghoo-include": "ensemble"}
    )
    x_aghoo = (decision.model_extra or {})["x_aghoo"]
    assert set(x_aghoo["calibration"]) == {"q0", "regression"} and "mapped_unavailable" not in x_aghoo
    stored = await stack.client(1).request("GET", f"/v1/decisions/{x_aghoo['decision_id']}", expect=200)
    assert stored.json()["provenance"]["profile_id"] == "router-default"
    assert [a.model_dump() for a in decision.answers] == example(OK)["answers"]


MIXED: Any = [
    {"type": "predicate", "name": "flag", "instructions": "Is this a regression of the export?"},
    {"type": "predicate", "name": "gone", "instructions": f"{DOWN}?"},
]


@pytest.mark.parametrize("ensemble", [False, True], ids=["plain", "ensemble"])
async def test_unavailable_answer_maps_to_refusal(stack: Stack, ensemble: bool) -> None:
    sdk = facade(stack)
    headers = {"x-aghoo-include": "ensemble"} if ensemble else {}
    body = unique({"model": "aghoo/router-default", "input": "Export fails.", "questions": MIXED})
    decision = await sdk.client.decisions.create(**body, extra_headers=headers)
    flag, gone = decision.answers
    assert flag.type == "predicate" and gone.model_dump() == {"type": "refusal", "name": "gone"}
    extra = decision.model_extra or {}
    if ensemble:
        assert set(extra["x_aghoo"]["mapped_unavailable"]) == {"gone"}
        assert set(extra["x_aghoo"]["calibration"]) == {"flag"}
    else:
        assert "x_aghoo" not in extra


@pytest.mark.parametrize(
    ("model", "echo"),
    [("aghoo/lightweight-model-decide", "aghoo/lightweight-model-decide@1"), ("aghoo/router-default@1", None)],
)
async def test_profile_ids_and_input_forms(stack: Stack, model: str, echo: str | None) -> None:
    sdk = facade(stack)
    pixel = example("openai-request.invalid-21-images.json")["input"][0]["content"][1]
    forms: list[Any] = [
        "Export fails in Safari.",
        [{"role": "user", "content": [{"type": "input_text", "text": "Export fails."}, pixel]}],
    ]
    for form in forms:
        body = unique({"model": model, "input": form, "questions": MIXED[:1]})
        decision = await sdk.client.decisions.create(**body)
        assert decision.model == (echo or model) and decision.answers[0].type == "predicate"
    assert [s for s, _ in sdk.seen] == [200, 200]


ERRORS = [
    ("model_not_available", {"model": "router-default", "input": "x", "questions": MIXED[:1]}, 400),
    ("profile_not_allowed", {"model": "aghoo/answer-verifier", "input": "x", "questions": MIXED[:1]}, 403),
    ("remote_image_url", example("openai-request.invalid-remote-image-url.json"), 400),
    ("21_images", example("openai-request.invalid-21-images.json"), 400),
    ("256_choices", example("openai-request.invalid-256-choices.json"), 400),
    ("bool_string_collision", example("openai-request.invalid-bool-string-collision.json"), 400),
]
CLASSES = {400: openai.BadRequestError, 401: openai.AuthenticationError, 403: openai.PermissionDeniedError}


@pytest.mark.parametrize(("name", "body", "status"), ERRORS, ids=[e[0] for e in ERRORS])
async def test_typed_errors_surface_as_sdk_exceptions(stack: Stack, name: str, body: Any, status: int) -> None:
    sdk = facade(stack)
    with pytest.raises(CLASSES[status]) as raised:
        await sdk.client.decisions.create(**body)
    expected = name if name in ("model_not_available", "profile_not_allowed") else "schema_violation"
    assert (raised.value.status_code, raised.value.code) == (status, expected)
    assert sdk.seen == [(status, expected)]  # no retry on a 4xx


async def test_invalid_key_is_an_authentication_error(stack: Stack) -> None:
    sdk = facade(stack, key="ak_test_" + "x" * 40)
    with pytest.raises(openai.AuthenticationError) as raised:
        await sdk.client.decisions.create(model="aghoo/router-default", input="x", questions=MIXED[:1])
    assert (raised.value.status_code, raised.value.code) == (401, "invalid_api_key")
