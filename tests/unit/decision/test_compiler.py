"""Question compilation and profile resolution (spec-06 §2, §3.1; IMPL-031, IMPL-069)."""

from __future__ import annotations

from typing import Any

import pytest
from tests.fakes.config import FakeConfigRegistry, example_snapshot

from noema.core.errors import ErrorCode, NoemaError
from noema.decision.compiler import QuestionCompilerImpl, flat_limit
from noema.decision.profiles import effective_quorum, qmax, resolve_facade, resolve_native

SNAP = example_snapshot()
COMPILER = QuestionCompilerImpl(FakeConfigRegistry())
ROUTER = SNAP.decision_profile("router-default")


def compile_err(questions: list[dict[str, Any]], **kw: Any) -> NoemaError:
    kw.setdefault("origin", "native")
    kw.setdefault("output_format", "native")
    with pytest.raises(NoemaError) as err:
        COMPILER.compile(kw.pop("profile", ROUTER), questions, **kw)
    return err.value


def test_validation_errors_and_params() -> None:
    choice = {"type": "choice", "name": "c", "instructions": "x", "choices": [{"value": "a"}, {"value": "b"}]}
    dup = compile_err([choice, {**choice}])
    assert (dup.code, dup.param) == (ErrorCode.SCHEMA_VIOLATION, "questions[1].name")
    levels = compile_err(
        [{"type": "score", "name": "s", "instructions": "x", "levels": [{"label": "a"}, {"label": "a"}]}]
    )
    assert levels.param == "questions[0].levels"
    groups = compile_err([{**choice, "choices": [{"value": "a", "group": "g"}, {"value": "b"}]}])
    assert groups.param == "questions[0].choices"
    one = compile_err([{**choice, "choices": [{"value": "a", "group": "g"}, {"value": "b", "group": "g"}]}])
    assert one.param == "questions[0].choices"
    auto = compile_err([{**choice, "name": "q1"}])
    assert auto.param == "questions[0].name"
    reserved = compile_err([{**choice, "name": "intent"}], origin="turn", platform_from=ROUTER)
    assert (reserved.code, reserved.param) == (ErrorCode.RESERVED_QUESTION_NAME, "decisions.questions[0].name")
    blocking = compile_err([choice], origin="turn", platform_from=ROUTER, blocking=["nope"])
    assert blocking.param == "decisions.blocking"


def test_turn_and_verification_ordering() -> None:
    app = [{"type": "predicate", "name": "wants_refund", "instructions": "x"}]
    qs = COMPILER.compile(
        ROUTER, app, origin="turn", output_format="native", blocking=["wants_refund"], platform_from=ROUTER
    )
    assert [q.name for q in qs.questions][-1] == "wants_refund" and qs.flat_limit == 16
    assert qs.commit_critical == {"intent", "complexity", "wants_refund"}
    assert [q.source for q in qs.questions] == ["platform"] * 5 + ["request"]
    native = COMPILER.compile(ROUTER, app, origin="native", output_format="native")
    assert [q.name for q in native.questions] == ["wants_refund"] and native.commit_critical == frozenset()
    verifier = SNAP.decision_profile("answer-verifier")
    ver = COMPILER.compile(
        verifier, [], origin="verification", output_format="native", select=["unsupported_claims", "answers_question"]
    )
    assert [q.name for q in ver.questions] == ["answers_question", "unsupported_claims"]  # config order


def test_platform_question_wire_signs_like_the_config() -> None:
    qs = COMPILER.compile(ROUTER, [], origin="turn", output_format="native", platform_from=ROUTER)
    intent = qs.questions[0]
    assert intent.wire["choices"][0] == {"value": "chitchat"} and intent.method == "flat"  # type: ignore[index]
    needs = next(q for q in qs.questions if q.name == "needs_fresh_info")
    assert "criteria" not in needs.wire and needs.criteria is None


def test_flat_limits_and_quorum_bounds() -> None:
    assert flat_limit(SNAP, ROUTER) == 16
    assert flat_limit(SNAP, SNAP.decision_profile("lightweight-model-decide")) == 26
    assert qmax(ROUTER) == 3 and effective_quorum(ROUTER, None) == 2
    with pytest.raises(NoemaError) as err:
        effective_quorum(SNAP.decision_profile("lightweight-model-decide"), 2)
    assert err.value.details == {"max": 1}


def test_profile_resolution() -> None:
    assert resolve_native(SNAP, "t_acme", None).id == "lightweight-model-decide"
    assert resolve_native(SNAP, "t_acme", "router-default@1").id == "router-default"
    with pytest.raises(NoemaError) as err:
        resolve_native(SNAP, "t_acme", "answer-verifier")
    assert err.value.code is ErrorCode.PROFILE_NOT_ALLOWED
    with pytest.raises(NoemaError) as err:
        resolve_native(SNAP, "t_acme", "router-plus@2")
    assert (err.value.code, err.value.param) == (ErrorCode.UNSUPPORTED_PARAMETER, "profile")
    assert resolve_facade(SNAP, "t_acme", "aghoo/router-plus").id == "router-plus"
    for model in ("router-plus", "aghoo/router-plus@3"):
        with pytest.raises(NoemaError) as err:
            resolve_facade(SNAP, "t_acme", model)
        assert err.value.code is ErrorCode.MODEL_NOT_AVAILABLE
        assert err.value.details == {
            "allowed": ["aghoo/lightweight-model-decide", "aghoo/router-default", "aghoo/router-plus"]
        }
