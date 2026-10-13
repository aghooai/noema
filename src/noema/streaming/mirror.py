"""`RedisEventMirror`: the best-effort Redis Streams copy of every turn's event log (IMPL-021, spec-02 §13).

Writes never block `TurnEventLog.append`: frames and seals go to an in-memory queue that `run()` flushes with one
script call per batch (`WRITE_LUA`: every queued XADD and EXPIRE in one EVALSHA, one small reply). Entries use the
frame id as the stream id (`<id>-0`, fields `t` = type, `p` = payload), so `XADD` is naturally idempotent and a
reader resumes with `XREAD <after>-0`. A key gets
`EXPIRE orphan_ttl_s` while its turn runs (a dead pod's stream frees itself) and `EXPIRE ttl_s` at seal. A turn
whose write failed is marked broken: its key is deleted and nothing more is written, so another pod sees "no
mirror" (`follow` -> None, the caller's 410 / 500) rather than a stream with gaps. Readers also stop at a gap.

A remote reader also ends when no entry arrived for `follow_idle_s` (default 660 s: the longest turn, Aghoo-Deadline-Ms
600 s, plus 60 s for its final snapshot and persistence), so a duplicate attached to a pod that died mid-turn is not
held until the orphan TTL (3600 s) expires the key.

Remote readers keep `RedisKeys.turn_attach(turn_id)` up to date for the abandon watchdog: `a:<epoch ms>` (PX 15 s,
refreshed every 5 s) while attached, `d:<epoch ms of the last delivered frame>` after they detach.

Every Redis call has a time limit (Wave E: a Redis that stops answering on an open connection must not wedge the
writer or the abandon watchdog): a single-key command `call_timeout_s`, a batch script call `write_timeout_s` (a
timeout fails the batch like a Redis error), an XREAD `block_ms` + `READ_SLACK_S`. At most `max_followers` remote
readers run per process (each holds one connection in a blocking XREAD); `follow` answers None above it.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator, Iterable
from typing import TYPE_CHECKING, Any, Final

import anyio
from redis.exceptions import RedisError

from noema.core.clock import Clock
from noema.core.protocols.events import SseFrame
from noema.core.redis_keys import RedisKeys

from .frames import TERMINAL_EVENT, frame_from_payload

if TYPE_CHECKING:
    from redis.asyncio import Redis
    from redis.typing import EncodableT

log = logging.getLogger("noema.streaming")
MAXLEN: Final = 20_000
ORPHAN_TTL_S: Final = 3600
MAX_PENDING: Final = 100_000
ATTACHED_TTL_MS: Final = 15_000
DETACHED_TTL_S: Final = 3600
FOLLOW_IDLE_S: Final = 660.0  # no live turn is silent longer: the max deadline (600 s) + finalize slack
CALL_TIMEOUT_S: Final = 0.5  # EXISTS, GET, SET: a silent Redis is "no mirror" / "no remote reader"
WRITE_TIMEOUT_S: Final = 2.0  # one batch script call
READ_SLACK_S: Final = 2.0  # an XREAD BLOCK block_ms answers within block_ms + this, else the read failed
MAX_FOLLOWERS: Final = 128  # concurrent remote readers per process (soft: counted once each starts reading)
BATCH_S: Final = 0.005  # the writer gathers this long after a wake: one script call for every turn's frames meanwhile
SCRIPT_FRAMES: Final = 1024  # frames per script call (a batch after a Redis outage is split, Valkey is not held long)
_Op = tuple[str, str, SseFrame | None, int]  # (kind "x" | "e", turn id, frame, ttl)
# One call writes a whole batch: KEYS = the keys to delete (ARGV[2] of them), then one key per turn; ARGV[1] =
# MAXLEN; per turn: its frame count, its EXPIRE (0 = none), then (frame id, type, payload) per frame. Returns the
# 1-based indices of the turn keys (after the deletes) with a rejected XADD or EXPIRE; a duplicate id is harmless.
WRITE_LUA: Final = """
local maxlen, ndel, a = ARGV[1], tonumber(ARGV[2]), 3
for k = 1, ndel do redis.pcall('DEL', KEYS[k]) end
local failed = {}
for k = ndel + 1, #KEYS do
  local key, n, ttl, bad = KEYS[k], tonumber(ARGV[a]), tonumber(ARGV[a + 1]), false
  a = a + 2
  for _ = 1, n do
    local r = redis.pcall('XADD', key, 'MAXLEN', '~', maxlen, ARGV[a] .. '-0', 't', ARGV[a + 1], 'p', ARGV[a + 2])
    if type(r) == 'table' and r.err and not string.find(r.err, 'equal or smaller', 1, true) then bad = true end
    a = a + 3
  end
  if ttl > 0 and type(redis.pcall('EXPIRE', key, ttl)) == 'table' then bad = true end
  if bad then failed[#failed + 1] = k - ndel end
end
return failed
"""


def _ms(clock: Clock) -> int:
    return int(clock.now().timestamp() * 1000)


class _Gap(Exception):
    """The mirror lost frames (trimmed or a failed write): the remote reader stops."""


def _entries(resp: Any) -> list[tuple[bytes, dict[bytes, bytes]]]:
    """XREAD entries of the single stream read, in RESP2 (`[[key, entries]]`) or RESP3 (`{key: entries}`) form."""
    if not resp:
        return []
    streams = list(resp.values()) if isinstance(resp, dict) else [item[1] for item in resp]
    out: list[tuple[bytes, dict[bytes, bytes]]] = []
    for entries in streams:
        if entries and isinstance(entries[0], list) and entries[0] and isinstance(entries[0][0], list | tuple):
            entries = entries[0]  # RESP3 may nest the entry list once more
        out.extend((bytes(entry_id), dict(fields)) for entry_id, fields in entries)
    return out


class _MirrorReads:
    """The read side of the mirror (mixed into `RedisEventMirror`)."""

    _redis: Redis
    _keys: RedisKeys
    _clock: Clock
    _block_ms: int
    _beat_s: float
    _follow_idle_s: float
    _call_s: float
    _max_followers: int
    _followers: int

    async def follow(self, turn_id: str, *, after: int = 0) -> AsyncIterator[SseFrame] | None:
        """Frames with id > after, then live ones, ending after turn.completed; None when the mirror key does not
        exist (expired, broken, never written) or Redis is unreachable."""
        key = self._keys.turn_events(turn_id)
        if self._followers >= self._max_followers:
            log.warning("turn event mirror: too many remote readers", extra={"turn_id": turn_id})
            return None
        try:
            with anyio.fail_after(self._call_s):
                exists = await self._redis.exists(key)
        except (RedisError, OSError, TimeoutError):
            return None
        if not exists:
            return None
        return self._tail(turn_id, key, after)

    async def _tail(self, turn_id: str, key: str, after: int) -> AsyncIterator[SseFrame]:
        last = after
        delivered: int | None = None
        beat_at = -1.0
        last_entry_at = self._clock.monotonic()
        self._followers += 1
        try:
            while True:
                if self._clock.monotonic() - beat_at >= self._beat_s:
                    beat_at = self._clock.monotonic()
                    await self._set_attach(turn_id, f"a:{_ms(self._clock)}", px=ATTACHED_TTL_MS)
                try:
                    with anyio.fail_after(self._block_ms / 1000 + READ_SLACK_S):
                        resp = await self._redis.xread({key: f"{last}-0"}, count=1000, block=self._block_ms)
                    entries = _entries(resp)
                    if not entries:
                        with anyio.fail_after(self._call_s):
                            if not await self._redis.exists(key):
                                return  # expired or deleted while we waited
                except (RedisError, OSError, TimeoutError):
                    log.warning("turn event mirror read failed", extra={"turn_id": turn_id})
                    return
                if entries:
                    last_entry_at = self._clock.monotonic()
                elif self._clock.monotonic() - last_entry_at >= self._follow_idle_s:
                    log.warning("turn event mirror idle; remote stream ended", extra={"turn_id": turn_id})
                    return  # the writing pod died mid-turn
                for entry_id, fields in entries:
                    frame_id = int(entry_id.split(b"-", 1)[0])
                    if frame_id != last + 1:
                        raise _Gap
                    frame = frame_from_payload(frame_id, fields[b"t"].decode(), fields[b"p"])
                    last = frame_id
                    yield frame
                    delivered = _ms(self._clock)
                    if frame.type == TERMINAL_EVENT:
                        return
        except _Gap:
            log.warning("turn event mirror has a gap; remote stream ended", extra={"turn_id": turn_id})
        finally:
            self._followers -= 1
            with anyio.CancelScope(shield=True), anyio.move_on_after(1.0):
                await self._set_attach(turn_id, f"d:{delivered or _ms(self._clock)}", ex=DETACHED_TTL_S)

    async def _set_attach(self, turn_id: str, value: str, *, px: int | None = None, ex: int | None = None) -> None:
        try:
            with anyio.fail_after(self._call_s):
                await self._redis.set(self._keys.turn_attach(turn_id), value, px=px, ex=ex)
        except (RedisError, OSError, TimeoutError):
            log.debug("turn_attach update failed", extra={"turn_id": turn_id})

    async def remote_delivery(self, turn_id: str) -> tuple[bool, float | None]:
        """(a remote reader is attached, seconds since a remote reader last got a frame or None)."""
        try:
            with anyio.fail_after(self._call_s):
                raw = await self._redis.get(self._keys.turn_attach(turn_id))
        except (RedisError, OSError, TimeoutError):
            return False, None
        if not raw:
            return False, None
        text = raw.decode() if isinstance(raw, bytes) else str(raw)
        state, _, ms = text.partition(":")
        if state == "a":
            return True, None
        try:
            return False, max(0.0, (_ms(self._clock) - int(ms)) / 1000)
        except ValueError:
            return False, None


class RedisEventMirror(_MirrorReads):
    """The mirror of `InProcessTurnEventLog` (see the module docstring). `redis` is the process's bytes client."""

    def __init__(
        self,
        redis: Redis,
        keys: RedisKeys,
        clock: Clock,
        *,
        maxlen: int = MAXLEN,
        orphan_ttl_s: int = ORPHAN_TTL_S,
        block_ms: int = 5000,
        beat_s: float = 5.0,
        max_pending: int = MAX_PENDING,
        retry_s: float = 1.0,
        follow_idle_s: float = FOLLOW_IDLE_S,
        batch_s: float = BATCH_S,
        call_timeout_s: float = CALL_TIMEOUT_S,
        write_timeout_s: float = WRITE_TIMEOUT_S,
        max_followers: int = MAX_FOLLOWERS,
    ) -> None:
        self._redis = redis
        self._keys = keys
        self._clock = clock
        self._maxlen = maxlen
        self._orphan_ttl_s = orphan_ttl_s
        self._block_ms = block_ms
        self._beat_s = beat_s
        self._max_pending = max_pending
        self._retry_s = retry_s
        self._follow_idle_s = follow_idle_s
        self._batch_s = batch_s
        self._call_s = call_timeout_s
        self._write_s = write_timeout_s
        self._max_followers = max_followers
        self._followers = 0
        self._pending: deque[_Op] = deque()
        self._orphan_ttl_set: set[str] = set()  # unsealed turns whose key already has the orphan TTL
        # one wake-up for the writer's life, cleared per round (a new anyio.Event per batch, plus one per flush below,
        # were 2 anyio and 4 asyncio Events every ~5 ms; E3). Every Noema process and test runs on asyncio.
        self._wake = asyncio.Event()
        self._broken: set[str] = set()
        self._sealed_broken: set[str] = set()  # broken turns sealed since the last batch: forgotten by the next one
        self._to_delete: set[str] = set()
        self._flushing = False
        self._flushed: asyncio.Event | None = None  # created by a flush that waits for the one in flight
        self._write = redis.register_script(WRITE_LUA)  # EVALSHA (loaded again after NOSCRIPT)

    # --- writes (sync enqueue, async flush) ------------------------------------------------------------------------
    def frame(self, turn_id: str, frame: SseFrame) -> None:
        """Queue one frame (sync). A full queue (Redis slow or down) marks the turn broken instead of growing."""
        if turn_id in self._broken:
            return
        if len(self._pending) >= self._max_pending:
            self._mark_broken((turn_id,), "mirror queue full")
            return
        self._pending.append(("x", turn_id, frame, 0))
        self._wake.set()

    def seal(self, turn_id: str, ttl_s: int) -> None:
        """Queue the seal EXPIRE (sync). A broken turn gets none; nothing follows its seal, so the next batch (which
        also drops its frames still queued) forgets it."""
        if turn_id in self._broken:
            self._sealed_broken.add(turn_id)
            self._wake.set()
            return
        self._pending.append(("e", turn_id, None, ttl_s))
        self._wake.set()

    @property
    def pending(self) -> int:
        """Queued operations not yet written."""
        return len(self._pending)

    def _mark_broken(self, turn_ids: Iterable[str], why: str) -> None:
        """Nothing more is queued for these turns and the next batch deletes their keys. Their ops still queued are
        dropped lazily by that batch, so breaking a turn costs O(1), also on the per-frame path of a full queue."""
        for turn_id in turn_ids:
            if turn_id not in self._broken:
                log.warning("turn event mirror broken: %s", why, extra={"turn_id": turn_id})
                self._broken.add(turn_id)
                self._to_delete.add(turn_id)
                self._orphan_ttl_set.discard(turn_id)

    def _drop_broken(self, batch: list[_Op]) -> list[_Op]:
        """`batch` without the ops of broken turns. A broken turn whose seal is dropped here, or that was sealed while
        broken, is forgotten (its key stays in `_to_delete` until deleted)."""
        broken = self._broken
        kept = [op for op in batch if op[1] not in broken]
        broken.difference_update([op[1] for op in batch if op[0] == "e"], self._sealed_broken)
        self._sealed_broken.clear()
        return kept

    async def run(self) -> None:
        """Background writer: flushes the queue whenever it is non-empty, `batch_s` after the wake so the frames of
        every running turn share one script call (after a failed write, `retry_s` later); runs until cancelled. One
        pipeline per frame cost ~18% of the api's CPU at 50 streams (C2b perf smoke); one pipeline per batch still
        ~8%, mostly redis-py packing and parsing one command and reply per frame (E sprint B: the script)."""
        while True:
            if not self._pending and not self._to_delete:
                await self._wake.wait()
                if self._batch_s > 0:
                    await self._clock.sleep(self._batch_s)
            self._wake.clear()
            if not await self.flush():
                await self._clock.sleep(self._retry_s)

    async def flush(self) -> bool:
        """Write everything queued so far (one script call per `SCRIPT_FRAMES` frames; best effort; never raises for
        Redis failures). False when Redis could not be reached. A flush already in flight is awaited instead of
        overlapped."""
        if self._flushing:
            flushed = self._flushed
            if flushed is None:
                flushed = self._flushed = asyncio.Event()
            await flushed.wait()
            return True
        self._flushing = True
        try:
            return await self._write_batch()
        finally:
            self._flushing = False
            flushed, self._flushed = self._flushed, None
            if flushed is not None:
                flushed.set()

    async def drain(self) -> None:
        """Wait until every frame and seal queued so far is written (or dropped as broken): graceful shutdown, and
        tests that read the mirror right after appending."""
        while self._pending or self._flushing:  # a failed flush drops its batch, so this always ends
            await self.flush()
        if self._to_delete:  # one attempt at removing broken turns' keys
            await self.flush()

    async def _write_batch(self) -> bool:
        batch = list(self._pending)
        self._pending.clear()
        if self._broken:
            batch = self._drop_broken(batch)
        deletes = sorted(self._to_delete)
        if not batch and not deletes:
            return True
        frames: dict[str, list[SseFrame]] = {}  # per turn, in append order (a turn's frames are in id order)
        ttls: dict[str, int] = {}
        for kind, turn_id, frame, ttl in batch:
            if kind == "e":
                ttls[turn_id] = ttl
                frames.setdefault(turn_id, [])
            else:
                assert frame is not None
                frames.setdefault(turn_id, []).append(frame)
        sealed = set(ttls)
        fresh = [t for t in frames if t not in sealed and t not in self._orphan_ttl_set]
        for turn_id in fresh:  # once per turn: the seal's EXPIRE replaces it, and no turn outlives it (600 s max)
            ttls[turn_id] = self._orphan_ttl_s
        chunks = _chunks(frames)
        for i, turns in enumerate(chunks):
            try:
                with anyio.fail_after(self._write_s):  # a timed-out call may still apply: its turns are broken
                    failed = await self._write_chunk(deletes if i == 0 else [], turns, frames, ttls)
            except (RedisError, OSError, TimeoutError) as exc:
                unwritten = [t for chunk in chunks[i:] for t in chunk]
                self._mark_broken(unwritten, type(exc).__name__)
                self._broken.difference_update(sealed)  # this batch carried their seal: nothing follows
                written = {t for chunk in chunks[:i] for t in chunk}  # the earlier calls set their TTLs
                self._orphan_ttl_set.update(t for t in fresh if t in written)
                self._orphan_ttl_set.difference_update(sealed)
                return False
            if i == 0:
                self._to_delete.difference_update(deletes)
            if failed:
                self._mark_broken(failed, "write rejected")
                self._broken.difference_update(sealed)
        self._orphan_ttl_set.update(fresh)
        self._orphan_ttl_set.difference_update(sealed)
        return True

    async def _write_chunk(
        self, deletes: list[str], turns: list[str], frames: dict[str, list[SseFrame]], ttls: dict[str, int]
    ) -> list[str]:
        """One script call (WRITE_LUA): the turns whose writes Redis rejected."""
        keys = [self._keys.turn_events(t) for t in (*deletes, *turns)]
        args: list[EncodableT] = [self._maxlen, len(deletes)]
        for turn_id in turns:
            own = frames[turn_id]
            args += (len(own), ttls.get(turn_id, 0))
            for frame in own:
                args += (frame.id, frame.type, frame.payload)
        failed: list[Any] = await self._write(keys=keys, args=args)
        return [turns[int(i) - 1] for i in failed]


def _chunks(frames: dict[str, list[SseFrame]], frames_per_call: int = SCRIPT_FRAMES) -> list[list[str]]:
    """The turns per script call: whole turns, a new call once the current one holds `frames_per_call` frames."""
    out: list[list[str]] = [[]]
    held = 0
    for turn_id, own in frames.items():
        if held >= frames_per_call:
            out.append([])
            held = 0
        out[-1].append(turn_id)
        held += len(own)
    return out
