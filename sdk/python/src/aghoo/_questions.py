"""Typed question sets (03 §9.2): declare questions as annotated class fields, get answers typed per field.

class Routing(DecisionSet):
    department: Choice[Literal["billing", "technical", "other"]] = choice("Which team?", {"billing": "Payments", ...})
    needs_human: Predicate = predicate("The customer asks for a human.")

r = client.decisions.create(Routing, input="...")
r.department          # ChoiceAnswer[Literal[...]] | RefusalAnswer | UnavailableAnswer | UnknownAnswer
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Literal, Self, cast, get_args, get_origin, get_type_hints

from pydantic import Discriminator, Tag, TypeAdapter

from ._decision_types import (
    ChoiceAnswer,
    DecisionResult,
    PredicateAnswer,
    RefusalAnswer,
    ScoreAnswer,
    UnavailableAnswer,
    UnknownAnswer,
)
from ._params import ChoiceOptionParam, Criteria, PredicateQuestionParam, QuestionParam, ScoreLevelParam

type Predicate = PredicateAnswer | RefusalAnswer | UnavailableAnswer | UnknownAnswer
type Choice[V] = ChoiceAnswer[V] | RefusalAnswer | UnavailableAnswer | UnknownAnswer
type Score[L] = ScoreAnswer[L] | RefusalAnswer | UnavailableAnswer | UnknownAnswer


@dataclass(frozen=True, slots=True)
class Option:
    """A choice option (or score level) with an optional description and hierarchical-method group."""

    value: str
    description: str | None = None
    group: str | None = None


Options = Mapping[str, str | None] | Sequence[Option | str]


@dataclass(frozen=True, slots=True)
class QuestionSpec:
    """A question's definition; its name is the DecisionSet field name."""

    type: Literal["predicate", "noul", "choice", "score"]
    instructions: str
    criteria: Mapping[str, str] | None = None
    options: tuple[Option, ...] | None = None  # choices or score levels; None = taken from the field's Literal

    def wire(self, name: str, literal: tuple[str, ...] | None) -> QuestionParam:
        if self.type in ("predicate", "noul"):
            kind: Literal["predicate", "noul"] = "noul" if self.type == "noul" else "predicate"
            q: PredicateQuestionParam = {"type": kind, "name": name, "instructions": self.instructions}
            if self.criteria is not None:
                q["criteria"] = cast(Criteria, dict(self.criteria))
            return q
        options = self.options if self.options is not None else tuple(Option(v) for v in literal or ())
        if self.type == "choice":
            choices: list[ChoiceOptionParam] = []
            for o in options:
                c: ChoiceOptionParam = {"value": o.value, "description": o.description}
                if o.group is not None:
                    c["group"] = o.group
                choices.append(c)
            return {"type": "choice", "name": name, "instructions": self.instructions, "choices": choices}
        levels: list[ScoreLevelParam] = [{"label": o.value, "description": o.description} for o in options]
        return {"type": "score", "name": name, "instructions": self.instructions, "levels": levels}


def _options(values: Options | None) -> tuple[Option, ...] | None:
    if values is None:
        return None
    if isinstance(values, Mapping):
        return tuple(Option(v, d) for v, d in values.items())
    return tuple(v if isinstance(v, Option) else Option(v) for v in values)


# The builders return Any so that `field: Choice[...] = choice(...)` type-checks; the value is a QuestionSpec.
def predicate(instructions: str, *, criteria: Mapping[str, str] | None = None) -> Any:
    """A boolean question (OpenAI Decisions naming); answers carry P(true)."""
    return QuestionSpec("predicate", instructions, criteria=criteria)


def noul(instructions: str, *, criteria: Mapping[str, str] | None = None) -> Any:
    """Jev naming for a boolean question; identical semantics to predicate."""
    return QuestionSpec("noul", instructions, criteria=criteria)


def choice(instructions: str, choices: Options | None = None) -> Any:
    """Options in declared order: a mapping value -> description, or a sequence of values / Option (with groups).
    Without `choices` the field's Literal values are the options."""
    return QuestionSpec("choice", instructions, options=_options(choices))


def score(instructions: str, levels: Options | None = None) -> Any:
    """2-16 levels, lowest first (level index = position). Without `levels` the field's Literal values are used."""
    return QuestionSpec("score", instructions, options=_options(levels))


_SCORED_KINDS: dict[str, frozenset[str]] = {
    "predicate": frozenset({"predicate", "noul"}),
    "noul": frozenset({"predicate", "noul"}),
    "choice": frozenset({"choice"}),
    "score": frozenset({"score"}),
}


def _tagger(kinds: frozenset[str]) -> Callable[[Any], str]:
    def tag(value: Any) -> str:
        kind = value.get("type") if isinstance(value, dict) else getattr(value, "type", None)
        if kind in kinds:
            return "scored"
        return kind if kind in ("refusal", "unavailable") else "unknown"

    return tag


def _answer_adapter(name: str, annotation: Any, spec: QuestionSpec) -> tuple[TypeAdapter[Any], tuple[str, ...] | None]:
    """The answer model of one field: the question's scored answer (parametrized by the Literal) or refusal,
    unavailable, unknown. Returns the adapter and the Literal values (None when the field is not parametrized)."""
    origin, args = get_origin(annotation), get_args(annotation)
    literal: tuple[str, ...] | None = None
    if args and get_origin(args[0]) is Literal:
        literal = tuple(str(a) for a in get_args(args[0]))
    if annotation is Predicate and spec.type in ("predicate", "noul"):
        scored: Any = PredicateAnswer
    elif (origin is Choice or annotation is Choice) and spec.type == "choice":
        scored = cast(Any, ChoiceAnswer)[args[0] if args else str]
    elif (origin is Score or annotation is Score) and spec.type == "score":
        scored = cast(Any, ScoreAnswer)[args[0] if args else str]
    else:
        raise TypeError(f"{name}: a {spec.type} question needs a {_EXPECTED[spec.type]} annotation, got {annotation!r}")
    if spec.type in ("choice", "score"):
        declared = None if spec.options is None else tuple(o.value for o in spec.options)
        if declared is None and literal is None:
            raise TypeError(f"{name}: give the options, or annotate with a Literal of the option values")
        if declared is not None and literal is not None and set(declared) != set(literal):
            raise TypeError(f"{name}: the Literal values {literal} differ from the declared options {declared}")
    union: Any = Annotated[
        Annotated[scored, Tag("scored")]
        | Annotated[RefusalAnswer, Tag("refusal")]
        | Annotated[UnavailableAnswer, Tag("unavailable")]
        | Annotated[UnknownAnswer, Tag("unknown")],
        Discriminator(_tagger(_SCORED_KINDS[spec.type])),
    ]
    return TypeAdapter(union), literal


_EXPECTED = {"predicate": "Predicate", "noul": "Predicate", "choice": "Choice[...]", "score": "Score[...]"}


class DecisionSet:
    """Base class of typed question sets. Instances (returned by `decisions.create`) carry one answer attribute per
    question field plus `result`, the full native DecisionResult."""

    __aghoo_fields__: ClassVar[dict[str, tuple[QuestionSpec, TypeAdapter[Any], tuple[str, ...] | None]]] = {}
    result: DecisionResult

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        hints = get_type_hints(cls)
        fields: dict[str, tuple[QuestionSpec, TypeAdapter[Any], tuple[str, ...] | None]] = {}
        for klass in reversed(cls.__mro__):
            for name, value in vars(klass).items():
                if isinstance(value, QuestionSpec):
                    if name not in hints:
                        raise TypeError(f"{name}: annotate the question with Choice[...], Predicate or Score[...]")
                    adapter, literal = _answer_adapter(name, hints[name], value)
                    fields[name] = (value, adapter, literal)
        if not fields:
            raise TypeError(f"{cls.__name__} declares no questions")
        cls.__aghoo_fields__ = fields

    @classmethod
    def questions(cls) -> list[QuestionParam]:
        """The wire `questions`, in field declaration order."""
        return [spec.wire(name, literal) for name, (spec, _, literal) in cls.__aghoo_fields__.items()]

    @classmethod
    def from_result(cls, result: DecisionResult, raw: Mapping[str, Any]) -> Self:
        """Validates each answer of the raw response with its field's typed model (undeclared values fail)."""
        answers = {a.get("name"): a for a in raw.get("answers", ()) if isinstance(a, Mapping)}
        obj = cls.__new__(cls)
        for name, (_, adapter, _) in cls.__aghoo_fields__.items():
            if name not in answers:
                raise ValueError(f"the decision has no answer named {name!r}")
            object.__setattr__(obj, name, adapter.validate_python(answers[name]))
        obj.result = result
        return obj

    def __repr__(self) -> str:
        parts = ", ".join(f"{n}={getattr(self, n)!r}" for n in self.__aghoo_fields__)
        return f"{type(self).__name__}({parts})"
