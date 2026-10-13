"""An independent oracle of the spec-09 §3 authorization matrix, written from the spec text (never imports
`noema.identity.authz`). It answers per operation and caller/resource relation: "ok", "403:<code>", "404:<code>" or
"empty" (a list read that yields an empty page)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# Operations table: operationId -> (scope or None, roles, role/scope failure answer)
OPS: Final[dict[str, tuple[str | None, frozenset[str], str]]] = {
    "createTurn": ("turns", frozenset({"developer", "end_user", "tenant_admin"}), "403:permission_denied"),
    "getTurn": ("turns", frozenset({"developer", "end_user", "tenant_admin", "auditor"}), "404:not_found"),
    "resumeTurnEvents": ("turns", frozenset({"developer", "end_user", "tenant_admin", "auditor"}), "404:not_found"),
    "cancelTurn": ("turns", frozenset({"developer", "end_user", "tenant_admin"}), "404:not_found"),
    "createDecision": ("decisions", frozenset({"developer", "end_user", "tenant_admin"}), "403:permission_denied"),
    "createDecisionOpenAICompat": (
        "decisions",
        frozenset({"developer", "end_user", "tenant_admin"}),
        "403:permission_denied",
    ),
    "getDecision": (
        "decisions",
        frozenset({"tenant_admin", "developer", "reviewer", "end_user", "auditor"}),
        "404:not_found",
    ),
    "listProfiles": (None, frozenset({"tenant_admin", "developer", "reviewer", "end_user", "auditor"}), "ok"),
    "createSession": ("sessions", frozenset({"developer", "end_user", "tenant_admin"}), "403:permission_denied"),
    "listSessions": ("sessions", frozenset({"developer", "end_user", "tenant_admin", "auditor"}), "empty"),
    "getSession": (
        "sessions",
        frozenset({"developer", "end_user", "tenant_admin", "auditor"}),
        "404:session_not_found",
    ),
    "listSessionTurns": (
        "sessions",
        frozenset({"developer", "end_user", "tenant_admin", "auditor"}),
        "404:session_not_found",
    ),
    "updateSession": ("sessions", frozenset({"developer", "end_user", "tenant_admin"}), "403:permission_denied"),
    "deleteSession": ("sessions", frozenset({"developer", "end_user", "tenant_admin"}), "403:permission_denied"),
    "uploadFile": ("files.write", frozenset({"developer", "end_user", "tenant_admin"}), "403:permission_denied"),
    "getFile": ("files.read", frozenset({"developer", "end_user", "tenant_admin", "auditor"}), "404:not_found"),
    "watchFile": ("files.read", frozenset({"developer", "end_user", "tenant_admin"}), "404:not_found"),
    "getFileDerivative": ("files.read", frozenset({"developer", "end_user", "tenant_admin"}), "404:not_found"),
    "deleteFile": ("files.write", frozenset({"developer", "end_user", "tenant_admin"}), "403:permission_denied"),
    "getUsage": ("usage.read", frozenset({"developer", "tenant_admin", "auditor"}), "403:permission_denied"),
    "listCalibrators": ("admin.read", frozenset({"tenant_admin", "reviewer", "auditor"}), "403:permission_denied"),
    "listLabelingRuns": ("admin.read", frozenset({"tenant_admin", "reviewer", "auditor"}), "403:permission_denied"),
    "listLabelingDropped": ("admin.read", frozenset({"tenant_admin", "reviewer"}), "403:permission_denied"),
}

# The resource each operation loads (None: no resource id).
RESOURCE: Final[dict[str, str | None]] = {
    "createTurn": "session",
    "getTurn": "turn",
    "resumeTurnEvents": "turn",
    "cancelTurn": "turn",
    "createDecision": "session",
    "createDecisionOpenAICompat": None,
    "getDecision": "decision",
    "listProfiles": None,
    "createSession": None,
    "listSessions": None,
    "getSession": "session",
    "listSessionTurns": "session",
    "updateSession": "session",
    "deleteSession": "session",
    "uploadFile": "session",
    "getFile": "file",
    "watchFile": "file",
    "getFileDerivative": "file",
    "deleteFile": "file",
    "getUsage": None,
    "listCalibrators": None,
    "listLabelingRuns": None,
    "listLabelingDropped": None,
}


@dataclass(frozen=True)
class Relation:
    """How the caller relates to the resource: it created/owns/uploaded it (`own`), owns its session
    (`session_owner`, turns and files), or neither; and whether it lives in the caller's project."""

    own: bool
    session_owner: bool
    same_project: bool


def _visible_for_role(role: str, op: str, kind: str, rel: Relation) -> bool:
    """Visibility V, one role at a time (spec-09 §3 table)."""
    if role == "tenant_admin":
        return True
    if role == "auditor":
        return not (kind == "file" and op in ("watchFile", "getFileDerivative"))
    if not rel.same_project:
        return False
    if kind == "session":
        return role in ("end_user", "developer") and rel.own
    if kind in ("turn", "file"):
        return role in ("end_user", "developer") and (rel.own or rel.session_owner)
    if kind == "decision":
        if role in ("developer", "reviewer"):
            return True
        return role == "end_user" and rel.own
    return False


def expected(op: str, roles: frozenset[str], scopes: frozenset[str], rel: Relation | None) -> str:
    """The oracle's answer for one call."""
    scope, allowed, failure = OPS[op]
    if (scope is not None and scope not in scopes) or not (roles & allowed):
        return failure
    kind = RESOURCE[op]
    if kind is None or rel is None:
        return "ok"
    if not any(_visible_for_role(r, op, kind, rel) for r in roles):
        return "404:session_not_found" if kind == "session" else "404:not_found"
    admin = "tenant_admin" in roles
    if op in ("createTurn", "createDecision", "uploadFile"):
        if rel.own or (admin and rel.same_project):
            return "ok"
        return "403:session_forbidden" if op == "createTurn" else "403:permission_denied"
    if op == "cancelTurn":
        return "ok" if rel.own or admin else "404:not_found"
    if op in ("updateSession", "deleteSession", "deleteFile"):
        return "ok" if rel.own or admin else "403:permission_denied"
    return "ok"
