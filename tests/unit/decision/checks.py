"""Contract checks for decision results in tests: the strict schema plus the vendored invariants."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from noema.contracts.validate import ContractValidator
from noema.core.protocols.decision import DecisionRequest

_VALIDATOR = ContractValidator()


def wire_request(req: DecisionRequest, value: Any = "x") -> dict[str, Any]:
    """The request JSON `result_invariants` pairs a result with."""
    return {"questions": [dict(q.wire) for q in req.questions.questions], "input": value, "include": ["members"]}


def assert_valid(result: Mapping[str, Any], request: Mapping[str, Any]) -> None:
    """Schema-valid `DecisionResult` that passes `check_examples.result_invariants`."""
    _VALIDATOR.validate_schema("getDecision", 200, "application/json", dict(result))
    _VALIDATOR.check_decision_result(result, request)


def assert_jev(result: Mapping[str, Any], request: Mapping[str, Any]) -> None:
    """Schema-valid `JevDecisionResponse` that passes `jev_invariants`."""
    _VALIDATOR.validate_schema("createDecision", 200, "application/json", dict(result))
    _VALIDATOR._invariants("jev_invariants", "createDecision", result, request)


def assert_oa(result: Mapping[str, Any], request: Mapping[str, Any]) -> None:
    """Schema-valid `OADecisionResponse` that passes `oa_invariants`."""
    _VALIDATOR.validate_schema("createDecisionOpenAICompat", 200, "application/json", dict(result))
    _VALIDATOR._invariants("oa_invariants", "createDecisionOpenAICompat", result, request)
