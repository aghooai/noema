"""The authorization matrix (spec-09 §3, IMPL-083): `MatrixAuthorizer` implements `Authorizer`.

Evaluation order (the first failing step decides):
1. scope: the action's scope is not in `principal.scopes`;
2. role: no role of the principal is in the action's role set. Steps 1-2 run before any lookup; their failure is
   the action's "role/scope failure": 403 `permission_denied` where the operation declares 403, else its 404 code
   (`not_found`, `session_not_found` for session reads); list actions answer an empty page (`list_scope` "none");
3. visibility V of the loaded resource (other tenants never reach here: the route loads under RLS): not visible ->
   404 (`session_not_found` for sessions, `not_found` otherwise). Visibility is the union over the roles;
4. the write rule on a visible resource: 403 (`session_forbidden` on createTurn, `permission_denied` elsewhere),
   except cancelTurn, which declares no 403 (a visible turn the caller didn't create -> 404 `not_found`).

Create actions take their referenced resources one by one: a session (owner, or a tenant_admin in its project),
and the file parts and `retry_of` turn of createTurn/createDecision (visibility only).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

from noema.core.errors import ErrorCode, NoemaError
from noema.core.principal import Principal, Role, Scope
from noema.core.protocols.auth import Action, ListScope, ResourceRef

Failure = Literal["permission_denied", "not_found", "session_not_found", "empty_page", "never"]

_WRITERS: Final[frozenset[Role]] = frozenset({"developer", "end_user", "tenant_admin"})
_READERS: Final[frozenset[Role]] = _WRITERS | {"auditor"}
_EVERYONE: Final[frozenset[Role]] = frozenset({"tenant_admin", "developer", "reviewer", "end_user", "auditor"})
_ADMIN_READERS: Final[frozenset[Role]] = frozenset({"tenant_admin", "reviewer", "auditor"})
_PERSONAL: Final[frozenset[Role]] = frozenset({"developer", "end_user"})
_NO_AUDITOR_FILE_ACTIONS: Final[frozenset[Action]] = frozenset({"file.watch", "file.derivative.read"})
_CREATE_ON_SESSION: Final[frozenset[Action]] = frozenset({"turn.create", "decision.create", "file.upload"})
_OWNER_OR_ADMIN_WRITES: Final[frozenset[Action]] = frozenset({"session.update", "session.delete", "file.delete"})


@dataclass(frozen=True, slots=True)
class Rule:
    """One spec-09 §3 row: the scope (None = any valid key), the roles and the role/scope failure."""

    scope: Scope | None
    roles: frozenset[Role]
    failure: Failure


MATRIX: Final[Mapping[Action, Rule]] = MappingProxyType(
    {
        "turn.create": Rule("turns", _WRITERS, "permission_denied"),
        "turn.read": Rule("turns", _READERS, "not_found"),
        "turn.cancel": Rule("turns", _WRITERS, "not_found"),
        "decision.create": Rule("decisions", _WRITERS, "permission_denied"),
        "decision.read": Rule("decisions", _EVERYONE, "not_found"),
        "profile.list": Rule(None, _EVERYONE, "never"),
        "session.create": Rule("sessions", _WRITERS, "permission_denied"),
        "session.read": Rule("sessions", _READERS, "session_not_found"),
        "session.update": Rule("sessions", _WRITERS, "permission_denied"),
        "session.delete": Rule("sessions", _WRITERS, "permission_denied"),
        "session.list": Rule("sessions", _READERS, "empty_page"),
        "file.upload": Rule("files.write", _WRITERS, "permission_denied"),
        "file.read": Rule("files.read", _READERS, "not_found"),
        "file.watch": Rule("files.read", _WRITERS, "not_found"),
        "file.derivative.read": Rule("files.read", _WRITERS, "not_found"),
        "file.delete": Rule("files.write", _WRITERS, "permission_denied"),
        "usage.read": Rule("usage.read", frozenset({"developer", "tenant_admin", "auditor"}), "permission_denied"),
        "admin.calibrators.read": Rule("admin.read", _ADMIN_READERS, "permission_denied"),
        "admin.labeling.read": Rule("admin.read", _ADMIN_READERS, "permission_denied"),
        "admin.labeling.dropped.read": Rule("admin.read", frozenset({"tenant_admin", "reviewer"}), "permission_denied"),
    }
)

# operationId -> Action (the source of `x-noema-roles`, IMPL-066; SEC-AZ-03 compares the two).
OPERATION_ACTIONS: Final[Mapping[str, Action]] = MappingProxyType(
    {
        "createTurn": "turn.create",
        "getTurn": "turn.read",
        "cancelTurn": "turn.cancel",
        "resumeTurnEvents": "turn.read",
        "createDecision": "decision.create",
        "getDecision": "decision.read",
        "createDecisionOpenAICompat": "decision.create",
        "listProfiles": "profile.list",
        "createSession": "session.create",
        "listSessions": "session.list",
        "getSession": "session.read",
        "updateSession": "session.update",
        "deleteSession": "session.delete",
        "listSessionTurns": "session.read",
        "uploadFile": "file.upload",
        "getFile": "file.read",
        "deleteFile": "file.delete",
        "watchFile": "file.watch",
        "getFileDerivative": "file.derivative.read",
        "getUsage": "usage.read",
        "listCalibrators": "admin.calibrators.read",
        "listLabelingRuns": "admin.labeling.read",
        "listLabelingDropped": "admin.labeling.dropped.read",
    }
)


def _error(code: ErrorCode) -> NoemaError:
    if code is ErrorCode.SESSION_NOT_FOUND:
        return NoemaError(code, "Session not found.")
    if code is ErrorCode.NOT_FOUND:
        return NoemaError(code, "Not found.")
    if code is ErrorCode.SESSION_FORBIDDEN:
        return NoemaError(code, "This session may not be used by the caller.")
    return NoemaError(code, "Permission denied.")


_FAILURE_CODES: Final[Mapping[Failure, ErrorCode]] = MappingProxyType(
    {
        "permission_denied": ErrorCode.PERMISSION_DENIED,
        "not_found": ErrorCode.NOT_FOUND,
        "session_not_found": ErrorCode.SESSION_NOT_FOUND,
        "empty_page": ErrorCode.PERMISSION_DENIED,  # list actions use list_scope; check() is a programming error
    }
)


def _permitted(principal: Principal, rule: Rule) -> bool:
    return (rule.scope is None or rule.scope in principal.scopes) and not principal.roles.isdisjoint(rule.roles)


def visible(principal: Principal, action: Action, resource: ResourceRef) -> bool:
    """spec-09 §3 visibility V (the union over the principal's roles). `usage` and `admin` resources are
    tenant-level: visible to every role that passed steps 1-2."""
    kind = resource.kind
    if kind in ("usage", "admin"):
        return True
    roles = principal.roles
    if "tenant_admin" in roles:
        return True
    if "auditor" in roles and not (kind == "file" and action in _NO_AUDITOR_FILE_ACTIONS):
        return True
    if resource.project_id is not None and resource.project_id != principal.project_id:
        return False
    me = principal.principal_id
    owner = resource.owner_principal_id == me
    if kind == "decision":
        return not roles.isdisjoint({"developer", "reviewer"}) or ("end_user" in roles and owner)
    if roles.isdisjoint(_PERSONAL):
        return False
    if kind == "session":
        return owner
    return owner or resource.session_owner_principal_id == me  # turn, file


def _write_rule(principal: Principal, action: Action, resource: ResourceRef) -> None:
    owner = resource.owner_principal_id == principal.principal_id
    admin = "tenant_admin" in principal.roles
    if action in _CREATE_ON_SESSION and resource.kind == "session":
        if owner or (admin and resource.project_id in (None, principal.project_id)):
            return
        raise _error(ErrorCode.SESSION_FORBIDDEN if action == "turn.create" else ErrorCode.PERMISSION_DENIED)
    if action == "turn.cancel" and not (owner or admin):
        raise _error(ErrorCode.NOT_FOUND)
    if action in _OWNER_OR_ADMIN_WRITES and not (owner or admin):
        raise _error(ErrorCode.PERMISSION_DENIED)


class MatrixAuthorizer:
    """Implements `Authorizer` with the spec-09 §3 matrix (see the module docstring). Stateless and sync."""

    def check(self, principal: Principal, action: Action, resource: ResourceRef | None = None) -> None:
        """Raises `NoemaError(permission_denied | not_found | session_not_found | session_forbidden)`."""
        rule = MATRIX[action]
        if not _permitted(principal, rule):
            if rule.failure == "never":
                return
            raise _error(_FAILURE_CODES[rule.failure])
        if resource is None:
            return
        if not visible(principal, action, resource):
            raise _error(ErrorCode.SESSION_NOT_FOUND if resource.kind == "session" else ErrorCode.NOT_FOUND)
        _write_rule(principal, action, resource)

    def list_scope(self, principal: Principal, action: Action) -> ListScope:
        """ "none" on a role/scope failure; "tenant" for tenant_admin, the auditor (except file watch/derivative
        reads), tenant-level admin reads and profiles; "project" for usage and decision reads of developers and
        reviewers; "own" otherwise."""
        rule = MATRIX[action]
        if not _permitted(principal, rule) and rule.failure != "never":
            return "none"
        roles = principal.roles
        if action == "profile.list" or action.startswith("admin.") or "tenant_admin" in roles:
            return "tenant"
        if "auditor" in roles and action not in _NO_AUDITOR_FILE_ACTIONS:
            return "tenant"
        if action == "usage.read" or (action == "decision.read" and not roles.isdisjoint({"developer", "reviewer"})):
            return "project"
        return "own"


def roles_for(action: Action) -> frozenset[Role]:
    """The roles column of an action (the `x-noema-roles` of its operations)."""
    return MATRIX[action].roles
