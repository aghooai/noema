"""Wave E2 (io CR item 3a): watchFile streams are capped per project and process. Above the cap a new watch is 429
`rpm_exceeded` (the only 429 code watchFile declares) with `retry-after`, before any stream starts; a closed
stream gives its place back. Raw ASGI calls keep the streams open (the file never reaches a terminal state)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import pytest
from asgi_lifespan import LifespanManager
from tests.fakes.auth import FakeAuthorizer
from tests.unit.api.apifakes import DEV_KEY, FakeServices, make_app
from tests.unit.media_pipeline import media_fixtures as fx
from tests.unit.media_pipeline.support import media_env

from noema.api.overload import WatchSlots
from noema.api.routes import files as files_routes
from noema.core.principal import Principal

pytestmark = pytest.mark.unit


class Watch:
    """One raw GET /v1/files/{id}/events: `started` once the response starts, `leave()` disconnects."""

    def __init__(self, app: Any, file_id: str) -> None:
        self.app, self.file_id = app, file_id
        self.started, self._leave, self.status, self.body = anyio.Event(), anyio.Event(), 0, b""

    def leave(self) -> None:
        self._leave.set()

    async def run(self) -> None:
        delivered = False

        async def receive() -> dict[str, Any]:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await self._leave.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                self.status = message["status"]
                self.started.set()
            else:
                self.body += message.get("body", b"")

        path = f"/v1/files/{self.file_id}/events"
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
            "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
            "headers": [(b"authorization", f"Bearer {DEV_KEY}".encode())],
            "client": ("127.0.0.1", 5000), "server": ("t", 80),
        }  # fmt: skip
        await self.app(scope, receive, send)


async def test_watch_streams_are_capped_per_project_and_give_their_place_back(tmp_path: Path) -> None:
    env = media_env(tmp_path)
    services = FakeServices()
    for name, value in {"files": env.files, "authz": FakeAuthorizer(), "db": env.world.db}.items():
        setattr(services, name, value)
    app, _ = make_app(services, routers=[files_routes.router])
    app.state.watch_slots = WatchSlots(2)
    who: Principal = services.authn.principals[DEV_KEY]
    async with LifespanManager(app) as manager:
        ref = await env.files.upload(
            who, _chunks(fx.png(32, 32)), declared_type="image/png", filename="a.png", session_id=None
        )
        watches = [Watch(manager.app, ref.file_id) for _ in range(4)]
        with anyio.fail_after(10):  # real time: a safety net only
            async with anyio.create_task_group() as tg:
                for w in watches[:3]:
                    tg.start_soon(w.run)
                    await w.started.wait()
                assert [w.status for w in watches[:3]] == [200, 200, 429]
                assert b'"code":"rpm_exceeded"' in watches[2].body
                watches[0].leave()
                await anyio.wait_all_tasks_blocked()
                tg.start_soon(watches[3].run)
                await watches[3].started.wait()
                assert watches[3].status == 200  # the closed stream gave its place back
                for w in watches:
                    w.leave()
    assert app.state.watch_slots.open == {}
    assert app.state.contract_violations == []


async def _chunks(data: bytes) -> Any:
    yield data
