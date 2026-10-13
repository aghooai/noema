"""Authentication and authorization (spec-02 §11, spec-09 §2-§3, IMPL-083)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from noema.core.principal import Principal

Action = Literal[
    "turn.create",
    "turn.read",
    "turn.cancel",
    "decision.create",
    "decision.read",
    "profile.list",
    "session.create",
    "session.read",
    "session.update",
    "session.delete",
    "session.list",
    "file.upload",
    "file.read",  # getFile
    "file.watch",  # watchFile (not the auditor, spec-09 §3)
    "file.derivative.read",  # getFileDerivative (not the auditor)
    "file.delete",
    "usage.read",
    "admin.calibrators.read",
    "admin.labeling.read",
    "admin.labeling.dropped.read",
]
ListScope = Literal["none", "own", "project", "tenant"]


@dataclass(frozen=True, slots=True)
class ResourceRef:
    """The resource of an authz check, loaded by the route before `check()` (spec-09 §3 resource rules).
    `owner_principal_id`: the turn/decision creator, session owner or file uploader; `session_owner_principal_id`:
    for turns and files in a session."""

    kind: Literal["turn", "decision", "session", "file", "usage", "admin"]
    id: str | None
    project_id: str | None
    owner_principal_id: str | None
    session_owner_principal_id: str | None = None


class Authenticator(Protocol):
    async def authenticate(self, headers: Mapping[str, str], client_ip: str) -> Principal:
        """Raises `NoemaError(invalid_api_key)` (401, never echoing the key) and, while the per-IP auth-failure
        bucket of `client_ip` is empty, `NoemaError(rpm_exceeded)` (429 with retry_after_s) before any key
        verification (spec-09 §2; bucket `RedisKeys.gcra("authfail:" + client_ip)`, debited through `RateLimiter`)."""
        ...


class Authorizer(Protocol):
    def check(self, principal: Principal, action: Action, resource: ResourceRef | None = None) -> None:
        """Sync. Raises `NoemaError(permission_denied | not_found | session_not_found | session_forbidden)` per the
        spec-09 §3 matrix (403 only where the operation declares it; otherwise the 404 code)."""
        ...

    def list_scope(self, principal: Principal, action: Action) -> ListScope:
        """Sync, for list operations: "none" = role or scope failure (an empty page, or 403 where declared);
        "own" = owner_principal_id is the caller; "project"; "tenant"."""
        ...
