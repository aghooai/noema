"""ConfigRegistry conformance (spec-02 §2): pinning, the 3-version `get` window, `on_change`."""

from __future__ import annotations

import pytest

from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.config import ConfigSnapshot

from .kits import ConfigRegistryKit
from .registry import kit_fixture

kit = kit_fixture("config_registry")


async def test_current_is_the_initial_snapshot(kit: ConfigRegistryKit) -> None:
    snap = kit.registry.current()
    assert snap.version == 1
    assert snap.digest.startswith("sha256:") and len(snap.digest) == 71
    assert kit.registry.get(1) is snap


async def test_snapshot_lookups(kit: ConfigRegistryKit) -> None:
    snap = kit.registry.current()
    assert snap.tenant("t_acme").aws_account_id == "111122223333"
    assert snap.tenant_ref("t_acme").home_region == "us-east-1"
    assert snap.model("sol-6.1").id == "us.openai.gpt-6.1-sol"
    assert snap.decision_profile("router-default").min_quorum == 2
    assert snap.generation_profile("opus-deep").mode == "fallback"
    assert snap.template("verbalized-dist@1").max_choices == 26
    for lookup in (
        snap.tenant,
        snap.tenant_ref,
        snap.model,
        snap.template,
        snap.decision_profile,
        snap.generation_profile,
    ):
        with pytest.raises(NoemaError) as err:
            lookup("does-not-exist")
        assert err.value.code is ErrorCode.NOT_FOUND


async def test_pinned_snapshots_survive_two_activations(kit: ConfigRegistryKit) -> None:
    pinned = kit.registry.current()
    kit.activate(kit.snapshot(2))
    kit.activate(kit.snapshot(3))
    assert kit.registry.current().version == 3
    assert kit.registry.get(1) is pinned
    kit.activate(kit.snapshot(4))
    with pytest.raises(NoemaError) as err:
        kit.registry.get(1)
    assert err.value.code is ErrorCode.NOT_FOUND
    assert kit.registry.get(2).version == 2


async def test_on_change_runs_after_current_changed(kit: ConfigRegistryKit) -> None:
    seen: list[tuple[int, int]] = []

    def cb(snap: ConfigSnapshot) -> None:
        seen.append((snap.version, kit.registry.current().version))

    kit.registry.on_change(cb)
    kit.activate(kit.snapshot(5))
    assert seen == [(5, 5)]


async def test_reactivating_the_current_snapshot_is_not_a_change(kit: ConfigRegistryKit) -> None:
    seen: list[int] = []
    kit.registry.on_change(lambda s: seen.append(s.version))
    kit.activate(kit.registry.current())
    assert seen == []
