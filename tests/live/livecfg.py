"""The live-smoke config: the shipped example config plus `config/live.patch.yaml` (and, for L8,
`config/smoke-router.patch.yaml`), with the ambient caller account substituted (spec-10 §6)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import yaml
from tests.fakes.config import patched_example

from noema.config.loader import load_config
from noema.config.registry import ConfigSnapshotImpl

CONFIG_DIR: Final = Path(__file__).parent / "config"
LIVE_TENANT: Final = "t_dev"


def _patch(name: str, account: str) -> dict[str, Any]:
    text = (CONFIG_DIR / name).read_text().replace("${AMBIENT_ACCOUNT}", account)
    loaded: dict[str, Any] = yaml.safe_load(text)
    return loaded


def _deep_merge(base: dict[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    for key, value in extra.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            base[key] = _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def merge_patch(base: dict[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """`base` with `patch` deep-merged in place (a None value deletes the key; lists replace)."""
    for key, value in patch.items():
        if value is None:
            base.pop(key, None)
        elif isinstance(value, Mapping) and isinstance(base.get(key), dict):
            base[key] = merge_patch(base[key], value)
        else:
            base[key] = value
    return base


def live_snapshot(
    account: str, *, smoke_router: bool = False, version: int = 1, extra: Mapping[str, Any] | None = None
) -> ConfigSnapshotImpl:
    """The live config for the ambient `account` (env dev); `smoke_router` adds the L8 activation, `extra` one more
    deep-merged patch (the capability probes' relaxed controls)."""
    patch = _patch("live.patch.yaml", account)
    if smoke_router:
        patch = _deep_merge(patch, _patch("smoke-router.patch.yaml", account))
    if extra is not None:
        patch = _deep_merge(patch, extra)
    body = patched_example(patch)
    body["config_version"] = version
    return load_config(body, env="dev")
