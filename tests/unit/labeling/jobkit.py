"""Test kit for the labeling jobs: stored decision rows (spec-03 `decision` with a full native result), a scripted
labeler adapter (answers chosen per labeler and question, read back from the `<app_question>` letters), an in-memory
Redis for the daily-cap counter, and the `LabelingDeps` of a fake world."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from tests.fakes.world import FakeWorld

from noema.core.clock import Deadline
from noema.core.errors import ClassifiedError
from noema.core.principal import TenantRef
from noema.core.protocols.adapters import (
    Adapter,
    AdapterError,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    StreamEvent,
    StreamTimeouts,
)
from noema.core.values import TextBlock, UsageRecord
from noema.labeling.deps import LabelingDeps

SOL, SONNET = "us.openai.gpt-6.1-sol", "us.anthropic.claude-sonnet-5-5"
ALIASES = {"sol-6.1": SOL, "sonnet-5.5": SONNET}
VIEW = "sha256:" + "5" * 64
INTENT = {
    "type": "choice",
    "name": "intent",
    "instructions": "What is the user's primary intent?",
    "choices": [{"value": v} for v in ("chitchat", "coding", "billing", "other")],
}
NEEDS_INFO = {"type": "predicate", "name": "needs_fresh_info", "instructions": "Needs information newer than training."}


def member(mid: str, dist: Mapping[str, float], *, status: str = "ok", weight: float = 0.3333) -> dict[str, Any]:
    out: dict[str, Any] = {"member_id": mid, "status": status, "weight": weight if status == "ok" else 0.0}
    if status == "ok":
        out["distribution"] = dict(dist)
    return out


def answer(wire: Mapping[str, Any], members: Sequence[dict[str, Any]], *, band: str = "low") -> dict[str, Any]:
    keys = [str(c["value"]) for c in wire["choices"]] if wire["type"] == "choice" else ["true", "false"]
    ok = [m for m in members if m["status"] == "ok"]
    pooled = {k: sum(m["distribution"].get(k, 0.0) for m in ok) / max(len(ok), 1) for k in keys}
    order = sorted(keys, key=lambda k: -pooled[k])

    def value(k: str) -> str | bool:
        return k == "true" if wire["type"] == "predicate" else k

    a: dict[str, Any] = {
        "type": wire["type"],
        "name": wire["name"],
        "candidates": [
            {"value": value(k), "probability": round(pooled[k], 4), "raw_score": pooled[k], "votes": 0} for k in order
        ],
        "calibration": {"status": "uncalibrated"},
        "confidence": {"band": band},
        "members": list(members),
    }
    if wire["type"] == "choice":
        a["choice"] = order[0]
    else:
        a["probability"] = round(pooled["true"], 4)
    return a


def decision_row(
    decision_id: str,
    answers: Sequence[dict[str, Any]],
    questions: Sequence[Mapping[str, Any]] = (INTENT,),
    *,
    stored_input: Any = "My invoice is wrong",
    turn_id: str | None = None,
    stages: Sequence[dict[str, Any]] = (),
    profile: str = "router-default",
) -> dict[str, Any]:
    result = {
        "id": decision_id,
        "object": "decision",
        "status": "completed",
        "answers": list(answers),
        "provenance": {
            "profile_id": profile,
            "profile_version": "1",
            "input_view_signature": VIEW,
            "stages": list(stages),
        },
    }
    return {
        "tenant_id": "t_acme",
        "project_id": "p1",
        "id": decision_id,
        "principal_id": "prn_dev",
        "turn_id": turn_id,
        "purpose": "router" if turn_id else "decision",
        "questions": [dict(q) for q in questions],
        "input": stored_input,
        "result": result,
    }


Script = Callable[[str, Mapping[str, Any]], str | ClassifiedError | None]  # None = provider refusal


def letter_of(app_question: Mapping[str, Any], value: str) -> str:
    """The letter showing `value` in this labeler's permuted option list."""
    return next(str(o["letter"]) for o in app_question["options"] if o["value"] == value)


@dataclass
class ScriptedAdapter:
    """An `Adapter` whose answers come from `script(alias, app_question)`; every request is recorded."""

    script: Script
    requests: list[ModelRequest] = field(default_factory=list)

    def capabilities(self, alias: str) -> ModelCapabilities:
        raise AssertionError("not used by labeling")

    async def complete(self, req: ModelRequest, *, tenant: TenantRef, deadline: Deadline) -> ModelResponse:
        self.requests.append(req)
        first = req.messages[0].content[0]
        assert isinstance(first, TextBlock)
        aq = json.loads(first.text.removeprefix("<app_question>").removesuffix("</app_question>\n"))
        out = self.script(req.model_alias, aq)
        if isinstance(out, ClassifiedError):
            raise AdapterError(out)
        usage = UsageRecord(input_tokens=100, output_tokens=3)
        if out is None:
            return ModelResponse("", None, True, "content_filter", usage, 900, tenant.home_region)
        return ModelResponse(out, None, False, "end_turn", usage, 900, tenant.home_region)

    def stream(
        self, req: ModelRequest, *, tenant: TenantRef, timeouts: StreamTimeouts, deadline: Deadline
    ) -> AbstractAsyncContextManager[AsyncIterator[StreamEvent]]:
        raise AssertionError("labelers never stream")

    def classify_error(self, exc: BaseException) -> ClassifiedError:
        raise AssertionError("not used by labeling")


@dataclass
class ScriptedRegistry:
    adapter: ScriptedAdapter

    def for_model(self, alias: str) -> Adapter:
        return self.adapter


def agree_on(values: Mapping[str, str]) -> Script:
    """Both labelers answer `values[question name]`; matching is by the question's instructions."""
    by_instructions = {
        str(q["instructions"]): name for name, q in (("intent", INTENT), ("needs_fresh_info", NEEDS_INFO))
    }

    def script(alias: str, aq: Mapping[str, Any]) -> str:
        return letter_of(aq, values[by_instructions[str(aq["instructions"])]])

    return script


@dataclass
class CounterRedis:
    """The Redis calls of the `labeling.item` job (INCR + EXPIRE of `RedisKeys.labeling_daily`), in memory;
    `fail` raises a connection error on every call."""

    values: dict[str, int] = field(default_factory=dict)
    ttls: dict[str, int] = field(default_factory=dict)
    fail: bool = False

    async def incr(self, key: str) -> int:
        if self.fail:
            raise ConnectionError("redis down")
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    async def expire(self, key: str, seconds: int) -> bool:
        if self.fail:
            raise ConnectionError("redis down")
        self.ttls[key] = seconds
        return key in self.values


def deps(world: FakeWorld, adapters: Any, *, redis: Any = None) -> LabelingDeps:
    client: Any = redis if redis is not None else CounterRedis()
    return LabelingDeps(
        clock=world.clock,
        ids=world.ids,
        config=world.config,
        db=world.db,
        jobs=world.jobs,
        adapters=adapters,
        admission=world.admission,
        estimator=world.estimator,
        cost=world.cost,
        files=world.files,
        views=world.views,
        telemetry=world.telemetry,
        redis=client,
        keys=world.keys,
    )


SIG = "sha256:" + "4" * 64


def run_row(
    run_id: str, started: Any, *, status: str = "running", version: str = "1", question: str = "intent"
) -> dict[str, Any]:
    return {
        "tenant_id": "t_acme",
        "id": run_id,
        "profile_id": "router-default",
        "profile_version": version,
        "question_name": question,
        "question_signature": SIG,
        "status": status,
        "labelers": [SOL, SONNET],
        "rule": "unanimous",
        "counts": {},
        "consensus_rate": None,
        "consensus_rate_by_class": {},
        "n_by_class": {},
        "usage": {},
        "started_at": started,
        "completed_at": None,
    }


def item_rows(
    ls: Any, run_id: str, *, view: str = VIEW, reason: str = "uniform", tag: str = "a"
) -> list[dict[str, Any]]:
    """Kept label items of a synthetic `LabeledSet` (both labelers agreeing with the label)."""
    rows: list[dict[str, Any]] = []
    for i, ex in enumerate(ls.examples):
        value: Any = ex.label == "true" if ls.kind == "predicate" else ex.label
        answers = [
            {
                "model": m,
                "status": "ok",
                "value": value,
                "usage": {"input_tokens": 10, "output_tokens": 1},
                "cost_usd": 0.001,
                "cost_estimated": True,
            }
            for m in (SOL, SONNET)
        ]
        members = {m: {"dist": dict(o.dist), "unresolved": o.unresolved, "weight": 1.0} for m, o in ex.members.items()}
        rows.append(
            {
                "tenant_id": "t_acme",
                "id": f"lab_{tag}{run_id[-4:]}{i:08d}",
                "run_id": run_id,
                "decision_id": f"dec_{tag}{i}",
                "question_name": "intent",
                "sample_reason": reason,
                "labeler_answers": answers,
                "kept": True,
                "value": value,
                "drop_reason": None,
                "ensemble": {
                    "value": value,
                    "probability": 0.5,
                    "band": "low",
                    "calibration_status": "uncalibrated",
                    "kind": ls.kind,
                    "options": list(ls.options),
                    "members": members,
                },
                "content_excerpt": None,
                "redacted": False,
                "input_view_signature": view,
                "member_subset": list(ls.member_ids),
                "created_at": ex.at,
            }
        )
    return rows


def dropped_row(run_id: str, i: int, *, reason: str = "disagreed", view: str = VIEW) -> dict[str, Any]:
    return {
        "tenant_id": "t_acme",
        "id": f"ldrop_{run_id[-4:]}{i:08d}",
        "run_id": run_id,
        "decision_id": f"dec_drop{i}",
        "question_name": "intent",
        "sample_reason": "uncertainty",
        "labeler_answers": [
            {"model": SOL, "status": "ok", "value": "opt0"},
            {"model": SONNET, "status": "ok", "value": "opt1"},
        ],
        "kept": False,
        "value": None,
        "drop_reason": reason,
        "ensemble": {
            "value": "opt0",
            "probability": 0.4,
            "band": "low",
            "calibration_status": "uncalibrated",
            "kind": "choice",
            "options": ["opt0", "opt1", "opt2", "opt3"],
            "members": {"m1": {"dist": {"opt0": 0.4, "opt1": 0.6}, "unresolved": 0.0, "weight": 1.0}},
        },
        "content_excerpt": "text",
        "redacted": False,
        "input_view_signature": view,
        "member_subset": ["m1"],
        "created_at": datetime(2026, 9, 1, tzinfo=UTC),
    }
