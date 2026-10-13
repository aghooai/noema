"""DecisionSet (03 §9.2): wire questions from annotated fields, typed answers, undeclared values rejected."""

from __future__ import annotations

import copy
import json
from typing import Literal

import httpx
import pytest
import respx
from pydantic import ValidationError

from aghoo import (
    Aghoo,
    Choice,
    ChoiceAnswer,
    DecisionResult,
    DecisionSet,
    JevDecisionResponse,
    Option,
    Predicate,
    PredicateAnswer,
    Score,
    ScoreAnswer,
    UnavailableAnswer,
    UnknownAnswer,
    choice,
    noul,
    predicate,
    score,
)
from support import example


class Routing(DecisionSet):
    department: Choice[Literal["billing", "technical", "shipping", "other"]] = choice(
        "Which team should handle this message?",
        {"billing": "Payments, refunds", "technical": "Bugs", "shipping": None, "other": None},
    )
    needs_human: Predicate = predicate("The customer explicitly asks for a human.", criteria={"true": "Asks"})
    urgency: Score[Literal["low", "medium", "high"]] = score("How urgent is the request?")


def test_wire_questions_follow_field_order_and_literal_values() -> None:
    assert Routing.questions() == [
        {
            "type": "choice",
            "name": "department",
            "instructions": "Which team should handle this message?",
            "choices": [
                {"value": "billing", "description": "Payments, refunds"},
                {"value": "technical", "description": "Bugs"},
                {"value": "shipping", "description": None},
                {"value": "other", "description": None},
            ],
        },
        {
            "type": "predicate",
            "name": "needs_human",
            "instructions": "The customer explicitly asks for a human.",
            "criteria": {"true": "Asks"},
        },
        {
            "type": "score",
            "name": "urgency",
            "instructions": "How urgent is the request?",
            "levels": [
                {"label": "low", "description": None},
                {"label": "medium", "description": None},
                {"label": "high", "description": None},
            ],
        },
    ]


def test_groups_and_noul() -> None:
    class Sizes(DecisionSet):
        size: Choice[str] = choice("Which size?", [Option("10", group="small"), Option("2", group="small"), "xl"])
        human: Predicate = noul("Asks for a human.")

    assert Sizes.questions()[0]["choices"] == [  # type: ignore[typeddict-item]
        {"value": "10", "description": None, "group": "small"},
        {"value": "2", "description": None, "group": "small"},
        {"value": "xl", "description": None},
    ]
    assert Sizes.questions()[1] == {"type": "noul", "name": "human", "instructions": "Asks for a human."}


def test_class_definition_errors() -> None:
    with pytest.raises(TypeError, match="differ from the declared options"):

        class _Mismatch(DecisionSet):
            d: Choice[Literal["a", "b"]] = choice("?", ["a", "c"])

    with pytest.raises(TypeError, match="needs a Choice"):

        class _WrongKind(DecisionSet):
            d: Predicate = choice("?", ["a", "b"])

    with pytest.raises(TypeError, match="give the options"):

        class _NoOptions(DecisionSet):
            d: Choice[str] = choice("?")

    with pytest.raises(TypeError, match="declares no questions"):

        class _Empty(DecisionSet):
            pass


def test_from_result_types_each_answer_and_rejects_undeclared_values() -> None:
    raw = example("decision-result.valid")
    r = Routing.from_result(DecisionResult.model_validate(raw), raw)
    assert isinstance(r.department, ChoiceAnswer)
    assert r.department.choice in ("billing", "technical", "shipping", "other")
    assert isinstance(r.needs_human, PredicateAnswer)
    assert isinstance(r.urgency, ScoreAnswer)
    assert r.result.id == raw["id"]
    assert "department=" in repr(r)

    class Narrow(DecisionSet):
        department: Choice[Literal["billing", "technical"]] = choice("?")
        needs_human: Predicate = predicate("?")
        urgency: Score[Literal["low", "medium", "high"]] = score("?")

    with pytest.raises(ValidationError):
        Narrow.from_result(DecisionResult.model_validate(raw), raw)


def test_refusal_unavailable_and_unknown_answers_and_a_missing_answer() -> None:
    raw = copy.deepcopy(example("decision-result.valid"))
    raw["answers"][0] = {"type": "unavailable", "name": "department", "reason": "no_quorum"}
    raw["answers"][1] = {"type": "ranking", "name": "needs_human"}
    raw["status"] = "partial"
    r = Routing.from_result(DecisionResult.model_validate(raw), raw)
    assert isinstance(r.department, UnavailableAnswer)
    assert isinstance(r.needs_human, UnknownAnswer)
    del raw["answers"][2]
    with pytest.raises(ValueError, match="no answer named 'urgency'"):
        Routing.from_result(DecisionResult.model_validate(raw), raw)


def test_create_with_a_decision_set_a_question_list_and_jev(mock: respx.MockRouter, client: Aghoo) -> None:
    request = example("decision-request.valid")
    route = mock.post("/v1/decisions").mock(return_value=httpx.Response(200, json=example("decision-result.valid")))
    typed = client.decisions.create(Routing, profile=request["profile"], input=request["input"])
    assert isinstance(typed, Routing)
    sent = json.loads(route.calls[0].request.content)
    assert sent == {"profile": request["profile"], "input": request["input"], "questions": Routing.questions()}

    plain = client.decisions.create(request["questions"], input=request["input"])
    assert isinstance(plain, DecisionResult)
    assert json.loads(route.calls[1].request.content)["questions"] == request["questions"]

    route.mock(return_value=httpx.Response(200, json=example("decision-jev-response.valid")))
    jev_request = example("decision-request.valid-jev")
    jev = client.decisions.create(jev_request["questions"], output_format="jev", input=jev_request["input"])
    assert isinstance(jev, JevDecisionResponse)
    assert json.loads(route.calls[2].request.content)["output_format"] == "jev"
