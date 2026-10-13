"""CalibratorRegistry conformance (spec-02 §9, ADR-003, IMPL-072): tenant-first lookup on the exact binding key,
the epoch string, "" for unknown tenants, and a `refresh` that never raises."""

from __future__ import annotations

from dataclasses import replace
from typing import Literal

from tests.fakes.labeling import IdentityTransform, TemperatureTransform

from noema.core.protocols.labeling import Calibrator, CalibratorBindingKey

from .kits import CalibratorKit
from .registry import kit_fixture

kit = kit_fixture("calibrator_registry")
KEY = CalibratorBindingKey("router-default", "1", "sha256:" + "a" * 64, ("m1", "m2", "m3"), "sha256:" + "b" * 64)


def calibrator(cid: str, scope: Literal["tenant", "platform"], key: CalibratorBindingKey = KEY) -> Calibrator:
    return Calibrator(
        id=cid,
        scope=scope,
        method="temperature",
        epsilon=1e-4,
        member_T={"m1": 1.2, "m2": 1.0, "m3": 1.0},  # a T_m for every member of the subset (ADR-003 §1)
        pooled=TemperatureTransform(1.1),
        member=lambda _m: IdentityTransform(),
        holdout={"n_test": 300},
        key=key,
    )


async def test_lookup_prefers_the_tenant_artifact(kit: CalibratorKit) -> None:
    assert kit.registry.lookup(kit.known_tenant, KEY) is None
    await kit.add(calibrator("cal_platform", "platform"), None)
    assert (kit.registry.lookup(kit.known_tenant, KEY) or calibrator("x", "platform")).id == "cal_platform"
    await kit.add(calibrator("cal_tenant", "tenant"), kit.known_tenant)
    found = kit.registry.lookup(kit.known_tenant, KEY)
    assert found is not None and found.id == "cal_tenant"
    assert (kit.registry.lookup("t_somebody_else", KEY) or calibrator("x", "platform")).id == "cal_platform"


async def test_binding_key_must_match_exactly(kit: CalibratorKit) -> None:
    await kit.add(calibrator("cal_platform", "platform"), None)
    other = CalibratorBindingKey(
        KEY.profile_id, KEY.profile_version, KEY.question_signature, ("m1", "m2"), KEY.input_view_signature
    )
    hierarchical = replace(KEY, choice_method="hierarchical")
    assert kit.registry.lookup(kit.known_tenant, other) is None
    assert kit.registry.lookup(kit.known_tenant, hierarchical) is None


async def test_epoch(kit: CalibratorKit) -> None:
    before = kit.registry.epoch(kit.known_tenant)
    platform_n, tenant_n = (int(x) for x in before.split("."))
    await kit.add(calibrator("cal_tenant", "tenant"), kit.known_tenant)
    assert kit.registry.epoch(kit.known_tenant) == f"{platform_n}.{tenant_n + 1}"
    assert kit.registry.epoch("t_unknown_tenant") == ""


async def test_refresh_never_raises(kit: CalibratorKit) -> None:
    await kit.registry.refresh()
    await kit.registry.refresh()
