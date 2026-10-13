"""`QuestionCompilerImpl`: validation, options, signatures and the method per question (spec-06 §2.4, §3; §19.1;
IMPL-036, IMPL-069).

Order: platform questions (turns and verification only) in config order, then the app questions in request order.
`/v1/decisions` and the facade ignore a profile's built-in questions and commit-critical list.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal

from noema.config.models import ConfigQuestionCfg, DecisionProfileCfg
from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.config import ConfigRegistry, ConfigSnapshot
from noema.core.protocols.decision import (
    CompiledQuestion,
    CompiledQuestionSet,
    GroupSpec,
    OptionSpec,
    Origin,
    OutputFormat,
)
from noema.core.values import JsonValue

from .hierarchical import form_groups
from .signatures import question_set_signature, question_signature

_TYPES: Final = frozenset({"predicate", "noul", "choice", "score"})
AUTO_NAME: Final = re.compile(r"^q[0-9]+$")


def flat_limit(snapshot: ConfigSnapshot, profile: DecisionProfileCfg) -> int:
    """`L(profile)`: the smallest `max_choices` over the templates of all members (spec-06 §3.1)."""
    return min(snapshot.template(m.template).max_choices for m in profile.members)


def option_count(wire: Mapping[str, Any]) -> int:
    """K: 2 for predicates, the choices of a choice question, the levels of a score question."""
    kind = wire.get("type")
    if kind == "choice":
        return len(wire.get("choices") or ())
    if kind == "score":
        return len(wire.get("levels") or ())
    return 2


def platform_wire(q: ConfigQuestionCfg) -> dict[str, JsonValue]:
    """A platform question as its config dict (absent fields omitted; the signature input)."""
    wire: dict[str, JsonValue] = {"type": q.type, "name": q.name, "instructions": q.instructions}
    if q.criteria is not None:
        wire["criteria"] = {k: v for k, v in (("true", q.criteria.true), ("false", q.criteria.false)) if v is not None}
    if q.choices is not None:
        wire["choices"] = [
            {"value": c.value, **({"description": c.description} if c.description is not None else {})}
            for c in q.choices
        ]
    if q.levels is not None:
        wire["levels"] = [
            {"label": lv.label, **({"description": lv.description} if lv.description is not None else {})}
            for lv in q.levels
        ]
    return wire


def _bad(param: str, message: str) -> NoemaError:
    return NoemaError(ErrorCode.SCHEMA_VIOLATION, message, param=param)


def _options(wire: Mapping[str, Any], param: str) -> tuple[OptionSpec, ...]:
    kind = wire["type"]
    if kind in ("predicate", "noul"):
        crit = wire.get("criteria") or {}
        return (OptionSpec("true", True, crit.get("true"), 0), OptionSpec("false", False, crit.get("false"), 1))
    field = "choices" if kind == "choice" else "levels"
    attr = "value" if kind == "choice" else "label"
    items = wire.get(field)
    if not isinstance(items, list) or len(items) < 2:
        raise _bad(f"{param}.{field}", "A question needs at least two options.")
    opts = tuple(OptionSpec(str(c[attr]), str(c[attr]), c.get("description"), i) for i, c in enumerate(items))
    keys = [o.key for o in opts]
    if len(set(keys)) != len(keys):
        what = "Choice values" if kind == "choice" else "Level labels"
        raise _bad(f"{param}.{field}", f"{what} must be unique.")
    if kind == "choice":
        groups = [c.get("group") for c in items]
        if any(g is not None for g in groups) and not all(g is not None for g in groups):
            raise _bad(f"{param}.choices", "Either every choice has a group or none does.")
        if all(g is not None for g in groups) and len(set(groups)) < 2:
            raise _bad(f"{param}.choices", "Groups need at least two distinct labels.")
    return opts


def compile_question(
    wire: Mapping[str, Any],
    *,
    source: Literal["platform", "request"],
    limit: int,
    output_format: OutputFormat,
    critical: bool,
    param: str,
) -> CompiledQuestion:
    """One validated question (spec-06 §3.1). K > L: platform questions are always flat (422 `decisions.profile`);
    jev/openai cannot carry `unresolved_mass` (422 `questions[i].choices`); otherwise hierarchical."""
    kind = wire.get("type")
    if kind not in _TYPES or not isinstance(wire.get("name"), str) or not isinstance(wire.get("instructions"), str):
        raise _bad(param, "Invalid question.")
    options = _options(wire, param)
    method: Literal["flat", "hierarchical"] = "flat"
    groups: tuple[GroupSpec, ...] = ()
    if kind == "choice" and len(options) > limit:
        if source == "platform":
            raise NoemaError(
                ErrorCode.UNSUPPORTED_PARAMETER,
                "The decision profile cannot carry a platform question flat.",
                param="decisions.profile",
                details={"max_choices": limit, "question": str(wire["name"])},
            )
        if output_format in ("jev", "openai"):
            raise NoemaError(
                ErrorCode.UNSUPPORTED_PARAMETER,
                "This output format cannot carry a question that needs the hierarchical method.",
                param=f"{param}.choices",
                details={"max_choices": limit, "method": "hierarchical"},
            )
        method = "hierarchical"
        groups = form_groups(options, [c.get("group") for c in wire["choices"]], limit)
    crit = wire.get("criteria")
    criteria = (crit.get("true"), crit.get("false")) if kind in ("predicate", "noul") and crit else None
    return CompiledQuestion(
        name=str(wire["name"]),
        type=kind,
        instructions=str(wire["instructions"]),
        options=options,
        criteria=criteria,
        wire=wire,
        signature=question_signature(wire),
        commit_critical=critical,
        source=source,
        method=method,
        groups=groups,
    )


class QuestionCompilerImpl:
    """Implements `QuestionCompiler` over the registry's current snapshot; `compile_with` takes a pinned one."""

    def __init__(self, config: ConfigRegistry) -> None:
        self._config = config

    def compile(
        self,
        profile: DecisionProfileCfg,
        app_questions: Sequence[Mapping[str, JsonValue]],
        *,
        origin: Origin,
        output_format: OutputFormat,
        blocking: Sequence[str] = (),
        select: Sequence[str] | None = None,
        platform_from: DecisionProfileCfg | None = None,
    ) -> CompiledQuestionSet:
        return self.compile_with(
            self._config.current(),
            profile,
            app_questions,
            origin=origin,
            output_format=output_format,
            blocking=blocking,
            select=select,
            platform_from=platform_from,
        )

    def compile_with(
        self,
        snapshot: ConfigSnapshot,
        profile: DecisionProfileCfg,
        app_questions: Sequence[Mapping[str, JsonValue]],
        *,
        origin: Origin,
        output_format: OutputFormat,
        blocking: Sequence[str] = (),
        select: Sequence[str] | None = None,
        platform_from: DecisionProfileCfg | None = None,
    ) -> CompiledQuestionSet:
        """`compile` against a pinned snapshot (the caller's, spec-02 §2). Raises `NoemaError` (spec-06 §3.1)."""
        limit = flat_limit(snapshot, profile)
        prefix = "decisions.questions" if origin == "turn" else "questions"
        source_profile = platform_from or profile
        platform: list[ConfigQuestionCfg] = []
        if origin in ("turn", "verification"):
            platform = [q for q in source_profile.questions if q.dynamic_choices is None]
            if select is not None:
                platform = [q for q in platform if q.name in select]
        critical = set(source_profile.commit_critical) if origin == "turn" else set()
        compiled = [
            compile_question(
                platform_wire(q),
                source="platform",
                limit=limit,
                output_format=output_format,
                critical=q.name in critical,
                param="decisions.profile",
            )
            for q in platform
        ]
        platform_names = {q.name for q in platform}
        app_names: list[str] = []
        for i, raw in enumerate(app_questions):
            param = f"{prefix}[{i}]"
            name = raw.get("name")
            if origin in ("native", "turn") and isinstance(name, str) and AUTO_NAME.match(name):
                raise _bad(f"{param}.name", "Names q<digits> are reserved.")
            if origin == "turn" and name in platform_names:
                raise NoemaError(
                    ErrorCode.RESERVED_QUESTION_NAME, "The question name is reserved.", param=f"{param}.name"
                )
            if name in app_names:
                raise _bad(f"{param}.name", "Question names must be unique.")
            q = compile_question(
                raw, source="request", limit=limit, output_format=output_format, critical=name in blocking, param=param
            )
            app_names.append(q.name)
            compiled.append(q)
        if unknown := [b for b in blocking if b not in app_names]:
            raise _bad("decisions.blocking", f"{len(unknown)} blocking name(s) are not app questions.")
        return CompiledQuestionSet(
            questions=tuple(compiled),
            signature=question_set_signature([q.wire for q in compiled]),
            commit_critical=frozenset(q.name for q in compiled if q.commit_critical),
            flat_limit=limit,
        )
