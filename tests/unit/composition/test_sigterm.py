"""The api process's SIGTERM chain (spec-11 §7 steps 1-3, `composition._chain_sigterm`): the signal starts the
runtime's drain hooks at once (`/readyz` 503) and runs the previous handler (uvicorn's) `drain_delay_s` later. The
drain must start inside a task: anyio finds its backend through sniffio's current task, and a plain loop callback
has none (NoEventLoopError, so no hook ever ran and stop() waited out the whole drain bound). The signal is
delivered in an empty context, as in a uvicorn process (no anyio.run around the loop)."""

from __future__ import annotations

import contextvars
import signal
import threading
from types import FrameType
from typing import Final

import anyio
import pytest

from noema.api.routes.health import ready
from noema.composition import SERVICE_FIELDS, build, start, stop

pytestmark = [pytest.mark.unit]
ALL_FAKES: Final = frozenset(SERVICE_FIELDS) - {"redis", "runtime"}


@pytest.mark.skipif(threading.current_thread() is not threading.main_thread(), reason="signals: main thread only")
async def test_sigterm_starts_the_drain_hooks_and_then_forwards_to_the_previous_handler() -> None:
    pytest.importorskip("sniffio")  # importable sniffio is the case that broke (the dev venv, any future dependency)
    forwarded = anyio.Event()

    def previous(signum: int, frame: FrameType | None) -> None:
        forwarded.set()

    original = signal.signal(signal.SIGTERM, previous)
    services = build("test", fakes=ALL_FAKES, environ={})  # test env: drain_delay_s 0
    try:
        await start(services, "api")
        hook_ran = anyio.Event()

        async def hook(grace_s: float) -> None:
            hook_ran.set()

        services.runtime.on_drain("probe", hook)
        assert ready(services)
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler) and handler is not previous
        # as under uvicorn: the handler runs in a context without anyio's sniffio variable (anyio.run and the pytest
        # plugin set it, and call_soon_threadsafe would copy it into the callback)
        contextvars.Context().run(handler, signal.SIGTERM, None)
        with anyio.fail_after(5):
            await hook_ran.wait()
            await forwarded.wait()
        assert services.runtime.draining and not ready(services)
        begin = anyio.current_time()
        await stop(services)  # not inside a scope: it leaves the runtime's task group entered by start()
        assert anyio.current_time() - begin < 5  # every hook finished: no wait for the 40 s drain bound
        assert signal.getsignal(signal.SIGTERM) is previous  # restored by stop()
    finally:
        await stop(services)
        signal.signal(signal.SIGTERM, original)
