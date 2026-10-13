"""The scenario DSL (spec-10 §2.4): loader, rule matching, answer rendering and stream plans.

One document drives the fakes under `FakeClock` (virtual ms) and `tools/mockbedrock` (wall-clock ms); the golden
fixtures of spec-04 §4.2 embed it. `tools/mockbedrock` imports this loader and never re-parses. Everything here is
pure: time is a caller-supplied `now_ms()` and nothing sleeps.

Public API:
- `load_scenario(source)` -> `Scenario` (JSON Schema `scenario.schema.json` + semantic checks; `ScenarioError`);
- `ScenarioRuntime(scenario, now_ms=...)`: `resolve(RequestInfo)` -> `Resolution` (rule, fired fault, latency,
  barrier, the seeded draw), with `nth`, `rate` and `window_ms` semantics; barriers via `release`/`is_released`;
- renderers: `option_letters(prompt)`, `letter_dist`, `verbalized_text`, `logprob_entries`;
- `stream_plan(resolution, request)` -> `StreamPlan` (timed provider events, ms from the call).
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, Final, Literal, cast

import jsonschema
import yaml

SCHEMA_PATH: Final = Path(__file__).with_name("scenario.schema.json")
Op = Literal["converse", "converse-stream", "chat", "chat-stream"]
WireStop = Literal["end_turn", "max_tokens", "stop_sequence", "content_filtered", "guardrail_intervened"]
FaultKind = Literal[
    "throttling",
    "access_denied",
    "validation",
    "http_5xx",
    "model_timeout",
    "connect_reset",
    "hang_before_first_byte",
    "stall_after",
    "exception_event",
    "reasoning_only",
    "empty_text",
    "malformed",
    "truncated_json",
    "invalid_verbalized",
    "refusal",
]
# Faults that fail the call before any response byte (Adapter.complete raises; Adapter.stream.__aenter__ raises).
PRE_BYTE_FAULTS: Final[frozenset[str]] = frozenset(
    {"throttling", "access_denied", "validation", "http_5xx", "model_timeout", "connect_reset"}
)
CHARS_PER_TOKEN: Final = 4


class ScenarioError(ValueError):
    """An invalid scenario document (`errors` lists every problem)."""

    def __init__(self, errors: Sequence[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = tuple(errors)


class NoRuleMatched(LookupError):
    """No rule answers this request (the mock answers HTTP 500; a test failure)."""


@dataclass(frozen=True, slots=True)
class Match:
    model: str | None = None
    op: Op | None = None
    prompt_contains: str | None = None
    nth: int | None = None
    tenant_akid: str | None = None


@dataclass(frozen=True, slots=True)
class Answer:
    dist: Mapping[str, float] | None = None
    raw_text: str | None = None
    top_k: int | None = None
    split_tokens: Mapping[str, float] = field(default_factory=dict)
    whitespace_variants: Mapping[str, float] = field(default_factory=dict)
    eos_mass: float = 0.0
    other_tokens: Mapping[str, float] = field(default_factory=dict)
    censor: tuple[str, ...] = ()
    harmony_final: bool = False


@dataclass(frozen=True, slots=True)
class LatencyDist:
    p5: float
    p95: float
    tail_p: float = 0.0
    tail_ms: tuple[float, float] | None = None


@dataclass(frozen=True, slots=True)
class StreamSpec:
    first_event_ms: float | None = None
    first_text_ms: float | None = None
    tokens_per_s: float = 60.0
    chunk_tokens: int = 4
    reasoning_deltas: int | None = None
    reasoning_every_ms: float = 20.0
    hidden_reasoning_tokens: int = 0
    text: str | None = None
    tokens: int | None = None
    chunks: tuple[str, ...] | None = None
    stop: WireStop = "end_turn"
    usage: Literal["auto", "none"] | Mapping[str, int] = "auto"
    pad: int = 0
    stamp: bool = False


@dataclass(frozen=True, slots=True)
class Fault:
    kind: FaultKind
    message: str | None = None
    status: int = 503
    n: int = 0
    type: str = "modelStreamErrorException"
    after: int = 0
    variant: str | None = None
    retry_after_s: float | None = None
    rate: float = 1.0
    window_ms: tuple[float, float] | None = None


@dataclass(frozen=True, slots=True)
class Rule:
    index: int
    match: Match
    answer: Answer | None = None
    latency_ms: float | None = None
    latency: LatencyDist | None = None
    barrier: str | None = None
    stream: StreamSpec | None = None
    fault: Fault | None = None


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    seed: int
    mantle_auth: Literal["bearer_short_term", "sigv4"]
    default_latency: Literal["probe"] | float
    rules: tuple[Rule, ...]
    document: Mapping[str, Any]


@cache
def _validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def _pair(value: Sequence[float] | None) -> tuple[float, float] | None:
    return None if value is None else (float(value[0]), float(value[1]))


def _rule(index: int, raw: Mapping[str, Any]) -> Rule:
    m = raw["match"]
    answer = raw.get("answer")
    stream = raw.get("stream")
    fault = raw.get("fault")
    lat = raw.get("latency")
    return Rule(
        index=index,
        match=Match(m.get("model"), m.get("op"), m.get("prompt_contains"), m.get("nth"), m.get("tenant_akid")),
        answer=None
        if answer is None
        else Answer(
            dist=answer.get("dist"),
            raw_text=answer.get("raw_text"),
            top_k=answer.get("top_k"),
            split_tokens=answer.get("split_tokens", {}),
            whitespace_variants=answer.get("whitespace_variants", {}),
            eos_mass=float(answer.get("eos_mass", 0.0)),
            other_tokens=answer.get("other_tokens", {}),
            censor=tuple(answer.get("censor", ())),
            harmony_final=bool(answer.get("harmony_final", False)),
        ),
        latency_ms=raw.get("latency_ms"),
        latency=None
        if lat is None
        else LatencyDist(lat["p5"], lat["p95"], float(lat.get("tail_p", 0.0)), _pair(lat.get("tail_ms"))),
        barrier=raw.get("barrier"),
        stream=None if stream is None else _stream(stream),
        fault=None
        if fault is None
        else Fault(
            kind=fault["kind"],
            message=fault.get("message"),
            status=int(fault.get("status", 503)),
            n=int(fault.get("n", 0)),
            type=str(fault.get("type", "modelStreamErrorException")),
            after=int(fault.get("after", 0)),
            variant=fault.get("variant"),
            retry_after_s=fault.get("retry_after_s"),
            rate=float(fault.get("rate", 1.0)),
            window_ms=_pair(fault.get("window_ms")),
        ),
    )


def _stream(s: Mapping[str, Any]) -> StreamSpec:
    reasoning = s.get("reasoning", {})
    chunks = s.get("chunks")
    return StreamSpec(
        first_event_ms=s.get("first_event_ms"),
        first_text_ms=s.get("first_text_ms"),
        tokens_per_s=float(s.get("tokens_per_s", 60.0)),
        chunk_tokens=int(s.get("chunk_tokens", 4)),
        reasoning_deltas=reasoning.get("deltas"),
        reasoning_every_ms=float(reasoning.get("every_ms", 20.0)),
        hidden_reasoning_tokens=int(reasoning.get("hidden_tokens", 0)),
        text=s.get("text"),
        tokens=s.get("tokens"),
        chunks=None if chunks is None else tuple(chunks),
        stop=s.get("stop", "end_turn"),
        usage=s.get("usage", "auto"),
        pad=int(s.get("pad", 0)),
        stamp=bool(s.get("stamp", False)),
    )


def _semantic_errors(doc: Mapping[str, Any]) -> list[str]:
    errors = []
    for i, r in enumerate(doc.get("rules", [])):
        s = r.get("stream", {})
        if sum(k in s for k in ("text", "tokens", "chunks")) > 1:
            errors.append(f"rules[{i}].stream: give one of text, tokens, chunks")
        f = r.get("fault", {})
        if f.get("window_ms") and f["window_ms"][0] > f["window_ms"][1]:
            errors.append(f"rules[{i}].fault.window_ms: start > end")
        lat = r.get("latency")
        if lat and lat["p5"] > lat["p95"]:
            errors.append(f"rules[{i}].latency: p5 > p95")
    return errors


def load_scenario(source: str | bytes | Path | Mapping[str, Any]) -> Scenario:
    """Parse (YAML/JSON text, a path or a decoded mapping) and validate one scenario document."""
    if isinstance(source, Path):
        source = source.read_text()
    doc: Any = yaml.safe_load(source) if isinstance(source, str | bytes) else source
    if not isinstance(doc, Mapping):
        raise ScenarioError(["a scenario is a mapping"])
    errors = [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in _validator().iter_errors(doc)]
    if errors:
        raise ScenarioError(errors)
    if errors := _semantic_errors(doc):
        raise ScenarioError(errors)
    latency = doc.get("defaults", {}).get("latency", "probe")
    return Scenario(
        name=doc["scenario"],
        seed=int(doc.get("seed", 0)),
        mantle_auth=doc.get("mantle_auth", "bearer_short_term"),
        default_latency="probe" if latency == "probe" else float(latency["fixed_ms"]),
        rules=tuple(_rule(i, r) for i, r in enumerate(doc["rules"])),
        document=cast("Mapping[str, Any]", json.loads(json.dumps(doc))),
    )


# --- matching --------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RequestInfo:
    """What a rule can match on. `model` is the Bedrock id; `prompt` the concatenated system + message text;
    `body` the canonical request body (its sha256 keys the seeded draw); `effort` picks the probe latency row;
    `max_tokens` (None = unknown) bounds the planned output, reasoning included, as Bedrock does."""

    model: str
    op: Op
    prompt: str
    body: str
    tenant_akid: str | None = None
    effort: str | None = None
    max_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class Resolution:
    """The rule answering one request. `fault` is the rule's fault iff it fires for this request (rate and window
    applied); `latency_ms` the sampled answer latency (non-streaming) or the default first-text time (streams);
    `draw` the request's seeded `random.Random` (spec-10 §2.2 determinism rule)."""

    rule: Rule
    fault: Fault | None
    latency_ms: float
    barrier: str | None
    draw: random.Random
    k: int


def _matches(m: Match, req: RequestInfo) -> bool:
    return (
        (m.model is None or m.model == req.model)
        and (m.op is None or m.op == req.op)
        and (m.prompt_contains is None or m.prompt_contains in req.prompt)
        and (m.tenant_akid is None or m.tenant_akid == req.tenant_akid)
    )


# Probe latency defaults (spec-10 §2.2): (model id fragment, op kind) -> [p5, p95] ms, optional tail. Wave E2: the
# call rows and the low-effort stream rows follow the live smoke of 2026-10-11 (tests/cassettes/live-*: call e2e,
# stream first text; n per row), which was faster than the 2026-10-09 probes; the other rows are the probes (P11).
_PROBES: Final[tuple[tuple[str, str, tuple[float, float], float, tuple[float, float] | None], ...]] = (
    ("deepseek", "call", (300.0, 560.0), 0.02, (4000.0, 12000.0)),  # live 299-553 ms (n 8); tail: P11's 11.7 s
    ("gpt-6-luna", "call", (350.0, 610.0), 0.0, None),  # live 353-601 ms (n 13)
    ("claude-haiku", "call", (640.0, 950.0), 0.0, None),  # live 647-942 ms (n 6 decision members)
    ("gpt-6.1-sol", "stream:low", (630.0, 1250.0), 0.0, None),  # live 632-1,463 ms (n 10)
    ("gpt-6.1-sol", "stream", (2000.0, 8000.0), 0.0, None),  # live high 5,511 ms (n 1), hidden reasoning first
    ("claude-opus", "stream:low", (1000.0, 2200.0), 0.0, None),  # live 1,078 ms (n 1), P11 2,039 ms
    ("claude-opus", "stream", (2000.0, 11600.0), 0.0, None),
    ("glm", "stream:none", (600.0, 1000.0), 0.0, None),
    ("glm", "stream:low", (350.0, 1000.0), 0.0, None),  # live 358 ms (n 1)
    ("glm", "stream", (2000.0, 4400.0), 0.0, None),
)
_PROBE_DEFAULT: Final = (500.0, 1000.0)


def _lognormal(draw: random.Random, lo: float, hi: float) -> float:
    if hi <= lo or lo <= 0:
        return lo
    mu = (math.log(lo) + math.log(hi)) / 2
    sigma = (math.log(hi) - math.log(lo)) / 3.29
    return math.exp(draw.gauss(mu, sigma))


def probe_latency(req: RequestInfo, draw: random.Random) -> float:
    """A latency draw from the probe table (first text for streams, the whole call otherwise)."""
    kind = "stream" if req.op.endswith("-stream") else "call"
    keys = (f"{kind}:{req.effort or 'none'}", kind) if kind == "stream" else (kind,)
    for key in keys:
        for frag, op_kind, (lo, hi), tail_p, tail in _PROBES:
            if frag in req.model and op_kind == key:
                if tail is not None and draw.random() < tail_p:
                    return draw.uniform(*tail)
                return _lognormal(draw, lo, hi)
    return _lognormal(draw, *_PROBE_DEFAULT)


class ScenarioRuntime:
    """The mutable state of one scenario run: `nth` counters, per-body draw counters, the load time for
    `window_ms`, released barriers and the request log (`requests`: (request, answering rule index or None))."""

    def __init__(self, scenario: Scenario, *, now_ms: Any = None) -> None:
        self.scenario = scenario
        self._now_ms = now_ms if now_ms is not None else (lambda: 0.0)
        self._t0: float = float(self._now_ms())
        self._match_counts: dict[int, int] = {}
        self._body_counts: dict[tuple[str, str, str], int] = {}
        self._released: set[str] = set()
        self.requests: list[tuple[RequestInfo, int | None]] = []

    def release(self, barrier: str) -> None:
        """Release a named barrier (every held and future request on it proceeds)."""
        self._released.add(barrier)

    def is_released(self, barrier: str) -> bool:
        return barrier in self._released

    def resolve(self, req: RequestInfo) -> Resolution:
        """The first applicable rule for `req` (NoRuleMatched if none)."""
        digest = hashlib.sha256(req.body.encode()).hexdigest()
        body_key = (req.model, req.op, digest)
        k = self._body_counts.get(body_key, 0)
        self._body_counts[body_key] = k + 1
        draw = random.Random(f"{self.scenario.seed}|{req.model}|{req.op}|{digest}|{k}")
        for rule in self.scenario.rules:
            if not _matches(rule.match, req):
                continue
            count = self._match_counts.get(rule.index, 0) + 1
            self._match_counts[rule.index] = count
            if rule.match.nth is not None and count != rule.match.nth:
                continue
            fault = self._fires(rule.fault, draw)
            if rule.fault is not None and fault is None and rule.answer is None and rule.stream is None:
                continue  # a scoped fault that does not fire falls through to the next rule
            self.requests.append((req, rule.index))
            return Resolution(rule, fault, self._latency(rule, req, draw), rule.barrier, draw, k)
        self.requests.append((req, None))
        raise NoRuleMatched(f"no scenario rule matches {req.model} {req.op}")

    def _fires(self, fault: Fault | None, draw: random.Random) -> Fault | None:
        if fault is None:
            return None
        if fault.window_ms is not None:
            elapsed = float(self._now_ms()) - self._t0
            if not fault.window_ms[0] <= elapsed < fault.window_ms[1]:
                return None
        if fault.rate < 1.0 and draw.random() >= fault.rate:
            return None
        return fault

    def _latency(self, rule: Rule, req: RequestInfo, draw: random.Random) -> float:
        if rule.latency_ms is not None:
            return float(rule.latency_ms)
        if rule.latency is not None:
            lat = rule.latency
            if lat.tail_ms is not None and lat.tail_p > 0 and draw.random() < lat.tail_p:
                return draw.uniform(*lat.tail_ms)
            return _lognormal(draw, lat.p5, lat.p95)
        if self.scenario.default_latency == "probe":
            return probe_latency(req, draw)
        return float(self.scenario.default_latency)


# --- answer rendering (spec-10 §2.2 template-aware answers) ----------------------------------------------------------
_AQ_OPEN: Final = "<app_question>"
_AQ_CLOSE: Final = "</app_question>"
HARMONY_FINAL: Final = "<|channel|>final<|message|>"
EOS_TOKEN: Final = "<|end|>"


def option_letters(prompt: str) -> dict[str, str]:
    """option value -> letter, read from the `<app_question>{...}` JSON block of a decision prompt (spec-06 §5.2);
    empty if the prompt has none. The block always opens with the JSON object, so a mention of the tag in the
    system text (every Phase 1 template has one, IMPL-053) is skipped."""
    start = prompt.find(_AQ_OPEN + "{")
    end = prompt.find(_AQ_CLOSE, start + 1)
    if start < 0 or end < 0:
        return {}
    try:
        obj = json.loads(prompt[start + len(_AQ_OPEN) : end])
    except ValueError:
        return {}
    return {str(o["value"]): str(o["letter"]) for o in obj.get("options", [])}


def letter_dist(answer: Answer, letters: Mapping[str, str]) -> dict[str, float]:
    """The answer's distribution by letter (values absent from `dist` get 0)."""
    dist = answer.dist or {}
    return {letter: float(dist.get(value, 0.0)) for value, letter in letters.items()}


def verbalized_text(answer: Answer, letters: Mapping[str, str], *, variant: str | None = None) -> str:
    """The verbalized-dist JSON (`{"A": p, ...}`, letters sorted), or `raw_text`; `variant` renders an
    invalid_verbalized fault (missing_letter | sum_out_of_range | not_json)."""
    if answer.raw_text is not None:
        return answer.raw_text
    by_letter = dict(sorted(letter_dist(answer, letters).items()))
    if variant == "not_json":
        return "I think the answer is " + (max(by_letter, key=lambda k: by_letter[k]) if by_letter else "A") + "."
    if variant == "missing_letter" and by_letter:
        by_letter.pop(sorted(by_letter)[-1])
    if variant == "sum_out_of_range":
        by_letter = {k: v * 0.2 for k, v in by_letter.items()}
    text = json.dumps({k: round(v, 6) for k, v in by_letter.items()})
    return f"{HARMONY_FINAL}{text}<|return|>" if answer.harmony_final else text


def logprob_entries(answer: Answer, letters: Mapping[str, str]) -> list[tuple[str, float]]:
    """The top-logprob list (token, probability), most probable first (ties by token), for logprob-codes@2: the
    letters of uncensored options, whitespace variants, split fragments, EOS and other tokens; `top_k` caps it."""
    measured = {v: lt for v, lt in letters.items() if v not in answer.censor}
    entries: dict[str, float] = {letter: p for letter, p in letter_dist(answer, measured).items() if p > 0}
    for value, p in answer.whitespace_variants.items():
        if value in letters and value not in answer.censor and p > 0:
            entries[" " + letters[value]] = entries.get(" " + letters[value], 0.0) + p
    for value, p in answer.split_tokens.items():
        if value in letters and p > 0:
            entries[letters[value].lower()] = entries.get(letters[value].lower(), 0.0) + p
    if answer.eos_mass > 0:
        entries[EOS_TOKEN] = answer.eos_mass
    for token, p in answer.other_tokens.items():
        entries[token] = entries.get(token, 0.0) + p
    ordered = sorted(entries.items(), key=lambda kv: (-kv[1], kv[0]))
    return ordered[: answer.top_k] if answer.top_k is not None else ordered


def _tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


@dataclass(frozen=True, slots=True)
class Completion:
    """A non-streaming answer. `error` is set for a fault that fails after the response started (a malformed or
    truncated body, an exception); `hang` means no response before the caller's deadline; `hidden_reasoning` the
    planned reasoning tokens Bedrock hides (`stream.reasoning.hidden_tokens`, within the request's max_tokens)."""

    text: str
    logprobs: tuple[tuple[str, float], ...] | None
    stop: WireStop
    refused: bool
    usage: Mapping[str, int] | None
    latency_ms: float
    error: str | None = None
    hang: bool = False
    hidden_reasoning: int = 0


def completion(res: Resolution, req: RequestInfo, *, logprobs: bool) -> Completion:
    """Render the non-streaming answer of a resolved request (pre-byte faults are the caller's to raise)."""
    fault = res.fault
    kind = fault.kind if fault is not None else None
    answer = res.rule.answer or Answer(raw_text="")
    letters = option_letters(req.prompt)
    entries: tuple[tuple[str, float], ...] | None = None
    if kind in ("hang_before_first_byte", "stall_after"):
        return Completion("", None, "end_turn", False, None, res.latency_ms, hang=True)
    if kind in ("exception_event", "malformed", "truncated_json"):
        error = (
            fault.type if kind == "exception_event" and fault else f"malformed:{fault.variant or kind}" if fault else ""
        )
        return Completion("", None, "end_turn", False, None, res.latency_ms, error=error)
    stop: WireStop = "end_turn"
    if kind == "refusal":
        text, stop = "", "content_filtered"
    elif kind == "reasoning_only":
        text, stop = "", "max_tokens"
    elif kind == "empty_text":
        text = ""
    elif logprobs and answer.raw_text is None:
        entries = tuple(logprob_entries(answer, letters))
        text = entries[0][0] if entries else ""
    else:
        text = verbalized_text(
            answer, letters, variant=fault.variant if kind == "invalid_verbalized" and fault else None
        )
    stream = res.rule.stream
    hidden = stream.hidden_reasoning_tokens if stream is not None and kind in (None, "invalid_verbalized") else 0
    if hidden and req.max_tokens is not None:  # reasoning counts toward maxTokens (P6b)
        if hidden >= req.max_tokens:  # the whole budget went to reasoning: no text (live Haiku, verify-dist@1)
            hidden, text, entries, stop = req.max_tokens, "", None, "max_tokens"
        elif hidden + _tokens(text) > req.max_tokens:
            text, stop = text[: (req.max_tokens - hidden) * CHARS_PER_TOKEN], "max_tokens"
    usage: Mapping[str, int] | None
    if stream is not None and stream.usage == "none":
        usage = None
    elif stream is not None and isinstance(stream.usage, Mapping):
        usage = dict(stream.usage)
    else:
        auto = {"input_tokens": _tokens(req.prompt), "output_tokens": _tokens(text) + hidden}
        usage = auto | {"reasoning_tokens": hidden} if hidden else auto
    return Completion(text, entries, stop, kind == "refusal", usage, res.latency_ms, hidden_reasoning=hidden)


# --- stream plans ----------------------------------------------------------------------------------------------------
EventKind = Literal["start", "reasoning", "text", "stop", "usage", "error"]
REASONING_CHARS: Final = 16  # characters per planned reasoning delta (4 tokens)
_FILLER: Final = "lorem ipsum dolor sit amet "


@dataclass(frozen=True, slots=True)
class PlannedEvent:
    """One provider event at `at_ms` (from the call). `error` is the exception type (exception_event) or
    "malformed:<variant>"."""

    at_ms: float
    kind: EventKind
    text: str = ""
    chars: int = 0
    stop: WireStop | None = None
    usage: Mapping[str, int] | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class StreamPlan:
    """The timed provider events of one stream. `hang`: nothing follows the last event (the consumer's
    first_event/idle timeouts fire). `text` is the full answer text the plan carries."""

    events: tuple[PlannedEvent, ...]
    hang: bool
    text: str


def _chunks(spec: StreamSpec, fallback_text: str) -> list[str]:
    if spec.chunks is not None:
        return list(spec.chunks)
    if spec.tokens is not None:
        text = (_FILLER * (spec.tokens * CHARS_PER_TOKEN // len(_FILLER) + 1))[: spec.tokens * CHARS_PER_TOKEN]
    else:
        text = spec.text if spec.text is not None else fallback_text
    size = spec.chunk_tokens * CHARS_PER_TOKEN
    return [text[i : i + size] for i in range(0, len(text), size)]


# Models that stream reasoning before the text under probe defaults, at any effort but these (live 2026-10-11: Opus,
# GLM and Sol at `low` stream none; Sol at `high` sends one hidden `redactedContent` delta; P11: GLM none at `none`).
_REASONING_STREAMS: Final = ("claude-opus", "glm", "gpt-6.1-sol")
_NO_REASONING_EFFORTS: Final = ("none", "low")


def _default_reasoning(req: RequestInfo, scenario_probe: bool) -> bool:
    return (
        scenario_probe and any(m in req.model for m in _REASONING_STREAMS) and req.effort not in _NO_REASONING_EFFORTS
    )


def stream_plan(res: Resolution, req: RequestInfo, *, probe_defaults: bool = True) -> StreamPlan:
    """The provider events of a streamed resolution (pre-byte faults are the caller's to raise). Timing: start at
    `first_event_ms` (default min(200, first text)), reasoning every `every_ms` after it, text chunks of
    `chunk_tokens` tokens from `first_text_ms` (default the resolution latency) at `tokens_per_s`, then the stop
    and the usage at the last chunk's time."""
    spec = res.rule.stream or StreamSpec()
    fault = res.fault
    kind = fault.kind if fault is not None else None
    if kind == "hang_before_first_byte":
        return StreamPlan((), True, "")
    first_text = spec.first_text_ms if spec.first_text_ms is not None else res.latency_ms
    first_event = spec.first_event_ms if spec.first_event_ms is not None else min(200.0, first_text)
    n_reasoning = spec.reasoning_deltas
    if n_reasoning is None:
        gap = max(0.0, first_text - first_event)
        n_reasoning = int(gap // spec.reasoning_every_ms) if _default_reasoning(req, probe_defaults) else 0
    if kind == "reasoning_only":
        n_reasoning = max(1, n_reasoning)
    events = [PlannedEvent(first_event, "start")]
    t = first_event
    for i in range(n_reasoning):
        t = first_event + (i + 1) * spec.reasoning_every_ms
        events.append(PlannedEvent(t, "reasoning", chars=REASONING_CHARS))
    first_text = max(first_text, t)
    answer_text = ""
    if res.rule.answer is not None:
        answer_text = verbalized_text(res.rule.answer, option_letters(req.prompt))
    chunks = [] if kind in ("reasoning_only", "empty_text", "refusal") else _chunks(spec, answer_text)
    step = spec.chunk_tokens / spec.tokens_per_s * 1000.0
    keep = len(chunks)
    if kind == "stall_after" and fault is not None:
        keep = min(keep, fault.n)
    elif kind in ("exception_event",) and fault is not None:
        keep = min(keep, fault.after)
    elif kind in ("malformed", "truncated_json"):
        keep = min(keep, 1)
    last = first_text if kind in ("reasoning_only", "empty_text", "refusal") else t
    for i, chunk in enumerate(chunks[:keep]):
        last = first_text + i * step
        events.append(PlannedEvent(last, "text", text=chunk))
    text = "".join(chunks[:keep])
    if kind == "stall_after":
        return StreamPlan(tuple(events), True, text)
    if kind in ("exception_event", "malformed", "truncated_json") and fault is not None:
        error = fault.type if kind == "exception_event" else f"malformed:{fault.variant or kind}"
        events.append(PlannedEvent(last if keep else first_text, "error", error=error))
        return StreamPlan(tuple(events), False, text)
    stop: WireStop = spec.stop
    if kind == "reasoning_only":
        stop = "max_tokens"
    elif kind == "refusal":
        stop = "content_filtered"
    elif kind == "empty_text":
        stop = "end_turn"
    events.append(PlannedEvent(last, "stop", stop=stop))
    reasoning_tokens = n_reasoning * REASONING_CHARS // CHARS_PER_TOKEN
    if req.max_tokens is not None:  # the planned cadence never bills more than maxTokens (reasoning included)
        reasoning_tokens = min(reasoning_tokens, max(0, req.max_tokens - _tokens(text)))
    if spec.usage == "auto":
        out = _tokens(text) + reasoning_tokens
        usage: Mapping[str, int] = {
            "input_tokens": _tokens(req.prompt),
            "output_tokens": out,
            "reasoning_tokens": reasoning_tokens,
        }
        events.append(PlannedEvent(last, "usage", usage=usage))
    elif isinstance(spec.usage, Mapping):
        events.append(PlannedEvent(last, "usage", usage=dict(spec.usage)))
    return StreamPlan(tuple(events), False, text)
