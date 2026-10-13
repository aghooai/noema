"""The price catalog (IMPL-019, spec-05 §2).

A catalog is a separate versioned document (`prices.yaml`, validated by `prices.schema.json`), pinned by a config
through `platform.cost.prices_ref`. Prices are `Decimal` USD per million tokens; a "TBD" or missing component is
None (it prices at 0 with `cost_estimated`, which the CostMeter applies). Pricing math is the CostMeter's job.
"""

from __future__ import annotations

import bisect
import datetime as dt
import functools
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Literal

import jsonschema_rs
import yaml

PRICES_SCHEMA_PATH: Final = Path(__file__).resolve().parent / "prices.schema.json"
_COMPONENTS: Final = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")


class PriceCatalogError(ValueError):
    """A catalog document failed validation; `errors` lists every problem."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors: tuple[str, ...] = tuple(errors)


@dataclass(frozen=True, slots=True)
class UsdPerMtok:
    """USD per million tokens per usage category; None = TBD or absent."""

    input: Decimal | None
    output: Decimal | None
    cache_read: Decimal | None = None
    cache_write_5m: Decimal | None = None
    cache_write_1h: Decimal | None = None


@dataclass(frozen=True, slots=True)
class PriceEntry:
    """One price row, keyed by (model, region) and effective from a UTC date."""

    model: str
    region: str
    tier: Literal["on_demand"]
    effective_from: dt.date
    usd_per_mtok: UsdPerMtok


@dataclass(frozen=True, slots=True)
class PriceCatalog:
    """An immutable catalog. `version` is the platform.price_catalog version (None for a file or empty catalog)."""

    entries: tuple[PriceEntry, ...]
    version: int | None = None
    _index: Mapping[tuple[str, str], tuple[PriceEntry, ...]] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        index: dict[tuple[str, str], list[PriceEntry]] = {}
        for e in sorted(self.entries, key=lambda e: e.effective_from):
            index.setdefault((e.model, e.region), []).append(e)
        object.__setattr__(self, "_index", {k: tuple(v) for k, v in index.items()})

    def lookup(self, model_id: str, region: str, at: dt.datetime) -> PriceEntry | None:
        """The newest entry for (model_id, region) with `effective_from <= at` (UTC date); None if there is none
        (every component is then TBD). Sync."""
        rows = self._index.get((model_id, region))
        if not rows:
            return None
        day = at.astimezone(dt.UTC).date() if at.tzinfo is not None else at.date()
        i = bisect.bisect_right([r.effective_from for r in rows], day)
        return rows[i - 1] if i else None


EMPTY_CATALOG: Final = PriceCatalog(entries=())


@functools.cache
def _validator() -> jsonschema_rs.Validator:
    schema = json.loads(PRICES_SCHEMA_PATH.read_text())
    return jsonschema_rs.Draft202012Validator(schema, validate_formats=True, offline=True)


def _plain(doc: Any) -> Any:
    """YAML dates become ISO strings, so a YAML file and its JSON form validate the same way."""
    if isinstance(doc, Mapping):
        return {str(k): _plain(v) for k, v in doc.items()}
    if isinstance(doc, list):
        return [_plain(v) for v in doc]
    if isinstance(doc, dt.date):
        return doc.isoformat()
    return doc


def _price(raw: str | None) -> Decimal | None:
    return None if raw is None or raw == "TBD" else Decimal(raw)


def parse_price_catalog(doc: Mapping[str, Any], *, version: int | None = None) -> PriceCatalog:
    """Validate a catalog document (prices.yaml content, or the JSON body of a platform.price_catalog row) and
    build the catalog. Raises PriceCatalogError listing every problem, including duplicate
    (model, region, tier, effective_from) rows."""
    plain = _plain(doc)
    errors = [
        f"prices: {'/'.join(map(str, e.instance_path)) or '<root>'}: {e.message[:160]}"
        for e in _validator().iter_errors(plain)
    ]
    if errors:
        raise PriceCatalogError(errors)
    entries: list[PriceEntry] = []
    seen: set[tuple[str, str, str, str]] = set()
    for i, raw in enumerate(plain["entries"]):
        tier = raw.get("tier", "on_demand")
        key = (raw["model"], raw["region"], tier, raw["effective_from"])
        if key in seen:
            errors.append(f"prices: entries/{i}: duplicate entry for {key}")
        seen.add(key)
        prices = raw["usd_per_mtok"]
        entries.append(
            PriceEntry(
                model=raw["model"],
                region=raw["region"],
                tier=tier,
                effective_from=dt.date.fromisoformat(raw["effective_from"]),
                usd_per_mtok=UsdPerMtok(**{c: _price(prices.get(c)) for c in _COMPONENTS}),
            )
        )
    if errors:
        raise PriceCatalogError(errors)
    return PriceCatalog(entries=tuple(entries), version=version)


def load_price_file(path: Path) -> PriceCatalog:
    """Read and parse a prices.yaml file (the `file:<path>` form, dev/test only: P1-P1)."""
    try:
        doc = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise PriceCatalogError([f"prices: cannot read {path}: {type(exc).__name__}"]) from exc
    if not isinstance(doc, Mapping):
        raise PriceCatalogError([f"prices: {path} is not a mapping"])
    return parse_price_catalog(doc)
