"""Helpers for the infra tests: the docker CLI, the repo compose file and throwaway compose projects.

Host-side infra tests drive docker through its CLI (a subprocess, so pytest-socket does not apply to it); they skip
when docker is unavailable or an image has not been built (`make images`). Throwaway projects are named after
`noema_run_id` and removed with `down -v` in a finalizer (spec-10 §1.1).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]
COMPOSE_FILE = REPO / "deploy" / "compose" / "docker-compose.yml"
SANDBOX_DIR = REPO / "deploy" / "sandbox"
BASE_SERVICES = frozenset({"postgres", "valkey", "moto"})
# Every host-port variable of the compose file; 0 asks docker for an ephemeral port (isolated projects).
PORT_VARIABLES = (
    "NOEMA_PG_PORT",
    "NOEMA_VALKEY_PORT",
    "NOEMA_MOTO_PORT",
    "NOEMA_FAULT_TOXIPROXY_PORT",
    "NOEMA_FAULT_PG_PORT",
    "NOEMA_FAULT_VALKEY_PORT",
    "NOEMA_FAULT_MOTO_PORT",
    "NOEMA_API1_PORT",
    "NOEMA_API1_METRICS_PORT",
    "NOEMA_API2_PORT",
    "NOEMA_API2_METRICS_PORT",
    "NOEMA_MOCKBEDROCK_PORT",
)


def run(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout: float = 120,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command (stdin = `input_text`) and return the completed process; never raises on a non-zero exit."""
    return subprocess.run(
        list(argv),
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=dict(env) if env else None,
        check=False,
    )


def docker_available() -> bool:
    return (
        shutil.which("docker") is not None
        and run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=30).returncode == 0
    )


def image_exists(image: str) -> bool:
    return run(["docker", "image", "inspect", image], timeout=30).returncode == 0


def require_image(image: str) -> str:
    if not image_exists(image):
        pytest.skip(f"image {image} not built: make images")
    return image


@dataclass
class Compose:
    """`docker compose -p <project> -f deploy/compose/docker-compose.yml` with an explicit variable environment."""

    project: str
    variables: dict[str, str] = field(default_factory=dict)

    def env(self) -> dict[str, str]:
        base = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "NOEMA_"))}
        return {**base, **self.variables}

    def __call__(
        self, *args: str, profiles: Sequence[str] = (), timeout: float = 300
    ) -> subprocess.CompletedProcess[str]:
        argv = ["docker", "compose", "-p", self.project, "-f", str(COMPOSE_FILE)]
        for profile in profiles:
            argv += ["--profile", profile]
        return run([*argv, *args], env=self.env(), timeout=timeout)

    def config(self, *profiles: str) -> dict[str, Any]:
        out = self("config", "--format", "json", profiles=profiles)
        assert out.returncode == 0, out.stderr
        doc: dict[str, Any] = json.loads(out.stdout)
        return doc

    def port(self, service: str, container_port: int) -> int:
        out = self("port", service, str(container_port))
        assert out.returncode == 0, out.stderr
        return int(out.stdout.strip().rsplit(":", 1)[1])


def project_name(run_id: str, layer: str) -> str:
    """A compose project name for this session (lower-case alphanumerics, '-' and '_')."""
    return re.sub(r"[^a-z0-9_-]", "-", f"noema-{layer}-{run_id}".lower())
