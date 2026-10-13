"""Platform dependency guarantees (PLAN Wave B, spec-02 §4.2): botocore >= 1.43 with Converse outputConfig."""

from __future__ import annotations

import importlib.metadata

import botocore.model
import botocore.session
import pytest

pytestmark = pytest.mark.unit


def _version(dist: str) -> tuple[int, ...]:
    return tuple(int(p) for p in importlib.metadata.version(dist).split(".")[:3])


def test_botocore_is_at_least_1_43() -> None:
    assert _version("botocore") >= (1, 43, 0)
    assert importlib.metadata.version("aiobotocore") == "3.9.2"


@pytest.mark.parametrize("operation", ["Converse", "ConverseStream"])
def test_converse_input_shape_has_output_config_effort(operation: str) -> None:
    model = botocore.session.get_session().get_service_model("bedrock-runtime")
    shape = model.operation_model(operation).input_shape
    assert shape is not None
    assert "outputConfig" in shape.members
    output_config = shape.members["outputConfig"]
    assert isinstance(output_config, botocore.model.StructureShape)
    assert "effort" in output_config.members


def test_pinned_generators() -> None:
    assert importlib.metadata.version("datamodel-code-generator") == "0.83.0"
    assert _version("pydantic") >= (2, 12, 0)
