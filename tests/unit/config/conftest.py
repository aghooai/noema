"""Shared fixtures of the config tests: the vendored example config."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from noema.contracts.bundle import VENDOR_DIR

EXAMPLE_PATH: Path = VENDOR_DIR / "examples" / "platform.example.yaml"
_EXAMPLE: dict[str, Any] = yaml.safe_load(EXAMPLE_PATH.read_text())


@pytest.fixture
def example() -> dict[str, Any]:
    """A fresh deep copy of examples/platform.example.yaml as a dict."""
    return copy.deepcopy(_EXAMPLE)
