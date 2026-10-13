"""Gate Q1 on the stack: schemathesis over every operation of the bundled OpenAPI, with real auth (fuzzkit).

Every generated case (schemathesis' examples, coverage and fuzzing phases; positive and negative data) is sent to
api-1 with the credentials of a principal the operation allows. A case with path parameters or id fields in its
body (`file_id`, `session_id`, `retry_of`) is sent a second time with the ids of real resources (resources.py; a
fresh session or file for the two deletes), so the handlers behind the 404 are exercised too.
Pass criterion per response (fuzzkit.GATED): no 5xx but resumeTurnEvents' 501, a declared status, content type
and required headers, a schema-valid body (schemathesis and the ContractValidator). Failures are collected per
operation (not raised at the first, so one run lists them all) and fail that operation's test.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from typing import Any

import orjson
import pytest
from hypothesis import HealthCheck, Phase, settings
from schemathesis import Response
from tests.qa.contract.fuzzkit import GATED, SCHEMA, Case, Tally, auth_headers
from tests.qa.contract.resources import REPORT_DIR, Real, make_real, realize

pytestmark = [pytest.mark.e2e, pytest.mark.slow, pytest.mark.timeout(1800)]
EXAMPLES = int(os.environ.get("NOEMA_QA_FUZZ_EXAMPLES", "50"))
TALLY = Tally()
FAILURES: dict[str, list[str]] = {}
END_USER = "x-aghoo-end-user"
AUTHFAIL_PAUSE_S = 0.15
IDEMPOTENCY = "Idempotency-Key"


@pytest.fixture(scope="module")
def real(sync_stack: dict[str, Any]) -> Iterator[Real]:
    created = make_real(sync_stack)
    yield created
    created.http.close()


@pytest.fixture(autouse=True)
def operation_failures(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fails an operation's test after all of its examples ran, with every failure seen."""
    yield
    found = FAILURES.get(request.node.nodeid, [])
    assert not found, f"{len(found)} contract failure(s):\n" + "\n".join(found[:20])


def _validate(nodeid: str, op_id: str, case: Case, response: Response, headers: dict[str, str]) -> None:
    TALLY.add(op_id, response)
    if response.status_code == 401:  # keep this host's per-IP auth-failure bucket (10/s, burst 50) from draining
        time.sleep(AUTHFAIL_PAUSE_S)
    try:
        case.validate_response(response, checks=GATED, headers=headers)
    except (Exception, BaseExceptionGroup) as exc:  # schemathesis' FailureGroup is a BaseExceptionGroup
        detail = str(exc).strip().splitlines()
        curl = case.as_curl_command(headers={"Authorization": "Bearer <key>"})
        FAILURES.setdefault(nodeid, []).append(f"- {op_id} {response.status_code}: {' | '.join(detail[:12])}\n  {curl}")


@SCHEMA.parametrize()
@settings(
    max_examples=EXAMPLES,
    deadline=None,
    derandomize=True,
    database=None,
    phases=[Phase.explicit, Phase.generate],
    suppress_health_check=list(HealthCheck),
)
def test_every_operation_on_the_stack(
    case: Case, sync_stack: dict[str, Any], real: Real, request: pytest.FixtureRequest
) -> None:
    op_id = str(case.operation.definition.raw["operationId"])
    base = sync_stack["endpoints"]["api"][0]
    headers = auth_headers(sync_stack, op_id)
    if case.headers and END_USER in case.headers:  # Phase 1: 422 feature_not_available before anything else
        del case.headers[END_USER]  # (asserted once per operation by test_end_user_header_is_phase_2)
    response = case.call(base_url=base, headers=headers)
    _validate(request.node.nodeid, op_id, case, response, headers)
    names = [p.name for p in case.operation.path_parameters]
    if case.method.upper() != case.operation.method.upper():
        return
    realized = realize(case.body, real.ids) if isinstance(case.body, dict) else case.body
    if not names and realized == case.body:
        return
    ids = {name: real.ids[name] for name in names}
    if op_id == "deleteSession":
        ids["session_id"] = real.session()
    elif op_id == "deleteFile":
        ids["file_id"] = real.file(wait=False)
    case.path_parameters = {**(case.path_parameters or {}), **ids}
    case.body = realized
    if case.headers and IDEMPOTENCY in case.headers:  # the same key with another body is 409 idempotency_key_reused
        case.headers[IDEMPOTENCY] = f"{case.headers[IDEMPOTENCY]}-real"
    response = case.call(base_url=base, headers=headers)
    _validate(request.node.nodeid, op_id, case, response, headers)


def test_zz_tally() -> None:
    """Writes what every operation answered (reports/tmp, for the track report); nothing to assert here."""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    lines = TALLY.lines()
    (REPORT_DIR / "schemathesis-stateless.txt").write_text("\n".join(lines) + "\n")
    (REPORT_DIR / "schemathesis-stateless-failures.json").write_bytes(
        orjson.dumps(FAILURES, option=orjson.OPT_INDENT_2)
    )
    print("\n".join(lines))
