"""Fixtures of the infra tests (helpers in tests/integration/infra/helpers.py)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from tests.integration.infra.helpers import PORT_VARIABLES, Compose, docker_available, project_name


@pytest.fixture(scope="session")
def docker() -> None:
    if not docker_available():
        pytest.skip("docker is not available")


@pytest.fixture
def compose_config() -> Compose:
    """A compose wrapper for `config` queries (creates nothing; default variables)."""
    return Compose(project="noema-dev")


@pytest.fixture
def isolated_compose(docker: None, noema_run_id: str) -> Iterator[Compose]:
    """A throwaway project with every host port ephemeral; `down -v` afterwards."""
    stack = Compose(project=project_name(noema_run_id, "infra"), variables=dict.fromkeys(PORT_VARIABLES, "0"))
    try:
        yield stack
    finally:
        stack("--profile", "*", "down", "-v", "--remove-orphans", timeout=180)
