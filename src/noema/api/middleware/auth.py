"""Step 4: authentication, `Aghoo-Version` and `x-aghoo-end-user` (spec-04 §1, IMPL-008, IMPL-033).

Authentication is delegated to the `Authenticator` protocol (identity): it raises 401 `invalid_api_key` (never
echoing the key) or 429 `rpm_exceeded` while the client IP's auth-failure bucket is empty (spec-09 §2). The
`Principal` always carries this request's id. Then the `Aghoo-Version` header must be one of the operation's
declared versions (400 `schema_violation`, `param: Aghoo-Version`), and any `x-aghoo-end-user` header is 422
`feature_not_available` in Phase 1.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Final

from noema.contracts.bundle import RawOperation
from noema.core.errors import ErrorCode, NoemaError
from noema.core.principal import Principal
from noema.core.protocols.auth import Authenticator

VERSION_PARAM: Final = "Aghoo-Version"
END_USER_HEADER: Final = "x-aghoo-end-user"


async def authenticate(authn: Authenticator, headers: Mapping[str, str], client_ip: str, request_id: str) -> Principal:
    """The caller's `Principal`, with `request_id` set to this request's id. Errors come from `authn`."""
    principal = await authn.authenticate(headers, client_ip)
    if principal.request_id != request_id:
        principal = dataclasses.replace(principal, request_id=request_id)
    return principal


def check_version(op: RawOperation, headers: Mapping[str, str]) -> str | None:
    """The requested `Aghoo-Version` (the schema default when absent); None if the operation doesn't declare it.
    Raises `NoemaError(schema_violation, param="Aghoo-Version")` for any undeclared value (never echoed)."""
    for p in op.parameters:
        if p.location == "header" and p.name.lower() == VERSION_PARAM.lower():
            value = headers.get(VERSION_PARAM.lower())
            if value is None:
                default = p.schema.get("default")
                return default if isinstance(default, str) else None
            allowed = p.schema.get("enum")
            if isinstance(allowed, list) and value not in allowed:
                raise NoemaError(ErrorCode.SCHEMA_VIOLATION, "Unsupported Aghoo-Version.", param=VERSION_PARAM)
            return value
    return None


def check_end_user(headers: Mapping[str, str]) -> None:
    """Raises 422 `feature_not_available` (`param: x-aghoo-end-user`) when the header is present (IMPL-033: end-user
    tokens are deferred to Phase 2 for every project)."""
    if END_USER_HEADER in headers:
        raise NoemaError(
            ErrorCode.FEATURE_NOT_AVAILABLE, "End-user tokens are not available in this phase.", param=END_USER_HEADER
        )
