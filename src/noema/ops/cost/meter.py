"""`LedgerCostMeter`: the `CostMeter` of spec-02 §8 (spec-11 §2, IMPL-019, IMPL-078).

`price(model_alias, region, usage, *, snapshot=None)` (sync, in memory) prices one call against the price catalog
of `snapshot`, the caller's pinned config (IMPL-019: decision, generation and labeling pass `ctx.snapshot`, so a
config activation during a turn never reprices the rest of it); None = the current snapshot. Catalog key
`models.<alias>.price_ref` else `models.<alias>.id` (the Bedrock id), plus `region`, newest `effective_from <= now`.
`region` must be the tenant's `home_region` (spec-11 §2, IMPL-019), never the region that served the call:
catalogs are keyed by home region, so a serving region (cross-region inference) finds no entry and the call is
priced 0, estimated. In `Decimal` (28 digits), per million tokens (08:L508):

    usd = ((input - cached - cache_write) * p_in + cached * p_cache_read + cache_write * p_cache_write_5m
           + output * p_out) / 10^6,  rounded half-even to 1e-6 per call

Phase 1 sets no cache points, so cache writes are implicit 5-minute writes (`cache_write_5m`; the 1-hour rate is
never applied). `estimated` is true when the usage is estimated, when any component with a non-zero token count has
no price (missing entry, missing field or "TBD", priced 0), or when the usage is inconsistent (cache tokens above
the input total: the uncached part is clamped to 0).

An alias the snapshot does not have (a caller without a pinned snapshot whose alias left the current config) is
priced 0, estimated, with one WARNING per alias, never an exception on this sync hot path.

`record(line)` (sync) puts one `usage_line` outbox record keyed by the line id and adds the cost to
`aghoo_cost_usd_total{tenant, line}`.
"""

from __future__ import annotations

import logging
from decimal import ROUND_HALF_EVEN, Context, Decimal
from typing import Final

from noema.core.clock import Clock
from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.config import ConfigRegistry, ConfigSnapshot
from noema.core.protocols.ops import UsageLine
from noema.core.protocols.storage import Outbox
from noema.core.protocols.telemetry import Instruments
from noema.core.values import Cost, UsageRecord
from noema.ops.cost.lines import usage_line_record

log: Final = logging.getLogger("noema.ops.cost")
MICRO: Final = Decimal("0.000001")
_UNPRICED: Final = Cost(usd=Decimal("0.000000"), estimated=True)
_MTOK: Final = Decimal(1_000_000)
_CTX: Final = Context(prec=28, rounding=ROUND_HALF_EVEN)


class LedgerCostMeter:
    """Implements `CostMeter` (see the module docstring). Never touches the network."""

    __slots__ = ("_clock", "_config", "_instruments", "_outbox", "_unknown")

    def __init__(self, clock: Clock, config: ConfigRegistry, outbox: Outbox, instruments: Instruments) -> None:
        self._clock = clock
        self._config = config
        self._outbox = outbox
        self._instruments = instruments
        self._unknown: set[str] = set()  # aliases already warned about

    def price(
        self, model_alias: str, region: str, usage: UsageRecord, *, snapshot: ConfigSnapshot | None = None
    ) -> Cost:
        """The cost of one call with the catalog of `snapshot` (None = the current one); `region` = the tenant's
        `home_region`. An alias the snapshot lacks costs 0, estimated (never raises)."""
        snap = snapshot if snapshot is not None else self._config.current()
        try:
            model = snap.model(model_alias)
        except NoemaError as exc:
            if exc.code is not ErrorCode.NOT_FOUND:
                raise
            if model_alias not in self._unknown:
                self._unknown.add(model_alias)
                log.warning("cost: model alias %r is not in the config snapshot; priced 0, estimated", model_alias)
            return _UNPRICED
        entry = snap.prices.lookup(model.price_ref or model.id, region, self._clock.now())
        rates = entry.usd_per_mtok if entry is not None else None
        uncached = usage.input_tokens - usage.cached_input_tokens - usage.cache_write_input_tokens
        estimated = usage.tokens_estimated or uncached < 0
        parts = (
            (max(uncached, 0), None if rates is None else rates.input),
            (usage.cached_input_tokens, None if rates is None else rates.cache_read),
            (usage.cache_write_input_tokens, None if rates is None else rates.cache_write_5m),
            (usage.output_tokens, None if rates is None else rates.output),
        )
        total = Decimal(0)
        for tokens, rate in parts:
            if rate is None:
                estimated = estimated or tokens > 0
            elif tokens:
                total = _CTX.add(total, _CTX.multiply(Decimal(tokens), rate))
        usd = _CTX.divide(total, _MTOK).quantize(MICRO, rounding=ROUND_HALF_EVEN, context=_CTX)
        return Cost(usd=usd, estimated=estimated)

    def record(self, line: UsageLine) -> None:
        """Outbox `usage_line` record (`at` defaults to `Clock.now()`) + `aghoo_cost_usd_total{tenant, line}`."""
        self._outbox.put(usage_line_record(line, line.at or self._clock.now()))
        self._instruments.add("aghoo_cost_usd_total", float(line.cost.usd), tenant=line.tenant_id, line=line.line)
