"""App factory for the real-server tests: `uvicorn tests.integration.api.uvicorn_app:create --factory`.

The test-only stubs of every Phase-1 operation (`stub_routers()`, not the route modules) plus a watchFile route
that streams two `file.updated` frames, over the fake services, with the test-mode response validator on.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, FastAPI, Request
from starlette.responses import Response
from tests.unit.api.apifakes import FakeServices, stub_routers

from noema.api import deps
from noema.api.app import create_app
from noema.api.sse_frame import FrameSequence, SendBytes, SseResponse, WriteOutcome, write_frames
from noema.core.protocols.events import SseFrame
from noema.core.values import JsonValue

FILE: dict[str, JsonValue] = {
    "id": "file_1",
    "filename": "a.pdf",
    "media_type": "application/pdf",
    "bytes": 3,
    "sha256": "ab",
}
SNAPSHOTS: list[dict[str, JsonValue]] = [
    {**FILE, "status": "processing", "derivatives": []},
    {**FILE, "status": "ready", "derivatives": [{"kind": "text", "id": "der_1", "status": "ready"}]},
]

sse_router = APIRouter()


@sse_router.get("/v1/files/{file_id}/events", name="watchFile")
async def watch_file(request: Request, svc: deps.ServicesDep) -> Response:
    async def frames() -> AsyncIterator[SseFrame]:
        seq = FrameSequence()
        for snapshot in SNAPSHOTS:
            yield seq.next(snapshot, event_type="file.updated")

    async def write(send: SendBytes) -> WriteOutcome:
        return await write_frames(frames(), send, clock=svc.clock)

    return SseResponse(write)


def create() -> FastAPI:
    return create_app(services=FakeServices(), routers=[sse_router, *stub_routers()], validate=True)  # type: ignore[arg-type]
