"""The spec-09 §3 matrix (SEC-AZ-01 unit level, SEC-AZ-02, SEC-AZ-03): `MatrixAuthorizer` against the independent
oracle for every operation x role set x scope set x caller/resource relation, plus hand-written golden cells."""

from __future__ import annotations

import itertools
from typing import get_args

import pytest
from tests.unit.identity.authz_oracle import OPS, RESOURCE, Relation, expected

from noema.contracts.bundle import default_bundle
from noema.core.errors import NoemaError
from noema.core.principal import ALL_SCOPES, Principal, Role, Scope
from noema.core.protocols.auth import Action, ResourceRef
from noema.identity.authz import MATRIX, OPERATION_ACTIONS, MatrixAuthorizer, roles_for

AUTHZ = MatrixAuthorizer()
ME = "prn_me"
ROLE_SETS: list[frozenset[Role]] = [frozenset({r}) for r in get_args(Role)] + [
    frozenset({"developer", "auditor"}),
    frozenset({"reviewer", "end_user"}),
    frozenset({"tenant_admin", "developer"}),
    frozenset({"auditor", "reviewer"}),
]
RELATIONS = [Relation(own, so, sp) for own, so, sp in itertools.product((True, False), repeat=3)]


def who(roles: frozenset[Role], scopes: frozenset[Scope] = ALL_SCOPES) -> Principal:
    return Principal("t_a", "p1", ME, "key_1", roles, scopes, "req_1")


def resource(op: str, rel: Relation) -> ResourceRef | None:
    kind = RESOURCE[op]
    if kind is None:
        return None
    return ResourceRef(
        kind,  # type: ignore[arg-type]
        f"{kind}_1",
        "p1" if rel.same_project else "p2",
        ME if rel.own else "prn_other",
        ME if rel.session_owner else "prn_other",
    )


def actual(op: str, principal: Principal, res: ResourceRef | None) -> str:
    action = OPERATION_ACTIONS[op]
    if OPS[op][2] == "empty":  # list reads: list_scope decides
        return "empty" if AUTHZ.list_scope(principal, action) == "none" else "ok"
    try:
        AUTHZ.check(principal, action, res)
    except NoemaError as err:
        return f"{err.spec.status}:{err.code.value}"
    return "ok"


def test_every_operation_is_mapped() -> None:
    bundle_ops = {op.operation_id for op in default_bundle().operations() if op.authenticated}
    assert bundle_ops == set(OPERATION_ACTIONS) == set(OPS)
    assert set(MATRIX) == set(get_args(Action))


@pytest.mark.parametrize("op", sorted(OPS))
def test_matrix_equals_the_oracle(op: str) -> None:
    """SEC-AZ-01 (unit): every role set x relation with every scope; zero disagreements."""
    disagreements = []
    for roles, rel in itertools.product(ROLE_SETS, RELATIONS):
        principal = who(roles)
        want = expected(op, roles, ALL_SCOPES, rel if RESOURCE[op] else None)
        got = actual(op, principal, resource(op, rel))
        if want != got:
            disagreements.append((sorted(roles), rel, want, got))
    assert disagreements == []


@pytest.mark.parametrize("scope", sorted(ALL_SCOPES))
def test_single_scope_keys(scope: Scope) -> None:
    """SEC-AZ-02: a single-scope key passes steps 1-2 only on the operations of its own scope."""
    principal = who(frozenset({"tenant_admin", "developer", "reviewer", "end_user", "auditor"}), frozenset({scope}))
    for op, (op_scope, _roles, failure) in OPS.items():
        got = actual(op, principal, None)
        if op_scope is None or op_scope == scope:
            assert got == "ok", op
        else:
            assert got == failure, op


def test_x_noema_roles_equal_the_roles_column() -> None:
    """SEC-AZ-03: each operation's `x-noema-roles` equals the matrix roles of its action."""
    for op in default_bundle().operations():
        if not op.authenticated:
            continue
        assert frozenset(op.roles) == roles_for(OPERATION_ACTIONS[op.operation_id]), op.operation_id
        assert frozenset(op.roles) == OPS[op.operation_id][1], op.operation_id


OWN = Relation(own=True, session_owner=False, same_project=True)
PEER = Relation(own=False, session_owner=False, same_project=True)
SESS = Relation(own=False, session_owner=True, same_project=True)
OTHER = Relation(own=False, session_owner=False, same_project=False)
OTHER_OWN = Relation(own=True, session_owner=False, same_project=False)
DEV: frozenset[Role] = frozenset({"developer"})
EU: frozenset[Role] = frozenset({"end_user"})
REV: frozenset[Role] = frozenset({"reviewer"})
TA: frozenset[Role] = frozenset({"tenant_admin"})
AUD: frozenset[Role] = frozenset({"auditor"})
GOLDEN: list[tuple[str, frozenset[Role], Relation, str]] = [
    ("createTurn", DEV, OWN, "ok"),
    ("createTurn", DEV, PEER, "404:session_not_found"),
    ("createTurn", TA, PEER, "ok"),
    ("createTurn", TA, OTHER, "403:session_forbidden"),
    ("createTurn", AUD, OWN, "403:permission_denied"),
    ("createTurn", REV, OWN, "403:permission_denied"),
    ("createDecision", TA, OTHER, "403:permission_denied"),
    ("createDecision", EU, OWN, "ok"),
    ("uploadFile", TA, OTHER, "403:permission_denied"),
    ("uploadFile", DEV, PEER, "404:session_not_found"),
    ("getTurn", EU, SESS, "ok"),
    ("getTurn", DEV, PEER, "404:not_found"),
    ("getTurn", REV, OWN, "404:not_found"),
    ("getTurn", AUD, OTHER, "ok"),
    ("cancelTurn", DEV, SESS, "404:not_found"),
    ("cancelTurn", TA, OTHER, "ok"),
    ("cancelTurn", AUD, OWN, "404:not_found"),
    ("getDecision", EU, PEER, "404:not_found"),
    ("getDecision", DEV, PEER, "ok"),
    ("getDecision", REV, PEER, "ok"),
    ("getDecision", REV, OTHER, "404:not_found"),
    ("getDecision", AUD, OTHER, "ok"),
    ("getSession", REV, OWN, "404:session_not_found"),
    ("getSession", DEV, PEER, "404:session_not_found"),
    ("getSession", AUD, OTHER, "ok"),
    ("getSession", DEV, OTHER_OWN, "404:session_not_found"),
    ("updateSession", DEV, OWN, "ok"),
    ("updateSession", TA, OTHER, "ok"),
    ("updateSession", AUD, OWN, "403:permission_denied"),
    ("updateSession", DEV, PEER, "404:session_not_found"),
    ("deleteSession", EU, OWN, "ok"),
    ("deleteSession", REV, OWN, "403:permission_denied"),
    ("listSessionTurns", AUD, OTHER, "ok"),
    ("listSessionTurns", REV, OWN, "404:session_not_found"),
    ("getFile", AUD, OTHER, "ok"),
    ("watchFile", AUD, OTHER, "404:not_found"),
    ("getFileDerivative", AUD, OWN, "404:not_found"),
    ("getFileDerivative", EU, SESS, "ok"),
    ("deleteFile", DEV, SESS, "403:permission_denied"),
    ("deleteFile", TA, OTHER, "ok"),
    ("getUsage", EU, OWN, "403:permission_denied"),
    ("getUsage", DEV, OWN, "ok"),
    ("listLabelingDropped", AUD, OWN, "403:permission_denied"),
    ("listLabelingDropped", REV, OWN, "ok"),
    ("listCalibrators", DEV, OWN, "403:permission_denied"),
    ("listProfiles", REV, OWN, "ok"),
    ("listSessions", REV, OWN, "empty"),
    ("listSessions", EU, OWN, "ok"),
]


@pytest.mark.parametrize(
    ("op", "roles", "rel", "want"), GOLDEN, ids=[f"{g[0]}-{sorted(g[1])}-{i}" for i, g in enumerate(GOLDEN)]
)
def test_golden_cells(op: str, roles: frozenset[Role], rel: Relation, want: str) -> None:
    assert len(GOLDEN) >= 30
    assert actual(op, who(roles), resource(op, rel)) == want
    assert expected(op, roles, ALL_SCOPES, rel if RESOURCE[op] else None) == want


def test_list_scopes() -> None:
    assert AUTHZ.list_scope(who(DEV), "session.list") == "own"
    assert AUTHZ.list_scope(who(EU), "session.list") == "own"
    assert AUTHZ.list_scope(who(TA), "session.list") == "tenant"
    assert AUTHZ.list_scope(who(AUD), "session.list") == "tenant"
    assert AUTHZ.list_scope(who(REV), "session.list") == "none"
    assert AUTHZ.list_scope(who(DEV | AUD), "session.list") == "tenant"
    assert AUTHZ.list_scope(who(DEV), "usage.read") == "project"
    assert AUTHZ.list_scope(who(AUD), "usage.read") == "tenant"
    assert AUTHZ.list_scope(who(EU), "usage.read") == "none"
    assert AUTHZ.list_scope(who(REV), "admin.labeling.dropped.read") == "tenant"
    assert AUTHZ.list_scope(who(AUD), "file.watch") == "none"
    assert AUTHZ.list_scope(who(DEV), "session.list") == "own"
    assert AUTHZ.list_scope(who(DEV, frozenset({"turns"})), "session.list") == "none"
    assert AUTHZ.list_scope(who(EU, frozenset()), "profile.list") == "tenant"


def test_create_checks_file_parts_and_retry_of_by_visibility() -> None:
    """createTurn/createDecision check file parts (file.read V) and `retry_of` (turn.read V): 404 not_found."""
    me = who(DEV)
    AUTHZ.check(me, "turn.create", ResourceRef("file", "file_1", "p1", ME, None))
    AUTHZ.check(me, "decision.create", ResourceRef("file", "file_1", "p1", "prn_other", ME))
    AUTHZ.check(me, "turn.create", ResourceRef("turn", "turn_1", "p1", ME, None))
    actions: tuple[Action, ...] = ("turn.create", "decision.create")
    for action in actions:
        with pytest.raises(NoemaError) as err:
            AUTHZ.check(me, action, ResourceRef("file", "file_1", "p1", "prn_other", None))
        assert err.value.code.value == "not_found"
    with pytest.raises(NoemaError) as err:
        AUTHZ.check(me, "turn.create", ResourceRef("turn", "turn_1", "p2", ME, None))
    assert err.value.code.value == "not_found"
