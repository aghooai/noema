"""The test-mode response validator (IMPL-041 point 4) and its documented contract-gap allowances."""

from __future__ import annotations

import pytest
from fastapi import APIRouter, Request
from starlette.responses import Response
from tests.unit.api.apifakes import asgi_client, auth, example_snapshot, make_app

from noema.api import deps
from noema.api.middleware.validation import validator_for
from noema.contracts.validate import ContractViolation
from noema.core.errors import ErrorCode, NoemaError

pytestmark = pytest.mark.unit


def _app(handler_body: object, status: int = 200) -> object:
    router = APIRouter()

    @router.get("/v1/turns/{turn_id}", name="getTurn")
    async def get_turn(request: Request) -> Response:
        if isinstance(handler_body, NoemaError):
            raise handler_body
        return deps.json_response(handler_body, status_code=status)  # type: ignore[arg-type]

    app, _ = make_app(routers=[router])
    return app


@pytest.mark.parametrize(
    ("body", "status", "message"),
    [
        ({"id": "turn_1"}, 200, "violates the schema"),
        ({"ok": True}, 201, "status 201 is not declared"),
        (NoemaError(ErrorCode.SESSION_NOT_FOUND, "Gone."), 0, "not in x-noema-error-codes"),
        (NoemaError(ErrorCode.NOT_IMPLEMENTED, "Raised without the stub helper."), 0, "status 501 is not declared"),
    ],
)
async def test_violations_are_raised_and_recorded(body: object, status: int, message: str) -> None:
    app = _app(body, status)
    with pytest.raises(ContractViolation, match=message):
        async with asgi_client(app, check_contract=False) as client:  # type: ignore[arg-type]
            await client.get("/v1/turns/turn_1", headers=auth())
    assert len(app.state.contract_violations) == 1  # type: ignore[attr-defined]
    assert message in app.state.contract_violations[0]  # type: ignore[attr-defined]


async def test_the_allowances_still_check_the_envelope() -> None:
    app, _ = make_app()  # stubs (501) and an undeclared 400 on getTurn
    async with asgi_client(app) as client:
        stub = await client.get("/v1/turns/turn_1", headers=auth())
        gap = await client.get("/v1/turns/turn_1?x=1", headers=auth())
        unmatched = await client.get("/v1/nothing")
    assert (stub.status_code, gap.status_code, unmatched.status_code) == (501, 400, 404)
    assert app.state.contract_violations == []


async def test_validation_is_off_unless_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOEMA_CONTRACT_VALIDATE", raising=False)
    router = APIRouter()

    @router.get("/v1/turns/{turn_id}", name="getTurn")
    async def get_turn(request: Request) -> Response:
        return deps.json_response({"not": "a turn"})

    app, _ = make_app(routers=[router], validate=None)
    async with asgi_client(app) as client:
        assert (await client.get("/v1/turns/t", headers=auth())).status_code == 200


def test_the_validator_is_parameterized_by_the_snapshot() -> None:
    validator = validator_for(example_snapshot())
    assert validator._bands == {"high_gte": 0.75, "low_lt": 0.4}  # routing.confidence_bands of the example
    assert validator._accept is not None
    assert set(validator._accept) == {"ece_max", "n_min", "class_n_min", "consensus_min", "family_lift_max"}


async def test_a_2xx_with_an_idempotency_key_requires_begin_idempotency() -> None:
    router = APIRouter()

    @router.post("/v1/sessions", name="createSession")
    async def forgetful(request: Request) -> Response:
        return deps.json_response(
            {
                "id": "ses_1",
                "created_at": 1,
                "last_seq": 0,
                "data_class": "internal",
                "retention": {"policy": "default", "expires_at": None},
                "metadata": {},
            },
            status_code=201,
        )

    app, _ = make_app(routers=[router])
    async with asgi_client(app, check_contract=False) as client:
        assert (await client.post("/v1/sessions", headers=auth())).status_code == 201  # no key: fine
        with pytest.raises(ContractViolation, match="no begin_idempotency"):
            await client.post("/v1/sessions", headers=auth(**{"idempotency-key": "k"}))
