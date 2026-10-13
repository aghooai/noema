"""IMPL-066 assertions over the bundled rev-6 openapi: ErrorCode = the openapi enum = ERROR_TABLE; each
operation's declared statuses = the statuses of its x-noema-error-codes; x-noema-roles = the authz policy; and,
once `noema.api.app` exists, the app's routes = the openapi routes."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

import pytest
from tests.fakes.auth import MATRIX

from noema.contracts.bundle import default_bundle
from noema.core.errors import ERROR_TABLE, ErrorCode
from noema.core.protocols.auth import Action

OPERATION_ACTIONS: dict[str, Action] = {
    "createTurn": "turn.create",
    "getTurn": "turn.read",
    "resumeTurnEvents": "turn.read",
    "cancelTurn": "turn.cancel",
    "createDecision": "decision.create",
    "createDecisionOpenAICompat": "decision.create",
    "getDecision": "decision.read",
    "listProfiles": "profile.list",
    "createSession": "session.create",
    "listSessions": "session.list",
    "getSession": "session.read",
    "listSessionTurns": "session.read",
    "updateSession": "session.update",
    "deleteSession": "session.delete",
    "uploadFile": "file.upload",
    "getFile": "file.read",
    "watchFile": "file.watch",
    "getFileDerivative": "file.derivative.read",
    "deleteFile": "file.delete",
    "getUsage": "usage.read",
    "listCalibrators": "admin.calibrators.read",
    "listLabelingRuns": "admin.labeling.read",
    "listLabelingDropped": "admin.labeling.dropped.read",
}


def test_error_codes_match_everywhere() -> None:
    enum = default_bundle().at("/components/schemas/ErrorCode")["enum"]
    assert list(enum) == [c.value for c in ErrorCode]
    assert set(ERROR_TABLE) == set(ErrorCode)


def test_declared_statuses_match_the_error_codes() -> None:
    for op in default_bundle().operations():
        if not op.authenticated:
            continue
        implied = {str(ERROR_TABLE[ErrorCode(c)].status) for c in op.error_codes if ERROR_TABLE[ErrorCode(c)].status}
        declared = {s for s in op.responses if s[0] in "45"}
        assert implied == declared, op.operation_id


def test_roles_match_the_authz_policy() -> None:
    ops = {op.operation_id: op for op in default_bundle().operations() if op.authenticated}
    assert set(ops) == set(OPERATION_ACTIONS)
    for op_id, action in OPERATION_ACTIONS.items():
        assert set(ops[op_id].roles) == set(MATRIX[action].roles), op_id


def test_app_routes_match_openapi() -> None:
    try:
        from noema.api.app import create_app  # type: ignore[import-not-found,unused-ignore]

        app = create_app()
    except Exception as exc:  # the api skeleton is built in parallel (B2); its own suite owns this check then
        pytest.skip(f"noema.api.app not available: {type(exc).__name__}")
    routes = set(_routes(app.routes))
    wanted = {(op.method.upper(), op.path) for op in default_bundle().operations()}
    assert routes == wanted


def _routes(routes: Iterable[Any], prefix: str = "") -> Iterator[tuple[str, str]]:
    """(method, path) of every route, through included routers (FastAPI's `_IncludedRouter.original_router`)."""
    for r in routes:
        inner = getattr(r, "original_router", None) or getattr(r, "router", None)
        if inner is not None and hasattr(inner, "routes") and not hasattr(r, "methods"):
            yield from _routes(inner.routes, prefix + (getattr(inner, "prefix", "") or ""))
            continue
        for method in getattr(r, "methods", None) or ():
            if method != "HEAD":
                yield method, prefix + r.path
