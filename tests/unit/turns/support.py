"""Small builders shared by the turns unit tests (answers, snapshots)."""

from __future__ import annotations

from collections.abc import Mapping

from noema.core.protocols.decision import AnswerSummary, DecisionSnapshot


def choice(
    name: str,
    top: tuple[tuple[str, float], ...],
    *,
    band: str = "high",
    calibrated: bool = False,
    final: bool = True,
    kind: str = "choice",
) -> AnswerSummary:
    """A scored choice/score answer: margin = p1 - p2."""
    margin = top[0][1] - (top[1][1] if len(top) > 1 else 0.0)
    return AnswerSummary(
        name=name,
        type=kind,  # type: ignore[arg-type]
        value=top[0][0],
        top=top,
        probability=top[0][1],
        margin=round(margin, 4),
        band=band,  # type: ignore[arg-type]
        calibrated=calibrated,
        final=final,
    )


def predicate(name: str, p_true: float, *, band: str = "high", final: bool = True) -> AnswerSummary:
    """A scored predicate: top is (bool, p) best first."""
    top = ((True, p_true), (False, 1 - p_true)) if p_true >= 0.5 else ((False, 1 - p_true), (True, p_true))
    return AnswerSummary(
        name=name,
        type="predicate",
        value=p_true,
        top=top,
        probability=p_true,
        margin=round(abs(2 * p_true - 1), 4),
        band=band,  # type: ignore[arg-type]
        calibrated=False,
        final=final,
    )


def unavailable(name: str, reason: str = "all_members_failed") -> AnswerSummary:
    return AnswerSummary(name, "unavailable", None, (), None, None, None, False, True, reason)


def snapshot_of(answers: Mapping[str, AnswerSummary], *, final: frozenset[str] | None = None) -> DecisionSnapshot:
    """A decision snapshot over `answers` (final names default to every answer marked final)."""
    names = final if final is not None else frozenset(n for n, a in answers.items() if a.final)
    return DecisionSnapshot(
        decision_id="dec_test",
        status="completed" if names == frozenset(answers) else "quorum",
        final=names == frozenset(answers),
        final_names=names,
        answers=dict(answers),
        result={},
    )


def standard_answers(
    *, intent: str = "factual_qa", p: float = 0.9, level: str = "moderate", injection: float = 0.05
) -> dict[str, AnswerSummary]:
    """The five router answers (the D-std shape)."""
    others = [v for v in ("chitchat", "coding", "factual_qa") if v != intent]
    rest = (1 - p) / 2
    return {
        "intent": choice("intent", ((intent, p), (others[0], rest))),
        "complexity": choice("complexity", ((level, 0.8), ("trivial", 0.1)), kind="score"),
        "needs_fresh_info": predicate("needs_fresh_info", 0.05),
        "sensitive_data": predicate("sensitive_data", 0.05),
        "injection_suspected": predicate("injection_suspected", injection),
    }
