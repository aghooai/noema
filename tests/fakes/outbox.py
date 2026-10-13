"""`FakeOutbox` (spec-02 §7, spec-11 §3) and `FakeCostMeter` (spec-02 §8, spec-11 §2).

`FakeOutbox.put` never raises and keeps every record in `records` (and `pending`); `drain()` / `flush()` apply the
pending records through an optional async `sink` (e.g. `FakeSystemCalls.outbox_apply` of the fake DB), keeping the
rejected ones pending; `sink_down = True` simulates both sinks unavailable (records stay pending, flush logs them
as lost). `FakeCostMeter` prices with the snapshot's catalog in Decimal (IMPL-019) and records usage lines through
the outbox.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Final

import anyio
import anyio.lowlevel

from noema.core.clock import Clock
from noema.core.protocols.config import ConfigRegistry, ConfigSnapshot
from noema.core.protocols.ops import UsageLine
from noema.core.protocols.storage import OutboxRecord
from noema.core.protocols.telemetry import Instruments
from noema.core.values import Cost, JsonValue, UsageRecord

Sink = Callable[[Sequence[OutboxRecord]], Awaitable[tuple[str | None, ...]]]
_log: Final = logging.getLogger("tests.fakes.outbox")
MICRO: Final = Decimal("0.000001")


class FakeOutbox:
    """Implements `Outbox` (see the module docstring)."""

    def __init__(self, sink: Sink | None = None) -> None:
        self.sink = sink
        self.records: list[OutboxRecord] = []
        self.pending: list[OutboxRecord] = []
        self.applied: list[OutboxRecord] = []
        self.lost: list[OutboxRecord] = []
        self.sink_down = False

    def put(self, record: OutboxRecord) -> None:
        self.records.append(record)
        self.pending.append(record)

    def of_kind(self, kind: str) -> list[OutboxRecord]:
        """Every record put with this kind, in put order."""
        return [r for r in self.records if r.kind == kind]

    async def drain(self) -> int:
        """Apply the pending records through the sink; returns how many were applied."""
        await anyio.lowlevel.checkpoint()
        if self.sink is None or self.sink_down or not self.pending:
            return 0
        batch, self.pending = self.pending, []
        results = await self.sink(batch)
        applied = 0
        for record, error in zip(batch, results, strict=True):
            if error is None:
                self.applied.append(record)
                applied += 1
            else:
                self.pending.append(record)
        return applied

    async def flush(self, timeout_s: float) -> None:
        """Apply what is pending (shielded); what the sink cannot take is logged as lost. Without a sink the
        outbox only collects, so flush keeps the records inspectable."""
        if self.sink is None:
            return
        with anyio.CancelScope(shield=True):
            if not self.sink_down:
                await self.drain()
        for record in self.pending:
            _log.error("outbox record lost at shutdown: kind=%s id=%s", record.kind, record.id)
        self.lost.extend(self.pending)
        self.pending = []


def usage_line_payload(line: UsageLine, at_iso: str) -> dict[str, JsonValue]:
    """The `noema.usage_line` row of a usage line (Decimal as string, RFC 3339 UTC; spec-11 §3)."""
    u = line.usage
    return {
        "tenant_id": line.tenant_id,
        "project_id": line.project_id,
        "id": line.id,
        "line": line.line,
        "model_id": line.model_id,
        "region": line.region,
        "session_id": line.session_id,
        "turn_id": line.turn_id,
        "decision_id": line.decision_id,
        "attempt_id": line.attempt_id,
        "labeling_run_id": line.labeling_run_id,
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cached_input_tokens": u.cached_input_tokens,
        "cache_write_input_tokens": u.cache_write_input_tokens,
        "reasoning_tokens": u.reasoning_tokens,
        "tokens_estimated": u.tokens_estimated,
        "cost_usd": str(line.cost.usd),
        "cost_estimated": line.cost.estimated,
        "at": at_iso,
    }


class FakeCostMeter:
    """Implements `CostMeter`: `price` per spec-11 §2 (08:L508), `record` -> outbox + `aghoo_cost_usd_total`."""

    def __init__(
        self, clock: Clock, config: ConfigRegistry, outbox: FakeOutbox, *, instruments: Instruments | None = None
    ) -> None:
        self._clock = clock
        self._config = config
        self._outbox = outbox
        self._instruments = instruments
        self.lines: list[UsageLine] = []

    def price(
        self, model_alias: str, region: str, usage: UsageRecord, *, snapshot: ConfigSnapshot | None = None
    ) -> Cost:
        snap = snapshot if snapshot is not None else self._config.current()
        model = snap.model(model_alias)
        entry = snap.prices.lookup(model.price_ref or model.id, region, self._clock.now())
        p = entry.usd_per_mtok if entry is not None else None
        uncached = usage.input_tokens - usage.cached_input_tokens - usage.cache_write_input_tokens
        parts = [
            (uncached, None if p is None else p.input),
            (usage.cached_input_tokens, None if p is None else p.cache_read),
            (usage.cache_write_input_tokens, None if p is None else p.cache_write_5m),
            (usage.output_tokens, None if p is None else p.output),
        ]
        estimated = usage.tokens_estimated
        with localcontext() as ctx:
            ctx.prec = 28
            total = Decimal(0)
            for tokens, rate in parts:
                if rate is None:
                    estimated = estimated or tokens > 0
                else:
                    total += Decimal(tokens) * rate
            usd = (total / Decimal(1_000_000)).quantize(MICRO, rounding=ROUND_HALF_EVEN)
        return Cost(usd=usd, estimated=estimated)

    def record(self, line: UsageLine) -> None:
        at = (line.at or self._clock.now()).astimezone(UTC).isoformat().replace("+00:00", "Z")
        self.lines.append(line)
        self._outbox.put(
            OutboxRecord(kind="usage_line", id=line.id, tenant_id=line.tenant_id, payload=usage_line_payload(line, at))
        )
        if self._instruments is not None:
            self._instruments.add("aghoo_cost_usd_total", float(line.cost.usd), tenant=line.tenant_id, line=line.line)
