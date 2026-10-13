"""Config fakes: the shipped example snapshot and `FakeConfigRegistry` (spec-02 §2).

`example_snapshot()` loads the vendored `platform.example.yaml` with the real loader (env `test`), so every fake
sees the shipped config (the golden fixtures use it). `snapshot_from(dict)` loads a modified copy (deep-merge a
patch with `patched_example(patch)`).
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable, Mapping
from functools import cache
from pathlib import Path
from typing import Any, Final

import yaml

import noema.contracts
from noema.config.loader import load_config
from noema.config.registry import ConfigSnapshotImpl
from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.config import ConfigSnapshot

EXAMPLE_CONFIG_PATH: Final = Path(noema.contracts.__file__).parent / "_vendor" / "examples" / "platform.example.yaml"
WINDOW: Final = 3  # current + the previous 2 (spec-02 §2)


@cache
def example_body() -> Mapping[str, Any]:
    """The example config as decoded YAML (do not mutate; use `patched_example`)."""
    loaded: Mapping[str, Any] = yaml.safe_load(EXAMPLE_CONFIG_PATH.read_text())
    return loaded


@cache
def example_snapshot() -> ConfigSnapshotImpl:
    """The shipped example config, loaded in env test (cached; snapshots are immutable)."""
    return load_config(dict(copy.deepcopy(example_body())), env="test")


def _merge(base: dict[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    for key, value in patch.items():
        if value is None:
            base.pop(key, None)
        elif isinstance(value, Mapping) and isinstance(base.get(key), dict):
            base[key] = _merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def patched_example(patch: Mapping[str, Any]) -> dict[str, Any]:
    """A deep copy of the example config with `patch` deep-merged (a None value deletes the key)."""
    return _merge(copy.deepcopy(dict(example_body())), patch)


def snapshot_from(source: Mapping[str, Any], *, version: int | None = None) -> ConfigSnapshotImpl:
    """Load a config dict with the real loader (env test); `version` overrides `config_version`."""
    body = copy.deepcopy(dict(source))
    if version is not None:
        body["config_version"] = version
    return load_config(body, env="test")


class FakeConfigRegistry:
    """Implements `ConfigRegistry` over given snapshots: `current`, a pinned window of 3 versions, `on_change`
    callbacks (run synchronously after `current` changed). `activate` is the test (and config watcher) hook."""

    def __init__(self, initial: ConfigSnapshot | None = None) -> None:
        self._lock = threading.Lock()
        self._window: list[ConfigSnapshot] = [initial if initial is not None else example_snapshot()]
        self._callbacks: list[Callable[[ConfigSnapshot], None]] = []
        self.activations = 0

    def current(self) -> ConfigSnapshot:
        return self._window[0]

    def get(self, version: int) -> ConfigSnapshot:
        for snap in tuple(self._window):
            if snap.version == version:
                return snap
        raise NoemaError(ErrorCode.NOT_FOUND, f"Config version {version} is not pinned in this process.")

    def on_change(self, cb: Callable[[ConfigSnapshot], None]) -> None:
        with self._lock:
            self._callbacks.append(cb)

    def activate(self, snapshot: ConfigSnapshot) -> bool:
        """Make `snapshot` current (False if it already is, same digest); callbacks run after the switch."""
        with self._lock:
            head = self._window[0]
            if head.version == snapshot.version and head.digest == snapshot.digest:
                return False
            self._window = [snapshot, *(s for s in self._window if s.version != snapshot.version)][:WINDOW]
            callbacks = tuple(self._callbacks)
            self.activations += 1
        for cb in callbacks:
            cb(snapshot)
        return True
