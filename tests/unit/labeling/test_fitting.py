"""Calibrator fitting on synthetic data with known calibration (06 §2.3 step 2; ADR-003 §1): per-member temperature
recovers the miscalibration factor, the pooled transform (temperature or Platt) fixes the rest, unresolved mass is
preserved, and the numpy prediction equals the runtime transforms."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest
from tests.unit.labeling.synth import T0, labeled_set

from noema.core.protocols.labeling import CalibratorBindingKey
from noema.labeling.artifact import artifact_row, calibrator_from_row
from noema.labeling.calibrators.platt import fit_platt
from noema.labeling.calibrators.temperature import T_MAX, T_MIN, fit_temperature
from noema.labeling.dataset import Example, LabeledSet, MemberObs, arrays, split
from noema.labeling.fitting import fit_params, predict
from noema.labeling.metrics import scores

pytestmark = pytest.mark.unit
KEY = CalibratorBindingKey("router-default", "1", "sha256:" + "a" * 64, ("m1", "m2"), "sha256:" + "b" * 64)


@pytest.mark.parametrize("temp", [0.4, 1.0, 2.5])
def test_member_temperature_recovers_the_miscalibration(temp: float) -> None:
    ls = labeled_set(3000, temp=temp, seed=5)
    params = fit_params(ls, arrays(ls))
    for t in params.member_t.values():
        assert t == pytest.approx(1 / temp, rel=0.12)
    assert params.method == "temperature"
    assert params.pooled["temperature"] == pytest.approx(1.0, abs=0.15)


@pytest.mark.parametrize("kind", ["choice", "predicate", "score"])
def test_calibration_improves_overconfident_members_on_the_test_split(kind: str) -> None:
    ls = labeled_set(3000, kind=kind, temp=0.4, seed=7)  # type: ignore[arg-type]
    calib, test = split(ls)
    params = fit_params(ls, arrays(ls, calib))
    a = arrays(ls, test)
    cal, raw = (
        scores(ls.kind, ls.options, predict(ls, a, params), a.y),
        scores(ls.kind, ls.options, predict(ls, a, None), a.y),
    )
    assert cal.nll < raw.nll and cal.ece < raw.ece
    assert params.method == ("platt" if kind == "predicate" else "temperature")


def test_split_is_time_ordered_and_class_stratified() -> None:
    ls = labeled_set(400, seed=2)
    calib, test = split(ls)
    assert sorted(calib + test) == list(range(400))
    for label in ls.options:
        cls_calib = [i for i in calib if ls.examples[i].label == label]
        cls_test = [i for i in test if ls.examples[i].label == label]
        assert abs(len(cls_test) - len(cls_calib)) <= 1
        if cls_calib and cls_test:
            assert max(ls.examples[i].at for i in cls_calib) <= min(ls.examples[i].at for i in cls_test)


def test_runtime_transforms_equal_the_fitted_prediction() -> None:
    ls = labeled_set(800, temp=0.5, seed=9)
    params = fit_params(ls, arrays(ls))
    row = artifact_row(
        calibrator_id="cal_x",
        key=KEY,
        question_name="q",
        params=params,
        holdout=None,
        acceptance={},
        reliability={},
        fitted_at=T0,
    )
    cal = calibrator_from_row({**row, "scope": "tenant", "holdout": {}})
    a = arrays(ls, [0, 1, 2])
    expected = predict(ls, a, params)
    for i, ex in enumerate(ls.examples[:3]):
        q = {m: cal.member(m).apply(obs.dist, obs.unresolved) for m, obs in ex.members.items()}
        pool = {o: sum(0.5 * q[m].get(o, 0.0) for m in q) for o in ls.options}
        out = cal.pooled.apply(pool, 0.0)
        assert [out[o] for o in ls.options] == pytest.approx(list(expected[i]), abs=1e-9)


def test_censored_options_and_unresolved_mass_are_preserved() -> None:
    opts = ("a", "b", "c")
    examples = [
        Example(
            {"m1": MemberObs({"a": 0.6, "b": 0.2}, 0.2)}, {"m1": 1.0}, "a" if i % 3 else "b", T0 + timedelta(seconds=i)
        )
        for i in range(60)
    ]
    ls = LabeledSet("choice", opts, ("m1",), tuple(examples))
    params = fit_params(ls, arrays(ls))
    out = predict(ls, arrays(ls), params)
    assert np.allclose(out.sum(axis=1), 0.8)
    assert np.all(out[:, 2] == 0.0)  # censored by every member: never given mass


def test_temperature_and_platt_edge_cases() -> None:
    empty = np.zeros((0, 2))
    assert fit_temperature(empty, empty.astype(np.bool_), np.zeros(0, dtype=np.intp)) == 1.0
    one_option = np.array([[0.0, 0.0]]), np.array([[True, False]]), np.array([0], dtype=np.intp)
    assert fit_temperature(*one_option) == 1.0  # a single measured option carries no information
    z = np.log(np.array([[0.99, 0.01]] * 50))
    always_right = fit_temperature(z, np.ones_like(z, dtype=np.bool_), np.zeros(50, dtype=np.intp))
    assert always_right == pytest.approx(T_MIN)
    always_wrong = fit_temperature(z, np.ones_like(z, dtype=np.bool_), np.zeros(50, dtype=np.intp) + 1)
    assert T_MAX >= always_wrong > 10
    assert fit_platt(np.zeros(0), np.zeros(0)) == (1.0, 0.0)
    a, b = fit_platt(np.array([-2.0, -1.0, 1.0, 2.0]), np.array([0.0, 0.0, 1.0, 1.0]))  # separable: stays finite
    assert 0 < a <= 100 and abs(b) <= 100


def test_labeled_set_validation() -> None:
    ex = Example({"m1": MemberObs({"a": 1.0})}, {"m1": 1.0}, "z", T0)
    with pytest.raises(ValueError, match="not a declared option"):
        LabeledSet("choice", ("a", "b"), ("m1",), (ex,))
    with pytest.raises(ValueError, match="at least two"):
        LabeledSet("choice", ("a",), ("m1",), ())
    with pytest.raises(ValueError, match="predicate"):
        LabeledSet("predicate", ("yes", "no"), ("m1",), ())
    other = Example({"m2": MemberObs({"a": 1.0})}, {"m2": 1.0}, "a", T0)
    with pytest.raises(ValueError, match="binding's members"):
        LabeledSet("choice", ("a", "b"), ("m1",), (other,))
