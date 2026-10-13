"""`PgListener`: one dedicated `LISTEN` connection (spec-11 §4) for the job workers (`jobs`) and the notify bridge
(`noema_events`).

The connection logs in as `noema_app` (role-checked by `noema.db.connect`) outside the pool, because a pooled
connection would lose its LISTEN on release. Notifications are delivered to a sync callback on the event loop.
When the connection drops, `listen` reconnects after 1, 2, 4 ... at most 30 s; notifications sent meanwhile are
lost, which every user tolerates (the job workers also poll every 5 s, pods poll config and calibrators every 5 s).
Liveness (Wave E): a LISTEN connection only receives, so one that died without FIN/RST would look idle forever;
every `CHECK_S` the listener runs `SELECT 1` on it (bounded by `PROBE_TIMEOUT_S`) and reconnects when that fails.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Final

import anyio
import asyncpg

from noema.core.clock import Clock
from noema.core.secret import SecretStr
from noema.db.engine import ROLE_APP, connect
from noema.ops.jobs.timing import wait_event

log: Final = logging.getLogger("noema.ops.listen")
CHECK_S: Final = 5.0  # connection liveness check while idle
PROBE_TIMEOUT_S: Final = 2.0  # the SELECT 1 of the check
MAX_BACKOFF_S: Final = 30.0

type Conn = asyncpg.Connection[asyncpg.Record]


class PgListener:
    """See the module docstring. One `listen()` call per channel (each holds its own connection)."""

    def __init__(
        self, dsn: SecretStr, clock: Clock, *, max_backoff_s: float = MAX_BACKOFF_S, check_s: float = CHECK_S
    ) -> None:
        self._dsn = dsn
        self._clock = clock
        self._max_backoff = max_backoff_s
        self._check_s = check_s
        self.connected = anyio.Event()  # set once the first LISTEN is active (tests, readiness of the bridge)

    async def listen(self, channel: str, on_notify: Callable[[str], None]) -> None:
        """LISTEN `channel` until cancelled, calling `on_notify(payload)` per notification (callback errors are
        logged, never raised). Never raises except on cancellation."""
        backoff = 1.0
        while True:
            try:
                conn = await connect(self._dsn, expected_role=ROLE_APP)
            except Exception as exc:
                log.warning("LISTEN %s: connect failed (%s); retry in %.0f s", channel, type(exc).__name__, backoff)
                await self._clock.sleep(backoff)
                backoff = min(backoff * 2, self._max_backoff)
                continue
            try:
                await self._serve(conn, channel, on_notify)
                backoff = 1.0
            except Exception as exc:
                log.warning("LISTEN %s: connection lost (%s)", channel, type(exc).__name__)
            finally:
                with anyio.CancelScope(shield=True):
                    await _close(conn)
            await self._clock.sleep(backoff)
            backoff = min(backoff * 2, self._max_backoff)

    async def _serve(self, conn: Conn, channel: str, on_notify: Callable[[str], None]) -> None:
        lost = anyio.Event()

        def deliver(_conn: object, _pid: int, _channel: str, payload: object) -> None:
            try:
                on_notify(str(payload))
            except Exception:
                log.exception("LISTEN %s: notification handler failed", channel)

        conn.add_termination_listener(lambda _conn: lost.set())
        await conn.add_listener(channel, deliver)
        self.connected.set()
        while not lost.is_set() and not conn.is_closed():
            if not await wait_event(lost, self._clock, self._check_s):
                await conn.fetchval("SELECT 1", timeout=PROBE_TIMEOUT_S)  # raises when the server stopped answering
        log.warning("LISTEN %s: connection closed; reconnecting", channel)


async def _close(conn: Conn) -> None:
    try:
        await conn.close(timeout=2)
    except Exception:
        conn.terminate()
