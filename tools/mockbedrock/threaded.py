"""`ThreadedMockBedrock`: the mock on its own event loop in a daemon thread, for sync clients (botocore, the
openai sync client) or for isolating the mock from the test's loop. Controls are forwarded thread-safely."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import Future
from types import TracebackType
from typing import Any, TypeVar

from tests.fakes.scenario import Scenario

from .app import DEFAULT_REGION, MockBedrock, ScenarioSource
from .state import DEFAULT_RECORD_BYTES, DEFAULT_RECORD_LIMIT, Quota

T = TypeVar("T")


class ThreadedMockBedrock:
    """`with ThreadedMockBedrock(scenario=doc) as mock: ...` binds 127.0.0.1 on an ephemeral port by default."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        scenario: ScenarioSource | None = None,
        record_limit: int = DEFAULT_RECORD_LIMIT,
        record_bytes: int = DEFAULT_RECORD_BYTES,
        region: str = DEFAULT_REGION,
    ) -> None:
        self._host, self._port = host, port
        self._scenario = scenario
        self._record_limit, self._record_bytes, self._region = record_limit, record_bytes, region
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._mock: MockBedrock | None = None
        self._ready: Future[None] = Future()
        self._stop: asyncio.Event | None = None
        self._stopped = threading.Event()

    def start(self) -> ThreadedMockBedrock:
        self._thread = threading.Thread(target=self._main, name="mockbedrock", daemon=True)
        self._thread.start()
        self._ready.result(timeout=10)
        return self

    def _main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            loop.run_until_complete(self._serve())
        finally:
            loop.close()
            self._stopped.set()

    async def _serve(self) -> None:
        try:
            mock = MockBedrock(
                scenario=self._scenario,
                record_limit=self._record_limit,
                record_bytes=self._record_bytes,
                region=self._region,
            )
            await mock.start(self._host, self._port)
        except BaseException as exc:
            self._ready.set_exception(exc)
            return
        self._mock = mock
        stop = self._stop = asyncio.Event()
        self._ready.set_result(None)
        await stop.wait()
        await mock.stop()

    def stop(self) -> None:
        if self._loop is not None and self._stop is not None and not self._stopped.is_set():
            self._loop.call_soon_threadsafe(self._stop.set)
            self._stopped.wait(timeout=10)

    def __enter__(self) -> ThreadedMockBedrock:
        return self.start()

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.stop()

    def _call(self, fn: Callable[[MockBedrock], T]) -> T:
        if self._loop is None or self._mock is None:
            raise RuntimeError("the mock is not started")
        mock = self._mock
        done: Future[T] = Future()

        def run() -> None:
            try:
                done.set_result(fn(mock))
            except BaseException as exc:
                done.set_exception(exc)

        self._loop.call_soon_threadsafe(run)
        return done.result(timeout=10)

    @property
    def endpoint_url(self) -> str:
        return self._call(lambda m: m.endpoint_url)

    @property
    def mantle_url(self) -> str:
        return self._call(lambda m: m.mantle_url)

    @property
    def port(self) -> int:
        return self._call(lambda m: m.port)

    def load_scenario(self, source: ScenarioSource) -> Scenario:
        return self._call(lambda m: m.load_scenario(source))

    def reset(self) -> None:
        self._call(lambda m: m.reset())

    def release(self, barrier: str) -> None:
        self._call(lambda m: m.release(barrier))

    def requests(self, after: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
        return self._call(lambda m: m.requests(after, limit))

    def unmatched(self) -> list[dict[str, Any]]:
        return self._call(lambda m: m.unmatched())

    def set_quotas(self, quotas: Iterable[Quota | Mapping[str, Any]]) -> None:
        items = list(quotas)
        self._call(lambda m: m.set_quotas(items))
