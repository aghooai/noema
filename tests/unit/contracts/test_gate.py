"""RequestGate (IMPL-041 point 1, IMPL-064, IMPL-065) and the semantic request rules (point 3)."""

from __future__ import annotations

import json

import orjson
import pytest

from noema.contracts.bundle import VENDOR_DIR
from noema.contracts.gate import RequestGate, media_type, param_path
from noema.contracts.rules import check_decision_request, check_openai_request
from noema.core.errors import ErrorCode, NoemaError
from noema.core.values import JsonValue

pytestmark = pytest.mark.unit
EXAMPLES = VENDOR_DIR / "examples"


@pytest.fixture(scope="module")
def gate() -> RequestGate:
    return RequestGate.from_bundle()


def _raises(code: ErrorCode, param: str | None = None) -> pytest.RaisesExc[NoemaError]:
    return pytest.raises(NoemaError, check=lambda e: e.code is code and (param is None or e.param == param))


def test_param_path() -> None:
    assert param_path(["questions", 1, "choices"]) == "questions[1].choices"
    assert param_path([0, "a"]) == "[0].a"
    assert param_path([]) == ""


def test_media_type() -> None:
    assert media_type("Application/JSON; charset=utf-8") == "application/json"
    assert media_type(None) is None
    assert media_type(" ; x") is None


def test_params_are_coerced_and_defaulted(gate: RequestGate) -> None:
    out = gate.check_params(
        "getUsage",
        path={},
        query={"limit": ["10"], "from": ["2026-10-01"]},
        headers={"AGHOO-VERSION": "2026-10-01", "X-Unknown": "ignored", "x-request-id": "anything goes"},
    )
    assert out == {
        "group_by": "line",
        "from": "2026-10-01",
        "limit": 10,
        "Aghoo-Version": "2026-10-01",
        "x-request-id": "anything goes",
    }


@pytest.mark.parametrize(
    ("query", "headers", "param"),
    [
        ({"bogus": ["1"]}, {}, "bogus"),
        ({"limit": ["0"]}, {}, "limit"),
        ({"limit": ["201"]}, {}, "limit"),
        ({"limit": ["1.5"]}, {}, "limit"),
        ({"limit": ["1", "2"]}, {}, "limit"),
        ({"from": ["2026-13-01"]}, {}, "from"),
        ({"group_by": ["week"]}, {}, "group_by"),
        ({}, {"Aghoo-Version": "2025-01-01"}, "Aghoo-Version"),
    ],
)
def test_params_rejected_with_400_and_param(
    gate: RequestGate, query: dict[str, list[str]], headers: dict[str, str], param: str
) -> None:
    with _raises(ErrorCode.SCHEMA_VIOLATION, param):
        gate.check_params("getUsage", path={}, query=query, headers=headers)


def test_header_params(gate: RequestGate) -> None:
    out = gate.check_params("createDecision", path={}, query={}, headers={"aghoo-deadline-ms": "250"})
    assert out["Aghoo-Deadline-Ms"] == 250
    assert gate.check_params("createDecision", path={}, query={}, headers={})["Aghoo-Deadline-Ms"] == 120000
    with _raises(ErrorCode.SCHEMA_VIOLATION, "Aghoo-Deadline-Ms"):
        gate.check_params("createDecision", path={}, query={}, headers={"Aghoo-Deadline-Ms": "99"})
    with _raises(ErrorCode.SCHEMA_VIOLATION, "Idempotency-Key"):
        gate.check_params("createDecision", path={}, query={}, headers={"Idempotency-Key": "has space"})


def test_path_params(gate: RequestGate) -> None:
    out = gate.check_params(
        "getFileDerivative", path={"file_id": "file_1", "kind": "page_images"}, query={"page": ["3"]}, headers={}
    )
    assert (out["kind"], out["page"]) == ("page_images", 3)
    with _raises(ErrorCode.SCHEMA_VIOLATION, "kind"):  # raster_1568 is internal (IMPL-065)
        gate.check_params("getFileDerivative", path={"file_id": "file_1", "kind": "raster_1568"}, query={}, headers={})
    with _raises(ErrorCode.SCHEMA_VIOLATION, "session_id"):
        gate.check_params("getSession", path={"session_id": "turn_1"}, query={}, headers={})
    with _raises(ErrorCode.SCHEMA_VIOLATION, "turn_id"):
        gate.check_params("getTurn", path={}, query={}, headers={})


def test_valid_decision_body(gate: RequestGate) -> None:
    body = (EXAMPLES / "decision-request.valid.json").read_bytes()
    assert gate.check_body("createDecision", "application/json; charset=utf-8", body) == json.loads(body)


def test_body_error_order(gate: RequestGate) -> None:
    body = (EXAMPLES / "decision-request.valid.json").read_bytes()
    with _raises(ErrorCode.REQUEST_TOO_LARGE):
        gate.check_body("createDecision", "text/plain", body, max_bytes=len(body) - 1)  # 413 before 415
    with _raises(ErrorCode.UNSUPPORTED_MEDIA_TYPE):
        gate.check_body("createDecision", "text/plain", b"{")  # 415 before decoding
    with _raises(ErrorCode.SCHEMA_VIOLATION, None):
        gate.check_body("createDecision", "application/json", b"{")
    with _raises(ErrorCode.SCHEMA_VIOLATION, None):
        gate.check_body("createDecision", "application/json", b"")
    big = RequestGate.from_bundle(max_json_body_bytes=10)
    with _raises(ErrorCode.REQUEST_TOO_LARGE):
        big.check_body("createDecision", "application/json", body)


def test_body_schema_error_param_is_the_first_error_path(gate: RequestGate) -> None:
    req = json.loads((EXAMPLES / "decision-request.valid.json").read_bytes())
    req["questions"][0] = {"type": "choice", "name": "q", "instructions": "x"}
    with _raises(ErrorCode.SCHEMA_VIOLATION, "questions[0]"):
        gate.check_body("createDecision", "application/json", orjson.dumps(req))


@pytest.mark.parametrize(
    ("operation", "prefix"),
    [("createDecision", "decision-request"), ("createDecisionOpenAICompat", "openai-request")],
)
def test_vendored_request_examples(gate: RequestGate, operation: str, prefix: str) -> None:
    """Valid examples pass the gate and the semantic rules; every invalid one is rejected by one of them."""
    paths = sorted(EXAMPLES.glob(f"{prefix}.*.json"))
    assert paths
    for path in paths:
        try:
            body = gate.check_body(operation, "application/json", path.read_bytes())
            assert isinstance(body, dict)
            (check_decision_request if operation == "createDecision" else check_openai_request)(body)
        except NoemaError as exc:
            assert ".invalid" in path.name, f"{path.name} rejected: {exc.message}"
            assert exc.code is ErrorCode.SCHEMA_VIOLATION
        else:
            assert ".valid" in path.name, f"{path.name} accepted"


def test_semantic_rule_params() -> None:
    req = json.loads((EXAMPLES / "decision-request.invalid-duplicate-choices.json").read_bytes())
    with _raises(ErrorCode.SCHEMA_VIOLATION) as info:
        check_decision_request(req, param_prefix="decisions.")
    assert str(info.value.param).startswith("decisions.questions")


def test_semantic_rule_messages_never_echo_client_values() -> None:
    req = json.loads((EXAMPLES / "decision-request.invalid-duplicate-choices.json").read_bytes())
    name = "zz_client_chosen_name"
    req["questions"][0]["name"] = name
    with _raises(ErrorCode.SCHEMA_VIOLATION) as info:
        check_decision_request(req)
    assert name not in info.value.message and info.value.message == "Choice values / level labels must be unique"
    facade: dict[str, JsonValue] = {
        "model": "m",
        "input": "x",
        "questions": [{"name": "q1", "type": "predicate", "instructions": "i"}],
    }
    with _raises(ErrorCode.SCHEMA_VIOLATION) as reserved:
        check_openai_request(facade)
    assert "q1" not in reserved.value.message and reserved.value.message.startswith("This name is reserved")


def test_operations_without_a_body_and_multipart(gate: RequestGate) -> None:
    assert gate.check_body("cancelTurn", "application/json", b"{}") is None
    assert gate.check_body("getTurn", None, b"") is None
    assert gate.check_content_type("uploadFile", "multipart/form-data; boundary=x") == "multipart/form-data"
    with _raises(ErrorCode.UNSUPPORTED_MEDIA_TYPE):
        gate.check_content_type("uploadFile", "application/json")
    with pytest.raises(TypeError, match="check_content_type"):
        gate.check_body("uploadFile", "multipart/form-data; boundary=x", b"--x--")
    assert set(gate.operations) >= {"createTurn", "uploadFile", "healthz"}
    assert gate.operation("createTurn").path == "/v1/turns"
