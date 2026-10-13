"""An in-process uvicorn server (127.0.0.1, port 0) over the decision routes, the fake world on the system clock
and the real decision engine, for the client-conformance integration tests."""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import anyio
import uvicorn
from fastapi import FastAPI
from tests.fakes.clock import FakeClock
from tests.fakes.world import build_world
from tests.unit.decision.conformance_impls import engine_deps
from tests.unit.decision.world import EngineWorld

from noema.core.clock import SystemClock
from noema.decision.compiler import QuestionCompilerImpl
from noema.decision.engine import DecisionEngineImpl


def real_clock_world() -> EngineWorld:
    """The real engine on the fake world, driven by the system clock (scenario latencies are real time)."""
    world = build_world(clock=SystemClock())
    clock = cast(FakeClock, world.clock)  # EngineWorld names it a FakeClock; these tests never move it
    return EngineWorld(world, DecisionEngineImpl(engine_deps(world)), QuestionCompilerImpl(world.config), clock)


@asynccontextmanager
async def serve(app: FastAPI) -> AsyncIterator[str]:
    """Serve `app` until the block exits; yields the base URL."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(cast(Any, app), lifespan="on", log_level="warning", access_log=False))
    async with anyio.create_task_group() as tg:
        tg.start_soon(server.serve, [sock])
        with anyio.fail_after(10):
            while not server.started:  # noqa: ASYNC110 (uvicorn exposes only a flag)
                await anyio.sleep(0.01)
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
