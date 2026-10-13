"""The SDK's copies stay identical to the contract, and the shared fixtures are contract-valid responses."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from aghoo import _decision_types as types
from support import REPO, fixture_json

VENDOR = REPO / "src/noema/contracts/_vendor"
FIXTURES = sorted((REPO / "sdk/fixtures").glob("*.json"))


def test_decision_types_are_a_byte_copy_of_the_vendored_contract() -> None:
    sdk_copy = Path(types.__file__).read_bytes()
    assert sdk_copy == (VENDOR / "python_sdk_types.py").read_bytes()


@pytest.mark.parametrize("path", sorted(VENDOR.glob("examples/decision-result.*.json")), ids=lambda p: p.stem)
def test_tolerant_decision_models_accept_valid_and_reject_model_level_invalid_examples(path: Path) -> None:
    kind = path.stem.removeprefix("decision-result.")
    expect_accept = kind.startswith("valid") or kind.removeprefix("invalid-") not in types.MODEL_LEVEL_INVALID
    try:
        types.DecisionResult.model_validate_json(path.read_text())
        accepted = True
    except ValidationError:
        accepted = False
    assert accepted is expect_accept


def test_tolerant_models_ignore_unknown_fields_and_keep_unknown_answer_types() -> None:
    body = json.loads((VENDOR / "examples/decision-result.valid.json").read_text())
    body["added_later"] = 1
    body["answers"].append({"type": "ranking", "name": "order", "order": [1, 2]})
    result = types.DecisionResult.model_validate(body)
    unknown = result.answers[-1]
    assert isinstance(unknown, types.UnknownAnswer)
    assert (unknown.raw_type, unknown.name, unknown.raw["order"]) == ("ranking", "order", [1, 2])


def _body(fx: dict[str, Any]) -> bytes:
    if "frames" in fx:
        return b"".join(
            f"id: {f['id']}\nevent: {f['event']}\ndata: {json.dumps(f['data'], separators=(',', ':'))}\n\n".encode()
            for f in fx["frames"]
        )
    return json.dumps(fx["body"]).encode()


@pytest.fixture(scope="module")
def validator() -> Any:
    validate = pytest.importorskip("noema.contracts.validate", reason="needs the Noema repo's contract validator")
    return validate.ContractValidator()


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_shared_fixture_is_a_contract_valid_response(path: Path, validator: Any) -> None:
    fx = fixture_json(path.stem)
    validator.check_response(path.name.split(".")[0], fx["status"], fx["headers"], _body(fx))


def test_the_validator_rejects_a_drifted_fixture(validator: Any) -> None:
    fx = copy.deepcopy(fixture_json("getSession.200"))
    fx["body"]["last_seq"] = "4"
    with pytest.raises(AssertionError, match="violates the schema"):
        validator.check_response("getSession", 200, fx["headers"], _body(fx))
