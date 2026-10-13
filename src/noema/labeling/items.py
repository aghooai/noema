"""The `labeling.item` job: label one sampled decision with the D4 labelers (02 §5.5; spec-11 §1.2, §2, §4).

Per job (span `labeling.item`): count the offer in the tenant's exact daily cap (first attempt only:
`RedisKeys.labeling_daily`, INCR + EXPIRE 2 days; over `labeling.sample.max_per_day_per_tenant` the job ends; a failed
or slow Redis fails open); load the decision row and its content (IMPL-071) under `tenant_uow`; pick the labelable
answers; make sure each answer's labeling run is open (one running run per question definition, `runs_open.py`);
reserve the labeler calls with the tenant's system principal (`mdl:` buckets only); ask every labeler every question
(labelers concurrently), blind, permuted and spotlighted (`labelers.py`); record one `labeling` usage line per call,
priced with the job's snapshot and the tenant's home region; store one `label_item` per answer (kept iff unanimous,
`lab_` id; drops keep both answers, `ldrop_` id) and count it in `aghoo_labeling_items_total{question, outcome}`; and
schedule the run's `labeling.run` refresh. A retryable labeler failure (throttled, 5xx, timeout) retries the job until
its last attempt, which records `labeler_error`; the answers that did not fail transiently are stored before the retry.
A retried job never labels an answer twice: it skips the answers stored in any run of their definition (`retries.py`,
one `items_for_decision` read) and schedules the refresh of the runs holding them again, so a lost enqueue after an
earlier commit is repaired.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Final

import anyio

from noema.config.models import DecisionMemberCfg, ModelCfg
from noema.core.clock import Deadline, deadline_scope
from noema.core.errors import ErrorKind, NoemaError
from noema.core.principal import Principal, TenantRef, system_principal
from noema.core.protocols.adapters import AdapterError, ModelRequest
from noema.core.protocols.config import ConfigSnapshot
from noema.core.protocols.media import VIEW_BUDGET_V1, ViewPart
from noema.core.protocols.ops import Reservation, UsageLine, WorkGraph
from noema.core.protocols.storage import Job, PermanentJobError, Row
from noema.core.protocols.telemetry import Span
from noema.core.values import CallKind, ContentBlock, JsonValue, UsageRecord
from noema.labeling import content as content_mod
from noema.labeling.deps import (
    ADMISSION_QUEUE_TIMEOUT_S,
    DAILY_COUNT_TTL_S,
    ITEM_MAX_ATTEMPTS,
    JOB_PRIORITY,
    JOB_RUN,
    LABELER_TIMEOUT_S,
    REDIS_TIMEOUT_S,
    RUN_REFRESH_DELAY_S,
    LabelingDeps,
    usage_line_id,
)
from noema.labeling.labelers import TEMPLATE, build_request, letter_order, parse_answer
from noema.labeling.records import LabelerResult, decide, labeler_answers
from noema.labeling.retries import stored_runs
from noema.labeling.runs_open import ensure_run
from noema.labeling.targets import Target, targets

_RETRYABLE: Final = frozenset({ErrorKind.RETRYABLE, ErrorKind.THROTTLED})
_log: Final = logging.getLogger("noema.labeling.items")


class RetryLabeling(Exception):
    """A transient labeler failure: the job is retried with the queue's backoff."""


def _payload(job: Job) -> tuple[str, str | None, list[str], str]:
    p = job.payload
    decision_id, turn_id, names, reason = (
        p.get("decision_id"),
        p.get("turn_id"),
        p.get("question_names"),
        p.get("sample_reason"),
    )
    if (
        not isinstance(decision_id, str)
        or not (turn_id is None or isinstance(turn_id, str))
        or not isinstance(names, list)
        or not all(isinstance(n, str) for n in names)
        or reason not in ("uniform", "uncertainty")
    ):
        raise PermanentJobError("malformed labeling.item payload")
    return decision_id, turn_id, [str(n) for n in names], str(reason)


class ItemJob:
    """The `labeling.item` handler (module docstring)."""

    def __init__(self, deps: LabelingDeps) -> None:
        self._d = deps

    async def __call__(self, job: Job) -> None:
        decision_id, turn_id, names, reason = _payload(job)
        with self._d.telemetry.span("labeling.item", **{"aghoo.tenant": job.tenant_id}) as span:
            retry = await self._label(job, span, decision_id, turn_id, names, reason)
        if retry:
            raise RetryLabeling("a labeler failed transiently")

    async def _label(
        self, job: Job, span: Span, decision_id: str, turn_id: str | None, names: list[str], reason: str
    ) -> bool:
        """One attempt; True when transiently failed answers are left for a retry."""
        snapshot = self._d.config.current()
        try:
            tenant = snapshot.tenant_ref(job.tenant_id)
        except NoemaError as exc:
            raise PermanentJobError("the tenant is not in the active config") from exc
        cap = snapshot.labeling.sample.max_per_day_per_tenant
        if job.attempt == 1 and cap is not None and not await self._within_daily_cap(job.tenant_id, cap):
            _log.info("labeling item skipped: the tenant's daily cap is reached", extra={"decision_id": decision_id})
            return False
        principal = system_principal(job.tenant_id, purpose="labeling", request_id=job.id)
        async with self._d.db.tenant_uow(job.tenant_id) as uow:
            row = await uow.decisions.get(decision_id)
            content = None if row is None else await content_mod.resolve(uow, row, turn_id, self._d.files, principal)
        if row is None or content is None:
            _log.info("labeling item skipped: no decision row or no stored input", extra={"decision_id": decision_id})
            return False
        todo = targets(row, names)
        if todo:
            span.set("aghoo.profile", ",".join(sorted({t.profile_id for t in todo})))
            span.set("aghoo.member.question", ",".join(t.question.name for t in todo))
        stored: set[str] = set()  # runs holding the answers an earlier attempt stored
        if job.attempt > 1:
            todo, stored = await self._unlabeled(job.tenant_id, decision_id, todo)
        if not todo:
            await self._schedule_runs(job.tenant_id, stored)
            return False
        labelers = [(alias, snapshot.model(alias)) for alias in snapshot.labeling.labelers]
        model_ids = [m.id for _, m in labelers]
        runs = {t.question.name: await ensure_run(self._d, job.tenant_id, t, model_ids) for t in todo}
        views = {alias: await self._view(content, alias, model, principal) for alias, model in labelers}
        results = await self._label_all(job, snapshot, tenant, principal, decision_id, todo, labelers, views)
        self._record_usage(job, row, todo, results, runs)
        transient = {t.question.name for t in todo if any(r.retryable for r in results[t.question.name])}
        retry = bool(transient) and job.attempt < ITEM_MAX_ATTEMPTS
        final = [t for t in todo if not (retry and t.question.name in transient)]
        excerpt, redacted = self._excerpt(content.excerpt_text)
        items = [
            self._item_row(t, runs[t.question.name], decision_id, reason, results[t.question.name], excerpt, redacted)
            for t in final
        ]
        async with self._d.db.tenant_uow(job.tenant_id) as uow:
            for item in items:
                await uow.labels.insert_item(item)
        for item in items:
            outcome = str(item["drop_reason"] or "kept")
            self._d.telemetry.instruments.add(
                "aghoo_labeling_items_total", question=str(item["question_name"]), outcome=outcome
            )
        await self._schedule_runs(job.tenant_id, stored | {str(runs[t.question.name]["id"]) for t in final})
        return retry

    async def _within_daily_cap(self, tenant_id: str, cap: int) -> bool:
        """Count this offer in the tenant's UTC-day counter (`RedisKeys.labeling_daily`, INCR + EXPIRE 2 days): False
        once the day's count exceeds `cap`. Fails open (WARNING) when Redis fails or is slower than REDIS_TIMEOUT_S."""
        clock, redis = self._d.clock, self._d.redis
        key = self._d.keys.labeling_daily(tenant_id, clock.now().strftime("%Y%m%d"))
        counted: list[int] = []
        try:
            async with deadline_scope(clock, clock.monotonic() + REDIS_TIMEOUT_S):
                counted.append(int(await redis.incr(key)))
                await redis.expire(key, DAILY_COUNT_TTL_S)
        except Exception as exc:
            _log.warning("labeling daily cap not checked: Redis failed", extra={"error": type(exc).__name__})
            return True
        if not counted:
            _log.warning("labeling daily cap not checked: Redis timed out", extra={"tenant_id": tenant_id})
            return True
        return counted[0] <= cap

    async def _unlabeled(self, tenant_id: str, decision_id: str, todo: list[Target]) -> tuple[list[Target], set[str]]:
        """The targets no earlier attempt stored, and the runs holding the ones it did."""
        async with self._d.db.tenant_uow(tenant_id) as uow:
            done = await stored_runs(uow, decision_id, todo)
        return [t for t in todo if t.question.name not in done], set(done.values())

    async def _view(
        self, content: content_mod.LabelInput, alias: str, model: ModelCfg, principal: Principal
    ) -> tuple[tuple[ContentBlock, ...], str]:
        """One labeler's rendered input and its view kind (a labeler lacking a modality gets the derivative view)."""
        media = content_mod.media_parts(content)
        if not media:
            return content_mod.render(content, {}), "native"
        member = DecisionMemberCfg(model=alias, elicitation="verbalized", template=TEMPLATE, weight=1.0)
        view = await self._d.views.resolve(
            member, media, policy="derive_for_text_members", budget=VIEW_BUDGET_V1, model=model, principal=principal
        )
        by_index: dict[int, ViewPart] = {p.part_index: p for p in view.parts}
        return content_mod.render(content, by_index), view.kind

    async def _label_all(
        self,
        job: Job,
        snapshot: ConfigSnapshot,
        tenant: TenantRef,
        principal: Principal,
        decision_id: str,
        todo: Sequence[Target],
        labelers: Sequence[tuple[str, ModelCfg]],
        views: Mapping[str, tuple[tuple[ContentBlock, ...], str]],
    ) -> dict[str, list[LabelerResult]]:
        requests: dict[str, list[tuple[str, ModelCfg, ModelRequest, list[int]]]] = {}
        for t in todo:
            for alias, model in labelers:
                order = letter_order(t.question, decision_id, model.id)
                req = build_request(t.question, order, views[alias][0], alias=alias, model=model, snapshot=snapshot)
                requests.setdefault(t.question.name, []).append((alias, model, req, order))
        reservation = await self._d.admission.reserve(
            principal, self._graph(requests), queue_timeout_s=ADMISSION_QUEUE_TIMEOUT_S
        )
        results: dict[str, list[LabelerResult]] = {}
        by_alias: dict[str, UsageRecord] = {}
        try:
            for t in todo:
                entries = requests[t.question.name]
                results[t.question.name] = await self._ask(t, entries, snapshot, tenant, reservation, views)
                for r in results[t.question.name]:
                    if r.usage is not None:
                        prev = by_alias.get(r.alias)
                        by_alias[r.alias] = r.usage if prev is None else prev + r.usage
        finally:
            with anyio.CancelScope(shield=True):
                await reservation.reconcile(by_alias)
                await reservation.release()
        return results

    async def _ask(
        self,
        t: Target,
        entries: Sequence[tuple[str, ModelCfg, ModelRequest, list[int]]],
        snapshot: ConfigSnapshot,
        tenant: TenantRef,
        reservation: Reservation,
        views: Mapping[str, tuple[tuple[ContentBlock, ...], str]],
    ) -> list[LabelerResult]:
        """Every labeler on one question, concurrently; results in labeler order."""
        out: list[LabelerResult | None] = [None] * len(entries)

        async def one(i: int, alias: str, model: ModelCfg, req: ModelRequest, order: list[int]) -> None:
            out[i] = await self._call(t, alias, model, req, order, snapshot, tenant, reservation, views[alias][1])

        async with anyio.create_task_group() as tg:
            for i, (alias, model, req, order) in enumerate(entries):
                tg.start_soon(one, i, alias, model, req, order)
        return [r for r in out if r is not None]

    def _graph(self, requests: Mapping[str, Sequence[tuple[str, ModelCfg, ModelRequest, list[int]]]]) -> WorkGraph:
        calls: dict[tuple[str, CallKind], int] = {}
        tokens: dict[str, int] = {}
        tokens_in = tokens_out = 0
        for entries in requests.values():
            for alias, _model, req, _order in entries:
                est = self._d.estimator.estimate(alias, req.system, [b for m in req.messages for b in m.content])
                calls[(alias, "labeling")] = calls.get((alias, "labeling"), 0) + 1
                tokens[alias] = tokens.get(alias, 0) + est + req.max_tokens
                tokens_in, tokens_out = tokens_in + est, tokens_out + req.max_tokens
        return WorkGraph(calls, tokens, sum(calls.values()), tokens_in, tokens_out, 0)

    async def _call(
        self,
        t: Target,
        alias: str,
        model: ModelCfg,
        req: ModelRequest,
        order: list[int],
        snapshot: ConfigSnapshot,
        tenant: TenantRef,
        reservation: Reservation,
        view_kind: str,
    ) -> LabelerResult:
        reservation.consume("labeling", alias)
        deadline = Deadline(self._d.clock.monotonic() + LABELER_TIMEOUT_S, self._d.clock)
        try:
            resp = await self._d.adapters.for_model(alias).complete(req, tenant=tenant, deadline=deadline)
        except AdapterError as exc:
            retryable = exc.error.kind in _RETRYABLE
            return LabelerResult(
                alias, model.id, "error", None, view_kind, error=exc.error.kind.value, retryable=retryable
            )
        except NoemaError as exc:  # e.g. tenant access misconfigured (fail closed, not retried)
            return LabelerResult(alias, model.id, "error", None, view_kind, error=exc.code.value)
        cost = self._d.cost.price(alias, tenant.home_region, resp.usage, snapshot=snapshot)  # the pinned catalog
        key = parse_answer(resp.text, t.question, order)
        refused = resp.refused or resp.stop_reason == "content_filter"
        return LabelerResult(
            alias,
            model.id,
            "refused" if refused else "ok" if key is not None else "parse_error",
            None if refused else key,
            view_kind,
            usage=resp.usage,
            region=resp.region,
            cost=cost,
        )

    def _record_usage(
        self,
        job: Job,
        row: Row,
        todo: Sequence[Target],
        results: Mapping[str, list[LabelerResult]],
        runs: Mapping[str, Row],
    ) -> None:
        """One `labeling` usage line per (job attempt, question, labeler) call with usage (spec-11 §2)."""
        for t in todo:
            for r in results.get(t.question.name, []):
                if r.usage is None or r.region is None or r.cost is None:
                    continue
                self._d.cost.record(
                    UsageLine(
                        id=usage_line_id(job.id, job.attempt, t.question.name, r.model_id),
                        tenant_id=job.tenant_id,
                        project_id=str(row["project_id"]),
                        line="labeling",
                        model_id=r.model_id,
                        region=r.region,
                        usage=r.usage,
                        cost=r.cost,
                        decision_id=str(row["id"]),
                        labeling_run_id=str(runs[t.question.name]["id"]),
                    )
                )

    def _excerpt(self, text: str) -> tuple[str | None, bool]:
        """The redacted content excerpt (at most 2,000 characters) and whether redaction touched it."""
        redacted = self._d.telemetry.redact(text)[: content_mod.EXCERPT_MAX_CHARS]
        return redacted, "[REDACTED" in redacted

    def _item_row(
        self,
        t: Target,
        run: Row,
        decision_id: str,
        reason: str,
        results: Sequence[LabelerResult],
        excerpt: str | None,
        redacted: bool,
    ) -> dict[str, object]:
        label, drop = decide(results)
        item_id = self._d.ids.new("ldrop" if drop is not None else "lab")
        value: JsonValue = None if label is None else (label == "true" if t.question.kind == "predicate" else label)
        return {
            "id": item_id,
            "run_id": str(run["id"]),
            "decision_id": decision_id,
            "question_name": t.question.name,
            "sample_reason": reason,
            "labeler_answers": labeler_answers(t.question, results),
            "kept": drop is None,
            "value": value,
            "drop_reason": drop,
            "ensemble": dict(t.ensemble),
            "content_excerpt": excerpt,
            "redacted": redacted,
            "input_view_signature": t.view_signature,
            "member_subset": list(t.member_subset),
            "created_at": self._d.clock.now(),
        }

    async def _schedule_runs(self, tenant_id: str, run_ids: set[str]) -> None:
        """Debounced `labeling.run` refresh per touched run (one live job per run)."""
        at = self._d.clock.now() + timedelta(seconds=RUN_REFRESH_DELAY_S)
        for run_id in sorted(run_ids):
            await self._d.jobs.enqueue(
                JOB_RUN, tenant_id, {"run_id": run_id}, priority=JOB_PRIORITY, run_after=at, dedupe_key=run_id
            )
