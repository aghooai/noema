"""The real authenticator behind the API middleware: SEC-AUTH-01 (byte-identical 401 bodies apart from request_id)
and SEC-AUTH-05 (X-Forwarded-For only from trusted proxies feeds the IP allowlist)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import orjson
from asgi_lifespan import LifespanManager
from tests.fakes.ops_admission import FakeRateLimiter
from tests.fakes.world import FakeWorld, build_world
from tests.unit.identity.helpers import KEY_ID, pepper_secrets, seed_key

from noema.api.app import create_app
from noema.api.routes.sessions import router
from noema.identity.authn import ApiKeyAuthenticator
from noema.identity.authz import MatrixAuthorizer


def world_with_real_authn() -> FakeWorld:
    world = build_world()
    world.authn = ApiKeyAuthenticator(  # type: ignore[assignment]
        db=world.db,
        secrets=pepper_secrets(world.fake_clock),
        limiter=FakeRateLimiter(world.clock),
        clock=world.clock,
        runtime=world.runtime,
        env="test",
    )
    world.authz = MatrixAuthorizer()  # type: ignore[assignment]
    return world


@asynccontextmanager
async def http(world: FakeWorld, peer: str) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        services=world,  # type: ignore[arg-type]
        routers=[router],
        validate=True,
        trusted_proxies="10.0.0.0/8",
    )
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app, client=(peer, 40000))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client
    assert app.state.contract_violations == []


async def test_401_bodies_are_identical() -> None:
    world = world_with_real_authn()
    token = seed_key(world.db, ip_allowlist=["192.0.2.0/24"], scopes=("sessions",))
    bad_secret = token[:-1] + ("A" if token[-1] != "A" else "B")
    cases = [
        {},
        {"authorization": "Basic Zm9vOmJhcg=="},
        {"authorization": f"Bearer {token.replace(KEY_ID, '01J9ZQ7ABCDEFGHJKMNPQRSTVX')}"},
        {"authorization": f"Bearer {bad_secret}"},
        {"authorization": f"Bearer {token.replace('ak_test_', 'ak_live_')}"},
        {"authorization": f"Bearer {token}"},  # ip_denied: the peer is not in the allowlist
    ]
    bodies = set()
    async with http(world, "203.0.113.7") as client:
        for i, headers in enumerate(cases):
            response = await client.get("/v1/sessions", headers={**headers, "x-request-id": f"req_{i}"})
            assert response.status_code == 401 and response.headers["www-authenticate"].startswith("Bearer")
            body = orjson.loads(response.content)
            assert body["error"]["request_id"] == f"req_{i}"
            body["error"]["request_id"] = "-"
            bodies.add(orjson.dumps(body, option=orjson.OPT_SORT_KEYS))
            for part in (token, token.split(".")[1], bad_secret.split(".")[1]):
                assert part.encode() not in response.content
    assert len(bodies) == 1


async def test_forwarded_for_only_from_trusted_proxies() -> None:
    world = world_with_real_authn()
    token = seed_key(world.db, ip_allowlist=["198.51.100.0/24"], scopes=("sessions",))
    auth = {"authorization": f"Bearer {token}"}
    cases = [
        ("203.0.113.7", "198.51.100.5", 401),  # untrusted peer: X-Forwarded-For ignored, the peer is the client
        ("10.1.1.1", "1.2.3.4, 198.51.100.5", 200),  # rightmost untrusted entry
        ("10.1.1.1", "198.51.100.5, 10.2.2.2", 200),  # trusted proxies skipped
        ("10.1.1.1", "198.51.100.5, 6.6.6.6", 401),  # entries left of the rightmost untrusted one are ignored
        ("198.51.100.9", "6.6.6.6", 200),  # the peer itself is allowed; its X-Forwarded-For is ignored
    ]
    for peer, forwarded, status in cases:
        async with http(world, peer) as client:
            response = await client.get("/v1/sessions", headers={**auth, "x-forwarded-for": forwarded})
        assert response.status_code == status, (peer, forwarded)
