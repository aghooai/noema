"""The live-smoke call budget and cassette recorder (spec-10 §6, §2.3).

`CountingAdapterRegistry(inner, ledger)` wraps an `AdapterRegistry`: every `Adapter.complete()` / `Adapter.stream()`
entry is one call (backups, fallbacks and restarts included; an in-adapter connect retry is not a new call). Before
any byte, `ledger.take()` refuses the call over the limit (`LiveBudgetExceeded`, which also stops the session),
an alias outside the 7 configured models, and the request's `max_tokens` is capped (256; 512 for generation; 1,024
for decision and verification members, `MAX_TOKENS_DECISION`; for labeling the labeler's own 16 + reasoning
reserve, `MAX_TOKENS_LABELING`, L9). With a `journal` (the live run:
`JOURNAL`, `.orchestration/reports/live-calls.jsonl`) the limit is global across every run: each call appends one
JSON line `{ts, model, alias, operation, test}` before any byte (`model`: the id the request resolves to, e.g. an
inference-profile ARN), and the count is the journal's line count. Each call is recorded as a redacted cassette
(shapes, event counts, timings, usage, `input_chars` (K1), the wire-shape tap of
`tests/live/wiretap.py`, error kind and redacted message; never prompt or answer text) under
`tests/cassettes/<mode>-<run>/` (mode `live`, or `mock` for the wire-mock rehearsal), and `ledger.write_report()`
writes `reports/live/calls.json`.
"""

from __future__ import annotations

import dataclasses
import os
import time
from collections import Counter
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any, Final

import orjson
import pytest
from tests.live.wiretap import open_tap

from noema.config.models import EFFORT_RESERVE
from noema.core.clock import Deadline
from noema.core.errors import ClassifiedError
from noema.core.principal import TenantRef
from noema.core.protocols.adapters import (
    Adapter,
    AdapterError,
    AdapterRegistry,
    ErrorEvent,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    Stop,
    StreamEvent,
    StreamTimeouts,
    TextDelta,
    UsageEvent,
)
from noema.core.protocols.ops import TokenEstimator
from noema.core.values import TextBlock, UsageRecord
from noema.labeling.labelers import ANSWER_TOKENS, LABELER_EFFORT
from noema.ops.cost.estimator import HeuristicTokenEstimator
from noema.telemetry.redact import Redactor

# The global hard stop over every run (spec-10 §6): 60 for the Wave E smoke (57 used); the owner approved +30
# calls for the Wave E3 re-verification on 2026-10-12 (IMPL-080 amendment), so 87 ledger entries in total.
LIMIT: Final = 87
MODEL_IDS: Final[Mapping[str, str]] = {
    "haiku-5.5": "us.anthropic.claude-haiku-5-5",
    "luna-6": "us.openai.gpt-6-luna",
    "deepseek-3.2": "deepseek.v3.2",
    "sol-6.1": "us.openai.gpt-6.1-sol",
    "opus-5.5": "us.anthropic.claude-opus-5-5",
    "glm-5.3": "us.zai.glm-5.3",
    "sonnet-5.5": "us.anthropic.claude-sonnet-5-5",
}  # the 7 configured models (platform.example.yaml)
ALLOWED_ALIASES: Final = frozenset(MODEL_IDS)
MAX_TOKENS: Final = 256
MAX_TOKENS_GENERATION: Final = 512
MAX_TOKENS_LABELING: Final = ANSWER_TOKENS + EFFORT_RESERVE[LABELER_EFFORT]  # labeler@1 at its effort (L9)
# Decision and verification members: the config's own budgets must reach Bedrock uncut (CR #26: Haiku's
# verify-dist@1 at low is 64 + 12*2 + 512 = 600 tokens, its router intent call 696); 256 would cut them.
MAX_TOKENS_DECISION: Final = 1024
CHARS4: Final = HeuristicTokenEstimator({})  # the IMPL-029c V1 rule (chars/4, no overhead): K1's before
ROOT: Final = Path(__file__).resolve().parents[2]
JOURNAL: Final = ROOT / ".orchestration" / "reports" / "live-calls.jsonl"


def model_id(req: ModelRequest) -> str:
    """The Bedrock id the request resolves to: its pinned snapshot's (an override such as an inference-profile ARN),
    else the configured one."""
    return req.snapshot.model(req.model_alias).id if req.snapshot is not None else MODEL_IDS[req.model_alias]


def input_chars(req: ModelRequest) -> int:
    """K1: the code points of the system strings and text blocks (what the estimator's text term counts)."""
    texts = [b.text for m in req.messages for b in m.content if isinstance(b, TextBlock)]
    return sum(map(len, req.system)) + sum(map(len, texts))


def current_test() -> str:
    """The running pytest node id (`PYTEST_CURRENT_TEST` without its phase), or "" outside a test."""
    return os.environ.get("PYTEST_CURRENT_TEST", "").rsplit(" ", 1)[0]


def journal_entries(path: Path) -> int:
    """The number of calls the persistent journal holds (0 when it does not exist)."""
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text().splitlines() if line.strip())


class LiveBudgetExceeded(RuntimeError):
    """The process-wide live call budget is spent (or a request broke the allowlist): nothing was sent."""


@dataclasses.dataclass
class Ledger:
    """Process-wide call counter and cassette log (one per pytest session)."""

    run: str
    root: Path = ROOT
    limit: int = LIMIT
    journal: Path | None = None  # the cross-run journal (JSON lines); None = this process only
    mode: str = "live"  # cassette directory prefix: live | mock
    count: int = 0
    exceeded: bool = False
    calls: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    taken: list[dict[str, str]] = dataclasses.field(default_factory=list)  # every charged call, in take() order
    estimator: TokenEstimator | None = None
    _redactor: Redactor = dataclasses.field(default_factory=Redactor)

    def used(self) -> int:
        """Calls charged so far: the journal's (every run) or this process's."""
        return journal_entries(self.journal) if self.journal is not None else self.count

    def remaining(self) -> int:
        return max(self.limit - self.used(), 0)

    def need(self, worst: int) -> None:
        """Skip the calling test (before any call) when fewer than `worst` calls are left in the budget."""
        if self.remaining() < worst:
            pytest.skip(f"live budget: {self.remaining()} calls left < this test's worst case {worst}")

    def take(self, req: ModelRequest, op: str = "complete") -> ModelRequest:
        """Account for one call before any byte; returns the request with `max_tokens` capped."""
        if req.model_alias not in ALLOWED_ALIASES:
            raise LiveBudgetExceeded(f"model alias {req.model_alias!r} is not on the live allowlist")
        if self.used() >= self.limit:
            self.exceeded = True
            raise LiveBudgetExceeded(f"live call budget of {self.limit} calls is spent")
        self.count += 1
        self.taken.append({"test": current_test(), "alias": req.model_alias, "op": op, "call_kind": req.call_kind})
        if self.journal is not None:
            line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "model": model_id(req),
                    "alias": req.model_alias, "operation": op, "test": current_test(), "run": self.run}  # fmt: skip
            self.journal.parent.mkdir(parents=True, exist_ok=True)
            with self.journal.open("ab") as fh:
                fh.write(orjson.dumps(line) + b"\n")
        caps = {"generation": MAX_TOKENS_GENERATION, "labeling": MAX_TOKENS_LABELING,
                "decision_member": MAX_TOKENS_DECISION, "verification_member": MAX_TOKENS_DECISION}  # fmt: skip
        cap = caps.get(req.call_kind, MAX_TOKENS)
        return dataclasses.replace(req, max_tokens=min(req.max_tokens, cap))

    def _estimate(self, req: ModelRequest, estimator: TokenEstimator | None) -> int | None:
        if estimator is None:
            return None
        blocks = [b for m in req.messages for b in m.content]
        return estimator.estimate(req.model_alias, req.system, blocks)

    def record(self, req: ModelRequest, op: str, entry: dict[str, Any], *, test: str | None = None) -> None:
        """One cassette; `test` = the node id at `take()` (a call may end after its test)."""
        error = entry.pop("error_obj", None)
        if isinstance(error, ClassifiedError):
            entry["error"] = {
                "kind": error.kind.value,
                "provider_code": error.provider_code,
                "message": self._redactor.redact(error.message),
            }
        test = current_test() if test is None else test
        cassette = {
            "n": len(self.calls) + 1,
            "test": test,
            "test_call": 1 + sum(1 for c in self.calls if c["test"] == test),
            "alias": req.model_alias,
            "model_id": model_id(req),
            "op": op,
            "call_kind": req.call_kind,
            "effort": req.effort,
            "max_tokens": req.max_tokens,
            "blocks": dict(Counter(type(b).__name__ for m in req.messages for b in m.content)),
            "estimated_input_tokens": self._estimate(req, self.estimator),  # the session's estimator (K1)
            "estimated_input_tokens_chars4": self._estimate(req, CHARS4 if self.estimator is not None else None),
            "input_chars": input_chars(req),
            **entry,
        }
        self.calls.append(cassette)
        directory = self.root / "tests" / "cassettes" / f"{self.mode}-{self.run}"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{cassette['n']:02d}-{req.model_alias}-{op}.json"
        path.write_bytes(orjson.dumps(cassette, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))

    def report_path(self, name: str) -> Path:
        """`reports/live/<name>` (the mock rehearsal: `reports/live/mock/<name>`)."""
        base = self.root / "reports" / "live"
        return base / name if self.mode == "live" else base / self.mode / name

    def write_report(self, name: str = "calls.json", body: Mapping[str, Any] | None = None) -> Path:
        """Write `report_path(name)` (default: the call count and every cassette)."""
        path = self.report_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        per_test = dict(Counter(t["test"] for t in self.taken))
        summary = {"count": self.count, "limit": self.limit, "per_test": per_test, "calls": self.calls}
        payload = body if body is not None else summary
        path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))
        return path


def _usage(u: UsageRecord) -> dict[str, int | bool]:
    return dataclasses.asdict(u)


class CountingAdapter:
    """An `Adapter` that charges every entry to the ledger and records its cassette."""

    def __init__(self, inner: Adapter, ledger: Ledger) -> None:
        self._inner = inner
        self._ledger = ledger

    def capabilities(self, alias: str) -> ModelCapabilities:
        return self._inner.capabilities(alias)

    def classify_error(self, exc: BaseException) -> ClassifiedError:
        return self._inner.classify_error(exc)

    async def complete(self, req: ModelRequest, *, tenant: TenantRef, deadline: Deadline) -> ModelResponse:
        capped, test = self._ledger.take(req, "complete"), current_test()
        t0 = time.monotonic()
        tap = open_tap()
        try:
            resp = await self._inner.complete(capped, tenant=tenant, deadline=deadline)
        except AdapterError as exc:
            entry_err = {"e2e_ms": _ms(t0), "error_obj": exc.error, "wire": tap.as_json()}
            self._ledger.record(capped, "complete", entry_err, test=test)
            raise
        except BaseException:  # cancelled by the caller (deadline, quorum, race loser): still a model call
            self._ledger.record(
                capped, "complete", {"e2e_ms": _ms(t0), "cancelled": True, "wire": tap.as_json()}, test=test
            )
            raise
        entry = {
            "wire": tap.as_json(),
            "e2e_ms": _ms(t0),
            "latency_ms": resp.latency_ms,
            "stop": resp.stop_reason,
            "refused": resp.refused,
            "text_chars": len(resp.text),
            "logprob_tokens": None if resp.logprobs is None else len(resp.logprobs),
            "usage": _usage(resp.usage),
            "region": resp.region,
        }
        self._ledger.record(capped, "complete", entry, test=test)
        return resp

    def stream(
        self, req: ModelRequest, *, tenant: TenantRef, timeouts: StreamTimeouts, deadline: Deadline
    ) -> AbstractAsyncContextManager[AsyncIterator[StreamEvent]]:
        return self._stream(req, tenant, timeouts, deadline)

    @asynccontextmanager
    async def _stream(
        self, req: ModelRequest, tenant: TenantRef, timeouts: StreamTimeouts, deadline: Deadline
    ) -> AsyncIterator[AsyncIterator[StreamEvent]]:
        capped, test = self._ledger.take(req, "stream"), current_test()
        t0 = time.monotonic()
        entry: dict[str, Any] = {"events": [], "counts": Counter[str]()}
        tap = open_tap()
        try:
            async with self._inner.stream(capped, tenant=tenant, timeouts=timeouts, deadline=deadline) as events:

                async def observed() -> AsyncIterator[StreamEvent]:
                    async for ev in events:
                        name = type(ev).__name__
                        if not entry["counts"][name]:
                            entry["events"].append(name)
                            entry[f"t_first_{name}_ms"] = _ms(t0)
                        entry["counts"][name] += 1
                        if isinstance(ev, Stop):
                            entry["stop"] = ev.reason
                        elif isinstance(ev, UsageEvent):
                            entry["usage"] = _usage(ev.usage)
                        elif isinstance(ev, ErrorEvent):
                            entry["error_obj"] = ev.error
                        elif isinstance(ev, TextDelta):
                            entry["text_chars"] = entry.get("text_chars", 0) + len(ev.text)
                        yield ev

                yield observed()
        except AdapterError as exc:
            entry["error_obj"] = exc.error
            raise
        except BaseException as exc:  # the consumer left by an exception (cancel scope, race loser, restart)
            entry["cancelled"] = type(exc).__name__
            raise
        finally:
            entry["e2e_ms"] = _ms(t0)
            entry["counts"] = dict(entry["counts"])
            entry["wire"] = tap.as_json()
            self._ledger.record(capped, "stream", entry, test=test)


class CountingAdapterRegistry:
    """An `AdapterRegistry` whose adapters charge the live ledger (spec-10 §6)."""

    def __init__(self, inner: AdapterRegistry, ledger: Ledger) -> None:
        self._inner = inner
        self._ledger = ledger

    def for_model(self, alias: str) -> Adapter:
        return CountingAdapter(self._inner.for_model(alias), self._ledger)


def _ms(t0: float) -> int:
    return round((time.monotonic() - t0) * 1000)
