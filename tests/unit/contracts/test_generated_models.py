"""The strict generated server models (IMPL-041 point 2, spec-08 §10.4)."""

from __future__ import annotations

import json
from pathlib import Path

import pydantic
import pytest

from noema.contracts._base import StrictBase
from noema.contracts._generated import models
from noema.contracts.bundle import VENDOR_DIR

pytestmark = pytest.mark.unit
EXAMPLES = VENDOR_DIR / "examples"
MODEL_OF: dict[str, type[StrictBase]] = {
    "decision-result": models.DecisionResult,
    "decision-request": models.DecisionCreateParams,
    "decision-jev-response": models.JevDecisionResponse,
    "openai-request": models.OADecisionCreateParams,
    "openai-response": models.OADecisionResponse,
}


def test_strict_base_config() -> None:
    config = StrictBase.model_config
    assert config.get("extra") == "forbid"
    assert config.get("strict") is True
    assert config.get("regex_engine") == "python-re"


@pytest.mark.parametrize("path", sorted(EXAMPLES.glob("*.valid*.json")), ids=lambda p: p.name)
def test_every_valid_example_parses(path: Path) -> None:
    model = MODEL_OF[path.name.split(".")[0]]
    assert isinstance(model.model_validate_json(path.read_text()), StrictBase)


def test_unknown_fields_and_coercion_are_rejected() -> None:
    body = json.loads((EXAMPLES / "decision-result.valid.json").read_text())
    with pytest.raises(pydantic.ValidationError, match="extra"):
        models.DecisionResult.model_validate({**body, "unexpected": 1})
    envelope = {"type": "server_error", "code": "internal_error", "message": "m", "param": None, "request_id": "r"}
    with pytest.raises(pydantic.ValidationError):
        models.Error.model_validate({"error": {**envelope, "retryable": "true"}})
    models.Error.model_validate({"error": {**envelope, "retryable": True}})


def test_closed_enums_are_literals() -> None:
    with pytest.raises(pydantic.ValidationError):
        models.ErrorEvent.model_validate({"type": "error", "code": "no_such_code", "message": "m", "retryable": False})
