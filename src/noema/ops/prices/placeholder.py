"""A placeholder price catalog for a config (IMPL-019, spec-05 §2): one entry per (price key, region) with every
component "TBD", so each priced call costs 0 with `cost_estimated: true` until real prices are imported.

Operator tooling only (never imported at runtime): `python -m noema.ops.prices placeholder --config <yaml>` prints
a `prices.yaml` that `noema-admin prices import` accepts. The price key of a model is `models.<alias>.price_ref`,
else its Bedrock id `models.<alias>.id`; the regions are the tenants' `home_region`s (or `--region`). Real prices
replace the "TBD" strings by decimal USD per million tokens before the import (the AWS Price List API is the
source; no Noema process calls it).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from typing import Any, Final

import yaml

from noema.config.prices import parse_price_catalog

COMPONENTS: Final = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")


def price_keys(config: Mapping[str, Any]) -> list[str]:
    """The catalog keys of every configured model, sorted and unique."""
    models = config.get("models") or {}
    return sorted({str(m.get("price_ref") or m["id"]) for m in models.values()})


def home_regions(config: Mapping[str, Any]) -> list[str]:
    """Every tenant's home region, sorted and unique."""
    tenants = config.get("tenants") or {}
    return sorted({str(t["home_region"]) for t in tenants.values()})


def placeholder_catalog(
    config: Mapping[str, Any], *, effective_from: dt.date, regions: Iterable[str] | None = None
) -> dict[str, Any]:
    """The placeholder catalog document of `config` (validated with the catalog schema; ValueError when the config
    names no model or no region)."""
    keys, where = price_keys(config), sorted(set(regions)) if regions is not None else home_regions(config)
    if not keys or not where:
        raise ValueError("the config names no model or no tenant region")
    doc: dict[str, Any] = {
        "version": 1,
        "entries": [
            {
                "model": key,
                "region": region,
                "tier": "on_demand",
                "effective_from": effective_from.isoformat(),
                "usd_per_mtok": dict.fromkeys(COMPONENTS, "TBD"),
            }
            for key in keys
            for region in where
        ],
    }
    parse_price_catalog(doc)  # PriceCatalogError if the document would not import
    return doc


def render(doc: Mapping[str, Any]) -> str:
    """The catalog as prices.yaml text (key order kept, dates as strings)."""
    header = "# Placeholder prices: every component TBD, so every cost is cost_estimated (IMPL-019).\n"
    return header + yaml.safe_dump(dict(doc), sort_keys=False)
