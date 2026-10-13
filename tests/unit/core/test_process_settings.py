"""`ProcessSettings` sandbox variables (spec-05 §5, ADR-002 §6-§7): NOEMA_SANDBOX_BACKEND, NOEMA_SPOOL_DIR and
NOEMA_SANDBOX_SOCKET, parsed by `composition.process_settings`."""

from __future__ import annotations

import pytest

from noema.composition import StartupError, process_settings
from noema.core.errors import ForbiddenInEnvironment

pytestmark = pytest.mark.unit
PROD = {"NOEMA_REDIS_URL": "rediss://cache.internal:6379/0"}


def test_the_defaults_are_the_remote_sandbox_on_the_spool() -> None:
    settings = process_settings("test", {})
    assert (settings.sandbox_backend, settings.spool_dir, settings.sandbox_socket) == (
        "remote",
        "/spool",
        "/spool/sandboxd.sock",
    )
    assert process_settings("prod", PROD).sandbox_backend == "remote"


def test_the_socket_defaults_into_the_spool_dir_and_both_can_be_set() -> None:
    settings = process_settings("dev", {"NOEMA_SANDBOX_BACKEND": "bwrap", "NOEMA_SPOOL_DIR": "/tmp/spool"})
    assert (settings.sandbox_backend, settings.sandbox_socket) == ("bwrap", "/tmp/spool/sandboxd.sock")
    explicit = process_settings(
        "test", {"NOEMA_SANDBOX_BACKEND": "inproc_rlimit", "NOEMA_SANDBOX_SOCKET": "/run/sbx.sock"}
    )
    assert (explicit.sandbox_backend, explicit.spool_dir, explicit.sandbox_socket) == (
        "inproc_rlimit",
        "/spool",
        "/run/sbx.sock",
    )


@pytest.mark.parametrize(
    ("env", "environ", "error"),
    [
        ("test", {"NOEMA_SANDBOX_BACKEND": "nsjail"}, StartupError),  # nsjail runs only inside sandboxd
        ("test", {"NOEMA_SANDBOX_BACKEND": "docker"}, StartupError),
        ("test", {"NOEMA_SPOOL_DIR": "spool"}, StartupError),
        ("test", {"NOEMA_SANDBOX_SOCKET": "sandboxd.sock"}, StartupError),
        ("dev", {"NOEMA_SANDBOX_BACKEND": "inproc_rlimit"}, ForbiddenInEnvironment),
        ("staging", {**PROD, "NOEMA_SANDBOX_BACKEND": "bwrap"}, ForbiddenInEnvironment),
        ("prod", {**PROD, "NOEMA_SANDBOX_BACKEND": "inproc_rlimit"}, ForbiddenInEnvironment),
    ],
)
def test_refusals(env: str, environ: dict[str, str], error: type[Exception]) -> None:
    with pytest.raises(error):
        process_settings(env, environ)
