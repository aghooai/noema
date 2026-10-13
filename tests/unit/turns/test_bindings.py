"""T-BIND-1…5 (spec-04 §5.1, §13): binding evaluation and the decided spec."""

from __future__ import annotations

from dataclasses import replace

import pytest
from tests.fakes.config import example_snapshot

from noema.config.models import BindingCfg, BindingThenCfg, BindingWhenCfg
from noema.core.protocols.generation import AttemptSpec
from noema.turns.bindings import Tightening, content_policy, evaluate, matches, policy_for
from noema.turns.spec import Pins, Spec0, decide

from .support import choice, predicate, snapshot_of, standard_answers, unavailable

pytestmark = pytest.mark.unit

CC = ("intent", "complexity")
SPEC0 = Spec0(AttemptSpec("sol-fast", None, None, "none"), "sticky", "spec0 from sticky")
NO_PINS = Pins()


def _decide(answers, *, pins: Pins = NO_PINS, final=None, usable=lambda _p: True, spec0: Spec0 = SPEC0):  # type: ignore[no-untyped-def]
    snap = example_snapshot()
    return decide(
        snap,
        snapshot_of(answers, final=final),
        commit_critical=CC,
        request_names=(),
        spec0=spec0,
        pins=pins,
        policy=Tightening(),
        usable=usable,
    )


def test_bind_1_first_match_per_pre_commit_key() -> None:
    """T-BIND-1: the first matching binding that sets a key wins it; later matches don't override."""
    bindings = (
        BindingCfg(BindingWhenCfg(answer="intent", p_max_gte=0.5), BindingThenCfg(effort="low", max_output_tokens=10)),
        BindingCfg(BindingWhenCfg(answer="intent"), BindingThenCfg(effort="high", generation_profile="opus-deep")),
    )
    answers = standard_answers()
    ev = evaluate(bindings, answers, frozenset(answers), Tightening())
    assert ev.decided == {"effort": "low", "max_output_tokens": 10, "generation_profile": "opus-deep"}
    assert ev.matched_names == ("bindings[0]", "bindings[1]")


def test_bind_1_shipped_config_coding_hard() -> None:
    """The shipped bindings: coding (0.85, margin 0.7) + hard -> (opus-deep, high, 4000), bindings 0 and 2."""
    answers = standard_answers(intent="coding", p=0.85, level="hard")
    decided = _decide(answers)
    assert decided.spec == AttemptSpec("opus-deep", "high", 4000, "none")
    assert decided.evaluation.matched_names == ("bindings[0]", "bindings[2]")
    assert decided.selection == "rule"
    assert [b["name"] for b in decided.basis] == ["intent", "complexity"]


def test_bind_2_tightening_is_most_restrictive() -> None:
    """T-BIND-2: log_content redacted then none -> none; verify_answer and spotlight OR-combine."""
    bindings = (
        BindingCfg(BindingWhenCfg(answer="a"), BindingThenCfg(log_content="redacted", spotlight="strict")),
        BindingCfg(BindingWhenCfg(answer="b"), BindingThenCfg(log_content="none", verify_answer=True)),
        BindingCfg(BindingWhenCfg(answer="c"), BindingThenCfg(log_content="full", spotlight="standard")),
    )
    answers = {n: predicate(n, 0.9) for n in "abc"}
    ev = evaluate(bindings, answers, frozenset(answers), Tightening())
    assert ev.tight == Tightening(verify_answer=True, spotlight="strict", log_content="none")
    assert content_policy(ev.tight.log_content) == "metadata_only"
    # the data class policy is the floor: restricted -> metadata_only stays
    floor = evaluate((), answers, frozenset(answers), policy_for("metadata_only"))
    assert floor.tight.log_content == "none"


def test_bind_3_provisional_answers_never_match() -> None:
    """T-BIND-3: an answer outside final_names (IMPL-002) and unscored answers match nothing."""
    when = BindingWhenCfg(answer="sensitive_data", probability_gte=0.3)
    hot = {"sensitive_data": predicate("sensitive_data", 0.9, final=False)}
    assert not matches(when, hot, frozenset())
    assert matches(when, hot, frozenset({"sensitive_data"}))
    assert not matches(when, {"sensitive_data": unavailable("sensitive_data")}, frozenset({"sensitive_data"}))
    # probability_gte applies to predicates only
    assert not matches(
        BindingWhenCfg(answer="intent", probability_gte=0.1),
        {"intent": choice("intent", (("x", 0.9),))},
        frozenset({"intent"}),
    )


def test_bind_3_condition_kinds() -> None:
    a = {"c": choice("c", (("hard", 0.6), ("expert", 0.3)), band="medium", calibrated=True)}
    names = frozenset(a)
    assert matches(BindingWhenCfg(answer="c", level_in=("hard",)), a, names)
    assert not matches(BindingWhenCfg(answer="c", value_in=("expert",)), a, names)
    assert matches(BindingWhenCfg(answer="c", p_max_gte=0.6, margin_gte=0.3), a, names)
    assert not matches(BindingWhenCfg(answer="c", margin_gte=0.31), a, names)
    assert matches(BindingWhenCfg(answer="c", confidence_band="medium", requires_calibrated=True), a, names)
    assert not matches(BindingWhenCfg(answer="c", confidence_band="high"), a, names)
    req = BindingWhenCfg(answer="c", source="request", answer_type="score")
    assert not matches(req, a, names)  # the answer's type is choice


def test_bind_4_intent_route_miss_and_uncertain_profile() -> None:
    """T-BIND-4: an intent_routes miss -> default_profile; a low-band intent with no profile binding ->
    uncertain_profile (IMPL-026)."""
    snap = example_snapshot()
    miss = standard_answers(intent="billing", p=0.9)  # billing has no intent route
    decided = _decide(miss)
    assert decided.spec.generation_profile == snap.routing.default_profile
    assert decided.selection == "default"
    bindings = (BindingCfg(BindingWhenCfg(answer="complexity"), BindingThenCfg(effort="low")),)
    patched = replace(snap.routing, bindings=bindings, uncertain_profile="glm-reason")
    low = {"intent": choice("intent", (("coding", 0.4), ("chitchat", 0.3)), band="low"), **standard_answers()}
    low["intent"] = choice("intent", (("coding", 0.4), ("chitchat", 0.3)), band="low")

    class _Snap:
        routing = patched

        def __getattr__(self, name: str) -> object:
            return getattr(snap, name)

    out = decide(
        _Snap(),  # type: ignore[arg-type]
        snapshot_of(low),
        commit_critical=CC,
        request_names=(),
        spec0=SPEC0,
        pins=Pins(),
        policy=Tightening(),
        usable=lambda _p: True,
    )
    assert out.spec.generation_profile == "glm-reason"
    assert out.reason == "intent band low: uncertain_profile"


def test_bind_5_request_pins_override_and_verify_false_keeps_binding() -> None:
    """T-BIND-5: request pins replace the pre-commit keys after the bindings; verify: false never lowers a
    binding's verify_answer; conditioning off -> no block."""
    answers = standard_answers(intent="coding", p=0.85, level="hard", injection=0.35)
    decided = _decide(answers, pins=Pins(generation_profile="glm-reason", effort="medium", verify=False))
    assert decided.spec == AttemptSpec("glm-reason", "medium", 4000, "none")
    assert decided.selection == "pinned"
    assert decided.evaluation.tight.verify_answer is True  # bindings[4] (injection >= 0.2)
    forced = _decide(standard_answers(), pins=Pins(verify=True))
    assert forced.evaluation.tight.verify_answer is True


def test_conditioning_binding_and_request_off() -> None:
    """A low-band intent hits bindings[1] (cover_top2_and_clarify): the block's signature is the spec's 4th
    field; conditioning off drops it."""
    answers = standard_answers()
    answers["intent"] = choice("intent", (("shipping", 0.45), ("billing", 0.35)), band="low")
    decided = _decide(answers)
    assert decided.conditioning is not None and decided.answer_mode == "cover_top2_and_clarify"
    assert decided.spec.conditioning_signature == decided.conditioning.signature
    assert decided.spec.generation_profile == "sol-fast"
    off = _decide(answers, pins=Pins(conditioning="off"))
    assert off.conditioning is None and off.spec.conditioning_signature == "none"


def test_decision_unavailable_keeps_spec0() -> None:
    """Every commit-critical answer unavailable -> decided = spec0, reason names the first one (04 §3)."""
    answers = {n: unavailable(n) for n in ("intent", "complexity", "sensitive_data")}
    decided = _decide(answers)
    assert decided.unavailable and decided.spec == SPEC0.spec and decided.selection == "sticky"
    assert decided.reason == "decision unavailable: all_members_failed"
    assert decided.evaluation.matched == ()


def test_hard_filter_falls_back_to_default() -> None:
    answers = standard_answers(intent="coding", p=0.85, level="hard")
    decided = _decide(answers, usable=lambda p: p != "opus-deep")
    assert decided.spec.generation_profile == "sol-fast" and decided.selection == "default"
    assert "opus-deep filtered" in decided.reason
