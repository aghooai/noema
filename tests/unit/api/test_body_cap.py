"""Step 3: the JSON body cap (IMPL-064, SEC-EDGE-01) and Content-Type (415): what the headers declare is decided
before authentication, the body itself is read after it (Wave E edge fix 7)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import orjson
import pytest
from starlette.types import Message
from tests.unit.api.apifakes import (
    TURN_BODY,
    asgi_client,
    auth,
    make_app,
    services_with,
    snapshot_with,
    valid_body,
)

from noema.api.middleware.body import ClientDisconnected, read_capped_body, replay_receive
from noema.core.errors import ErrorCode, NoemaError

pytestmark = pytest.mark.unit
LIMIT = 1_048_576  # the schema minimum of platform.limits.max_json_body_bytes


def _receiver(*messages: Message) -> Callable[[], Awaitable[Message]]:
    it = iter(messages)

    async def receive() -> Message:
        return next(it)

    return receive


async def test_read_capped_body_joins_chunks_and_enforces_the_limit() -> None:
    chunks: list[Message] = [
        {"type": "http.request", "body": b"ab", "more_body": True},
        {"type": "http.request", "body": b"cd"},
    ]
    assert await read_capped_body(_receiver(*chunks), 4, None) == b"abcd"
    with pytest.raises(NoemaError) as exc:
        await read_capped_body(_receiver(*chunks), 3, None)
    assert exc.value.code is ErrorCode.REQUEST_TOO_LARGE
    with pytest.raises(NoemaError):  # decided from Content-Length without reading anything
        await read_capped_body(_receiver(), 3, "4")
    with pytest.raises(ClientDisconnected):
        await read_capped_body(_receiver({"type": "http.disconnect"}), 3, None)


async def test_replay_receive_yields_the_buffered_body_once() -> None:
    receive = replay_receive(b"xyz", _receiver({"type": "http.disconnect"}))
    assert await receive() == {"type": "http.request", "body": b"xyz", "more_body": False}
    assert (await receive())["type"] == "http.disconnect"


def _padded(size: int) -> bytes:
    head, tail = b'{"pad":"', b'"}'
    return head + b"x" * (size - len(head) - len(tail)) + tail


async def _chunks(data: bytes, step: int = 65536) -> AsyncIterator[bytes]:
    for i in range(0, len(data), step):
        yield data[i : i + step]


async def test_a_declared_size_one_byte_over_the_cap_is_413_before_authentication() -> None:
    app, services = make_app(services_with(snapshot_with(max_json_body_bytes=LIMIT)))
    json_ct = {"content-type": "application/json"}
    async with asgi_client(app) as client:
        resp = await client.post("/v1/turns", content=_padded(LIMIT + 1), headers=json_ct)
        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == "request_too_large"
        assert services.authn.calls == []  # a declared Content-Length is decided before authentication
        at_limit = await client.post("/v1/turns", content=_padded(LIMIT), headers=json_ct)
    assert at_limit.status_code == 401  # within the cap: the next step decides
    assert len(services.authn.calls) == 1


async def test_an_undeclared_size_over_the_cap_is_413_after_authentication() -> None:
    """Wave E: a body without Content-Length is read only for an authenticated caller (see the test below)."""
    app, services = make_app(services_with(snapshot_with(max_json_body_bytes=LIMIT)))
    body = _padded(LIMIT + 1)
    json_ct = {"content-type": "application/json"}
    async with asgi_client(app) as client:
        anonymous = await client.post("/v1/turns", content=_chunks(body), headers=json_ct)
        keyed = await client.post("/v1/turns", content=_chunks(body), headers=auth(**json_ct))
    assert anonymous.status_code == 401
    assert keyed.status_code == 413 and keyed.json()["error"]["code"] == "request_too_large"
    assert len(services.authn.calls) == 2


async def test_the_cap_comes_from_the_pinned_snapshot() -> None:
    app, services = make_app()  # example config: the 50 MiB default
    async with asgi_client(app) as client:
        resp = await client.post(
            "/v1/turns", content=_padded(LIMIT + 1), headers=auth(**{"content-type": "application/json"})
        )
    assert resp.status_code == 400  # past the cap and the auth step: the gate rejects "pad"
    assert services.config.current().platform.limits.max_json_body_bytes == 52_428_800


@pytest.mark.parametrize("ctype", [None, "text/plain", "application/jsonx", "multipart/form-data; boundary=x"])
async def test_wrong_content_type_is_415_before_authentication(ctype: str | None) -> None:
    app, services = make_app()
    headers = {} if ctype is None else {"content-type": ctype}
    async with asgi_client(app) as client:
        resp = await client.post("/v1/turns", content=b'{"input": []}', headers=headers)
    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == "unsupported_media_type"
    assert services.authn.calls == []


async def test_json_with_charset_and_optional_empty_bodies_pass_the_content_type_check() -> None:
    app, _ = make_app()
    async with asgi_client(app) as client:
        with_charset = await client.post(
            "/v1/turns",
            content=orjson.dumps(TURN_BODY),
            headers=auth(**{"content-type": "application/json; charset=utf-8"}),
        )
        empty_optional = await client.post("/v1/sessions", headers=auth())  # createSession: body optional
    assert with_charset.status_code == 501
    assert empty_optional.status_code == 501


async def test_multipart_uploads_are_not_buffered_but_their_content_type_is_checked() -> None:
    app, _ = make_app()
    ctype, body = valid_body("uploadFile")
    async with asgi_client(app) as client:
        ok = await client.post("/v1/files", content=body, headers=auth(**{"content-type": ctype or ""}))
        wrong = await client.post("/v1/files", content=b"{}", headers=auth(**{"content-type": "application/json"}))
    assert ok.status_code == 501
    assert wrong.status_code == 415


async def test_an_unauthenticated_streamed_body_is_never_buffered() -> None:
    """Wave E edge fix 7: authentication (step 4) runs before the body of undeclared length is read, so a client
    without a key makes the process buffer nothing (the declared-size 413 and the 415 keep their precedence)."""
    app, services = make_app()
    pulled = 0

    async def ten_mib() -> AsyncIterator[bytes]:
        nonlocal pulled
        for _ in range(160):
            pulled += 65536
            yield b"x" * 65536

    async with asgi_client(app) as client:
        resp = await client.post("/v1/turns", content=ten_mib(), headers={"content-type": "application/json"})
    assert resp.status_code == 401 and resp.json()["error"]["code"] == "invalid_api_key"
    assert len(services.authn.calls) == 1
    assert pulled <= 65536  # at most the chunk the transport had ready; never the body
