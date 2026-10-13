"""Authenticator + Authorizer conformance (spec-02 §11, spec-09 §2-§3): authn hygiene, the matrix rows
(scope -> role -> visibility -> write rule) with their exact codes, and `list_scope`."""

from __future__ import annotations

import pytest

from noema.core.errors import ErrorCode, NoemaError
from noema.core.principal import ALL_SCOPES
from noema.core.protocols.auth import Action, ResourceRef

from .kits import AuthKit, principal
from .registry import kit_fixture

kit = kit_fixture("auth")
DEV = principal(principal_id="prn_dev", roles=("developer",))
PEER = principal(principal_id="prn_peer", roles=("developer",))
EU = principal(principal_id="prn_eu", roles=("end_user",))
REV = principal(principal_id="prn_rev", roles=("reviewer",))
AUD = principal(principal_id="prn_aud", roles=("auditor",), project_id="p2")
ADMIN = principal(principal_id="prn_admin", roles=("tenant_admin",))
ADMIN_P2 = principal(principal_id="prn_admin2", roles=("tenant_admin",), project_id="p2")


def res(
    kind: str, owner: str | None = "prn_dev", *, project: str = "p1", session_owner: str | None = None
) -> ResourceRef:
    return ResourceRef(kind, f"{kind}_1", project, owner, session_owner)  # type: ignore[arg-type]


CASES: list[tuple[str, object, Action, ResourceRef | None, ErrorCode | None]] = [
    ("auditor cannot create turns", AUD, "turn.create", None, ErrorCode.PERMISSION_DENIED),
    ("reviewer turn read is 404", REV, "turn.read", res("turn"), ErrorCode.NOT_FOUND),
    ("creator reads own turn", DEV, "turn.read", res("turn"), None),
    ("peer turn is invisible", PEER, "turn.read", res("turn"), ErrorCode.NOT_FOUND),
    ("session owner sees the turn", PEER, "turn.read", res("turn", session_owner="prn_peer"), None),
    ("tenant admin reads any project", ADMIN_P2, "turn.read", res("turn"), None),
    ("auditor reads turns tenant-wide", AUD, "turn.read", res("turn"), None),
    (
        "visible but not creator: cancel is 404",
        PEER,
        "turn.cancel",
        res("turn", session_owner="prn_peer"),
        ErrorCode.NOT_FOUND,
    ),
    ("tenant admin cancels", ADMIN, "turn.cancel", res("turn"), None),
    ("reviewer session read", REV, "session.read", res("session"), ErrorCode.SESSION_NOT_FOUND),
    ("non-owner session read", PEER, "session.read", res("session"), ErrorCode.SESSION_NOT_FOUND),
    ("owner updates", DEV, "session.update", res("session"), None),
    ("admin updates any session", ADMIN, "session.update", res("session"), None),
    ("auditor cannot delete", AUD, "session.delete", res("session"), ErrorCode.PERMISSION_DENIED),
    ("turn on own session", DEV, "turn.create", res("session"), None),
    ("turn on foreign session", PEER, "turn.create", res("session"), ErrorCode.SESSION_NOT_FOUND),
    ("admin, same project", ADMIN, "turn.create", res("session"), None),
    ("admin, other project", ADMIN_P2, "turn.create", res("session"), ErrorCode.SESSION_FORBIDDEN),
    ("decision, admin other project", ADMIN_P2, "decision.create", res("session"), ErrorCode.PERMISSION_DENIED),
    ("end user reads own decision", EU, "decision.read", res("decision", "prn_eu"), None),
    ("end user, other's decision", EU, "decision.read", res("decision"), ErrorCode.NOT_FOUND),
    ("developer, project decision", PEER, "decision.read", res("decision"), None),
    ("reviewer, project decision", REV, "decision.read", res("decision"), None),
    ("reviewer, other project", REV, "decision.read", res("decision", project="p2"), ErrorCode.NOT_FOUND),
    ("uploader deletes", DEV, "file.delete", res("file"), None),
    (
        "session owner cannot delete the file",
        PEER,
        "file.delete",
        res("file", session_owner="prn_peer"),
        ErrorCode.PERMISSION_DENIED,
    ),
    ("end user cannot read usage", EU, "usage.read", None, ErrorCode.PERMISSION_DENIED),
    ("auditor cannot read dropped items", AUD, "admin.labeling.dropped.read", None, ErrorCode.PERMISSION_DENIED),
    ("auditor reads a file", AUD, "file.read", res("file", project="p2"), None),
    ("auditor cannot watch a file", AUD, "file.watch", res("file", project="p2"), ErrorCode.NOT_FOUND),
    ("auditor cannot read derivatives", AUD, "file.derivative.read", None, ErrorCode.NOT_FOUND),
    ("uploader watches", DEV, "file.watch", res("file"), None),
    ("session owner reads derivatives", PEER, "file.derivative.read", res("file", session_owner="prn_peer"), None),
    ("reviewer reads dropped items", REV, "admin.labeling.dropped.read", None, None),
    ("profiles need no scope", principal(scopes=frozenset()), "profile.list", None, None),
    ("scope gate", principal(scopes=ALL_SCOPES - {"turns"}), "turn.create", None, ErrorCode.PERMISSION_DENIED),
]


@pytest.mark.parametrize(("name", "who", "action", "resource", "code"), CASES, ids=[c[0] for c in CASES])
async def test_matrix(
    kit: AuthKit, name: str, who: object, action: Action, resource: ResourceRef | None, code: ErrorCode | None
) -> None:
    if code is None:
        kit.authz.check(who, action, resource)  # type: ignore[arg-type]
        return
    with pytest.raises(NoemaError) as err:
        kit.authz.check(who, action, resource)  # type: ignore[arg-type]
    assert err.value.code is code, name


async def test_list_scopes(kit: AuthKit) -> None:
    assert kit.authz.list_scope(DEV, "session.list") == "own"
    assert kit.authz.list_scope(AUD, "session.list") == "tenant"
    assert kit.authz.list_scope(REV, "session.list") == "none"
    assert kit.authz.list_scope(DEV, "usage.read") == "project"
    assert kit.authz.list_scope(ADMIN, "usage.read") == "tenant"
    assert kit.authz.list_scope(EU, "usage.read") == "none"


KEY_ID = "01J9ZQ7ABCDEFGHJKMNPQRSTVW"  # 26 Crockford base32 characters
SECRET = "Zm9vYmFyLXNlY3JldC1wYXJ0LW9mLWFuLWFwaS1rZXk"  # 43 base64url characters (IMPL-008)
TOKEN = f"ak_test_{KEY_ID}.{SECRET}"  # spec-09 §2 step 0: ^ak_(live|test)_<26>\.<43>$


async def test_authentication(kit: AuthKit) -> None:
    assert len(SECRET) == 43
    await kit.register_key(TOKEN, DEV)
    who = await kit.authn.authenticate({"Authorization": f"Bearer {TOKEN}", "x-request-id": "req_9"}, "127.0.0.1")
    assert who.principal_id == "prn_dev"
    wrong_secret = f"ak_test_{KEY_ID}.{SECRET[:-1]}A" if SECRET[-1] != "A" else f"ak_test_{KEY_ID}.{SECRET[:-1]}B"
    malformed = (
        f"ak_test_{KEY_ID}.secretpart",  # 10-character secret: fails the format check before any lookup
        f"ak_prod_{KEY_ID}.{SECRET}",  # unknown environment
        f"ak_test_{KEY_ID[:-1]}.{SECRET}",  # 25-character key id
        f"ak_test_{KEY_ID}.{SECRET}x",  # 44-character secret
    )
    for headers in (
        {},
        {"Authorization": "Bearer nope"},
        {"Authorization": TOKEN},  # no scheme
        {"Authorization": f"Bearer {wrong_secret}"},
        *({"Authorization": f"Bearer {bad}"} for bad in malformed),
    ):
        with pytest.raises(NoemaError) as err:
            await kit.authn.authenticate(headers, "127.0.0.1")
        assert err.value.code is ErrorCode.INVALID_API_KEY
        assert TOKEN not in err.value.message and SECRET not in repr(err.value)
