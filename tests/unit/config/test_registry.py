"""InMemoryConfigRegistry (spec-02 §2): pinning window, on_change, rollback."""

from __future__ import annotations

from typing import Any

import pytest

from noema.config.loader import load_config
from noema.config.registry import InMemoryConfigRegistry
from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.config import ConfigRegistry, ConfigSnapshot

pytestmark = pytest.mark.unit


def _snap(example: dict[str, Any], version: int, holdback_ms: int = 1000) -> ConfigSnapshot:
    cfg = {**example, "config_version": version}
    cfg["routing"] = {
        **example["routing"],
        "speculation": {**example["routing"]["speculation"], "holdback_ms": holdback_ms},
    }
    return load_config(cfg, env="test")


def test_current_get_and_the_pinning_window(example: dict[str, Any]) -> None:
    s1, s2, s3, s4 = (_snap(example, v) for v in (1, 2, 3, 4))
    registry: ConfigRegistry = InMemoryConfigRegistry(s1)
    assert registry.current() is s1
    impl = registry
    assert isinstance(impl, InMemoryConfigRegistry)
    for s in (s2, s3, s4):
        assert impl.activate(s)
    assert registry.current() is s4
    assert registry.get(4) is s4 and registry.get(3) is s3 and registry.get(2) is s2
    with pytest.raises(NoemaError) as info:
        registry.get(1)
    assert info.value.code is ErrorCode.NOT_FOUND


def test_on_change_runs_after_current_changed(example: dict[str, Any]) -> None:
    s1, s2 = _snap(example, 1), _snap(example, 2)
    registry = InMemoryConfigRegistry(s1)
    seen: list[tuple[int, int]] = []
    registry.on_change(lambda snap: seen.append((snap.version, registry.current().version)))
    assert registry.activate(s2)
    assert not registry.activate(_snap(example, 2))  # same version and digest: no change
    assert seen == [(2, 2)]


def test_rollback_moves_a_pinned_version_to_the_front(example: dict[str, Any]) -> None:
    s1, s2, s3 = _snap(example, 1), _snap(example, 2), _snap(example, 3)
    registry = InMemoryConfigRegistry(s1)
    registry.activate(s2)
    registry.activate(s3)
    assert registry.activate(s1)
    assert registry.current() is s1
    assert {registry.get(v).version for v in (1, 2, 3)} == {1, 2, 3}


def test_a_changed_body_under_the_same_version_replaces_it(example: dict[str, Any]) -> None:
    a, b = _snap(example, 7), _snap(example, 7, holdback_ms=1500)
    assert a.digest != b.digest
    registry = InMemoryConfigRegistry(a)
    assert registry.activate(b)
    assert registry.get(7) is b


def test_failing_callbacks_are_isolated(example: dict[str, Any]) -> None:
    registry = InMemoryConfigRegistry(_snap(example, 1))
    calls: list[str] = []

    def boom(_: ConfigSnapshot) -> None:
        calls.append("boom")
        raise RuntimeError("subscriber bug")

    registry.on_change(boom)
    registry.on_change(lambda _: calls.append("ok"))
    with pytest.raises(ExceptionGroup):
        registry.activate(_snap(example, 2))
    assert calls == ["boom", "ok"]
    assert registry.current().version == 2
