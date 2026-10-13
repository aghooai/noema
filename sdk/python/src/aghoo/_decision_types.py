"""Python SDK type surface (proposal v0.2): Pydantic models mirroring decision.schema.json / types.ts.

Responses ignore unknown fields and tolerate unknown enum values (additive API changes must not break clients):
an unrecognized `status` / `reason` stays a plain string, and an unrecognized answer `type` becomes an
`UnknownAnswer` that preserves `raw_type` and the raw payload. Requests are strict.
Validators enforce the model-level invariants: calibration completeness and acceptance, probability ranges,
normalization, argmax, expected score, confidence method/kind, non-negative usage, distinct labelers,
per-answer quorum vs members (required, refused, total), view/status consistency.
Invariants that need the request or routing config (answer names, declared values, bands) are server-side
and in check_examples.py.
"""
from __future__ import annotations

from typing import Annotated, Any, Generic, Literal, TypeVar, Union

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag, model_validator

V = TypeVar("V")
Prob = Annotated[float, Field(ge=0, le=1)]
Count = Annotated[int, Field(ge=0)]
ACCEPT_ECE, ACCEPT_N, TOL = 0.05, 300, 1e-3
ACCEPT_CLASS_N, ACCEPT_CONSENSUS = 30, 0.6  # per-class holdout labels (below: flagged) and per-class consensus rate
ACCEPT_FAMILY_LIFT = 0.10  # initial labeling.family_overlap_lift_max (config); tuned in Phase 0


class Response(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, protected_namespaces=())  # `model_revision` is a wire field


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ScopeSlice(Response):
    name: str
    n: Annotated[int, Field(ge=1)]


class HoldoutScope(Response):
    target: Literal["llm_consensus_agreement"]
    retained_share: Prob
    slices: Annotated[list[ScopeSlice], Field(min_length=1)]


class FamilyOverlap(Response):
    max_member_lift: Annotated[float, Field(ge=-1, le=1)]
    threshold: Prob


class HoldoutMetrics(Response):
    dataset_id: str
    label_source: Literal["llm_consensus"]  # D4
    labelers: Annotated[list[str], Field(min_length=2)]
    consensus_rate: Prob
    consensus_rate_by_class: Annotated[dict[str, Prob], Field(min_length=2)]
    n: Annotated[int, Field(ge=1)]
    n_by_class: Annotated[dict[str, Count], Field(min_length=2)]
    ece: Prob
    brier: Annotated[float, Field(ge=0, le=2)]
    nll: Annotated[float, Field(ge=0)]
    nll_uncalibrated: Annotated[float, Field(ge=0)]
    flagged_classes: list[str]
    scope: HoldoutScope
    family_overlap: FamilyOverlap
    evaluated_at: str

    @model_validator(mode="after")
    def _distinct_labelers(self):
        if len(set(self.labelers)) != len(self.labelers):
            raise ValueError("labelers must be distinct models")
        return self

    def acceptance_errors(self) -> list[str]:
        """Holdout acceptance (06 §2.3): what must hold before an answer may be served as calibrated."""
        errs = []
        if self.ece > ACCEPT_ECE or self.n < ACCEPT_N:
            errs.append(f"ece {self.ece} / n {self.n} fail acceptance")
        if self.nll > self.nll_uncalibrated:
            errs.append("calibrated NLL is worse than uncalibrated")
        low = {c: r for c, r in self.consensus_rate_by_class.items() if r < ACCEPT_CONSENSUS}
        if low or self.consensus_rate < ACCEPT_CONSENSUS:
            errs.append(f"consensus rate below {ACCEPT_CONSENSUS}: overall {self.consensus_rate}, classes {low}")
        if set(self.n_by_class) != set(self.consensus_rate_by_class) or sum(self.n_by_class.values()) != self.n:
            errs.append("n_by_class must cover the same classes as consensus_rate_by_class and sum to n")
        if sorted(self.flagged_classes) != sorted(c for c, k in self.n_by_class.items() if k < ACCEPT_CLASS_N):
            errs.append(f"flagged_classes must be exactly the classes with n < {ACCEPT_CLASS_N}")
        if self.family_overlap.threshold > ACCEPT_FAMILY_LIFT or self.family_overlap.max_member_lift > self.family_overlap.threshold:
            errs.append("labeler-family overlap lift exceeds its threshold")
        return errs


class CalibrationBinding(Response):
    profile_id: str
    profile_version: Annotated[str, Field(pattern=r"^[0-9]+$")]
    question_signature: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    input_view_signature: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    choice_method: Literal["flat", "hierarchical"]


class Calibration(Response):
    status: Literal["calibrated", "provider_reported", "uncalibrated"] | str  # unknown values: treat as uncalibrated
    source: Literal["logprob", "verbalized", "mixed"] | None = None
    method: Literal["temperature", "platt", "isotonic", "dirichlet"] | None = None
    calibrator_id: str | None = None
    member_subset: list[str] | None = None
    binding: CalibrationBinding | None = None
    holdout: HoldoutMetrics | None = None

    @model_validator(mode="after")
    def _calibrated_needs_evidence(self):
        if self.status == "calibrated":
            missing = [f for f in ("method", "calibrator_id", "member_subset", "binding", "holdout") if getattr(self, f) is None]
            if missing:
                raise ValueError(f"calibrated requires {missing}")
            errs = self.holdout.acceptance_errors()
            if errs:
                raise ValueError("calibrated but holdout fails acceptance: " + "; ".join(errs))
        return self


class HeuristicConfidence(Response):
    kind: Literal["heuristic"]
    value: Prob
    method: Literal["normalized_max", "ordinal_dispersion", "agreement", "provider"]
    band: Literal["high", "medium", "low"]
    margin: Prob
    normalized_entropy: Prob
    agreement: Prob
    member_divergence: Prob | None


class Usage(Response):
    """Disjoint categories: input_tokens includes cache reads and writes; output_tokens includes reasoning."""
    input_tokens: Count
    output_tokens: Count
    cached_input_tokens: Count
    cache_write_input_tokens: Count
    reasoning_tokens: Count
    cost_usd: Annotated[float, Field(ge=0)]
    cost_estimated: bool
    tokens_estimated: bool  # True when the provider returned no usage (cancelled call): an estimate, not zero

    @model_validator(mode="after")
    def _parts(self):
        if self.cached_input_tokens + self.cache_write_input_tokens > self.input_tokens:
            raise ValueError("cache reads + writes are parts of input_tokens")
        if self.reasoning_tokens > self.output_tokens:
            raise ValueError("reasoning_tokens is a part of output_tokens")
        return self


class QuorumInfo(Response):
    required: Count
    responded: Count
    refused: Count
    total: Count

    @model_validator(mode="after")
    def _counts(self):
        if self.required > self.total or self.responded + self.refused > self.total:
            raise ValueError("quorum counts inconsistent")
        return self


class MemberVote(Response):
    member_id: str
    provider: Literal["bedrock-converse", "bedrock-mantle-chat"]
    model: str
    model_revision: str | None = None  # provider-reported revision when the API returns one; profile versions pin ids, not revisions
    region: str | None = None
    elicitation: Literal["token_logprobs", "verbalized", "sampling", "native_decision"]
    input_view: Literal["native", "thumbnail", "ocr_text", "derived_text", "keyframes_transcript", "metadata_only", "skipped"]
    status: Literal["ok", "refused", "timeout", "error", "late", "skipped"] | str  # unknown values are kept verbatim
    distribution: dict[str, Prob] | None = None
    calibrated_distribution: dict[str, Prob] | None = None
    calibrator_id: str | None = None
    weight: Annotated[float, Field(ge=0)]
    latency_ms: Count
    samples: int | None = None
    usage: Usage | None = None
    error: dict[str, str] | None = None
    skip_reason: Literal["input_view", "too_many_choices"] | None = None
    censored_options: list[str] | None = None  # declared options this member could not measure (no floor is invented)
    unresolved_mass: Prob = 0  # mass not attributed to any option; distribution sums to 1 - unresolved_mass

    @model_validator(mode="after")
    def _view_status(self):
        if self.input_view == "skipped" and self.status != "skipped":
            raise ValueError("input_view skipped implies status skipped")
        if self.status == "skipped" and self.skip_reason is None:
            raise ValueError("a skipped member states why (skip_reason)")
        if self.input_view == "skipped" and self.skip_reason not in (None, "input_view"):
            raise ValueError("a skipped view is skip_reason input_view")
        if self.status == "ok":
            if self.distribution is None:
                raise ValueError("an ok member carries a distribution")
            if abs(sum(self.distribution.values()) + self.unresolved_mass - 1) > TOL:
                raise ValueError("member distribution plus unresolved_mass must sum to 1")
            if set(self.censored_options or []) & set(self.distribution):
                raise ValueError("a censored option carries no probability")
        return self


class Candidate(Response, Generic[V]):
    value: V
    probability: Prob
    raw_score: float
    votes: int


class LevelCandidate(Candidate[V], Generic[V]):
    index: int


class _Scored(Response):
    name: str
    calibration: Calibration
    confidence: HeuristicConfidence
    quorum: QuorumInfo
    members: list[MemberVote] | None = None
    unresolved_mass: Prob = 0  # never spread over options; confidence counts it as one extra bucket

    def _common_checks(self, cands, expected_method):
        probs = [c.probability for c in cands]
        if abs(sum(probs) + self.unresolved_mass - 1) > TOL:
            raise ValueError("candidate probabilities plus unresolved_mass must sum to 1")
        if probs != sorted(probs, reverse=True):
            raise ValueError("candidates must be sorted by probability")
        if self.confidence.method in ("normalized_max", "ordinal_dispersion") and self.confidence.method != expected_method:
            raise ValueError(f"confidence method must be {expected_method}")
        if self.quorum.responded < self.quorum.required:
            raise ValueError("answered below its quorum")
        if self.members:
            ok = sum(1 for m in self.members if m.status == "ok")
            if ok != self.quorum.responded or len(self.members) != self.quorum.total:
                raise ValueError("quorum disagrees with members")
            if sum(1 for m in self.members if m.status == "refused") != self.quorum.refused:
                raise ValueError("quorum.refused disagrees with members")


class PredicateAnswer(_Scored):
    type: Literal["predicate", "noul"]  # OpenAI and Jev names are synonyms; echoes the request
    probability: Prob
    candidates: list[Candidate[bool]]

    @model_validator(mode="after")
    def _check(self):
        self._common_checks(self.candidates, "normalized_max")
        p_true = next((c.probability for c in self.candidates if c.value is True), None)
        if p_true is None or abs(p_true - self.probability) > TOL:
            raise ValueError("probability must equal P(true)")
        return self


class Group(Response):
    id: Annotated[str, Field(pattern=r"^g[0-9]+$")]
    label: str
    options: Annotated[list[str], Field(min_length=1)]
    probability: Prob  # pooled stage-1 mass
    expanded: bool  # stage 2 ran inside the group; its options are candidates only then


class ChoiceAnswer(_Scored, Generic[V]):
    type: Literal["choice"]
    choice: V
    candidates: list[Candidate[V]]
    choice_method: Literal["flat", "hierarchical"] = "flat"
    groups: Annotated[list[Group], Field(min_length=2)] | None = None

    @model_validator(mode="after")
    def _check(self):
        self._common_checks(self.candidates, "normalized_max")
        if self.choice != self.candidates[0].value:
            raise ValueError("choice must be the top candidate")
        if (self.choice_method == "hierarchical") != (self.groups is not None):
            raise ValueError("groups are present exactly when choice_method is hierarchical")
        if self.groups:
            self._group_checks()
        return self

    def _group_checks(self):
        """Hierarchical answers (08 §3.4): mass is conserved, nothing is spread, and the top groups were expanded."""
        g = self.groups
        if abs(sum(x.probability for x in g) - 1) > TOL:
            raise ValueError("stage-1 group probabilities must sum to 1")
        if abs(self.unresolved_mass - sum(x.probability for x in g if not x.expanded)) > TOL:
            raise ValueError("unresolved_mass must equal the mass of the unexpanded groups")
        by_value = {c.value: c.probability for c in self.candidates}
        expanded_options = [o for x in g if x.expanded for o in x.options]
        if sorted(map(str, by_value)) != sorted(expanded_options):
            raise ValueError("candidates must be exactly the options of the expanded groups")
        for x in g:
            if x.expanded and abs(sum(by_value[o] for o in x.options) - x.probability) > 2 * TOL:
                raise ValueError(f"candidates of group {x.id} must sum to the group's stage-1 mass")
        units = [round(x.probability * 10_000) for x in g]  # emitted 4-dp masses as integers (the server's expansion rule)
        order = sorted(range(len(g)), key=lambda i: (-units[i], i))
        need, covered, n_options = 0, 0, 0
        for i in order[:3]:  # top groups covering >= 0.9 of the stage-1 mass and holding >= 2 options, at most 3
            need += 1
            covered += units[i]
            n_options += len(g[i].options)
            if covered >= 9_000 and n_options >= 2:
                break
        if {i for i, x in enumerate(g) if x.expanded} != set(order[:need]):
            raise ValueError("expanded groups must be the top groups covering >= 0.9 of stage-1 mass and holding >= 2 options (at most 3)")


class ScoreAnswer(_Scored, Generic[V]):
    type: Literal["score"]
    score: float
    level: V
    candidates: list[LevelCandidate[V]]

    @model_validator(mode="after")
    def _check(self):
        self._common_checks(self.candidates, "ordinal_dispersion")
        if abs(sum(c.index * c.probability for c in self.candidates) - self.score) > TOL:
            raise ValueError("score must be the expected level index")
        if self.level != self.candidates[0].value:
            raise ValueError("level must be the most probable label")
        return self


class RefusalAnswer(Response):
    type: Literal["refusal"]
    name: str
    quorum: QuorumInfo | None = None
    members: list[MemberVote] | None = None


class UnavailableAnswer(Response):
    type: Literal["unavailable"]
    name: str
    reason: Literal["deadline_exceeded", "no_quorum", "all_members_failed", "no_capable_member"] | str
    quorum: QuorumInfo | None = None
    members: list[MemberVote] | None = None


class UnknownAnswer(Response):
    """An answer `type` this SDK version doesn't know. Treat it as unavailable; never read values from `raw` as a decision."""
    model_config = ConfigDict(extra="allow", frozen=True)
    type: Literal["unknown"]
    raw_type: str
    name: str | None = None
    raw: dict[str, Any]

    @model_validator(mode="before")
    @classmethod
    def _wrap_wire_payload(cls, value):
        if isinstance(value, dict) and "raw_type" not in value:
            return {"type": "unknown", "raw_type": str(value.get("type")), "name": value.get("name"), "raw": dict(value)}
        return value


def _answer_tag(value):
    kind = value.get("type") if isinstance(value, dict) else getattr(value, "type", None)
    return "predicate" if kind in ("predicate", "noul") else kind if kind in ("choice", "score", "refusal", "unavailable") else "unknown"


Answer = Annotated[
    Union[
        Annotated[PredicateAnswer, Tag("predicate")],
        Annotated[ChoiceAnswer[str], Tag("choice")],
        Annotated[ScoreAnswer[str], Tag("score")],
        Annotated[RefusalAnswer, Tag("refusal")],
        Annotated[UnavailableAnswer, Tag("unavailable")],
        Annotated[UnknownAnswer, Tag("unknown")],
    ],
    Discriminator(_answer_tag),
]


def _jev_sum_tol(k: int) -> float:
    """K values each rounded to 2 decimals can drift by K * 0.005 in total (never below 0.011)."""
    return max(0.011, k * 0.005)


class JevNoul(Response):
    type: Literal["noul"]
    noul: Prob


class JevChoice(Response):
    type: Literal["choice"]
    choice: str
    probabilities: dict[str, Prob]
    confidence: Prob

    @model_validator(mode="after")
    def _check(self):
        if abs(sum(self.probabilities.values()) - 1) > _jev_sum_tol(len(self.probabilities)):
            raise ValueError("probabilities must sum to 1 within the 2-decimal rounding tolerance")
        if self.probabilities.get(self.choice, -1) < max(self.probabilities.values()):
            raise ValueError("choice must be the most probable value")
        return self


class JevScore(Response):
    type: Literal["score"]
    score: Annotated[float, Field(ge=0)]
    legend: dict[str, str]
    probabilities: dict[str, Prob]
    confidence: Prob

    @model_validator(mode="after")
    def _check(self):
        if set(self.legend) != set(self.probabilities):
            raise ValueError("legend and probabilities must share level keys")
        if abs(sum(self.probabilities.values()) - 1) > _jev_sum_tol(len(self.probabilities)):
            raise ValueError("probabilities must sum to 1 within the 2-decimal rounding tolerance")
        if abs(sum(int(k) * v for k, v in self.probabilities.items()) - self.score) > 0.02:
            raise ValueError("score must be the expected level index")
        return self


class JevRefusal(Response):
    type: Literal["refusal"]


class JevUnavailable(Response):
    type: Literal["unavailable"]
    reason: str


JevAnswer = Annotated[Union[JevNoul, JevChoice, JevScore, JevRefusal, JevUnavailable], Field(discriminator="type")]


class JevDecisionResponse(Response):
    """output_format="jev": Jev-shaped answers keyed by question name (produced on Bedrock, e.g. gpt-6-luna)."""
    model: str
    answers: dict[str, JevAnswer]
    usage: dict[str, int]
    decision_id: str


class Provenance(Response):
    trace_id: str
    profile_id: str
    profile_version: str
    question_signature: str
    input_digest: str
    input_view_signature: str
    option_order_seed: int
    prompt_templates: dict[str, str]
    aggregation: dict[str, str]
    deadline_ms: int
    quorum: QuorumInfo
    cache: dict[str, bool]
    stages: list[dict[str, Any]]
    started_at: str
    completed_at: str


class DecisionResult(Response):
    id: str
    object: Literal["decision"]
    created_at: int
    status: Literal["completed", "quorum", "partial"] | str  # unknown values are kept verbatim
    answers: list[Answer]
    provenance: Provenance
    usage: Usage


# --- Typed question sets (SDK ergonomics) -------------------------------------------------------
# class Routing(DecisionSet):
#     department: Choice[Literal["billing", "technical", "shipping", "other"]] = choice("Which team?", {...})
#     needs_human: Predicate = predicate("The customer asks for a human.", criteria={"true": "...", "false": "..."})
#
# result = client.decisions.create(Routing, profile="router-default@1", input="...")
# result.department            -> ChoiceAnswer[Literal[...]] | RefusalAnswer | UnavailableAnswer
# result.department.choice     -> Literal["billing", "technical", "shipping", "other"]  (after narrowing)
#
# The SDK builds the wire `questions` from the class fields (Literal args become choice values) and validates each
# answer with the parametrized model, so an undeclared value fails validation.

# Rejected here (model-level invariants); the rest need the request or routing config (check_examples.py).
MODEL_LEVEL_INVALID = {
    "calibrated-without-holdout", "choice-not-argmax", "probabilities-not-normalized", "confidence-labeled-probability",
    "score-not-expected-index", "consensus-labels-without-labelers", "calibrated-below-acceptance", "human-label-source",
    "wrong-confidence-method", "answer-quorum-mismatch", "calibrated-class-consensus-below-minimum",
    "calibrated-family-overlap-lift", "calibrated-nll-worse-than-uncalibrated",
    "hierarchical-unresolved-renormalized", "hierarchical-coverage", "usage-cache-exceeds-input",
}

if __name__ == "__main__":
    import json
    import pathlib
    import sys

    from pydantic import TypeAdapter, ValidationError

    ex = pathlib.Path(__file__).parent / "examples"
    failures = 0
    for path in sorted(ex.glob("decision-result.*.json")):
        kind = path.name.removeprefix("decision-result.").removesuffix(".json")
        try:
            DecisionResult.model_validate_json(path.read_text())
            accepted = True
        except ValidationError:
            accepted = False
        expect_accept = kind.startswith("valid") or kind.removeprefix("invalid-") not in MODEL_LEVEL_INVALID
        failures += accepted != expect_accept
        print(f"{'PASS' if accepted == expect_accept else 'FAIL'}  pydantic {path.name:60s} {'accepted' if accepted else 'rejected'}")
    for path in sorted(ex.glob("decision-jev-response.*.json")):
        try:
            JevDecisionResponse.model_validate_json(path.read_text())
            accepted = True
        except ValidationError:
            accepted = False
        expect_accept = ".valid" in path.name
        failures += accepted != expect_accept
        print(f"{'PASS' if accepted == expect_accept else 'FAIL'}  pydantic {path.name:60s} {'accepted' if accepted else 'rejected'}")
    valid = json.loads((ex / "decision-result.valid.json").read_text())
    dept = valid["answers"][0]
    ChoiceAnswer[Literal["billing", "technical", "shipping", "other"]].model_validate(dept)
    try:  # an undeclared choice value must be rejected by the typed model
        TypeAdapter(ChoiceAnswer[Literal["billing", "technical"]]).validate_python(dept)
        failures += 1
        print("FAIL  typed model accepted undeclared values")
    except ValidationError:
        print("PASS  typed model rejects undeclared values")
    extra = {**valid, "new_field_added_later": 1}
    DecisionResult.model_validate(extra)  # additive change must not break clients
    print("PASS  responses tolerate unknown fields")
    print(f"pydantic {'PASS' if not failures else 'FAIL'} ({failures} unexpected)")
    sys.exit(1 if failures else 0)
