"""Which sandbox backend may run where (ADR-002 §7; SEC-SBX-07). Checked at process start and by every runner's
constructor: a disallowed combination raises `ForbiddenInEnvironment` and the process must exit non-zero."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, Literal

from noema.core.errors import ForbiddenInEnvironment

Backend = Literal["remote", "nsjail", "bwrap", "inproc_rlimit"]
SandboxProcess = Literal["media-worker", "sandboxd"]
ALL_ENVS: Final = frozenset({"test", "dev", "staging", "prod"})
BACKEND_ENVS: Final[Mapping[Backend, frozenset[str]]] = {
    "remote": ALL_ENVS,
    "nsjail": ALL_ENVS,
    "bwrap": frozenset({"test", "dev"}),
    "inproc_rlimit": frozenset({"test"}),
}
REMOTE_REQUIRED_ENVS: Final = frozenset({"staging", "prod"})
COMPONENT_KIND: Final[Mapping[Backend, str]] = {  # the composition.check_component kinds
    "remote": "sandbox.remote",
    "nsjail": "sandbox.nsjail",
    "bwrap": "sandbox.bwrap",
    "inproc_rlimit": "sandbox.inproc_rlimit",
}


def assert_backend_allowed(backend: Backend, env: str, *, process: SandboxProcess | None = None) -> None:
    """Refuse `backend` in `env` (ADR-002 §7 table). With `process`: the media-worker must use `remote` in
    staging/prod, and `nsjail` runs only inside sandboxd outside test/dev."""
    allowed = BACKEND_ENVS.get(backend)
    if allowed is None:
        raise ForbiddenInEnvironment(f"unknown sandbox backend {backend!r}")
    if env not in allowed:
        raise ForbiddenInEnvironment(f"sandbox backend {backend} is not allowed with NOEMA_ENV={env}")
    if process == "media-worker" and env in REMOTE_REQUIRED_ENVS and backend != "remote":
        raise ForbiddenInEnvironment(f"the media-worker must use the remote sandbox with NOEMA_ENV={env}")
    if process == "sandboxd" and backend != "nsjail":
        raise ForbiddenInEnvironment("sandboxd runs jobs only with the nsjail backend")
