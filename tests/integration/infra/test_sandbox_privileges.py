"""The media-sandbox privilege ladder (ADR-002 §2, SEC-SBX-01): the recorded minimum works, each relaxation is needed.

Runs deploy/sandbox/selftest.py (checks 1-10) in noema-media:dev: once through `docker compose run media-sandbox`
(the compose wiring, incl. the relative seccomp path), then with `docker run` and one privilege changed at a time.
The outcomes are the ones recorded in deploy/sandbox/PRIVILEGES.md.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from tests.integration.infra.helpers import SANDBOX_DIR, Compose, require_image, run

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.usefixtures("docker")]

MEDIA_IMAGE = "noema-media:dev"
COMPOSE_CAPS = ("SYS_ADMIN", "SETUID", "SETGID", "CHOWN", "DAC_OVERRIDE", "FOWNER", "KILL")
OUTER_PROFILE = f"seccomp={SANDBOX_DIR / 'docker-seccomp.json'}"
SELFTEST = ("/etc/noema-sandbox/selftest.sh", "--json")


def parse_report(stdout: str, stderr: str) -> dict[str, Any]:
    assert stdout.startswith("{"), stderr[-2000:]
    report: dict[str, Any] = json.loads(stdout)
    return report


def selftest_with(caps: tuple[str, ...], security_opt: str | None) -> dict[str, Any]:
    argv = ["docker", "run", "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp", "--tmpfs", "/spool"]
    argv += ["--security-opt", "no-new-privileges:true"]
    argv += ["--cap-drop", "ALL", *(arg for cap in caps for arg in ("--cap-add", cap))]
    if security_opt is not None:
        argv += ["--security-opt", security_opt]
    out = run([*argv, require_image(MEDIA_IMAGE), *SELFTEST], timeout=240)
    return parse_report(out.stdout, out.stderr)


def failed(report: dict[str, Any]) -> list[str]:
    return [r["name"] for r in report["results"] if not r["passed"]]


@pytest.mark.timeout(300)
def test_selftest_passes_under_the_compose_settings(isolated_compose: Compose) -> None:
    require_image(MEDIA_IMAGE)
    out = isolated_compose(
        "run", "--rm", "--no-deps", "-T", "media-sandbox", *SELFTEST, profiles=["media"], timeout=240
    )
    report = parse_report(out.stdout, out.stderr)
    assert report["status"] == 0, failed(report)
    assert report["environment"]["container_interfaces"] == "lo"
    assert report["environment"]["NoNewPrivs"] == "1"
    assert {r["adr"] for r in report["results"]} == set(range(1, 11))


@pytest.mark.timeout(300)
def test_selftest_passes_without_cap_kill() -> None:
    report = selftest_with(tuple(c for c in COMPOSE_CAPS if c != "KILL"), OUTER_PROFILE)
    assert report["status"] == 0, failed(report)


@pytest.mark.timeout(300)
def test_docker_default_seccomp_blocks_pivot_root() -> None:
    report = selftest_with(COMPOSE_CAPS, None)
    assert report["status"] == 2
    assert all("pivot_root" in r["stderr_tail"] for r in report["results"])


@pytest.mark.timeout(300)
def test_unconfined_outer_seccomp_also_works() -> None:
    """ADR-002 §2's fallback; not used, because Docker's default plus pivot_root suffices."""
    report = selftest_with(COMPOSE_CAPS, "seccomp=unconfined")
    assert report["status"] == 0, failed(report)


@pytest.mark.timeout(300)
@pytest.mark.parametrize("cap", ["SYS_ADMIN", "SETUID", "SETGID"])
def test_each_namespace_capability_is_required(cap: str) -> None:
    report = selftest_with(tuple(c for c in COMPOSE_CAPS if c != cap), OUTER_PROFILE)
    assert report["status"] == 2, report["results"][0]
