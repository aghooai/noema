"""`ApiKeyAuthenticator` on the fakes (spec-09 §2): SEC-AUTH-01, -02 (Redis down), -03, -04, -06, the LRU cache,
touch/rehash housekeeping and fail-safe behavior."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from tests.fakes.db_system import FakeSystemCalls
from tests.unit.identity.helpers import KEY_ID, AuthnRig, authn_rig, bearer, seed_key

from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.storage import Row
from noema.identity.apikeys import PEPPER_REF

OTHER_KEY = "01J9ZQ7ABCDEFGHJKMNPQRSTVX"


@asynccontextmanager
async def running(rig: AuthnRig) -> AsyncIterator[AuthnRig]:
    await rig.runtime.open()
    try:
        yield rig
    finally:
        await rig.runtime.close()


async def fail(rig: AuthnRig, headers: dict[str, str], ip: str = "198.51.100.1") -> NoemaError:
    with pytest.raises(NoemaError) as err:
        await rig.authn.authenticate(headers, ip)
    return err.value


async def test_valid_key_builds_the_principal() -> None:
    rig = authn_rig()
    token = seed_key(rig.db, roles=("developer", "tenant_admin"), scopes=("turns", "usage.read"))
    async with running(rig):
        who = await rig.authn.authenticate(bearer(token, **{"x-request-id": "req_7"}), "198.51.100.1")
    assert (who.tenant_id, who.project_id, who.principal_id, who.key_id) == ("t_a", "p1", "prn_dev", KEY_ID)
    assert who.roles == {"developer", "tenant_admin"} and who.scopes == {"turns", "usage.read"}
    assert who.request_id == "req_7"


async def test_every_failure_is_the_same_401() -> None:
    """SEC-AUTH-01: every reason gives the same envelope apart from request_id; none echoes any part of the key."""
    rig = authn_rig(env="prod")
    now = rig.clock.now()
    tokens = {
        "good": seed_key(rig.db, ip_allowlist=["10.0.0.0/8"]),
        "revoked": seed_key(rig.db, key_id="01J9ZQ7ABCDEFGHJKMNPQRSTV0", env="live", revoked_at=now),
        "expired": seed_key(rig.db, key_id="01J9ZQ7ABCDEFGHJKMNPQRSTV1", env="live", expires_at=now),
    }
    good = tokens["good"]
    cases = {
        "format": {"authorization": "Basic Zm9vOmJhcg=="},
        "unknown_key": bearer(good.replace(KEY_ID, OTHER_KEY)),
        "bad_secret": bearer(good[:-1] + ("A" if good[-1] != "A" else "B")),
        "env_mismatch": bearer(good),  # ak_test_ under NOEMA_ENV=prod
        "revoked": bearer(tokens["revoked"]),
        "expired": bearer(tokens["expired"]),
        "no_header": {},
    }
    bodies: set[str] = set()
    async with running(rig):
        for name, headers in cases.items():
            err = await fail(rig, headers, ip=f"198.51.100.{len(bodies) + 2}")
            assert err.code is ErrorCode.INVALID_API_KEY, name
            body = err.envelope("req_x")
            bodies.add(repr(body))
            for token in tokens.values():
                for part in (token, token.split(".")[1], token.split("_")[2].split(".")[0]):
                    assert part not in repr(body) and part not in str(err), name
    assert len(bodies) == 1


async def test_env_rules() -> None:
    """SEC-AUTH-06: ak_test_ with NOEMA_ENV=prod -> 401; an ak_live_ prefix on a test row -> 401."""
    prod = authn_rig(env="prod")
    live_token = seed_key(prod.db, env="live")
    async with running(prod):
        assert (await prod.authn.authenticate(bearer(live_token), "198.51.100.1")).key_id == KEY_ID
    rig = authn_rig(env="test")
    test_token = seed_key(rig.db, env="test")
    async with running(rig):
        assert (await rig.authn.authenticate(bearer(test_token), "198.51.100.1")).key_id == KEY_ID
        forged = test_token.replace("ak_test_", "ak_live_")
        assert (await fail(rig, bearer(forged))).code is ErrorCode.INVALID_API_KEY
    prod_test = authn_rig(env="prod")
    token = seed_key(prod_test.db, env="test")
    async with running(prod_test):
        assert (await fail(prod_test, bearer(token))).code is ErrorCode.INVALID_API_KEY


async def test_pepper_rotation() -> None:
    """SEC-AUTH-03: a previous-pepper key verifies and is re-hashed; an older pepper version -> 401."""
    rig = authn_rig(current_pepper=3)
    token_v2 = seed_key(rig.db, pepper_version=2)
    token_v1 = seed_key(rig.db, key_id=OTHER_KEY, pepper_version=1)
    async with running(rig):
        await rig.authn.authenticate(bearer(token_v2), "198.51.100.1")
        await rig.clock.settle()
        row = rig.db.store.table("api_key")[(KEY_ID,)]
        assert row["pepper_version"] == 3 and row["key_hash"].startswith("hmac-sha256:v3:")
        rig.authn.evict(KEY_ID)
        await rig.authn.authenticate(bearer(token_v2), "198.51.100.1")  # now verified with the current pepper
        assert (await fail(rig, bearer(token_v1))).code is ErrorCode.INVALID_API_KEY


async def test_auth_failure_bucket() -> None:
    """SEC-AUTH-04: the 51st bad key in 1 s from one IP -> 429 before verification; another IP is unaffected."""
    rig = authn_rig()
    token = seed_key(rig.db)
    bad = bearer(token.replace(KEY_ID, OTHER_KEY))
    async with running(rig):
        for _ in range(50):
            assert (await fail(rig, bad, ip="203.0.113.9")).code is ErrorCode.INVALID_API_KEY
        limited = await fail(rig, bearer(token), ip="203.0.113.9")  # even a valid key: checked before verification
        assert limited.code is ErrorCode.RPM_EXCEEDED and limited.retry_after_s is not None
        assert limited.retry_after_s >= 1
        assert (await rig.authn.authenticate(bearer(token), "203.0.113.10")).key_id == KEY_ID
        for _ in range(20):  # successes never consume the bucket
            await rig.authn.authenticate(bearer(token), "203.0.113.11")
        assert "authfail:203.0.113.11" not in rig.limiter.tat
        await rig.clock.advance(1.0)
        assert (await rig.authn.authenticate(bearer(token), "203.0.113.9")).key_id == KEY_ID


async def test_ip_allowlist() -> None:
    rig = authn_rig()
    token = seed_key(rig.db, ip_allowlist=["10.1.0.0/16", "2001:db8::/32"])
    async with running(rig):
        await rig.authn.authenticate(bearer(token), "10.1.2.3")
        await rig.authn.authenticate(bearer(token), "2001:db8::1")
        await rig.authn.authenticate(bearer(token), "::ffff:10.1.2.3")  # IPv4-mapped peer of a dual-stack socket
        for ip in ("10.2.0.1", "not-an-ip", "::ffff:10.2.0.1", "2001:db9::1"):
            assert (await fail(rig, bearer(token), ip=ip)).code is ErrorCode.INVALID_API_KEY


def count_lookups(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every `lookup_api_key` call of the fake system calls."""
    calls: list[str] = []
    original = FakeSystemCalls.lookup_api_key

    async def lookup(self: FakeSystemCalls, key_id: str) -> Row | None:
        calls.append(key_id)
        return await original(self, key_id)

    monkeypatch.setattr(FakeSystemCalls, "lookup_api_key", lookup)
    return calls


async def test_lru_cache_and_negative_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = authn_rig()
    token = seed_key(rig.db)
    lookups = count_lookups(monkeypatch)
    unknown = bearer(token.replace(KEY_ID, OTHER_KEY))
    async with running(rig):
        for _ in range(5):
            await rig.authn.authenticate(bearer(token), "198.51.100.1")
        assert len(lookups) == 1
        await rig.clock.advance(59.0)
        await rig.authn.authenticate(bearer(token), "198.51.100.1")
        assert len(lookups) == 1
        await rig.clock.advance(1.0)  # TTL 60 s
        await rig.authn.authenticate(bearer(token), "198.51.100.1")
        assert len(lookups) == 2
        for _ in range(3):
            await fail(rig, unknown)
        assert len(lookups) == 3  # negative entry
        await rig.clock.advance(10.0)  # negative TTL 10 s
        await fail(rig, unknown)
        assert len(lookups) == 4


async def test_revocation_is_bounded_by_the_ttl_when_redis_is_down() -> None:
    """SEC-AUTH-02 (Redis down): no keys_revoked message arrives; the revoked key fails within 60 s."""
    rig = authn_rig()
    token = seed_key(rig.db)
    async with running(rig):
        await rig.authn.authenticate(bearer(token), "198.51.100.1")
        rig.db.store.table("api_key")[(KEY_ID,)]["revoked_at"] = rig.clock.now()
        await rig.clock.advance(59.9)
        await rig.authn.authenticate(bearer(token), "198.51.100.1")  # still the cached row
        await rig.clock.advance(0.1)
        assert (await fail(rig, bearer(token))).code is ErrorCode.INVALID_API_KEY
    rig2 = authn_rig()
    token2 = seed_key(rig2.db)
    async with running(rig2):
        await rig2.authn.authenticate(bearer(token2), "198.51.100.1")
        rig2.db.store.table("api_key")[(KEY_ID,)]["revoked_at"] = rig2.clock.now()
        assert rig2.authn.evict(KEY_ID)  # what the keys_revoked listener does with Redis up
        assert (await fail(rig2, bearer(token2))).code is ErrorCode.INVALID_API_KEY


async def test_touch_at_most_once_a_minute() -> None:
    rig = authn_rig()
    token = seed_key(rig.db)
    touches: list[str] = []
    async with running(rig):
        original = rig.authn._spawn

        def spy(name: str, key_id: str, rehash: tuple[str, int] | None) -> None:
            touches.append(name)
            original(name, key_id, rehash)

        rig.authn._spawn = spy  # type: ignore[method-assign]
        for _ in range(10):
            await rig.authn.authenticate(bearer(token), "198.51.100.1")
        await rig.clock.settle()
        assert touches == ["identity.touch_api_key"]
        assert rig.db.store.table("api_key")[(KEY_ID,)]["last_used_at"] == rig.clock.now()
        await rig.clock.advance(30.0)
        await rig.authn.authenticate(bearer(token), "198.51.100.1")
        await rig.clock.advance(30.0)
        await rig.authn.authenticate(bearer(token), "198.51.100.1")
        assert touches == ["identity.touch_api_key"] * 2


async def test_backend_failures_are_500_and_never_cached() -> None:
    rig = authn_rig()
    token = seed_key(rig.db)
    async with running(rig):
        rig.db.store.available = False
        err = await fail(rig, bearer(token))
        assert err.code is ErrorCode.INTERNAL_ERROR
        rig.db.store.available = True
        await rig.authn.authenticate(bearer(token), "198.51.100.1")
    broken = authn_rig()
    token = seed_key(broken.db)
    broken.secrets.fail(PEPPER_REF)
    async with running(broken):
        assert (await fail(broken, bearer(token))).code is ErrorCode.INTERNAL_ERROR
        broken.secrets.fail(PEPPER_REF, False)
        await broken.clock.advance(301.0)  # the provider's own cache expired too
        await broken.authn.authenticate(bearer(token), "198.51.100.1")
        broken.secrets.fail(PEPPER_REF)
        await broken.clock.advance(301.0)
        await broken.authn.authenticate(bearer(token), "198.51.100.1")  # keeps the loaded peppers


async def test_expiry_is_checked_against_the_clock() -> None:
    rig = authn_rig()
    token = seed_key(rig.db, expires_at=rig.clock.now() + timedelta(seconds=30))
    async with running(rig):
        await rig.authn.authenticate(bearer(token), "198.51.100.1")
        await rig.clock.advance(30.0)
        assert (await fail(rig, bearer(token))).code is ErrorCode.INVALID_API_KEY


async def test_postgres_down_serves_the_last_good_key_row() -> None:
    """Wave E edge fix 31 (06 §7, spec-10 F11: `store: false` turns continue while Postgres is down): once a key's
    60 s entry expired, a failing lookup falls back to its last good row for `STALE_S` (Postgres is tried again
    after `STALE_RETRY_S`, never refreshing the row); a revocation (`evict`, the keys_revoked listener) still drops
    it, and a key never seen stays a 500."""
    from noema.identity.keycache import STALE_S

    rig = authn_rig()
    token = seed_key(rig.db)
    unknown = bearer(token.replace(KEY_ID, OTHER_KEY))
    async with running(rig):
        await rig.authn.authenticate(bearer(token), "198.51.100.1")
        rig.db.store.available = False
        await rig.clock.advance(61.0)  # past the TTL, Postgres still down
        principal = await rig.authn.authenticate(bearer(token), "198.51.100.1")
        assert principal.key_id == KEY_ID
        assert (await fail(rig, unknown)).code is ErrorCode.INTERNAL_ERROR
        await rig.clock.advance(STALE_S)  # the stale window (STALE_S after the expiry) ended
        assert (await fail(rig, bearer(token))).code is ErrorCode.INTERNAL_ERROR
        rig.db.store.available = True
        await rig.authn.authenticate(bearer(token), "198.51.100.1")  # a fresh row again
        rig.db.store.available = False
        await rig.clock.advance(61.0)
        assert rig.authn.evict(KEY_ID)  # revoked meanwhile: the stale row is gone too
        assert (await fail(rig, bearer(token))).code is ErrorCode.INTERNAL_ERROR
