"""ErrorCode / ERROR_TABLE / NoemaError against contract rev 6 (spec-08 §2, IMPL-030, IMPL-066)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jsonschema_rs
import pytest
import yaml

from noema.contracts.bundle import VENDOR_DIR, default_bundle
from noema.core.errors import (
    ERROR_TABLE,
    ClassifiedError,
    ErrorCode,
    ErrorKind,
    ErrorSpec,
    ErrorType,
    NoemaError,
    TenantAccessMisconfigured,
)

pytestmark = pytest.mark.unit

# spec-08 §2, transcribed row by row: code -> (HTTP status, error.type, retryable)
SPEC_08_TABLE = {
    "schema_violation": (400, "invalid_request_error", False),
    "unsupported_question_type": (400, "invalid_request_error", False),
    "reserved_question_name": (400, "invalid_request_error", False),
    "model_not_available": (400, "invalid_request_error", False),
    "invalid_api_key": (401, "authentication_error", False),
    "invalid_end_user_token": (401, "authentication_error", False),
    "permission_denied": (403, "permission_error", False),
    "profile_not_allowed": (403, "permission_error", False),
    "session_forbidden": (403, "permission_error", False),
    "not_found": (404, "not_found_error", False),
    "session_not_found": (404, "not_found_error", False),
    "idempotency_key_reused": (409, "conflict_error", False),
    "session_conflict": (409, "conflict_error", False),
    "session_busy": (409, "conflict_error", True),
    "file_not_ready": (409, "conflict_error", True),
    "file_rejected": (409, "conflict_error", False),
    "resume_window_expired": (410, "invalid_request_error", False),
    "request_too_large": (413, "invalid_request_error", False),
    "file_too_large": (413, "invalid_request_error", False),
    "image_too_large": (413, "invalid_request_error", False),
    "unsupported_media_type": (415, "invalid_request_error", False),
    "no_route_satisfies_constraints": (422, "capability_error", False),
    "no_capable_member": (422, "capability_error", False),
    "unsupported_parameter": (422, "capability_error", False),
    "feature_not_available": (422, "capability_error", False),
    "rpm_exceeded": (429, "rate_limit_error", True),
    "tpm_exceeded": (429, "rate_limit_error", True),
    "queue_timeout": (429, "rate_limit_error", True),
    "budget_exhausted": (429, "rate_limit_error", False),
    "internal_error": (500, "server_error", True),
    "not_implemented": (501, "invalid_request_error", False),
    "all_routes_unavailable": (503, "upstream_error", True),
    "tenant_access_misconfigured": (503, "upstream_error", False),
    "deadline_exceeded": (504, "upstream_error", True),
    "upstream_stalled": (None, "upstream_error", True),
    "client_too_slow": (None, "upstream_error", False),
}


def _openapi(path: Path) -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load(path.read_text())
    return doc


def test_error_code_enum_equals_vendored_openapi_enum_in_order() -> None:
    enum = _openapi(VENDOR_DIR / "openapi.yaml")["components"]["schemas"]["ErrorCode"]["enum"]
    assert [c.value for c in ErrorCode] == enum
    assert len(ErrorCode) == 36


def test_error_code_enum_equals_bundle_enum() -> None:
    assert [c.value for c in ErrorCode] == default_bundle().doc["components"]["schemas"]["ErrorCode"]["enum"]


def test_error_type_enum_equals_openapi() -> None:
    schema = _openapi(VENDOR_DIR / "openapi.yaml")["components"]["schemas"]["Error"]
    assert [t.value for t in ErrorType] == schema["properties"]["error"]["properties"]["type"]["enum"]


def test_error_table_is_exactly_spec_08() -> None:
    assert set(ERROR_TABLE) == set(ErrorCode)
    actual = {c.value: (s.status, s.type.value, s.retryable) for c, s in ERROR_TABLE.items()}
    assert actual == SPEC_08_TABLE


def test_error_table_is_read_only() -> None:
    with pytest.raises(TypeError):
        ERROR_TABLE[ErrorCode.NOT_FOUND] = ErrorSpec(400, ErrorType.SERVER, True)  # type: ignore[index]


def test_every_operation_code_is_known_and_its_status_is_declared() -> None:
    for op in default_bundle().operations():
        for code in op.error_codes:
            status = ERROR_TABLE[ErrorCode(code)].status
            if status is not None:
                assert str(status) in op.responses, f"{op.operation_id}: {code} needs a {status} response"


def _validator(schema_name: str) -> jsonschema_rs.Validator:
    return default_bundle().validator(f"/components/schemas/{schema_name}")


@pytest.mark.parametrize("code", [c for c in ErrorCode if ERROR_TABLE[c].status is not None])
def test_envelope_matches_the_error_schema(code: ErrorCode) -> None:
    err = NoemaError(code, "Something went wrong.", param="questions[0].name", retry_after_s=3)
    body = err.envelope("req_123")
    _validator("Error").validate(body)
    inner = body["error"]
    assert isinstance(inner, dict)
    assert inner["code"] == code.value
    assert inner["type"] == ERROR_TABLE[code].type.value
    assert inner["retryable"] is ERROR_TABLE[code].retryable
    assert inner["request_id"] == "req_123"
    assert inner["param"] == "questions[0].name"
    assert "details" not in inner
    assert err.retry_after_s == 3


def test_envelope_param_null_and_details() -> None:
    body = NoemaError(ErrorCode.MODEL_NOT_AVAILABLE, "Unknown model.", details={"allowed": ["a", "b"]}).envelope("r")
    _validator("Error").validate(body)
    assert body == {
        "error": {
            "type": "invalid_request_error",
            "code": "model_not_available",
            "message": "Unknown model.",
            "param": None,
            "request_id": "r",
            "retryable": False,
            "details": {"allowed": ["a", "b"]},
        }
    }


@pytest.mark.parametrize("code", list(ErrorCode))
def test_sse_event_matches_the_error_event_schema(code: ErrorCode) -> None:
    event = NoemaError(code, "x").sse_event()
    _validator("ErrorEvent").validate(event)
    assert event == {"type": "error", "code": code.value, "message": "x", "retryable": ERROR_TABLE[code].retryable}


def test_noema_error_accepts_code_strings_and_hides_the_message_in_repr() -> None:
    err = NoemaError("not_found", "secret-ish message")  # type: ignore[arg-type]
    assert err.code is ErrorCode.NOT_FOUND
    assert err.spec == ERROR_TABLE[ErrorCode.NOT_FOUND]
    assert "secret-ish" not in repr(err)
    with pytest.raises(ValueError, match="not a valid ErrorCode"):
        NoemaError("no_such_code", "x")  # type: ignore[arg-type]


def test_tenant_access_misconfigured() -> None:
    err = TenantAccessMisconfigured("account_mismatch")
    assert isinstance(err, NoemaError)
    assert err.code is ErrorCode.TENANT_ACCESS_MISCONFIGURED
    assert err.cause == "account_mismatch"
    assert err.details == {"cause": "account_mismatch"}
    assert err.spec.status == 503
    assert err.spec.retryable is False
    assert err.message == "Tenant access is misconfigured."


def test_error_kind_values() -> None:
    assert [k.value for k in ErrorKind] == ["retryable", "throttled", "fatal", "invalid_request", "tenant_access"]


def test_classified_error_cause_iff_tenant_access() -> None:
    ok = ClassifiedError(
        ErrorKind.TENANT_ACCESS, "denied", provider_code="AccessDeniedException", cause="access_denied"
    )
    assert ok.cause == "access_denied"
    assert ClassifiedError(ErrorKind.THROTTLED, "slow down", retry_after_s=1.5).retry_after_s == 1.5
    with pytest.raises(ValueError, match="cause"):
        ClassifiedError(ErrorKind.TENANT_ACCESS, "denied")
    with pytest.raises(ValueError, match="cause"):
        ClassifiedError(ErrorKind.FATAL, "boom", cause="access_denied")
