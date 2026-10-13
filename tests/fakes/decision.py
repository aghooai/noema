"""`FakeQuestionCompiler` and `FakeDecisionCache` (spec-02 §5, spec-06 §3, §14). The engine fake is in
`decision_engine.py`.

The compiler follows spec-06 §3: platform questions (turns and verification only; `/v1/decisions` and the facade
ignore a profile's built-in questions) in config order, then the app questions in request order; the §3.1 checks
(unique names, unique values and labels, reserved names in turns, `blocking` must name app questions, K > L ->
422 for jev/openai, else the hierarchical method); signatures exactly as §3.3 (sha256 of the canonical JSON of
the question as validated; the platform question dict is rebuilt from its config fields).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal

import anyio.lowlevel

from noema.config.models import ConfigQuestionCfg, DecisionProfileCfg
from noema.core.clock import Clock
from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.config import ConfigSnapshot
from noema.core.protocols.decision import (
    CompiledQuestion,
    CompiledQuestionSet,
    GroupSpec,
    OptionSpec,
    Origin,
    OutputFormat,
)
from noema.core.redis_keys import RedisKeys
from noema.core.values import JsonValue

_TYPES: Final = frozenset({"predicate", "noul", "choice", "score"})


def canon(obj: object) -> bytes:
    """spec-06 §3.3 canonical JSON (sorted keys, compact, ensure_ascii)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def question_signature(wire: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canon(wire)).hexdigest()


def question_set_signature(wires: Sequence[Mapping[str, Any]]) -> str:
    return "sha256:" + hashlib.sha256(canon(list(wires))).hexdigest()


def platform_wire(q: ConfigQuestionCfg) -> dict[str, Any]:
    """The config dict of a platform question as loaded from YAML (absent fields omitted)."""
    wire: dict[str, Any] = {"type": q.type, "name": q.name, "instructions": q.instructions}
    if q.criteria is not None:
        wire["criteria"] = {k: v for k, v in (("true", q.criteria.true), ("false", q.criteria.false)) if v is not None}
    if q.choices is not None:
        wire["choices"] = [
            {"value": c.value, **({"description": c.description} if c.description else {})} for c in q.choices
        ]
    if q.levels is not None:
        wire["levels"] = [
            {"label": lv.label, **({"description": lv.description} if lv.description else {})} for lv in q.levels
        ]
    if q.dynamic_choices is not None:
        wire["dynamic_choices"] = q.dynamic_choices
    return wire


def _options(wire: Mapping[str, Any]) -> tuple[OptionSpec, ...]:
    kind = wire["type"]
    if kind in ("predicate", "noul"):
        crit = wire.get("criteria") or {}
        return (OptionSpec("true", True, crit.get("true"), 0), OptionSpec("false", False, crit.get("false"), 1))
    if kind == "choice":
        return tuple(
            OptionSpec(str(c["value"]), str(c["value"]), c.get("description"), i)
            for i, c in enumerate(wire.get("choices", []))
        )
    return tuple(
        OptionSpec(str(lv["label"]), str(lv["label"]), lv.get("description"), i)
        for i, lv in enumerate(wire.get("levels", []))
    )


def _groups(wire: Mapping[str, Any], options: tuple[OptionSpec, ...], limit: int) -> tuple[GroupSpec, ...]:
    labels = [c.get("group") for c in wire.get("choices", [])]
    chunks: list[tuple[str, tuple[OptionSpec, ...]]] = []
    if labels and all(labels):
        order: list[str] = []
        for label in labels:
            if label not in order:
                order.append(str(label))
        chunks = [(g, tuple(o for o, lab in zip(options, labels, strict=True) if lab == g)) for g in order]
    else:
        n_groups = math.ceil(len(options) / limit)
        size = math.ceil(len(options) / n_groups)
        for start in range(0, len(options), size):
            part = options[start : start + size]
            chunks.append((f"options {start + 1}-{start + len(part)}", part))
    return tuple(
        GroupSpec(f"g{i + 1}", label, opts, ", ".join(o.key for o in opts)) for i, (label, opts) in enumerate(chunks)
    )


def _bad(param: str, message: str) -> NoemaError:
    return NoemaError(ErrorCode.SCHEMA_VIOLATION, message, param=param)


class FakeQuestionCompiler:
    """Implements `QuestionCompiler` (see the module docstring); `config` gives L(profile) (template limits)."""

    def __init__(self, config: Any) -> None:
        self._config = config

    def _limit(self, snapshot: ConfigSnapshot, profile: DecisionProfileCfg) -> int:
        return min(snapshot.template(m.template).max_choices for m in profile.members)

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
        snapshot: ConfigSnapshot = self._config.current()
        limit = self._limit(snapshot, profile)
        prefix = "decisions.questions" if origin == "turn" else "questions"
        source_profile = platform_from or profile
        platform: list[ConfigQuestionCfg] = []
        if origin in ("turn", "verification"):
            platform = [q for q in source_profile.questions if q.dynamic_choices is None]
            if select is not None:
                platform = [q for q in platform if q.name in select]
        platform_names = {q.name for q in platform}
        compiled: list[CompiledQuestion] = []
        critical = set(source_profile.commit_critical) if origin == "turn" else set()
        for q in platform:
            wire = platform_wire(q)
            compiled.append(self._one(wire, "platform", limit, output_format, q.name in critical, f"{prefix}[?]"))
        app_names: list[str] = []
        for i, raw in enumerate(app_questions):
            wire = dict(raw)
            param = f"{prefix}[{i}]"
            name = wire.get("name")
            if (
                wire.get("type") not in _TYPES
                or not isinstance(name, str)
                or not isinstance(wire.get("instructions"), str)
            ):
                raise _bad(param, "Invalid question.")
            if origin == "turn" and name in platform_names:
                raise NoemaError(
                    ErrorCode.RESERVED_QUESTION_NAME, "The question name is reserved.", param=f"{param}.name"
                )
            app_names.append(name)
            compiled.append(self._one(wire, "request", limit, output_format, name in blocking, param))
        names = [c.name for c in compiled]
        for i, name in enumerate(names):
            if names.index(name) != i:
                raise _bad(f"{prefix}[{i - len(platform)}].name", "Question names must be unique.")
        if unknown := [b for b in blocking if b not in app_names]:
            raise _bad("decisions.blocking", f"{len(unknown)} blocking name(s) are not app questions.")
        wires = [dict(c.wire) for c in compiled]
        return CompiledQuestionSet(
            questions=tuple(compiled),
            signature=question_set_signature(wires),
            commit_critical=frozenset(c.name for c in compiled if c.commit_critical),
            flat_limit=limit,
        )

    def _one(
        self, wire: Mapping[str, Any], source: str, limit: int, output_format: OutputFormat, critical: bool, param: str
    ) -> CompiledQuestion:
        options = _options(wire)
        keys = [o.key for o in options]
        field = "levels" if wire["type"] == "score" else "choices"
        if wire["type"] in ("choice", "score") and len(set(keys)) != len(keys):
            raise _bad(f"{param}.{field}", "Choice values and level labels must be unique.")
        if wire["type"] in ("choice", "score") and len(keys) < 2:
            raise _bad(f"{param}.{field}", "A question needs at least two options.")
        method: Literal["flat", "hierarchical"] = "flat"
        groups: tuple[GroupSpec, ...] = ()
        if wire["type"] == "choice" and len(keys) > limit:
            if source == "platform":  # platform questions are always flat (spec-06 §2.4, IMPL-069)
                raise NoemaError(
                    ErrorCode.UNSUPPORTED_PARAMETER,
                    "The profile cannot carry this question.",
                    param="decisions.profile",
                    details={"max_choices": limit, "question": str(wire["name"])},
                )
            if output_format in ("jev", "openai"):
                raise NoemaError(
                    ErrorCode.UNSUPPORTED_PARAMETER,
                    "Too many choices for this output format.",
                    param=f"{param}.choices",
                    details={"max_choices": limit, "method": "hierarchical"},
                )
            method, groups = "hierarchical", _groups(wire, options, limit)
        crit = wire.get("criteria")
        criteria = (crit.get("true"), crit.get("false")) if wire["type"] in ("predicate", "noul") and crit else None
        return CompiledQuestion(
            name=str(wire["name"]),
            type=wire["type"],
            instructions=str(wire["instructions"]),
            options=options,
            criteria=criteria,
            wire=dict(wire),
            signature=question_signature(wire),
            commit_critical=critical,
            source="platform" if source == "platform" else "request",
            method=method,
            groups=groups,
        )


class FakeDecisionCache:
    """Implements `DecisionCache` in memory with TTLs on the clock. Fail-open: `down = True` (or a `delay_s`
    above the timeout) makes `get` return None and `put` a no-op; `errors` counts swallowed failures."""

    def __init__(self, clock: Clock, keys: RedisKeys | None = None) -> None:
        self._clock = clock
        self._keys = keys or RedisKeys("ns:fake:")
        self._data: dict[str, tuple[bytes, float]] = {}
        self.down = False
        self.delay_s = 0.0
        self.errors = 0

    async def _latency(self, timeout_s: float) -> bool:
        if self.down:
            self.errors += 1
            await anyio.lowlevel.checkpoint()
            return False
        if self.delay_s > timeout_s:
            await self._clock.sleep(timeout_s)
            self.errors += 1
            return False
        if self.delay_s:
            await self._clock.sleep(self.delay_s)
        else:
            await anyio.lowlevel.checkpoint()
        return True

    async def get(self, key: str, *, timeout_s: float = 0.02) -> bytes | None:
        if not await self._latency(timeout_s):
            return None
        hit = self._data.get(key)
        if hit is None or hit[1] <= self._clock.monotonic():
            self._data.pop(key, None)
            return None
        return hit[0]

    async def put(self, key: str, artifact: bytes, ttl_s: int, *, timeout_s: float = 0.02) -> None:
        if await self._latency(timeout_s):
            self._data[key] = (artifact, self._clock.monotonic() + ttl_s)

    async def purge_tenant(self, tenant_id: str) -> int:
        await anyio.lowlevel.checkpoint()
        prefix = self._keys.l2(tenant_id, "")
        doomed = [k for k in self._data if k.startswith(prefix)]
        for k in doomed:
            del self._data[k]
        return len(doomed)
