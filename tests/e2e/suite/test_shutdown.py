"""Graceful shutdown of an api pod during a turn (spec-11 §7): SIGTERM to api-1's container while it streams a turn.
`/readyz` turns 503 at once (the SIGTERM chain starts the drain), the running turn streams to its end within the
30 s grace, the stored turn and its usage line exist (finalize, then the outbox flush in stop()), and the process
exits well before the 45 s stop grace. The container is started again and the test waits until api-1 is ready, so
the rest of the suite runs on both replicas. Needs the docker CLI (the stack's compose project)."""

from __future__ import annotations

import os
import shutil

import anyio
import httpx
import pytest
from tests.e2e.conftest import Mock, Stack
from tests.e2e.suite import scenarios as sc
from tests.e2e.suite.kit import deltas_text, live_turn, report, tag, turn_body
from tests.e2e.suite.multipod import wait_for
from tests.e2e.suite.turns import new_session, stored_turn

pytestmark = [pytest.mark.e2e, pytest.mark.slow]


async def docker(*argv: str) -> str:
    done = await anyio.run_process(["docker", *argv], check=True)
    return done.stdout.decode().strip()


async def container(service: str) -> str:
    project = os.environ.get("NOEMA_E2E_PROJECT") or "noema-e2e"
    labels = [f"label=com.docker.compose.project={project}", f"label=com.docker.compose.service={service}"]
    found = (await docker("ps", "-aq", "--filter", labels[0], "--filter", labels[1])).split()
    assert len(found) == 1, (project, service, found)
    return found[0]


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs the docker CLI")
async def test_sigterm_drains_the_running_turn_and_flushes_before_exit(stack: Stack, mock: Mock) -> None:
    api1 = await container("api-1")
    started_before = await docker("inspect", "-f", "{{.State.StartedAt}}", api1)
    await mock.load(sc.document("e2e-drain", [sc.stream(sc.SOL, first_text_ms=300, step_ms=500)], sc.router()))
    session_id = await new_session(stack.client(0))
    async with live_turn(
        stack.client(0), turn_body(f"What is the capital of France? ({tag()})", session_id=session_id)
    ) as turn:
        await turn.until(lambda e: e.event == "generation.committed")
        await docker("kill", "--signal", "SIGTERM", api1)
        signalled = anyio.current_time()

        async def draining() -> bool:
            return (await stack.client(0).request("GET", "/readyz")).status_code == 503

        await wait_for(draining, timeout_s=3)  # before uvicorn's own handler (NOEMA_DRAIN_DELAY_S 5 s)
        events = await turn.rest(timeout_s=30)
    assert events[-1].event == "turn.completed" and deltas_text(events) == sc.TEXT, turn.timeline()
    stored = await stored_turn(stack, turn.turn_id, replica=1)
    assert stored["text"] == sc.TEXT

    async def exited_or_restarted() -> bool:
        state = await docker("inspect", "-f", "{{.State.Status}} {{.State.StartedAt}}", api1)
        return state.startswith("exited") or state.split()[-1] != started_before

    await wait_for(exited_or_restarted, timeout_s=40, every_s=0.5)
    assert anyio.current_time() - signalled < 30  # the drain hooks ran and finished: no wait for the 40 s bound
    if (await docker("inspect", "-f", "{{.State.Status}}", api1)) == "exited":
        await docker("start", api1)  # restart: unless-stopped may already have done it

    async def ready() -> bool:
        try:
            return (await stack.client(0).request("GET", "/readyz")).status_code == 200
        except httpx.TransportError:  # the container is still starting
            return False

    await wait_for(ready, timeout_s=60, every_s=0.5)

    async def usage_line() -> bool:  # written behind, flushed by stop() before the process exited
        rows = await report(stack.client(1), "session")  # every page: the stack keeps every run's sessions
        return any(row["key"] == session_id and row["line"] == "generation" for row in rows)

    await wait_for(usage_line, timeout_s=30, every_s=0.5)
