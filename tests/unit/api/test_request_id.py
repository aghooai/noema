"""Step 1: request ids (IMPL-033): echoed when valid, replaced otherwise, never rejected, on every response."""

from __future__ import annotations

import re

import pytest
from tests.unit.api.apifakes import ManualClock, asgi_client, auth, make_app

from noema.api.middleware.request_id import is_valid_request_id, resolve_request_id
from noema.core.ids import UlidIdGen

pytestmark = pytest.mark.unit
GENERATED = re.compile(r"req_[0-9A-HJKMNP-TV-Z]{26}")


@pytest.mark.parametrize("value", ["a", "A.b_c:d-9", "x" * 128, "req_01J000"])
def test_valid_ids_are_echoed(value: str) -> None:
    assert is_valid_request_id(value)
    assert resolve_request_id(value, UlidIdGen(ManualClock())) == value


@pytest.mark.parametrize("value", [None, "", "x" * 129, "has space", "semi;colon", "ümlaut", "a/b", "line\nbreak"])
def test_invalid_ids_are_replaced(value: str | None) -> None:
    assert GENERATED.fullmatch(resolve_request_id(value, UlidIdGen(ManualClock())))


@pytest.mark.parametrize(
    ("path", "headers", "status"),
    [
        ("/v1/turns/turn_1", {}, 401),
        ("/v1/turns/turn_1", auth(), 501),
        ("/nowhere", {}, 404),
        ("/healthz", {}, 200),
    ],
)
async def test_every_response_carries_the_request_id(path: str, headers: dict[str, str], status: int) -> None:
    app, _ = make_app()
    async with asgi_client(app) as client:
        echoed = await client.get(path, headers={**headers, "x-request-id": "client-id.1"})
        replaced = await client.get(path, headers={**headers, "x-request-id": "bad id"})
        absent = await client.get(path, headers=headers)
    assert echoed.status_code == replaced.status_code == absent.status_code == status
    assert echoed.headers["x-request-id"] == "client-id.1"
    assert GENERATED.fullmatch(replaced.headers["x-request-id"])
    assert GENERATED.fullmatch(absent.headers["x-request-id"])
    assert replaced.headers["x-request-id"] != absent.headers["x-request-id"]
    if status >= 400:
        assert echoed.json()["error"]["request_id"] == "client-id.1"


async def test_the_principal_and_authenticator_see_the_resolved_id() -> None:
    app, services = make_app()
    async with asgi_client(app) as client:
        await client.get("/v1/turns/turn_1", headers=auth(**{"x-request-id": "bad id"}))
    headers, _ = services.authn.calls[-1]
    assert GENERATED.fullmatch(headers["x-request-id"])
