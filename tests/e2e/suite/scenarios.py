"""Wire-mock scenario documents for the full e2e suite (scenario DSL: tests/fakes/scenario.schema.json).

They port the golden SSE fixtures' scenarios (tests/golden/sse_scenarios.py) to the compose stack, which runs on real
time: the latencies are spread so that every ordering a test asserts (first text vs S1 vs the 400 ms hold cap vs the
1,000 ms router deadline) has at least ~200 ms of margin. Rules are tried in order (the first match wins).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

SOL: Final = "us.openai.gpt-6.1-sol"
OPUS: Final = "us.anthropic.claude-opus-5-5"
GLM: Final = "us.zai.glm-5.3"
HAIKU: Final = "us.anthropic.claude-haiku-5-5"
LUNA: Final = "us.openai.gpt-6-luna"
DEEPSEEK: Final = "deepseek.v3.2"

INTENT, COMPLEXITY = "primary intent", "How hard"
SENSITIVE, INJECTION = "sensitive personal data", "instruct the AI system"
VERIFY_ANSWERS, VERIFY_CLAIMS = "directly addresses", "not supported by the conversation"

FACTUAL: Final = {"factual_qa": 0.9, "chitchat": 0.05, "coding": 0.05}
CODING: Final = {"coding": 0.85, "factual_qa": 0.15}
MODERATE: Final = {"trivial": 0.1, "moderate": 0.8, "hard": 0.1}
HARD: Final = {"moderate": 0.1, "hard": 0.8, "expert": 0.1}
LOW: Final = {"true": 0.05, "false": 0.95}
HIGH: Final = {"true": 0.9, "false": 0.1}
CHUNKS: Final = ("The capital ", "of France ", "is Paris. ", "It is also ", "the largest ", "city in ", "the country.")
TEXT: Final = "".join(CHUNKS)
ROUTER_MS: Final = 500  # every router member answers here (the profile deadline is 1,000 ms)
Rule = dict[str, Any]


def answer(match: Mapping[str, Any], dist: Mapping[str, float], ms: float) -> Rule:
    return {"match": dict(match), "answer": {"dist": dict(dist)}, "latency_ms": ms}


def fault(match: Mapping[str, Any], kind: str, ms: float = 50, **extra: Any) -> Rule:
    return {"match": dict(match), "fault": {"kind": kind, **extra}, "latency_ms": ms}


def stream(
    model: str,
    *,
    first_text_ms: float,
    first_event_ms: float = 100,
    chunks: Sequence[str] = CHUNKS,
    step_ms: float = 100,
    reasoning: int = 0,
    fault: Mapping[str, Any] | None = None,
) -> Rule:
    """A generation stream of `model` (ConverseStream): one 4-token chunk every `step_ms`."""
    spec: dict[str, Any] = {
        "first_event_ms": first_event_ms,
        "first_text_ms": first_text_ms,
        "chunks": list(chunks),
        "chunk_tokens": 4,
        "tokens_per_s": 4000.0 / step_ms,
    }
    if reasoning:
        spec["reasoning"] = {"deltas": reasoning, "every_ms": 20}
    rule: Rule = {"match": {"model": model, "op": "converse-stream"}, "stream": spec}
    if fault is not None:
        rule["fault"] = dict(fault)
    return rule


def router(
    *,
    intent: Mapping[str, float] = FACTUAL,
    level: Mapping[str, float] = MODERATE,
    injection: Mapping[str, float] = LOW,
    ms: float = ROUTER_MS,
    haiku_hangs_on: str | None = SENSITIVE,
    haiku_silent: bool = False,
) -> list[Rule]:
    """The five router-default questions, then a catch-all (every other predicate: confidently false), so put any
    other decision rule (verifier, decision tests) before these. By default Haiku hangs on one non-blocking question,
    so S1 (quorum) and the final snapshot (at the 1,000 ms deadline) differ and the turn sees a `decision.updated`."""
    rules: list[Rule] = []
    if haiku_silent:
        rules.append(fault({"model": HAIKU}, "hang_before_first_byte", 0))
    elif haiku_hangs_on is not None:
        rules.append(fault({"model": HAIKU, "prompt_contains": haiku_hangs_on}, "hang_before_first_byte", 0))
    for text, dist in ((INTENT, intent), (COMPLEXITY, level), (INJECTION, injection)):
        rules.append(answer({"prompt_contains": text}, dist, ms))
    rules.append(answer({}, LOW, ms))
    return rules


def verifier(ms: float = 300) -> list[Rule]:
    """answer-verifier: the answer addresses the question and makes no unsupported claim."""
    return [
        answer({"prompt_contains": VERIFY_ANSWERS}, HIGH, ms),
        answer({"prompt_contains": VERIFY_CLAIMS}, {"true": 0.1, "false": 0.9}, ms),
    ]


def document(name: str, *groups: Sequence[Rule]) -> dict[str, Any]:
    return {
        "scenario": name,
        "seed": 7,
        "mantle_auth": "bearer_short_term",
        "defaults": {"latency": {"fixed_ms": 0}},
        "rules": [rule for group in groups for rule in group],
    }
