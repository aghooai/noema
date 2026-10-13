"""Schemathesis smoke over every Phase-1 operation of the bundled OpenAPI against the stub app (spec-10 Q1).

Each generated case is sent without and with a valid key. Expected: typed 401 / 501 / 4xx envelopes that agree with
ERROR_TABLE, never a 5xx (readyz's stub 503 excepted), and no response that the in-app ContractValidator rejects.
"""

from __future__ import annotations

from typing import Any

import pytest
import schemathesis
import yaml
from hypothesis import HealthCheck, settings
from tests.unit.api.apifakes import DEV_KEY, make_app

from noema.contracts.bundle import BUNDLE_PATH, default_bundle
from noema.core.errors import ERROR_TABLE, ErrorCode

pytestmark = [pytest.mark.integration, pytest.mark.slow]

SCHEMA = schemathesis.openapi.from_dict(yaml.safe_load(BUNDLE_PATH.read_text()))
APP, _SERVICES = make_app()
ERROR_SCHEMA = default_bundle().validator("/components/schemas/Error")
OPERATIONS = {op.operation_id: op for op in default_bundle().operations()}
STEP3_CODES = {"request_too_large", "unsupported_media_type", "not_found"}  # decided before authentication
FILE_PART_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif", "image/heic", "image/heif", "application/pdf")


@schemathesis.serializer(*FILE_PART_TYPES, "text/markdown")
def _file_part(ctx: Any, value: Any) -> bytes:
    """uploadFile's `file` part, whose encoding now lists the accepted media types (finding 18): the generated
    value as raw bytes."""
    return value if isinstance(value, bytes) else str(value).encode()


def _check(case: Any, response: Any, *, authenticated: bool) -> None:
    op_id = case.operation.definition.raw["operationId"]
    status = response.status_code
    (request_id,) = response.headers["x-request-id"]  # schemathesis keeps every header value
    if case.method.upper() != case.operation.method.upper():  # coverage phase: an undeclared method
        assert (status, response.json()["error"]["code"]) == (404, "not_found")  # IMPL-064: 405 -> 404
        return
    if op_id == "healthz":
        assert status == 200
        return
    if op_id == "readyz":
        assert status == 503
        return
    assert status < 500 or status == 501, (op_id, status, response.text)
    body = response.json()
    assert ERROR_SCHEMA.is_valid(body), body
    error = body["error"]
    spec = ERROR_TABLE[ErrorCode(error["code"])]
    assert (spec.status, spec.type.value, spec.retryable) == (status, error["type"], error["retryable"])
    assert error["request_id"] == request_id
    if not authenticated:
        assert status == 401 or error["code"] in STEP3_CODES, (op_id, error)
        assert DEV_KEY not in response.text
    else:
        declared = OPERATIONS[op_id].error_codes
        assert error["code"] in {*declared, "not_found", "not_implemented", "schema_violation"}, (op_id, error)


@SCHEMA.parametrize()
@settings(max_examples=6, deadline=None, derandomize=True, suppress_health_check=list(HealthCheck))
def test_every_operation_answers_typed_errors_and_never_a_5xx(case: Any) -> None:
    for headers in ({}, {"Authorization": f"Bearer {DEV_KEY}"}):
        response = case.call(app=APP, headers=headers)
        _check(case, response, authenticated=bool(headers))
    assert APP.state.contract_violations == []
