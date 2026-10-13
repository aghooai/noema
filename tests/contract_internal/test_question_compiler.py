"""QuestionCompiler conformance (spec-02 §5, spec-06 §2.4, §3): ordering, commit-critical set, validation
errors with their params, the hierarchical method, and the §3.3 signature vector."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import noema.contracts
from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.decision import OutputFormat

from .kits import CompilerKit
from .registry import kit_fixture

kit = kit_fixture("question_compiler")
EXAMPLES = Path(noema.contracts.__file__).parent / "_vendor" / "examples"
DEPARTMENT_SIGNATURE = "sha256:734dae2685c4f2a89e350ac3cb8a74f58f9c67d32264a5cee3cd87eebce9455a"
ROUTER_NAMES = ["intent", "complexity", "needs_fresh_info", "sensitive_data", "injection_suspected"]


def example_questions(name: str = "decision-request.valid.json") -> list[dict[str, Any]]:
    loaded: list[dict[str, Any]] = json.loads((EXAMPLES / name).read_text())["questions"]
    return loaded


def choice(name: str, k: int) -> dict[str, Any]:
    return {
        "type": "choice",
        "name": name,
        "instructions": "Pick one.",
        "choices": [{"value": f"v{i}"} for i in range(k)],
    }


async def test_signature_vector(kit: CompilerKit) -> None:
    profile = kit.snapshot.decision_profile("router-default")
    qs = kit.compiler.compile(profile, example_questions(), origin="native", output_format="native")
    department = next(q for q in qs.questions if q.name == "department")
    assert department.signature == DEPARTMENT_SIGNATURE
    assert qs.signature.startswith("sha256:") and len(qs.signature) == 71


async def test_native_ignores_platform_questions(kit: CompilerKit) -> None:
    profile = kit.snapshot.decision_profile("router-default")
    qs = kit.compiler.compile(profile, example_questions(), origin="native", output_format="native")
    assert [q.source for q in qs.questions] == ["request"] * len(qs.questions)
    assert qs.commit_critical == frozenset()


async def test_turn_order_and_commit_critical(kit: CompilerKit) -> None:
    router = kit.snapshot.decision_profile("router-default")
    app = [{"type": "predicate", "name": "wants_refund", "instructions": "The user asks for a refund."}]
    qs = kit.compiler.compile(
        router, app, origin="turn", output_format="native", blocking=["wants_refund"], platform_from=router
    )
    assert [q.name for q in qs.questions] == [*ROUTER_NAMES, "wants_refund"]
    assert qs.commit_critical == frozenset({"intent", "complexity", "wants_refund"})
    assert qs.flat_limit == 16
    predicate = qs.questions[-1]
    assert [(o.key, o.value) for o in predicate.options] == [("true", True), ("false", False)]


async def test_turn_reserved_name(kit: CompilerKit) -> None:
    router = kit.snapshot.decision_profile("router-default")
    app = [{"type": "predicate", "name": "intent", "instructions": "x"}]
    with pytest.raises(NoemaError) as err:
        kit.compiler.compile(router, app, origin="turn", output_format="native", platform_from=router)
    assert err.value.code is ErrorCode.RESERVED_QUESTION_NAME and err.value.param == "decisions.questions[0].name"


async def test_duplicate_choice_values(kit: CompilerKit) -> None:
    profile = kit.snapshot.decision_profile("router-default")
    bad: dict[str, Any] = {
        "type": "choice",
        "name": "q_dup",
        "instructions": "x",
        "choices": [{"value": "a"}, {"value": "a"}],
    }
    with pytest.raises(NoemaError) as err:
        kit.compiler.compile(profile, [bad], origin="native", output_format="native")
    assert err.value.code is ErrorCode.SCHEMA_VIOLATION and err.value.param == "questions[0].choices"


async def test_blocking_must_name_app_questions(kit: CompilerKit) -> None:
    router = kit.snapshot.decision_profile("router-default")
    with pytest.raises(NoemaError) as err:
        kit.compiler.compile(
            router, [], origin="turn", output_format="native", blocking=["intent"], platform_from=router
        )
    assert err.value.code is ErrorCode.SCHEMA_VIOLATION and err.value.param == "decisions.blocking"


async def test_too_many_choices(kit: CompilerKit) -> None:
    profile = kit.snapshot.decision_profile("router-default")
    formats: tuple[OutputFormat, ...] = ("jev", "openai")
    for fmt in formats:
        with pytest.raises(NoemaError) as err:
            kit.compiler.compile(profile, [choice("big", 20)], origin="native", output_format=fmt)
        assert err.value.code is ErrorCode.UNSUPPORTED_PARAMETER and err.value.param == "questions[0].choices"
    qs = kit.compiler.compile(profile, [choice("big", 20)], origin="native", output_format="native")
    big = qs.questions[0]
    assert big.method == "hierarchical" and len(big.groups) >= 2
    assert [g.id for g in big.groups] == [f"g{i + 1}" for i in range(len(big.groups))]
    assert sum(len(g.options) for g in big.groups) == 20


async def test_verification_selection(kit: CompilerKit) -> None:
    verifier = kit.snapshot.decision_profile("answer-verifier")
    qs = kit.compiler.compile(
        verifier, [], origin="verification", output_format="native", select=["answers_question", "unsupported_claims"]
    )
    assert [q.name for q in qs.questions] == ["answers_question", "unsupported_claims"]
    assert qs.commit_critical == frozenset()
