"""ContractValidator (IMPL-041 point 4, IMPL-039): responses, error envelopes, decision invariants and SSE."""

from __future__ import annotations

import json
from typing import Any

import orjson
import pytest

from noema.contracts import vendor
from noema.contracts.bundle import VENDOR_DIR
from noema.contracts.validate import ContractValidator, ContractViolation
from noema.core.errors import ErrorCode, ForbiddenInEnvironment, NoemaError

pytestmark = pytest.mark.unit
EXAMPLES = VENDOR_DIR / "examples"
JSON_HEADERS = {"x-request-id": "req_1", "content-type": "application/json"}
SSE_HEADERS = {"x-request-id": "req_1", "content-type": "text/event-stream"}


@pytest.fixture(scope="module")
def validator() -> ContractValidator:
    return ContractValidator()


def _load(name: str) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((EXAMPLES / name).read_text())
    return doc


def _response_cases() -> list[tuple[str, str, str]]:
    """(operation, response example, paired request example), pairing exactly like check_examples.check."""
    pairs = vendor.constant("check_examples", "PAIRS")
    jev_pairs = vendor.constant("check_examples", "JEV_PAIRS")
    oa_pairs = vendor.constant("check_examples", "OA_PAIRS")
    cases = []
    for path in sorted(EXAMPLES.glob("*.json")):
        n = path.name
        if n.startswith("decision-result"):
            cases.append(("createDecision", n, pairs.get(n, "decision-request.valid.json")))
        elif n.startswith("decision-jev-response"):
            cases.append(("createDecision", n, jev_pairs.get(n, vendor.constant("check_examples", "JEV_REQUEST"))))
        elif n.startswith("openai-response"):
            cases.append(("createDecisionOpenAICompat", n, oa_pairs.get(n, "openai-request.valid.json")))
    return cases


@pytest.mark.parametrize(("operation", "response", "request_name"), _response_cases())
def test_vendored_response_examples(
    validator: ContractValidator, operation: str, response: str, request_name: str
) -> None:
    body = (EXAMPLES / response).read_bytes()
    request = _load(request_name)
    if ".valid" in response:
        validator.check_response(operation, 200, JSON_HEADERS, body, request)
    else:
        with pytest.raises(ContractViolation):
            validator.check_response(operation, 200, JSON_HEADERS, body, request)


def test_valid_result_without_its_request_is_schema_checked_only(validator: ContractValidator) -> None:
    body = (EXAMPLES / "decision-result.invalid-score-not-expected-index.json").read_bytes()
    validator.check_response("getDecision", 200, JSON_HEADERS, (EXAMPLES / "decision-result.valid.json").read_bytes())
    with pytest.raises(ContractViolation):  # the tolerant client models also enforce the invariant
        validator.check_response("getDecision", 200, JSON_HEADERS, body)


def test_parameterized_bands_change_the_verdict() -> None:
    body, request = (EXAMPLES / "decision-result.valid.json").read_bytes(), _load("decision-request.valid.json")
    ContractValidator().check_response("createDecision", 200, JSON_HEADERS, body, request)
    strict = ContractValidator(bands={"high_gte": 0.99, "low_lt": 0.98})
    with pytest.raises(ContractViolation, match="result_invariants"):
        strict.check_response("createDecision", 200, JSON_HEADERS, body, request)


def _error(code: ErrorCode, request_id: str = "req_1") -> bytes:
    return orjson.dumps(NoemaError(code, "x").envelope(request_id))


def test_error_envelopes(validator: ContractValidator) -> None:
    validator.check_response("getDecision", 404, JSON_HEADERS, _error(ErrorCode.NOT_FOUND))
    validator.check_response("createTurn", 409, JSON_HEADERS, _error(ErrorCode.SESSION_BUSY))
    cases = [
        ("getDecision", 404, JSON_HEADERS, _error(ErrorCode.SESSION_NOT_FOUND), "x-noema-error-codes"),
        ("getDecision", 500, JSON_HEADERS, _error(ErrorCode.NOT_FOUND), "must be HTTP 404"),
        ("getDecision", 404, JSON_HEADERS, _error(ErrorCode.NOT_FOUND, "other"), "request_id"),
        ("getDecision", 404, {"content-type": "application/json"}, _error(ErrorCode.NOT_FOUND), "x-request-id"),
        ("getDecision", 418, JSON_HEADERS, _error(ErrorCode.NOT_FOUND), "not declared"),
        (
            "getDecision",
            404,
            {**JSON_HEADERS, "content-type": "text/html"},
            _error(ErrorCode.NOT_FOUND),
            "content type",
        ),
        ("getDecision", 404, JSON_HEADERS, b"{", "not valid JSON"),
        ("getDecision", 404, JSON_HEADERS, b'{"error": {}}', "schema"),
    ]
    for operation, status, headers, body, match in cases:
        with pytest.raises(ContractViolation, match=match):
            validator.check_response(operation, status, headers, body)
    tampered = json.loads(_error(ErrorCode.NOT_FOUND))
    tampered["error"]["retryable"] = True
    with pytest.raises(ContractViolation, match="retryable"):
        validator.check_response("getDecision", 404, JSON_HEADERS, orjson.dumps(tampered))
    with pytest.raises(ContractViolation, match="unknown operation"):
        validator.check_response("noSuchOperation", 200, JSON_HEADERS, b"{}")


def _frame(frame_id: int | None, event: dict[str, Any]) -> bytes:
    head = b"" if frame_id is None else b"id: %d\n" % frame_id
    return head + b"event: " + str(event["type"]).encode() + b"\ndata: " + orjson.dumps(event) + b"\n\n"


CREATED = {"type": "turn.created", "turn_id": "turn_1", "session_id": None, "created_at": 1}
DELTA = {"type": "generation.delta", "attempt_id": "att_1", "seq": 1, "text": "Hi"}
TOO_SLOW = NoemaError(ErrorCode.CLIENT_TOO_SLOW, "Client too slow.").sse_event()


def test_sse_stream(validator: ContractValidator) -> None:
    stream = _frame(1, CREATED) + b": ping\n\n" + _frame(2, DELTA) + _frame(None, TOO_SLOW)
    events = validator.check_sse_stream("createTurn", stream)
    assert [(e.id, e.event) for e in events] == [("1", "turn.created"), ("2", "generation.delta"), (None, "error")]
    validator.check_response("createTurn", 200, SSE_HEADERS, stream.replace(b"\n", b"\r\n"))


@pytest.mark.parametrize(
    ("stream", "match"),
    [
        (_frame(2, CREATED), "out of order"),
        (_frame(1, CREATED) + _frame(1, DELTA), "out of order"),
        (_frame(None, CREATED), "has no id"),
        (b"id: 1\nevent: generation.delta\ndata: " + orjson.dumps(CREATED) + b"\n\n", "differs from data.type"),
        (_frame(1, {**DELTA, "extra": 1}), "violates the schema"),
        (b"id: x\nevent: turn.created\ndata: " + orjson.dumps(CREATED) + b"\n\n", "not a positive integer"),
        (b"id: 1\nevent: turn.created\ndata: {\n\n", "not valid JSON"),
        (b"id: 1\nevent: turn.created\ndata: {}\ndata: {}\n\n", "exactly one"),
        (b"id: 1\nretry: 5\nevent: turn.created\ndata: {}\n\n", "malformed"),
    ],
)
def test_sse_violations(validator: ContractValidator, stream: bytes, match: str) -> None:
    with pytest.raises(ContractViolation, match=match):
        validator.check_sse_stream("createTurn", stream)


def test_file_stream(validator: ContractValidator) -> None:
    file_object = {
        "id": "file_1",
        "filename": "a.pdf",
        "media_type": "application/pdf",
        "bytes": 10,
        "sha256": "0" * 64,
        "status": "ready",
        "derivatives": [],
    }
    frame = b"id: 1\nevent: file.updated\ndata: " + orjson.dumps(file_object) + b"\n\n"
    validator.check_sse_stream("watchFile", frame)
    with pytest.raises(ContractViolation, match=r"file\.updated"):
        validator.check_sse_stream("watchFile", frame.replace(b"file.updated", b"file.changed"))


def test_decision_frames_are_checked_against_the_request(validator: ContractValidator) -> None:
    result = _load("decision-result.valid.json")
    request = _load("decision-request.valid.json")
    event = {"type": "decision.completed", "decision": result, "arrived": "before_generation"}
    validator.check_sse_frame("createTurn", "3", "decision.completed", orjson.dumps(event), decision_request=request)
    broken = _load("decision-result.invalid-choice-not-argmax.json")
    bad_event = {**event, "decision": broken}
    validator.check_sse_frame("createTurn", "3", "decision.completed", orjson.dumps(bad_event))  # schema only
    with pytest.raises(ContractViolation, match="result_invariants"):
        validator.check_sse_frame(
            "createTurn", "3", "decision.completed", orjson.dumps(bad_event), decision_request=request
        )


def test_a_turn_stream_is_not_paired_with_the_turn_body(validator: ContractValidator) -> None:
    # createTurn's body (TurnCreateParams) has no `questions`: pairing it with a decision frame raised KeyError
    turn_body: dict[str, Any] = {"input": "Where is my order?", "decisions": {"profile": "router-default"}}
    result = _load("decision-result.valid.json")
    decision = {"type": "decision.completed", "decision": result, "arrived": "before_generation"}
    stream = _frame(1, CREATED) + _frame(2, decision)
    validator.check_response("createTurn", 200, SSE_HEADERS, stream, turn_body)
    broken = {**decision, "decision": {**result, "object": "nope"}}
    with pytest.raises(ContractViolation, match="violates the schema"):  # the frames are still schema-checked
        validator.check_response("createTurn", 200, SSE_HEADERS, _frame(1, CREATED) + _frame(2, broken), turn_body)


def test_non_json_derivative_content_is_accepted(validator: ContractValidator) -> None:
    validator.check_response(
        "getFileDerivative", 200, {"x-request-id": "r", "content-type": "image/png"}, b"\x89PNG\r\n"
    )


def test_refused_in_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOEMA_ENV", "prod")
    with pytest.raises(ForbiddenInEnvironment):
        ContractValidator()
