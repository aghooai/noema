"""The mutable state of one mock instance: the loaded scenario and its runtime (`nth`, draw counters, `window_ms`
origin), barriers, the request log and the per-(AKID, model) quotas. All methods run on the server's event loop."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from tests.fakes.scenario import Scenario, ScenarioRuntime

from .clock import MockClock

DEFAULT_RECORD_LIMIT: Final = 50_000
DEFAULT_RECORD_BYTES: Final = 256 * 1024 * 1024
QUOTA_WINDOW_MS: Final = 60_000.0
_EVICTED: Final[dict[str, Any]] = {}
_COMPACT_AT: Final = 1024


class RequestLog:
    """A ring of request records (JSON-ready dicts), bounded by count (`limit`) and by approximate retained bytes
    (`max_bytes`, the sizes passed to `add`/`charge`). `seq` is 1-based, dense and never reused; evicted records
    (oldest first) are counted in `dropped`. `since` costs O(page), never O(ring)."""

    def __init__(self, limit: int = DEFAULT_RECORD_LIMIT, max_bytes: int = DEFAULT_RECORD_BYTES) -> None:
        if limit < 1 or max_bytes < 1:
            raise ValueError("the record limit and the record byte budget must be >= 1")
        self.limit = limit
        self.max_bytes = max_bytes
        self._records: list[dict[str, Any]] = []  # [_head:] are held, oldest first; [:_head] are evicted
        self._sizes: list[int] = []
        self._head = 0
        self._seq = 0
        self.bytes = 0
        self.dropped = 0

    def __len__(self) -> int:
        return len(self._records) - self._head

    def add(self, record: dict[str, Any], size: int = 0) -> dict[str, Any]:
        """Assign `seq` and append (the caller keeps updating the same dict while the request runs); `size` is the
        record's approximate retained bytes."""
        self._seq += 1
        record["seq"] = self._seq
        self._records.append(record)
        self._sizes.append(size)
        self.bytes += size
        self._evict()
        return record

    def charge(self, record: dict[str, Any], size: int) -> None:
        """Add `size` bytes to a held record (e.g. its body, parsed after `add`); a no-op once it was evicted."""
        i = self._head + record["seq"] - self.first_seq
        if self._head <= i < len(self._records) and self._records[i] is record:
            self._sizes[i] += size
            self.bytes += size
            self._evict()

    def since(self, after: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
        """Copies of at most `limit` records with seq > after, oldest first (ValueError if either is negative)."""
        if after < 0 or (limit is not None and limit < 0):
            raise ValueError("after and limit must be >= 0")
        if limit == 0 or after >= self._seq or not len(self):
            return []
        start = self._head + max(0, after - self.first_seq + 1)
        stop = len(self._records) if limit is None else min(len(self._records), start + limit)
        return [dict(r) for r in self._records[start:stop]]

    def clear(self) -> None:
        self._records, self._sizes, self._head = [], [], 0
        self.bytes = 0
        self.dropped = 0

    @property
    def first_seq(self) -> int:
        """The seq of the oldest held record (last_seq + 1 when empty)."""
        return self._seq - len(self) + 1

    @property
    def last_seq(self) -> int:
        return self._seq

    def _evict(self) -> None:
        while len(self) > self.limit or (self.bytes > self.max_bytes and len(self) > 1):
            self.bytes -= self._sizes[self._head]
            self._records[self._head] = _EVICTED
            self._head += 1
            self.dropped += 1
        if self._head >= _COMPACT_AT and 2 * self._head >= len(self._records):
            del self._records[: self._head]
            del self._sizes[: self._head]
            self._head = 0


SHARED: Final = "*"  # a quota `akid` that shares one window over every AKID (Bedrock quotas are per account)


@dataclass(frozen=True, slots=True)
class Quota:
    """A Bedrock quota for one model: requests and tokens (input estimate + max_tokens) per 60 s, per AKID
    (`akid` None applies to every AKID, each with its own window; `akid` "*" is one window shared by every AKID, a
    per-account quota when every caller is one account's STS sessions, QL-4)."""

    model: str
    rpm: int | None = None
    tpm: int | None = None
    akid: str | None = None

    @staticmethod
    def parse(raw: Mapping[str, Any]) -> Quota:
        unknown = set(raw) - {"model", "rpm", "tpm", "akid"}
        if unknown or not isinstance(raw.get("model"), str):
            raise ValueError(f"a quota needs 'model' and only rpm/tpm/akid (unknown: {sorted(unknown)})")
        for key in ("rpm", "tpm"):
            value = raw.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                raise ValueError(f"quota {key} must be an integer >= 1")
        return Quota(raw["model"], raw.get("rpm"), raw.get("tpm"), raw.get("akid"))


class QuotaBook:
    """Sliding 60 s windows per (AKID, model); a request that would exceed rpm or tpm is refused uncounted. `now_ms`
    is absolute (the mock clock, not the scenario origin), so windows stay valid across scenario loads."""

    def __init__(self) -> None:
        self._quotas: dict[tuple[str | None, str], Quota] = {}
        self._windows: dict[tuple[str, str], deque[tuple[float, int]]] = {}
        self._tokens: dict[tuple[str, str], int] = {}

    def set(self, quotas: Iterable[Quota]) -> None:
        """Replace every quota (and forget the windows)."""
        self._quotas = {(q.akid, q.model): q for q in quotas}
        self._windows.clear()
        self._tokens.clear()

    def admit(self, akid: str | None, model: str, tokens: int, now_ms: float) -> Literal["rpm", "tpm"] | None:
        """None if admitted (and counted), else the exhausted dimension."""
        quota = self._quotas.get((akid, model)) or self._quotas.get((SHARED, model)) or self._quotas.get((None, model))
        if quota is None:
            return None
        key = (SHARED if quota.akid == SHARED else akid or "", model)
        window = self._windows.setdefault(key, deque())
        used = self._tokens.get(key, 0)
        while window and window[0][0] <= now_ms - QUOTA_WINDOW_MS:
            used -= window.popleft()[1]
        if quota.rpm is not None and len(window) >= quota.rpm:
            self._tokens[key] = used
            return "rpm"
        if quota.tpm is not None and used + tokens > quota.tpm:
            self._tokens[key] = used
            return "tpm"
        window.append((now_ms, tokens))
        self._tokens[key] = used + tokens
        return None


class MockState:
    """Scenario, barriers, request log and quotas of one mock instance."""

    def __init__(
        self, clock: MockClock, *, record_limit: int = DEFAULT_RECORD_LIMIT, record_bytes: int = DEFAULT_RECORD_BYTES
    ) -> None:
        self.clock = clock
        self.scenario: Scenario | None = None
        self.runtime: ScenarioRuntime | None = None
        self.log = RequestLog(record_limit, record_bytes)
        self.quotas = QuotaBook()
        self._barriers: dict[str, asyncio.Event] = {}
        self.t0_ns = clock.now_ns()

    def now_ms(self) -> float:
        """Milliseconds since the last scenario load or reset (`window_ms` origin, record `at_ms`)."""
        return (self.clock.now_ns() - self.t0_ns) / 1e6

    def load(self, scenario: Scenario) -> None:
        """Install a scenario with fresh runtime state (nth counters, draws, barriers, window origin); the request
        log and the quotas are kept. Requests held on the old barriers are released."""
        self._release_all()
        self.t0_ns = self.clock.now_ns()
        self.scenario = scenario
        self.runtime = ScenarioRuntime(scenario, now_ms=self.now_ms)

    def reset(self) -> None:
        """Forget the scenario, the request log and the quotas (between tests)."""
        self._release_all()
        self.scenario = None
        self.runtime = None
        self.log.clear()
        self.quotas.set(())
        self.t0_ns = self.clock.now_ns()

    def release(self, name: str) -> None:
        """Release a barrier: held and future requests on it proceed."""
        if self.runtime is not None:
            self.runtime.release(name)
        self._barriers.setdefault(name, asyncio.Event()).set()

    async def wait_barrier(self, name: str) -> None:
        """Hold until `name` is released (the handler is cancelled if the client goes away)."""
        if self.runtime is not None and self.runtime.is_released(name):
            return
        await self._barriers.setdefault(name, asyncio.Event()).wait()

    def _release_all(self) -> None:
        for event in self._barriers.values():
            event.set()
        self._barriers = {}
