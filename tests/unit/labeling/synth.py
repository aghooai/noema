"""Synthetic labeled data with known calibration (ADR-003 §6: the machinery is tested on synthetic labels).

`labeled_set(...)` draws true logits, samples the consensus label from the true distribution, and lets every member
report `softmax(logits / temp + noise)`: `temp = 1` is calibrated, `temp < 1` over-confident and `temp > 1`
under-confident (temperature scaling can repair both). `inverted=True` makes confidence anti-correlated with
correctness (a 0.95 answer is right 55% of the time, a 0.55 answer 95%), which no monotone calibrator repairs.
`sampled_items` builds the matching consensus view (both labelers agreeing with the label, kept, uniform).
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

from noema.labeling.consensus import LabelerVote, SampledItem
from noema.labeling.dataset import Example, LabeledSet, MemberObs, QuestionKind

T0 = datetime(2026, 9, 1, tzinfo=UTC)
LABELERS = ("us.openai.gpt-6.1-sol", "us.anthropic.claude-sonnet-5-5")
VIEW = "sha256:" + "7" * 64


def softmax(z: Sequence[float]) -> list[float]:
    top = max(z)
    w = [math.exp(v - top) for v in z]
    total = sum(w)
    return [x / total for x in w]


def options_for(kind: QuestionKind, k: int = 4) -> tuple[str, ...]:
    if kind == "predicate":
        return ("true", "false")
    prefix = "lvl" if kind == "score" else "opt"
    return tuple(f"{prefix}{i}" for i in range(k))


def labeled_set(
    n: int,
    *,
    kind: QuestionKind = "choice",
    temp: float = 1.0,
    seed: int = 1,
    members: Sequence[str] = ("m1", "m2"),
    k: int = 4,
    inverted: bool = False,
    spread: float = 2.0,
    options: Sequence[str] | None = None,
) -> LabeledSet:
    rng = random.Random(seed)
    opts = tuple(options) if options is not None else options_for(kind, k)
    out: list[Example] = []
    for i in range(n):
        if inverted:
            top = rng.randrange(len(opts))
            conf = rng.choice((0.55, 0.95))
            right = rng.random() < (0.55 if conf == 0.95 else 0.95)
            label = opts[top] if right else opts[(top + 1 + rng.randrange(len(opts) - 1)) % len(opts)]
            dist = {o: (conf if j == top else (1 - conf) / (len(opts) - 1)) for j, o in enumerate(opts)}
            obs = {m: MemberObs(dict(dist)) for m in members}
        else:
            logits = [rng.gauss(0.0, spread) for _ in opts]
            truth = softmax(logits)
            r, acc, label = rng.random(), 0.0, opts[-1]
            for o, p in zip(opts, truth, strict=True):
                acc += p
                if r < acc:
                    label = o
                    break
            obs = {
                m: MemberObs(dict(zip(opts, softmax([v / temp + rng.gauss(0.0, 0.05) for v in logits]), strict=True)))
                for m in members
            }
        out.append(Example(obs, dict.fromkeys(members, 1.0), label, T0 + timedelta(minutes=i)))
    return LabeledSet(kind, opts, tuple(sorted(members)), tuple(out))


def _argmax(options: Sequence[str], dist: Mapping[str, float]) -> str:
    best = max(dist.get(o, -1.0) for o in options)
    return next(o for o in options if dist.get(o, -1.0) == best)


def sampled_items(ls: LabeledSet) -> list[SampledItem]:
    items: list[SampledItem] = []
    for ex in ls.examples:
        top = {m: _argmax(ls.options, obs.dist) for m, obs in ex.members.items()}
        votes = tuple(LabelerVote(model, "ok", ex.label) for model in LABELERS)
        items.append(SampledItem(True, ex.label, "uniform", votes, top))
    return items
