"""The placeholder price catalog (IMPL-019): one TBD entry per price key and region, accepted by the catalog schema
and the config loader, so every priced call is `cost_estimated`; the CLI prints it."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from tests.fakes.clock import FakeClock
from tests.fakes.config import FakeConfigRegistry, example_body, patched_example
from tests.fakes.outbox import FakeOutbox
from tests.fakes.telemetry import FakeTelemetry

from noema.config.loader import load_config
from noema.core.values import Cost, UsageRecord
from noema.ops.cost import LedgerCostMeter
from noema.ops.prices import placeholder_catalog, price_keys
from noema.ops.prices.__main__ import main

pytestmark = pytest.mark.unit
DAY = dt.date(2026, 10, 1)


def test_one_tbd_entry_per_key_and_region() -> None:
    body = patched_example({"models": {"opus-5.5": {"price_ref": "anthropic.opus.list"}}})
    doc = placeholder_catalog(body, effective_from=DAY, regions=["us-west-2", "us-east-1"])
    keys = price_keys(body)
    assert "anthropic.opus.list" in keys and "us.anthropic.claude-opus-5-5" not in keys
    assert len(doc["entries"]) == 2 * len(keys)
    assert all(set(e["usd_per_mtok"].values()) == {"TBD"} for e in doc["entries"])
    assert {e["region"] for e in doc["entries"]} == {"us-east-1", "us-west-2"}


def test_placeholders_price_every_call_as_estimated() -> None:
    doc = placeholder_catalog(dict(example_body()), effective_from=DAY)  # regions: the tenants' home regions
    body = patched_example({"platform": {"cost": {"prices_ref": "db:9"}}})
    registry = FakeConfigRegistry(load_config(body, env="test", prices=lambda v: doc if v == 9 else None))
    meter = LedgerCostMeter(FakeClock(), registry, FakeOutbox(), FakeTelemetry().instruments)
    assert len(registry.current().prices.entries) == len(doc["entries"])
    assert meter.price("sol-6.1", "us-east-1", UsageRecord(100, 10)) == Cost(Decimal("0.000000"), True)


def test_a_config_without_models_is_refused() -> None:
    with pytest.raises(ValueError, match="no model"):
        placeholder_catalog({"models": {}, "tenants": {}}, effective_from=DAY)


def test_cli_prints_an_importable_catalog(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "platform.yaml"
    config.write_text(yaml.safe_dump(dict(example_body())))
    assert main(["placeholder", "--config", str(config), "--effective-from", "2026-10-01"]) == 0
    printed = yaml.safe_load(capsys.readouterr().out)
    assert printed["version"] == 1 and printed["entries"][0]["effective_from"] == "2026-10-01"
    assert main(["placeholder", "--config", str(tmp_path / "missing.yaml")]) == 2
