"""Schemathesis over the bundled OpenAPI against a running stack (contract track, gate Q1).

- `SCHEMA`: the committed bundle (`src/noema/contracts/build/openapi.bundled.yaml`), with whole SSE bodies kept
  (a turn stream has far more than schemathesis' default 20 events).
- `principal_for(op)`: real auth per operation: the bootstrap `developer` key of t_acme/main when the operation's
  `x-noema-roles` allow it, else `tenant_admin` (the admin views), else no credentials (healthz, readyz).
- `GATED`: the pass criteria: no 5xx (except resumeTurnEvents' 501, IMPL-036), the status declared, the content
  type and required headers declared, the body valid against the declared schema (schemathesis) AND the
  `ContractValidator` (the same checks plus ERROR_TABLE / x-noema-error-codes agreement, decision invariants, SSE
  frame by frame, the tolerant client models).
- `Tally`: what every call answered, per operation (printed at the end of a run, for the report).
"""

from __future__ import annotations

import collections
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

import orjson
import schemathesis
import yaml
from schemathesis import Case as _Case
from schemathesis import CheckContext, CheckFunction, Response
from schemathesis.checks import not_a_server_error
from schemathesis.specs.openapi.checks import (
    content_type_conformance,
    response_headers_conformance,
    response_schema_conformance,
    status_code_conformance,
)

from noema.api.middleware.routing import OperationTable
from noema.contracts.bundle import BUNDLE_PATH, default_bundle
from noema.contracts.validate import ContractValidator, ContractViolation
from noema.core.errors import ERROR_TABLE, ErrorCode

Case = _Case[Any]  # one generated request (schemathesis Case over an OpenAPI operation)
AGHOO_VERSION: Final = "2026-10-01"
SCHEMA: Final = schemathesis.openapi.from_dict(yaml.safe_load(BUNDLE_PATH.read_text()))
SCHEMA.config.max_stream_events = 100_000
SCHEMA.config.request_timeout = 90
OPS: Final = {op.operation_id: op for op in default_bundle().operations()}
TABLE: Final = OperationTable(default_bundle().operations())
VALIDATOR: Final = ContractValidator()
ERROR_SCHEMA: Final = default_bundle().validator("/components/schemas/Error")
ALLOWED_5XX: Final = {("resumeTurnEvents", 501)}  # Phase 1 answers 501 not_implemented (IMPL-036)


def principal_for(operation_id: str) -> str | None:
    roles = OPS[operation_id].roles
    if not OPS[operation_id].authenticated:
        return None
    return "developer" if "developer" in roles else "tenant_admin"


def auth_headers(state: Mapping[str, Any], operation_id: str) -> dict[str, str]:
    role = principal_for(operation_id)
    headers = {"Aghoo-Version": AGHOO_VERSION}
    if role is not None:
        key = state["tenants"]["t_acme"]["projects"]["main"]["principals"][role]["key"]
        headers["Authorization"] = f"Bearer {key}"
    return headers


def request_json(case: Case) -> Any:
    media = (case.media_type or "").split(";")[0]
    return case.body if media == "application/json" and isinstance(case.body, (dict, list)) else None


def contract_validator_check(ctx: CheckContext, response: Response, case: Case) -> bool | None:
    """The server's own ContractValidator on the received response (the request it pairs with is the case body)."""
    method, path = response.request.method or "", str(response.request.path_url).split("?")[0]
    headers = {k: v[0] for k, v in response.headers.items() if v}
    match = TABLE.match(method, path)
    if match is None:  # an undeclared method or path: IMPL-064 maps 405 to 404 not_found, with the Error envelope
        body = orjson.loads(response.content)
        assert response.status_code == 404 and ERROR_SCHEMA.is_valid(body), (method, path, response.status_code)
        assert body["error"]["code"] == "not_found" and body["error"]["request_id"] == headers.get("x-request-id")
        return True
    try:
        VALIDATOR.check_response(
            match.operation.operation_id, response.status_code, headers, response.content, request_json(case)
        )
    except ContractViolation as exc:
        raise AssertionError(f"ContractValidator: {exc}") from None
    return True


def no_unexpected_5xx(ctx: CheckContext, response: Response, case: Case) -> bool | None:
    op_id = case.operation.definition.raw.get("operationId", "")
    if response.status_code >= 500 and (op_id, response.status_code) not in ALLOWED_5XX:
        raise AssertionError(f"{op_id}: HTTP {response.status_code}: {response.content[:400]!r}")
    return True


def json_schema_conformance(ctx: CheckContext, response: Response, case: Case) -> bool | None:
    """schemathesis' response_schema_conformance for every body but SSE: schemathesis 4 reads a `text/event-stream`
    schema as the OpenAPI 3.2 event envelope ({data, event, id}), while the bundle (3.1) declares the `data` payload
    there (TurnEvent, FileObject). SSE frames are validated by contract_validator_check instead."""
    content_type = (response.headers.get("content-type") or [""])[0]
    if content_type.startswith("text/event-stream"):
        return None
    return response_schema_conformance(ctx, response, case)


GATED: Final[list[CheckFunction]] = [
    no_unexpected_5xx,
    status_code_conformance,
    content_type_conformance,
    response_headers_conformance,
    json_schema_conformance,
    contract_validator_check,
]
_ = not_a_server_error  # replaced by no_unexpected_5xx (the same rule with the declared Phase-1 501)


def error_code(response: Response) -> str | None:
    if response.status_code < 400 or not response.content.startswith(b"{"):
        return None
    try:
        code = orjson.loads(response.content).get("error", {}).get("code")
    except orjson.JSONDecodeError:
        return None
    return str(code) if code in {c.value for c in ErrorCode} and ErrorCode(code) in ERROR_TABLE else None


@dataclass
class Tally:
    """Per operation: Counter of (status, error code) over every call made."""

    seen: dict[str, collections.Counter[tuple[int, str | None]]] = field(
        default_factory=lambda: collections.defaultdict(collections.Counter)
    )

    def add(self, operation_id: str, response: Response) -> None:
        self.seen[operation_id][(response.status_code, error_code(response))] += 1

    def lines(self) -> list[str]:
        out = []
        for op_id in sorted(self.seen):
            calls = sum(self.seen[op_id].values())
            parts = ", ".join(
                f"{s}{'/' + c if c else ''} x{n}" for (s, c), n in sorted(self.seen[op_id].items(), key=str)
            )
            out.append(f"{op_id}: {calls} calls: {parts}")
        return out
