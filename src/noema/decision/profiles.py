"""Profile references, resolution, allowlists and the effective stage-0 parameters (spec-06 §2; IMPL-031,
IMPL-038, IMPL-069).

Native `/v1/decisions`: `profile ?? tenants.<t>.defaults.decision_profile`; allowlist `{default} ∪
request_overrides.decision_profiles`; an unknown or non-allowlisted id is 403 `profile_not_allowed` (the two
look the same); a stale pin is 422 `unsupported_parameter`. The facade takes `model: aghoo/<id>[@v]`: a
malformed model or a stale pin is 400 `model_not_available`.
"""

from __future__ import annotations

import re
from typing import Final

from noema.config.models import DecisionProfileCfg
from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.config import ConfigSnapshot
from noema.core.protocols.decision import DecisionInput, DecisionRequest, VerificationInput
from noema.core.values import JsonValue

NATIVE_REF: Final = re.compile(r"^(?P<id>[a-z0-9][a-z0-9._-]{0,63})(@(?P<ver>[0-9]+))?$")
FACADE_REF: Final = re.compile(r"^aghoo/(?P<id>[a-z0-9][a-z0-9._-]{0,63})(@(?P<ver>[0-9]+))?$")
VERIFY_TEMPLATE: Final = "verify-dist@1"


def allowlist(snapshot: ConfigSnapshot, tenant_id: str) -> list[str]:
    """`{defaults.decision_profile} ∪ request_overrides.decision_profiles`, sorted."""
    tenant = snapshot.tenant(tenant_id)
    allowed = {tenant.defaults.decision_profile}
    if tenant.request_overrides is not None:
        allowed |= set(tenant.request_overrides.decision_profiles)
    return sorted(allowed)


def _not_allowed(allowed: list[str]) -> NoemaError:
    details: dict[str, JsonValue] = {"allowed": list(allowed)}
    return NoemaError(
        ErrorCode.PROFILE_NOT_ALLOWED, "The decision profile is not available to this caller.", details=details
    )


def _lookup(snapshot: ConfigSnapshot, profile_id: str, allowed: list[str]) -> DecisionProfileCfg:
    if profile_id not in allowed:
        raise _not_allowed(allowed)
    try:
        return snapshot.decision_profile(profile_id)
    except NoemaError:
        raise _not_allowed(allowed) from None


def resolve_native(snapshot: ConfigSnapshot, tenant_id: str, requested: str | None) -> DecisionProfileCfg:
    """The profile of a `/v1/decisions` request (spec-06 §2.2)."""
    allowed = allowlist(snapshot, tenant_id)
    ref = requested if requested is not None else snapshot.tenant(tenant_id).defaults.decision_profile
    m = NATIVE_REF.match(ref)
    if m is None:  # the schema pattern already guarantees the form
        raise _not_allowed(allowed)
    profile = _lookup(snapshot, m.group("id"), allowed)
    if m.group("ver") is not None and int(m.group("ver")) != profile.version:
        raise NoemaError(
            ErrorCode.UNSUPPORTED_PARAMETER,
            "The pinned profile version is not the active one.",
            param="profile",
            details={"requested": int(m.group("ver")), "active": profile.version},
        )
    return profile


def resolve_facade(snapshot: ConfigSnapshot, tenant_id: str, model: str) -> DecisionProfileCfg:
    """The profile of a facade request: `aghoo/<id>[@v]` (spec-06 §2.1-§2.2)."""
    allowed = allowlist(snapshot, tenant_id)
    m = FACADE_REF.match(model)
    if m is None:
        raise NoemaError(
            ErrorCode.MODEL_NOT_AVAILABLE,
            "The model must be aghoo/<profile>[@version].",
            param="model",
            details={"allowed": [f"aghoo/{p}" for p in allowed]},
        )
    profile = _lookup(snapshot, m.group("id"), allowed)
    if m.group("ver") is not None and int(m.group("ver")) != profile.version:
        raise NoemaError(
            ErrorCode.MODEL_NOT_AVAILABLE,
            "The pinned profile version is not the active one.",
            param="model",
            details={"allowed": [f"aghoo/{p}" for p in allowed]},
        )
    return profile


def qmax(profile: DecisionProfileCfg) -> int:
    """Distinct model aliases among the non-optional members with weight > 0."""
    return len({m.model for m in profile.members if not m.optional and m.weight > 0})


def effective_quorum(profile: DecisionProfileCfg, requested: int | None, *, param: str = "min_quorum") -> int:
    """`requested ?? profile.min_quorum`, within `1..Qmax` (else 422 `unsupported_parameter`, `details.max`)."""
    q = requested if requested is not None else profile.min_quorum
    top = qmax(profile)
    if not 1 <= q <= top:
        raise NoemaError(
            ErrorCode.UNSUPPORTED_PARAMETER,
            "min_quorum exceeds the members that can answer.",
            param=param,
            details={"max": top},
        )
    return q


def check_family(profile: DecisionProfileCfg, value: DecisionInput, *, param: str = "profile") -> None:
    """A `verify-dist@1` profile accepts only a verification input, and no other profile does (422)."""
    verify = any(m.template == VERIFY_TEMPLATE for m in profile.members)
    if verify != isinstance(value, VerificationInput):
        raise NoemaError(
            ErrorCode.UNSUPPORTED_PARAMETER, "The profile does not accept this kind of input.", param=param
        )


def check_request(snapshot: ConfigSnapshot, req: DecisionRequest) -> DecisionProfileCfg:
    """The engine-side request checks of `run().__aenter__` (the routes run the same rules earlier)."""
    try:
        profile = snapshot.decision_profile(req.profile.id)
    except NoemaError:
        raise NoemaError(ErrorCode.PROFILE_NOT_ALLOWED, "The decision profile is not available.") from None
    if profile.version != req.profile.version:
        raise NoemaError(
            ErrorCode.UNSUPPORTED_PARAMETER,
            "The pinned profile version is not the active one.",
            param="profile",
            details={"requested": req.profile.version, "active": profile.version},
        )
    effective_quorum(profile, req.min_quorum)
    check_family(profile, req.input)
    return profile


_MODALITY_ORDER: Final = ("text", "image", "document")
_PHASE2_MODES: Final = frozenset({"best_of_n", "fuse"})


def _modalities(sets: list[frozenset[str]]) -> list[JsonValue]:
    union = set().union(*sets) if sets else set()
    return [m for m in _MODALITY_ORDER if m in union]


def decision_profile_info(snapshot: ConfigSnapshot, profile: DecisionProfileCfg) -> dict[str, JsonValue]:
    """`ProfileInfo` of a decision profile (spec-06 §12.4)."""
    aliases = [m.model for m in profile.members]
    n = len(aliases)
    return {
        "id": profile.id,
        "version": str(profile.version),
        "kind": "decision",
        "description": f"{n} member{'s' if n != 1 else ''}: {', '.join(aliases)}; {profile.aggregation}; "
        f"deadline {profile.deadline_ms} ms",
        "models": [snapshot.model(a).id for a in aliases],
        "modalities": _modalities([frozenset(snapshot.model(a).modalities) for a in aliases]),
    }


def list_profiles(snapshot: ConfigSnapshot, tenant_id: str, kind: str | None) -> list[JsonValue]:
    """`GET /v1/profiles`: the caller's allowlists only (decision: default ∪ overrides; generation: default ∪
    overrides without the Phase 2 modes), deduplicated and sorted by id within each kind."""
    tenant = snapshot.tenant(tenant_id)
    out: list[JsonValue] = []
    if kind in (None, "decision"):
        for pid in allowlist(snapshot, tenant_id):
            try:
                out.append(decision_profile_info(snapshot, snapshot.decision_profile(pid)))
            except NoemaError:
                continue  # an allowlisted id the config no longer has
    if kind in (None, "generation"):
        ids = {tenant.defaults.generation_profile} if tenant.defaults.generation_profile else set()
        if tenant.request_overrides is not None:
            ids |= set(tenant.request_overrides.generation_profiles)
        for gid in sorted(ids):
            try:
                g = snapshot.generation_profile(gid)
            except NoemaError:
                continue
            if g.mode in _PHASE2_MODES or not g.members:
                continue
            aliases = [m.model for m in g.members]
            out.append(
                {
                    "id": g.id,
                    "version": str(snapshot.version),
                    "kind": "generation",
                    "description": f"{g.mode}: {', '.join(aliases)}",
                    "models": [snapshot.model(a).id for a in aliases],
                    "mode": g.mode,
                    "modalities": _modalities([frozenset(snapshot.model(aliases[0]).modalities)]),
                    "streaming": "live",
                }
            )
    return out
