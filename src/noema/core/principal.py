"""The authenticated caller and tenant references (spec-02 §1, spec-09 §3, IMPL-083)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, get_args

Role = Literal["tenant_admin", "developer", "reviewer", "end_user", "auditor"]
Scope = Literal["turns", "decisions", "sessions", "files.read", "files.write", "usage.read", "admin.read"]

ALL_ROLES: Final[frozenset[Role]] = frozenset(get_args(Role))
ALL_SCOPES: Final[frozenset[Scope]] = frozenset(get_args(Scope))


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is calling. `project_id` is "" for a tenant-wide system principal; `key_id` is None for system
    principals."""

    tenant_id: str
    project_id: str
    principal_id: str
    key_id: str | None
    roles: frozenset[Role]
    scopes: frozenset[Scope]
    request_id: str


def system_principal(tenant_id: str, *, purpose: str, request_id: str) -> Principal:
    """The principal of a job acting inside one tenant (labeling, sweeper, session.delete).

    principal_id `f"system:{purpose}"`, project_id "" (tenant-wide), no key, role tenant_admin and every scope.
    Admission skips the key and project buckets for it (spec-11 §1.2).
    """
    if not tenant_id:
        raise ValueError("tenant_id must be non-empty")
    return Principal(
        tenant_id=tenant_id,
        project_id="",
        principal_id=f"system:{purpose}",
        key_id=None,
        roles=frozenset({"tenant_admin"}),
        scopes=ALL_SCOPES,
        request_id=request_id,
    )


@dataclass(frozen=True, slots=True)
class TenantRef:
    """The tenant facts every model call needs (from `tenants.<id>` in the pinned config snapshot)."""

    tenant_id: str
    aws_account_id: str
    home_region: str
