"""Drop stale per-session test databases (spec-10 §1.1): `make db-reap-test` (python -m tools.reap_test_dbs).

Drops `noema_t_*` clones older than 6 h or left by a dead process on this host, and `noema_tpl_*` templates of other
migration hashes older than 6 h. The pytest fixtures do the same at every session start; this is the manual sweep.
Uses NOEMA_TEST_PG_ADMIN_DSN (a superuser DSN; default: the compose Postgres of .env.example).
"""

from __future__ import annotations

import asyncio
import os

from tests.fixtures.db import _with, reap

DEFAULT_ADMIN_DSN = "postgresql://postgres:noema-dev-only@127.0.0.1:25432/postgres"


def main() -> int:
    admin_dsn = _with(os.environ.get("NOEMA_TEST_PG_ADMIN_DSN", DEFAULT_ADMIN_DSN), dbname="postgres")
    dropped = asyncio.run(reap(admin_dsn))
    print(f"dropped {len(dropped)} stale test database(s)" + "".join(f"\n  {name}" for name in dropped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
