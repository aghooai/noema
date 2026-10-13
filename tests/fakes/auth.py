"""`FakeAuthenticator` and `FakeAuthorizer` (spec-02 §11, spec-09 §2-§3, IMPL-083).

The authorizer implements the spec-09 §3 matrix in its evaluation order: scope, role (the "role/scope failure"
column: 403 where the operation declares it, else its 404 code), visibility V of the loaded resource (404:
`session_not_found` for sessions, `not_found` otherwise), then the write rule on a visible resource (403
`session_forbidden` for createTurn, `permission_denied` elsewhere, except cancelTurn which answers 404). A principal
with several roles is allowed if any role allows; visibility is the union. The auditor may getFile (`file.read`)
but not watchFile (`file.watch`) or getFileDerivative (`file.derivative.read`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Final, Literal

import anyio.lowlevel

from noema.core.errors import ErrorCode, NoemaError
from noema.core.principal import Principal, Role, Scope
from noema.core.protocols.auth import Action, ListScope, ResourceRef

Failure = Literal["403", "404", "session_404", "empty", "none"]
_DEV_EU_TA: Final[frozenset[Role]] = frozenset({"developer", "end_user", "tenant_admin"})
_READERS: Final[frozenset[Role]] = _DEV_EU_TA | {"auditor"}
_ALL: Final[frozenset[Role]] = frozenset({"tenant_admin", "developer", "reviewer", "end_user", "auditor"})
_ADMIN: Final[frozenset[Role]] = frozenset({"tenant_admin", "reviewer", "auditor"})
TENANT_WIDE: Final[frozenset[Role]] = frozenset({"tenant_admin", "auditor"})


@dataclass(frozen=True, slots=True)
class Rule:
    """One spec-09 §3 operation row."""

    scope: Scope | None
    roles: frozenset[Role]
    failure: Failure


MATRIX: Final[Mapping[Action, Rule]] = {
    "turn.create": Rule("turns", _DEV_EU_TA, "403"),
    "turn.read": Rule("turns", _READERS, "404"),
    "turn.cancel": Rule("turns", _DEV_EU_TA, "404"),
    "decision.create": Rule("decisions", _DEV_EU_TA, "403"),
    "decision.read": Rule("decisions", _ALL, "404"),
    "profile.list": Rule(None, _ALL, "none"),
    "session.create": Rule("sessions", _DEV_EU_TA, "403"),
    "session.read": Rule("sessions", _READERS, "session_404"),
    "session.update": Rule("sessions", _DEV_EU_TA, "403"),
    "session.delete": Rule("sessions", _DEV_EU_TA, "403"),
    "session.list": Rule("sessions", _READERS, "empty"),
    "file.upload": Rule("files.write", _DEV_EU_TA, "403"),
    "file.read": Rule("files.read", _READERS, "404"),
    "file.watch": Rule("files.read", _DEV_EU_TA, "404"),
    "file.derivative.read": Rule("files.read", _DEV_EU_TA, "404"),
    "file.delete": Rule("files.write", _DEV_EU_TA, "403"),
    "usage.read": Rule("usage.read", frozenset({"developer", "tenant_admin", "auditor"}), "403"),
    "admin.calibrators.read": Rule("admin.read", _ADMIN, "403"),
    "admin.labeling.read": Rule("admin.read", _ADMIN, "403"),
    "admin.labeling.dropped.read": Rule("admin.read", frozenset({"tenant_admin", "reviewer"}), "403"),
}


def _denied(failure: Failure) -> NoemaError:
    if failure == "session_404":
        return NoemaError(ErrorCode.SESSION_NOT_FOUND, "Session not found.")
    if failure == "404":
        return NoemaError(ErrorCode.NOT_FOUND, "Not found.")
    return NoemaError(ErrorCode.PERMISSION_DENIED, "Permission denied.")


def _not_visible(resource: ResourceRef) -> NoemaError:
    if resource.kind == "session":
        return NoemaError(ErrorCode.SESSION_NOT_FOUND, "Session not found.")
    return NoemaError(ErrorCode.NOT_FOUND, "Not found.")


def visible(principal: Principal, resource: ResourceRef) -> bool:
    """spec-09 §3 visibility V (other tenant: never; RLS project scope for non tenant-wide roles)."""
    roles = principal.roles
    wide = bool(roles & TENANT_WIDE)
    if resource.project_id is not None and not wide and resource.project_id != principal.project_id:
        return False
    me = principal.principal_id
    owner = resource.owner_principal_id == me
    session_owner = resource.session_owner_principal_id == me
    if resource.kind in ("usage", "admin"):
        return True
    if wide:
        return True
    if resource.kind == "session":
        return bool(roles & {"end_user", "developer"}) and owner
    if resource.kind in ("turn", "file"):
        return bool(roles & {"end_user", "developer"}) and (owner or session_owner)
    if resource.kind == "decision":
        return ("end_user" in roles and owner) or bool(roles & {"developer", "reviewer"})
    return False


class FakeAuthorizer:
    """Implements `Authorizer` (see the module docstring)."""

    def check(self, principal: Principal, action: Action, resource: ResourceRef | None = None) -> None:
        rule = MATRIX[action]
        if (rule.scope is not None and rule.scope not in principal.scopes) or not (principal.roles & rule.roles):
            raise _denied(rule.failure)
        if resource is None:
            return
        if not visible(principal, resource):
            raise _not_visible(resource)
        self._write_rule(principal, action, resource)

    def _write_rule(self, principal: Principal, action: Action, resource: ResourceRef) -> None:
        me = principal.principal_id
        admin = "tenant_admin" in principal.roles
        owner = resource.owner_principal_id == me
        if action in ("turn.create", "decision.create", "file.upload") and resource.kind == "session":
            if owner:
                return
            if admin and resource.project_id not in (None, principal.project_id):
                code = ErrorCode.SESSION_FORBIDDEN if action == "turn.create" else ErrorCode.PERMISSION_DENIED
                raise NoemaError(code, "Permission denied.")
            if not admin:
                raise _not_visible(resource)
        elif action == "turn.cancel" and not (owner or admin):
            raise NoemaError(ErrorCode.NOT_FOUND, "Not found.")
        elif action in ("session.update", "session.delete", "file.delete") and not (owner or admin):
            raise NoemaError(ErrorCode.PERMISSION_DENIED, "Permission denied.")

    def list_scope(self, principal: Principal, action: Action) -> ListScope:
        rule = MATRIX[action]
        if (rule.scope is not None and rule.scope not in principal.scopes) or not (principal.roles & rule.roles):
            return "none"
        if principal.roles & TENANT_WIDE or action.startswith("admin."):
            return "tenant"
        if action == "usage.read" or action == "decision.read":
            return "project"
        return "own"


class FakeAuthenticator:
    """Implements `Authenticator`: `Authorization: Bearer <token>` -> the registered principal (with the request's
    `x-request-id`); anything else -> 401 invalid_api_key, never echoing the key. `failures` counts 401s."""

    def __init__(self, keys: Mapping[str, Principal] | None = None) -> None:
        self._keys = dict(keys or {})
        self.failures = 0

    def register(self, token: str, principal: Principal) -> None:
        self._keys[token] = principal

    async def authenticate(self, headers: Mapping[str, str], client_ip: str) -> Principal:
        await anyio.lowlevel.checkpoint()
        lowered = {k.lower(): v for k, v in headers.items()}
        auth = lowered.get("authorization", "")
        scheme, _, token = auth.partition(" ")
        principal = self._keys.get(token.strip()) if scheme.lower() == "bearer" else None
        if principal is None:
            self.failures += 1
            raise NoemaError(ErrorCode.INVALID_API_KEY, "Invalid API key.")
        request_id = lowered.get("x-request-id")
        return replace(principal, request_id=request_id) if request_id else principal
