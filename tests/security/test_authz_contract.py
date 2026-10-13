"""SEC-AZ-03 and the SEC-AZ-01 generator, without a stack: each OpenAPI operation's `x-noema-roles` equals the
spec-09 §3 Roles column as written in the independent oracle, and the generated matrix covers every
authenticated operation of the bundle."""

from __future__ import annotations

import pytest
from tests.security.authz_oracle import RULES, Kind, Relation, Role, expect
from tests.security.stack.cells import generate

from noema.contracts.bundle import default_bundle

pytestmark = [pytest.mark.unit]


def test_x_noema_roles_equal_the_spec_roles_column() -> None:
    operations = {op.operation_id: op for op in default_bundle().operations() if op.authenticated}
    assert set(operations) == set(RULES)
    mismatched = {
        op_id: (sorted(op.roles), sorted(RULES[op_id].roles))
        for op_id, op in operations.items()
        if set(op.roles) != RULES[op_id].roles
    }
    assert mismatched == {}


def test_every_operation_has_cells() -> None:
    authenticated = {op.operation_id for op in default_bundle().operations() if op.authenticated}
    cells = generate()
    assert {c.operation for c in cells} == authenticated
    # 5 roles x 4 relations minus the reviewer's and auditor's `own` = 18 cells per resource variant
    per_variant: dict[tuple[str, str], int] = {}
    for c in cells:
        per_variant[(c.operation, c.variant)] = per_variant.get((c.operation, c.variant), 0) + 1
    assert all(n in (5, 18) for n in per_variant.values()), per_variant


# Hand-written from spec-09 §3 (not derived from the oracle's code): (operation, variant, role, relation, kind) ->
# (status, code). They pin the oracle itself.
GOLDEN: list[tuple[str, str, Role, Relation, Kind | None, int, str | None]] = [
    ("getSession", "", "developer", "own", "session", 200, None),
    ("getSession", "", "developer", "peer", "session", 404, "session_not_found"),
    ("getSession", "", "reviewer", "peer", "session", 404, "session_not_found"),
    ("getSession", "", "auditor", "other_project", "session", 200, None),
    ("getSession", "", "tenant_admin", "other_tenant", "session", 404, "session_not_found"),
    ("listSessionTurns", "", "end_user", "peer", "session", 404, "session_not_found"),
    ("updateSession", "", "auditor", "peer", "session", 403, "permission_denied"),
    ("updateSession", "", "tenant_admin", "other_project", "session", 200, None),
    ("deleteSession", "", "developer", "peer", "session", 404, "session_not_found"),
    ("deleteSession", "", "reviewer", "peer", "session", 403, "permission_denied"),
    ("getTurn", "", "auditor", "peer", "turn", 200, None),
    ("getTurn", "", "reviewer", "peer", "turn", 404, "not_found"),
    ("getTurn", "", "end_user", "peer", "turn", 404, "not_found"),
    ("resumeTurnEvents", "", "developer", "own", "turn", 501, "not_implemented"),
    ("resumeTurnEvents", "", "developer", "peer", "turn", 404, "not_found"),
    ("cancelTurn", "", "auditor", "own", "turn", 404, "not_found"),
    ("cancelTurn", "", "tenant_admin", "peer", "turn", 202, None),
    ("cancelTurn", "", "developer", "other_tenant", "turn", 404, "not_found"),
    ("getDecision", "", "reviewer", "peer", "decision", 200, None),
    ("getDecision", "", "reviewer", "other_project", "decision", 404, "not_found"),
    ("getDecision", "", "end_user", "peer", "decision", 404, "not_found"),
    ("getDecision", "", "developer", "peer", "decision", 200, None),
    ("getDecision", "", "auditor", "other_project", "decision", 200, None),
    ("createTurn", "session_id", "tenant_admin", "peer", "session", 200, None),
    ("createTurn", "session_id", "tenant_admin", "other_project", "session", 403, "session_forbidden"),
    ("createTurn", "session_id", "developer", "peer", "session", 404, "session_not_found"),
    ("createTurn", "session_id", "auditor", "peer", "session", 403, "permission_denied"),
    ("createTurn", "file", "developer", "peer", "file", 404, "not_found"),
    ("createDecision", "session_id", "tenant_admin", "other_project", "session", 403, "permission_denied"),
    ("createDecision", "file", "end_user", "peer", "file", 404, "not_found"),
    ("uploadFile", "session_id", "tenant_admin", "other_project", "session", 403, "permission_denied"),
    ("getFile", "", "auditor", "peer", "file", 200, None),
    ("getFileDerivative", "", "auditor", "peer", "file", 404, "not_found"),
    ("watchFile", "", "tenant_admin", "other_project", "file", 200, None),
    ("deleteFile", "", "tenant_admin", "peer", "file", 200, None),
    ("deleteFile", "", "developer", "other_project", "file", 404, "not_found"),
    ("getUsage", "", "reviewer", "none", None, 403, "permission_denied"),
    ("listLabelingDropped", "", "auditor", "none", None, 403, "permission_denied"),
    ("listCalibrators", "", "reviewer", "none", None, 200, None),
    ("createDecisionOpenAICompat", "", "auditor", "none", None, 403, "permission_denied"),
]


@pytest.mark.parametrize(("op", "variant", "role", "relation", "kind", "status", "code"), GOLDEN)
def test_golden_cells(
    op: str, variant: str, role: Role, relation: Relation, kind: Kind | None, status: int, code: str | None
) -> None:
    got = expect(op, role, relation, kind, variant)
    assert (got.status, got.code) == (status, code)


def test_list_sessions_role_failure_is_an_empty_page() -> None:
    assert expect("listSessions", "reviewer", "none").empty
    assert not expect("listSessions", "auditor", "none").empty
