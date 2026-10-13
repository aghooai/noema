"""The price catalog (IMPL-019, spec-05 §2) and its loading through platform.cost.prices_ref (P1-P1)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from noema.config.loader import ConfigError, load_config, load_config_file
from noema.config.prices import EMPTY_CATALOG, PriceCatalogError, load_price_file, parse_price_catalog

from .conftest import EXAMPLE_PATH

pytestmark = pytest.mark.unit

PRICES_YAML = """
version: 1
entries:
  - model: us.anthropic.claude-haiku-5-5
    region: us-east-1
    tier: on_demand
    effective_from: 2026-10-01
    usd_per_mtok: { input: "TBD", output: "TBD", cache_read: "TBD", cache_write_5m: "TBD", cache_write_1h: "TBD" }
  - model: us.anthropic.claude-haiku-5-5
    region: us-east-1
    effective_from: 2026-11-01
    usd_per_mtok: { input: "1.25", output: "6.0", cache_read: "0.125" }
  - model: deepseek.v3.2
    region: us-east-1
    effective_from: 2026-09-01
    usd_per_mtok: { input: "0.62", output: "1.85" }
"""


def _doc() -> dict[str, Any]:
    doc = yaml.safe_load(PRICES_YAML)
    assert isinstance(doc, dict)
    return doc


def _at(day: str) -> dt.datetime:
    return dt.datetime.fromisoformat(day).replace(tzinfo=dt.UTC)


def test_parse_and_lookup_newest_effective_entry() -> None:
    catalog = parse_price_catalog(_doc(), version=3)
    assert catalog.version == 3 and len(catalog.entries) == 3
    assert catalog.lookup("us.anthropic.claude-haiku-5-5", "us-east-1", _at("2026-09-30")) is None
    tbd = catalog.lookup("us.anthropic.claude-haiku-5-5", "us-east-1", _at("2026-10-31T23:59:59"))
    assert tbd is not None and tbd.usd_per_mtok.input is None and tbd.tier == "on_demand"
    new = catalog.lookup("us.anthropic.claude-haiku-5-5", "us-east-1", _at("2026-11-01"))
    assert new is not None
    assert (new.usd_per_mtok.input, new.usd_per_mtok.output, new.usd_per_mtok.cache_read) == (
        Decimal("1.25"), Decimal("6.0"), Decimal("0.125"),
    )  # fmt: skip
    assert new.usd_per_mtok.cache_write_5m is None
    assert catalog.lookup("deepseek.v3.2", "us-west-2", _at("2026-11-01")) is None
    assert EMPTY_CATALOG.lookup("deepseek.v3.2", "us-east-1", _at("2026-11-01")) is None


def test_lookup_uses_the_utc_date() -> None:
    catalog = parse_price_catalog(_doc())
    late_utc = dt.datetime(2026, 10, 31, 20, 0, tzinfo=dt.timezone(dt.timedelta(hours=-5)))  # 2026-11-01 01:00 UTC
    entry = catalog.lookup("us.anthropic.claude-haiku-5-5", "us-east-1", late_utc)
    assert entry is not None and entry.effective_from == dt.date(2026, 11, 1)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(version=2),
        lambda d: d["entries"][0]["usd_per_mtok"].update(input="1,25"),
        lambda d: d["entries"][0]["usd_per_mtok"].update(input="-1"),
        lambda d: d["entries"][0]["usd_per_mtok"].pop("output"),
        lambda d: d["entries"][0].update(tier="batch"),
        lambda d: d["entries"][0].update(region="us-east"),
        lambda d: d["entries"][0].update(effective_from="2026-02-30"),
        lambda d: d["entries"][0].update(extra=1),
        lambda d: d["entries"].append(dict(d["entries"][0])),  # duplicate (model, region, tier, effective_from)
    ],
)
def test_invalid_catalogs_are_rejected(mutate: Any) -> None:
    doc = _doc()
    mutate(doc)
    with pytest.raises(PriceCatalogError) as info:
        parse_price_catalog(doc)
    assert info.value.errors


def test_load_price_file(tmp_path: Path) -> None:
    path = tmp_path / "prices.yaml"
    path.write_text(PRICES_YAML)
    assert len(load_price_file(path).entries) == 3
    with pytest.raises(PriceCatalogError, match="cannot read"):
        load_price_file(tmp_path / "missing.yaml")
    path.write_text("- 1\n")
    with pytest.raises(PriceCatalogError, match="not a mapping"):
        load_price_file(path)


def test_config_loads_a_file_catalog_in_test(tmp_path: Path) -> None:
    (tmp_path / "prices.yaml").write_text(PRICES_YAML)
    cfg = yaml.safe_load(EXAMPLE_PATH.read_text())
    cfg["platform"] = {"cost": {"prices_ref": "file:prices.yaml"}}
    (tmp_path / "platform.yaml").write_text(yaml.safe_dump(cfg))
    snap = load_config_file(tmp_path / "platform.yaml", env="test")
    assert len(snap.prices.entries) == 3 and snap.prices.version is None


def test_config_loads_a_db_catalog_through_the_resolver(example: dict[str, Any]) -> None:
    example["platform"] = {"cost": {"prices_ref": "db:4"}}
    seen: list[int] = []

    def resolver(version: int) -> dict[str, Any] | None:
        seen.append(version)
        return _doc() if version == 4 else None

    snap = load_config(example, env="prod", prices=resolver)
    assert seen == [4] and snap.prices.version == 4
    example["platform"] = {"cost": {"prices_ref": "db:5"}}
    with pytest.raises(ConfigError, match=r"db:5 does not exist \(P1-P1\)"):
        load_config(example, env="prod", prices=resolver)
    with pytest.raises(ConfigError, match="does not exist"):
        load_config(example, env="prod")


def test_an_invalid_db_catalog_rejects_the_config(example: dict[str, Any]) -> None:
    example["platform"] = {"cost": {"prices_ref": "db:1"}}
    with pytest.raises(ConfigError, match="prices_ref db:1"):
        load_config(example, env="test", prices=lambda v: {"version": 1, "entries": [{"model": "x"}]})
