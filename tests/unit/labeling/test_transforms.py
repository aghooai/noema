"""Runtime transforms (spec-06 §8.3): temperature with the epsilon floor, Platt, unresolved mass preserved; the
numbers equal the conformance fakes' (tests/fakes/labeling.py), so a calibrated fake decision matches the engine."""

from __future__ import annotations

import math

import pytest
from tests.fakes.labeling import PlattTransform as FakePlatt
from tests.fakes.labeling import TemperatureTransform as FakeTemperature

from noema.labeling.calibrators.transforms import IdentityTransform, PlattTransform, TemperatureTransform, sigmoid

pytestmark = pytest.mark.unit


def test_temperature_one_is_identity_and_preserves_unresolved_mass() -> None:
    dist = {"a": 0.5, "b": 0.3}
    out = TemperatureTransform(1.0).apply(dist, 0.2)
    assert out == pytest.approx(dist)
    sharp = TemperatureTransform(0.5).apply(dist, 0.2)
    assert sum(sharp.values()) == pytest.approx(0.8)
    assert sharp["a"] > dist["a"]


def test_temperature_floors_stated_zeros_at_epsilon() -> None:
    out = TemperatureTransform(2.0, epsilon=1e-4).apply({"a": 1.0, "b": 0.0}, 0.0)
    expected_b = math.exp(math.log(1e-4) / 2) / (1 + math.exp(math.log(1e-4) / 2))
    assert out["b"] == pytest.approx(expected_b)
    assert out["a"] + out["b"] == pytest.approx(1.0)


@pytest.mark.parametrize("t", [0.3, 1.0, 1.7, 5.0])
@pytest.mark.parametrize("u", [0.0, 0.25])
def test_temperature_matches_the_fake(t: float, u: float) -> None:
    dist = {"x": 0.45 * (1 - u), "y": 0.35 * (1 - u), "z": 0.2 * (1 - u)}
    assert TemperatureTransform(t).apply(dist, u) == pytest.approx(FakeTemperature(t).apply(dist, u))


@pytest.mark.parametrize(("a", "b"), [(1.0, 0.0), (0.6, -0.4), (2.5, 1.0)])
@pytest.mark.parametrize("u", [0.0, 0.1])
def test_platt_matches_the_fake_and_sums_to_one_minus_u(a: float, b: float, u: float) -> None:
    dist = {"true": 0.7 * (1 - u), "false": 0.3 * (1 - u)}
    out = PlattTransform(a, b).apply(dist, u)
    assert out == pytest.approx(FakePlatt(a, b).apply(dist, u))
    assert out["true"] + out["false"] == pytest.approx(1 - u)


def test_platt_identity_parameters_and_clamping() -> None:
    assert PlattTransform(1.0, 0.0).apply({"true": 0.8, "false": 0.2}, 0.0)["true"] == pytest.approx(0.8)
    assert PlattTransform(1.0, 0.0, epsilon=1e-3).apply({"true": 1.0, "false": 0.0}, 0.0)["true"] == pytest.approx(
        1 - 1e-3
    )
    assert PlattTransform(1.0, 0.0).apply({"false": 1.0}, 0.0)["true"] == pytest.approx(1e-4)  # censored true


def test_degenerate_inputs() -> None:
    assert TemperatureTransform(2.0).apply({}, 0.0) == {}
    assert TemperatureTransform(2.0).apply({"a": 0.0}, 1.0) == {"a": 0.0}
    assert IdentityTransform().apply({"a": 0.4}, 0.6) == {"a": 0.4}
    assert sigmoid(-800.0) == pytest.approx(0.0) and sigmoid(800.0) == pytest.approx(1.0)
