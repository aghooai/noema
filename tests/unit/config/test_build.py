"""The dict -> frozen *Cfg conversion (config/_build.py) and the default filling."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import MappingProxyType
from typing import Any

import pytest

from noema.config._build import ConfigBuildError, build_config
from noema.config.loader import fill_defaults

pytestmark = pytest.mark.unit


def test_build_produces_frozen_read_only_values(example: dict[str, Any]) -> None:
    cfg = build_config(fill_defaults(example))
    assert isinstance(cfg.tenants, MappingProxyType)
    assert isinstance(cfg.decision_profiles["router-default"].members, tuple)
    assert cfg.models["haiku-5.5"].roles == frozenset({"decision", "selector"})
    with pytest.raises(FrozenInstanceError):
        cfg.routing.policy_version = 2  # type: ignore[misc]
    with pytest.raises(TypeError):
        cfg.tenants["t_new"] = cfg.tenants["t_acme"]  # type: ignore[index]


def test_fill_does_not_invent_optional_objects(example: dict[str, Any]) -> None:
    filled = fill_defaults(example)
    tenant = filled["tenants"]["t_acme"]
    assert "limits" not in tenant and "allowed_models" not in tenant
    assert "escalation" not in filled["decision_profiles"]["router-plus"]
    assert "prices_ref" not in filled["platform"]["cost"]
    assert example.get("platform") is None  # the input is not mutated


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda c: c["routing"].update(policy_version="1"), "expected int"),
        (lambda c: c["routing"].update(extra=1), "unexpected keys"),
        (lambda c: c["routing"]["confidence_bands"].update(high_gte="x"), "expected a number"),
        (lambda c: c["routing"].update(bindings={}), "expected a list"),
        (lambda c: c.update(tenants=[]), "expected an object"),
        (lambda c: c["routing"]["speculation"].pop("holdback_ms"), "holdback_ms"),
        (lambda c: c["decision_profiles"]["router-plus"].update(aggregation="median"), "is not one of"),
        (lambda c: c["routing"]["bindings"][0]["when"].update(value_in=[None]), "null is not allowed"),
    ],
)
def test_build_errors(example: dict[str, Any], mutate: Any, match: str) -> None:
    filled = fill_defaults(example)
    mutate(filled)
    with pytest.raises(ConfigBuildError, match=match):
        build_config(filled)
