"""SEC-AZ-01 oracle: the expected outcome of one request, written from spec-09 §3 alone (it never imports
`noema.identity.authz`). Evaluation order: scope -> role -> visibility V -> write rule on a visible resource.

A cell is (operation, variant, caller role, relation of the resource to the caller):
- `own`: the caller created it (only the roles that can create: developer, end_user, tenant_admin);
- `peer`: another principal of the caller's project created it;
- `other_project`: a principal of another project of the caller's tenant created it;
- `other_tenant`: a principal of another tenant created it;
- `none`: the request names no resource.
Every principal holds every scope here (the bootstrap keys), so step 1 always passes; SEC-AZ-02 is the identity
module's (unit level).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

Role = Literal["end_user", "developer", "reviewer", "tenant_admin", "auditor"]
Relation = Literal["own", "peer", "other_project", "other_tenant", "none"]
Kind = Literal["session", "turn", "decision", "file", "file_stream"]

ROLES: Final[tuple[Role, ...]] = ("end_user", "developer", "reviewer", "tenant_admin", "auditor")
RELATIONS: Final[tuple[Relation, ...]] = ("own", "peer", "other_project", "other_tenant")
CREATORS: Final[frozenset[Role]] = frozenset({"developer", "end_user", "tenant_admin"})
WRITERS: Final = CREATORS
READERS: Final = CREATORS | {"auditor"}
ADMIN_READERS: Final = frozenset({"tenant_admin", "reviewer", "auditor"})


@dataclass(frozen=True)
class Rule:
    """One spec-09 §3 Operations row: the Roles column and the outcome of a role failure."""

    roles: frozenset[str]
    role_failure: Literal["403", "404", "empty", "none"]
    success: int


# operationId -> rule (spec-09 §3 "Operations"; healthz/readyz need no key and are checked separately).
RULES: Final[dict[str, Rule]] = {
    "createTurn": Rule(WRITERS, "403", 200),
    "getTurn": Rule(READERS, "404", 200),
    "resumeTurnEvents": Rule(READERS, "404", 501),  # visible -> 501 not_implemented (IMPL-036)
    "cancelTurn": Rule(WRITERS, "404", 202),
    "createDecision": Rule(WRITERS, "403", 200),
    "createDecisionOpenAICompat": Rule(WRITERS, "403", 200),
    "getDecision": Rule(frozenset(ROLES), "404", 200),
    "listProfiles": Rule(frozenset(ROLES), "none", 200),
    "createSession": Rule(WRITERS, "403", 201),
    "listSessions": Rule(READERS, "empty", 200),
    "getSession": Rule(READERS, "404", 200),
    "listSessionTurns": Rule(READERS, "404", 200),
    "updateSession": Rule(WRITERS, "403", 200),
    "deleteSession": Rule(WRITERS, "403", 202),
    "uploadFile": Rule(WRITERS, "403", 202),
    "getFile": Rule(READERS, "404", 200),
    "watchFile": Rule(WRITERS, "404", 200),  # the auditor has file.read only
    "getFileDerivative": Rule(WRITERS, "404", 200),
    "deleteFile": Rule(WRITERS, "403", 200),
    "getUsage": Rule(frozenset({"developer", "tenant_admin", "auditor"}), "403", 200),
    "listCalibrators": Rule(ADMIN_READERS, "403", 200),
    "listLabelingRuns": Rule(ADMIN_READERS, "403", 200),
    "listLabelingDropped": Rule(frozenset({"tenant_admin", "reviewer"}), "403", 200),
}


@dataclass(frozen=True)
class Expected:
    status: int
    code: str | None = None  # the error code for a 4xx/5xx (501: not_implemented)
    empty: bool = False  # a list read that must return an empty page

    @property
    def allowed(self) -> bool:
        return self.status < 400 and not self.empty


def visible(kind: Kind, role: Role, relation: Relation) -> bool:
    """V (spec-09 §3 "Visibility V"); in this matrix the creator of a turn or file also owns its session."""
    if relation == "other_tenant":
        return False
    if role in ("tenant_admin", "auditor"):
        return not (kind == "file_stream" and role == "auditor")
    if kind == "decision" and role in ("developer", "reviewer"):
        return relation in ("own", "peer")  # same project
    if role == "reviewer":
        return False  # no session, turn or file visibility
    return relation == "own"


def _not_found(kind: Kind) -> Expected:
    return Expected(404, "session_not_found" if kind == "session" else "not_found")


def expect(operation: str, role: Role, relation: Relation, kind: Kind | None = None, variant: str = "") -> Expected:
    """The outcome of `operation` (`variant` = which body field names the resource, e.g. `session_id`, `file`)
    for `role` against a resource of `kind` at `relation`."""
    rule = RULES[operation]
    ok = Expected(rule.success, "not_implemented" if rule.success == 501 else None)
    if role not in rule.roles:  # step 2, before any lookup
        if rule.role_failure == "403":
            return Expected(403, "permission_denied")
        if rule.role_failure == "empty":
            return Expected(200, empty=True)
        assert kind is not None, operation
        return _not_found(kind)
    if kind is None:
        return ok
    if not visible(kind, role, relation):  # step 3
        return _not_found(kind)
    # step 4: write rules on a visible resource
    if variant == "session_id":  # createTurn / createDecision / uploadFile naming a session
        if relation == "own" or (role == "tenant_admin" and relation == "peer"):
            return ok
        return Expected(403, "session_forbidden" if operation == "createTurn" else "permission_denied")
    if operation == "cancelTurn" and not (relation == "own" or role == "tenant_admin"):
        return Expected(404, "not_found")
    if operation in ("updateSession", "deleteSession", "deleteFile") and not (
        relation == "own" or role == "tenant_admin"
    ):
        return Expected(403, "permission_denied")
    return ok
