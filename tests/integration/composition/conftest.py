"""A seeded single-host "stack" for the composition integration tests: an in-process moto (S3, STS, IAM, Secrets
Manager), a scratch database cloned from the migrated template on the compose Postgres, the compose Valkey and an
in-process `tools.mockbedrock`, seeded by `tools.bootstrap_dev.bootstrap` (the same steps as `make e2e-bootstrap`).
`seeded` sets the process environment of a real `composition.build("test")` against it (monkeypatch, undone after
each test) and yields the bootstrap state."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
from moto.server import ThreadedMotoServer
from tests.fixtures.db import create_scratch_database
from tools.bootstrap_dev import FAKE_AWS_KEY, State, Target, bootstrap
from tools.mockbedrock import MockBedrock

from noema.core.secret import SecretStr
from noema.db.engine import connect, drop_database

DEFAULT_SCENARIO = Path(__file__).resolve().parents[3] / "deploy" / "compose" / "mockbedrock" / "default.yaml"


@dataclass(frozen=True)
class Seeded:
    target: Target
    state: dict[str, Any]
    mock: MockBedrock

    def key(self, tenant: str = "t_acme", project: str = "main", role: str = "developer") -> str:
        return str(self.state["tenants"][tenant]["projects"][project]["principals"][role]["key"])


@pytest.fixture(scope="module")
def moto_url() -> Iterator[str]:
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    try:
        yield f"http://{host}:{port}"
    finally:
        server.stop()


@pytest.fixture(scope="module")
async def scratch_db(pg_template: str, noema_services: Any) -> AsyncIterator[tuple[str, str]]:
    """(host:port, database name) of a migrated scratch database (dropped at module end)."""
    admin_dsn = str(noema_services.pg_admin_dsn)
    admin = await connect(SecretStr(admin_dsn), expected_role=None)
    try:
        name = await create_scratch_database(admin, template=pg_template)
        parsed = urlparse(admin_dsn)
        yield f"{parsed.hostname}:{parsed.port or 5432}", name
        await drop_database(admin, name)
    finally:
        await admin.close()


@pytest.fixture(scope="module")
async def bootstrapped(
    moto_url: str, scratch_db: tuple[str, str], noema_services: Any, tmp_path_factory: pytest.TempPathFactory
) -> AsyncIterator[Seeded]:
    host, name = scratch_db
    parsed = urlparse(str(noema_services.pg_admin_dsn))
    async with MockBedrock(scenario=DEFAULT_SCENARIO) as mock:
        with pytest.MonkeyPatch.context() as mp:  # the bootstrap's own noema-admin calls use the default AWS chain
            mp.setenv("AWS_ACCESS_KEY_ID", FAKE_AWS_KEY)
            mp.setenv("AWS_SECRET_ACCESS_KEY", FAKE_AWS_KEY)
            target = Target(
                project="composition-it",
                env="test",
                pg_host=host,
                moto_url=moto_url,
                stack_db_host=host,
                redis_url=str(noema_services.redis_url),
                mock_url=mock.endpoint_url,
                db_name=name,
                pg_superuser=(parsed.username or "postgres", parsed.password or ""),
            )
            state = await bootstrap(target, State(tmp_path_factory.mktemp("bootstrap") / "state.json"))
        yield Seeded(target, state.data, mock)


@pytest.fixture
def seeded(bootstrapped: Seeded, monkeypatch: pytest.MonkeyPatch) -> Seeded:
    """The environment of a real build against the seeded stack (host_env: moto, the mock, Valkey, the DSN)."""
    for name, value in bootstrapped.target.host_env().items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("NOEMA_DB_DSN_REF", "noema/db-dsn")
    monkeypatch.setenv("NOEMA_CONTRACT_VALIDATE", "1")
    monkeypatch.setenv("NOEMA_ENV", "test")
    assert json.loads(bootstrapped.target.host_env()["NOEMA_AWS_ENDPOINT_OVERRIDES"])["mantle"].endswith("/v1")
    return bootstrapped
