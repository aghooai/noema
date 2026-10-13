"""Wave E2 (io CR item 3c, edge defect 22 for the shared file-events connection): a watcher whose event channel
is live re-reads its file row on events and every `WATCH_POLL_S` only, not every 2 s (N watchers made N row reads
every 2 s against the DB pool). The shared connection is PINGed while quiet; when nothing answers for `DEAD_S` it
is no longer live: every watcher re-reads at once and falls back to the 2 s poll. Virtual clock, scripted pub/sub."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from tests.unit.api.apifakes import DEV_KEY, principal

from noema.core.clock import deadline_scope
from noema.core.redis_keys import RedisKeys
from noema.media.pipeline import MediaFileService, RedisFileEvents

from . import media_fixtures as fx
from .support import media_env

pytestmark = pytest.mark.unit


class ScriptedPubSub:
    """Never delivers a file event; `answers` makes it reply PONG to each PING."""

    def __init__(self, clock: Any, answers: bool) -> None:
        self.clock, self.answers, self.pongs_due, self.pings = clock, answers, 0, 0
        self.connection = self

    async def subscribe(self, channel: str) -> None:
        return None

    async def unsubscribe(self, channel: str) -> None:
        return None

    async def get_message(self, **kwargs: Any) -> dict[str, Any] | None:  # timeout=, as redis-py's
        if self.pongs_due:
            self.pongs_due -= 1
            return {"type": "pong", "channel": None, "data": b""}
        await self.clock.sleep(kwargs["timeout"])
        return None

    async def ping(self) -> None:
        self.pings += 1
        self.pongs_due += int(self.answers)

    async def disconnect(self) -> None:  # `pubsub.connection.disconnect()`: the next read reconnects
        return None

    async def aclose(self) -> None:
        return None


class ScriptedRedis:
    def __init__(self, clock: Any, answers: bool) -> None:
        self.clock, self.answers = clock, answers
        self.made: list[ScriptedPubSub] = []

    def pubsub(self) -> ScriptedPubSub:
        self.made.append(ScriptedPubSub(self.clock, self.answers))
        return self.made[-1]

    async def publish(self, channel: str, payload: str) -> int:
        return 0


async def _chunks(data: bytes) -> Any:
    yield data


@pytest.mark.parametrize(("answers", "reads"), [(True, 3), (False, 24)])
async def test_a_live_event_channel_spares_the_two_second_rereads(tmp_path: Path, answers: bool, reads: int) -> None:
    env = media_env(tmp_path)
    w, clock = env.world, env.clock
    events = RedisFileEvents(ScriptedRedis(clock, answers), RedisKeys("test:"), clock=clock)
    files = MediaFileService(
        db=w.db, jobs=w.jobs, store=env.store, config=w.config, clock=w.clock, ids=w.ids, events=events
    )
    who = principal(DEV_KEY)
    ref = await files.upload(who, _chunks(fx.png(32, 32)), declared_type="image/png", filename="a.png", session_id=None)
    done: list[bool] = []  # per row read: was the event channel live
    read = files._read

    async def counting(*args: Any, **kwargs: Any) -> Any:
        done.append(events.live)
        return await read(*args, **kwargs)

    files._read = counting  # type: ignore[method-assign]
    async with clock.autojump(), deadline_scope(clock, clock.monotonic() + 61.0):
        async for _ in files.watch(who, ref.file_id):  # the file stays quarantined: no change, no frame
            pass
    # live: the first read, then at 30 s and 60 s; dead after 15 s: one re-read then, then one every 2 s
    assert len(done) == reads
    assert done[-1] is answers
