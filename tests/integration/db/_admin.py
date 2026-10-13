"""Helpers of the admin.* tests: operators, key material in the IMPL-008 formats and a pg_notify collector.

Since 0003 (Wave E) a noema_cli connection acts only as the operator whose key it proved (`noema.operator_key`).
`operator()` creates operators with a real key hash under `PEPPER`, and `Acting(conn)` is `PgAdminCalls` that proves
the key of the operator named by each call's first argument before the call (the CLI proves its one operator)."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
import asyncpg
import orjson

from noema.core.ids import CROCKFORD32
from noema.core.secret import SecretStr
from noema.db.system import PgAdminCalls
from noema.identity.apikeys import Pepper, hash_key

PEPPER = Pepper(1, SecretStr("p" * 32))
_KEYS: dict[str, tuple[str, str]] = {}  # operator id -> (key_id, secret) of the operators created here

type Conn = asyncpg.Connection[asyncpg.Record]


def key_id() -> str:
    """A 26-character Crockford ULID-shaped key id."""
    return "".join(secrets.choice(CROCKFORD32) for _ in range(26))


def key_hash(pepper_version: int = 1) -> str:
    return f"hmac-sha256:v{pepper_version}:{secrets.token_hex(32)}"


async def operator(admin: Conn, *, revoked: bool = False) -> str:
    """A live (or revoked) operator inserted directly (superuser), its key hashed under `PEPPER`."""
    op_id, kid, secret = f"op_{uuid.uuid4().hex[:20]}", key_id(), secrets.token_urlsafe(32)
    await admin.execute(
        "INSERT INTO platform.operator (id, name, key_id, key_hash, revoked_at)"
        " VALUES ($1, $1, $2, $3, CASE WHEN $4 THEN now() END)",
        op_id,
        kid,
        hash_key(PEPPER, kid, secret),
        revoked,
    )
    _KEYS[op_id] = (kid, secret)
    return op_id


async def login(conn: Conn, operator_id: str) -> None:
    """Prove the key of an operator created by `operator()` on `conn` (`PgAdminCalls.prove_operator`)."""
    kid, secret = _KEYS[operator_id]
    await PgAdminCalls(conn).prove_operator(kid, secret, PEPPER.version, PEPPER.key.get_secret_value())


class Acting:
    """`PgAdminCalls` on `conn` that prove the key of the operator of each call's first argument first (operators
    this module did not create, e.g. an unknown id, are passed through without a proof)."""

    def __init__(self, conn: Conn) -> None:
        self._conn = conn
        self._calls = PgAdminCalls(conn)

    def __getattr__(self, name: str) -> Any:
        method = getattr(self._calls, name)

        async def call(*args: Any, **kwargs: Any) -> Any:
            if args and isinstance(args[0], str) and args[0] in _KEYS:
                await login(self._conn, args[0])
            return await method(*args, **kwargs)

        return call


class Notifications:
    """Collects `noema_events` payloads ({channel, payload}) received on a listening connection."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._changed = anyio.Event()

    def __call__(self, con_ref: object, pid: int, channel: str, payload: object) -> None:
        self.events.append(orjson.loads(str(payload)))
        self._changed.set()

    def of(self, channel: str) -> Iterator[dict[str, Any]]:
        return (e["payload"] for e in self.events if e["channel"] == channel)

    async def wait_for(self, channel: str, predicate: Any = lambda _p: True) -> dict[str, Any]:
        """The first matching payload; fails after 5 s (an event wait, not a sleep)."""
        with anyio.fail_after(5):
            while True:
                for payload in self.of(channel):
                    if predicate(payload):
                        return payload
                self._changed = anyio.Event()
                await self._changed.wait()


@asynccontextmanager
async def listening(conn: Conn) -> Any:
    notes = Notifications()
    await conn.add_listener("noema_events", notes)
    try:
        yield notes
    finally:
        await conn.remove_listener("noema_events", notes)
