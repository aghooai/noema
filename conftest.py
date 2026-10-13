"""Root pytest configuration (spec-10 §1, §1.1).

- Environment isolation: every `AWS_*` variable (including `AWS_BEARER_TOKEN_BEDROCK`) is removed and the shared
  AWS config/credentials files point at /dev/null, so no test can sign with ambient credentials; `NOEMA_ENV=test`.
- Network: pytest-socket allows connections to loopback only (`--allow-hosts` in pyproject).
- Async: every `async def` test runs on anyio's pytest plugin with the asyncio backend (no marker needed).
- `noema_run_id`: the per-session isolation id `f"{NOEMA_AGENT_ID or 'local'}-{xdist worker or 'w0'}-{uuid8}"` used
  for Postgres databases, Redis namespaces and S3 buckets.
- `noema_services`: the URLs of the base compose services (deploy/compose/docker-compose.yml), from
  `NOEMA_TEST_PG_ADMIN_DSN`, `NOEMA_TEST_REDIS_URL` and `NOEMA_TEST_MOTO_URL` (defaults: the .env.example ports).
- Live tests (`@pytest.mark.live`) are skipped unless `NOEMA_LIVE=1`.
- Plugins: `tests.fixtures.db` (the Postgres fixtures of spec-10 §1.1) for every test package.
- Hypothesis: the `ci` profile (default) is derandomized with no example database, so `make check` gives the same
  result on every host; `NOEMA_HYPOTHESIS_PROFILE=nightly` explores with random seeds and more examples (a failure
  prints its reproduction blob; add it to the test as an `@example`).
"""

from __future__ import annotations

import inspect
import os
import uuid
from dataclasses import dataclass

import pytest
from hypothesis import settings

_SCRUBBED_PREFIXES = ("AWS_",)
pytest_plugins = ("tests.fixtures.db",)  # the Postgres fixtures (pg_template, pg_database, pg_db, pg_admin, ...)

settings.register_profile("ci", derandomize=True, database=None, deadline=None, print_blob=True)
settings.register_profile("nightly", max_examples=2_000, deadline=None, print_blob=True)
settings.load_profile(os.environ.get("NOEMA_HYPOTHESIS_PROFILE") or "ci")


def _isolate_environment() -> None:
    for name in list(os.environ):
        if name.startswith(_SCRUBBED_PREFIXES):
            del os.environ[name]
    os.environ["AWS_CONFIG_FILE"] = os.devnull
    os.environ["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
    os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
    os.environ["NOEMA_ENV"] = "test"


_isolate_environment()  # at import time, before any test module (or botocore) reads the environment


def pytest_configure(config: pytest.Config) -> None:
    _isolate_environment()


@pytest.hookimpl(tryfirst=True)
def pytest_pycollect_makeitem(collector: pytest.Collector, name: str, obj: object) -> None:
    """Mark coroutine test functions for anyio before anyio's own hook inspects them."""
    if collector.istestfunction(obj, name):  # type: ignore[attr-defined]
        inner = getattr(getattr(obj, "hypothesis", None), "inner_test", obj)
        if inspect.iscoroutinefunction(inner):
            pytest.mark.anyio(obj)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("NOEMA_LIVE") == "1":
        return
    skip_live = pytest.mark.skip(reason="live Bedrock test: set NOEMA_LIVE=1 (at most 60 calls, spec-10 §6)")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Async tests run on asyncio (uvloop is a production choice; tests stay on the stdlib loop)."""
    return "asyncio"


@pytest.fixture(scope="session")
def noema_run_id(worker_id: str) -> str:
    """The per-session isolation id (spec-10 §1.1): `{agent}-{xdist worker}-{8 hex}`."""
    agent = os.environ.get("NOEMA_AGENT_ID") or "local"
    worker = worker_id if worker_id != "master" else "w0"
    return f"{agent}-{worker}-{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class NoemaServices:
    """Connection URLs of the shared compose services; isolate inside them with `noema_run_id`."""

    pg_admin_dsn: str  # superuser DSN: creates the template and per-session databases (db fixtures)
    redis_url: str
    moto_url: str  # S3, STS, Secrets Manager and IAM (server mode)


@pytest.fixture(scope="session")
def noema_services() -> NoemaServices:
    return NoemaServices(
        pg_admin_dsn=os.environ.get(
            "NOEMA_TEST_PG_ADMIN_DSN", "postgresql://postgres:noema-dev-only@127.0.0.1:25432/postgres"
        ),
        redis_url=os.environ.get("NOEMA_TEST_REDIS_URL", "redis://127.0.0.1:26379/0"),
        moto_url=os.environ.get("NOEMA_TEST_MOTO_URL", "http://127.0.0.1:25000"),
    )


@pytest.fixture(autouse=True)
def _no_aws_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-assert the AWS isolation for every test (a test may set AWS_* through monkeypatch; it is undone after)."""
    for name in [n for n in os.environ if n.startswith(_SCRUBBED_PREFIXES)]:
        if name not in ("AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE", "AWS_EC2_METADATA_DISABLED"):
            monkeypatch.delenv(name)
