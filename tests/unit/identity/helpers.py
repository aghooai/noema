"""Shared fixtures of the identity unit tests: peppers, seeded keys and an authenticator on the fakes."""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from tests.fakes.clock import FakeClock
from tests.fakes.db import FakeDatabase
from tests.fakes.db_system import seed_identity
from tests.fakes.ops_admission import FakeRateLimiter
from tests.fakes.secrets import FakeSecretsProvider

from noema.core.runtime import TaskGroupRuntime
from noema.core.secret import SecretStr
from noema.core.settings import Env
from noema.identity.apikeys import PEPPER_REF, Pepper, PepperRing, hash_key, new_secret, pepper_value
from noema.identity.authn import ApiKeyAuthenticator

PEPPER_V1: Final = "pepper-version-one-0123456789abcdef0123"
PEPPER_V2: Final = "pepper-version-two-0123456789abcdef0123"
PEPPER_V3: Final = "pepper-version-three-0123456789abcdef01"
TENANT: Final = "t_a"
PROJECT: Final = "p1"
PRINCIPAL: Final = "prn_dev"
KEY_ID: Final = "01J9ZQ7ABCDEFGHJKMNPQRSTVW"


def pepper(version: int, value: str) -> Pepper:
    return Pepper(version=version, key=SecretStr(value))


RING_V2: Final = PepperRing(current=pepper(2, PEPPER_V2), previous=pepper(1, PEPPER_V1))


def pepper_secrets(clock: FakeClock, *, current: int = 2) -> FakeSecretsProvider:
    """The pepper secret with `current` as the current version and `current - 1` as AWSPREVIOUS."""
    values = {1: PEPPER_V1, 2: PEPPER_V2, 3: PEPPER_V3}
    secrets = FakeSecretsProvider(clock)
    if current > 1:
        secrets.put(PEPPER_REF, pepper_value(current - 1, values[current - 1]), version="AWSPREVIOUS")
    secrets.put(PEPPER_REF, pepper_value(current, values[current]))
    return secrets


def seed_key(
    db: FakeDatabase,
    *,
    key_id: str = KEY_ID,
    env: str = "test",
    pepper_version: int = 2,
    roles: Sequence[str] = ("developer",),
    scopes: Sequence[str] = ("turns", "sessions"),
    ip_allowlist: Sequence[str] | None = None,
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
    tenant_id: str = TENANT,
    principal_id: str = PRINCIPAL,
) -> str:
    """Seed tenant/project/principal and one API key; returns the bearer token."""
    seed_identity(db.store, tenant_id=tenant_id, project_id=PROJECT, principal_id=principal_id, roles=roles)
    secret = new_secret()
    values = {1: PEPPER_V1, 2: PEPPER_V2, 3: PEPPER_V3}
    db.store.seed(
        "api_key",
        {
            "key_id": key_id,
            "tenant_id": tenant_id,
            "project_id": PROJECT,
            "principal_id": principal_id,
            "key_hash": hash_key(pepper(pepper_version, values[pepper_version]), key_id, secret),
            "pepper_version": pepper_version,
            "env": env,
            "scopes": list(scopes),
            "ip_allowlist": None if ip_allowlist is None else [ipaddress.ip_network(n) for n in ip_allowlist],
            "expires_at": expires_at,
            "revoked_at": revoked_at,
            "created_at": db.store.now(),
            "last_used_at": None,
        },
    )
    return f"ak_{env}_{key_id}.{secret}"


@dataclass
class AuthnRig:
    clock: FakeClock
    db: FakeDatabase
    secrets: FakeSecretsProvider
    limiter: FakeRateLimiter
    runtime: TaskGroupRuntime
    authn: ApiKeyAuthenticator


def authn_rig(*, env: Env = "test", current_pepper: int = 2, clock: FakeClock | None = None) -> AuthnRig:
    clock = clock or FakeClock()
    db = FakeDatabase(clock)
    secrets = pepper_secrets(clock, current=current_pepper)
    limiter = FakeRateLimiter(clock)
    runtime = TaskGroupRuntime(clock)
    authn = ApiKeyAuthenticator(db=db, secrets=secrets, limiter=limiter, clock=clock, runtime=runtime, env=env)
    return AuthnRig(clock, db, secrets, limiter, runtime, authn)


def bearer(token: str, **extra: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}", **extra}
