# noema.labeling: sampling, D4 labels, calibrators, gate, drift, registry, W4 views (owner: labeling, Wave C1; C2a)

Spec: ADR-003 (all), spec-02 §9 (frozen Protocols), spec-03 (`labeling_run`, `label_item`, `calibrator`,
`calibrator_epoch`, RLS, definer functions), spec-11 §1.2/§2/§3/§4 (admission, usage lines, outbox, jobs),
IMPL-039/042/043/071/072/073, 06 §2.2-§2.3 (design). Phase 1 runs the whole machinery on synthetic labels; no
artifact is fitted on live data, and live traffic serves `uncalibrated` until an operator activates one (ADR-003 §6).

## Layout

| Module | Contents |
|---|---|
| `sampler.py` | `Sampler`: implements `LabelingSampler` (sync, never raises) |
| `registry.py` | `DbCalibratorRegistry`: implements `CalibratorRegistry` (per-pod snapshot), plus `listen()` for pub/sub |
| `jobs.py` | `make_handlers(services)` -> `labeling.item`, `labeling.run`, `labeling.drift`; `deps_from`, `DRIFT_PERIOD_S` |
| `items.py` / `retries.py` | the `labeling.item` job (exact daily cap, span, outcome counter); what an earlier attempt stored (one `items_for_decision` read) |
| `runs_open.py` | opening runs: one running run per question definition (name + signature, revision 0002); a labeler-set change closes the open run |
| `runs.py` / `aggregate.py` / `window.py` | the `labeling.run` job: aggregates, closing, fitting + gate; the compact, one-per-decision fit window; deterministic `calibrator_id` |
| `drift.py` | the weekly `labeling.drift` job (downgrade on drift, widening thin slices, one `kept_items` read per slice; closes stale runs) |
| `labelers.py` | the `labeler@1` prompt, permutation, request, parse (blind, permuted, spotlighted) |
| `content.py` / `targets.py` / `records.py` | ContentRef resolution and rendering; labelable answers; label_item encoding and the unanimous rule |
| `calibrators/transforms.py` | runtime `TemperatureTransform`, `PlattTransform`, `IdentityTransform` (pure Python, hot path) |
| `calibrators/temperature.py`, `calibrators/platt.py` | numpy fitters (safeguarded Newton on 1/T; Newton + line search for Platt) |
| `dataset.py`, `fitting.py`, `metrics.py`, `consensus.py`, `evaluation.py` | labeled sets and the time-ordered stratified split, the artifact fit, ECE/Brier/NLL/reliability/bootstrap, consensus rates and the family-overlap lift, holdout builder |
| `acceptance.py`, `lifecycle.py`, `artifact.py` | the A1-A8 gate, lifecycle edges and two-person rule, row <-> `Calibrator` |
| `views.py` | W4 mapping and list functions used by `api/routes/admin_readonly.py` |

Imports: `noema.core` (Protocols and values), `noema.config` (the `*Cfg` types) and numpy. The database and the
telemetry are reached through core Protocols only. No other domain module is imported (import-linter: 27 contracts
kept).

## Public surface and wiring (composition.py, C2)

```python
from noema.labeling import DbCalibratorRegistry, Sampler, make_handlers, DRIFT_PERIOD_S

registry = DbCalibratorRegistry(db)        # db: the process Database (PgDatabase[PgUnitOfWork]); keep this concrete
                                           # instance: Services.calibrators is typed as the CalibratorRegistry
                                           # Protocol, which has no listen()
sampler = Sampler(clock, config, outbox, expected_pods=settings.expected_pods)
# build(): Services(..., calibrators=registry, sampler=sampler, ...)
# start(services, "api" | "jobs"): keep the existing 5 s _calibrator_poll (it calls refresh()) and ALSO
services.runtime.spawn(
    "calibrator-listen", lambda: registry.listen(services.redis, services.keys.calibrators_changed(), clock=services.clock)
)
# jobs process: handlers = merge_handlers(..., noema.labeling.make_handlers(services), ...); `--kinds labeling.*`
# periodic scheduler (ops-ledger): SystemCalls.ensure_periodic("labeling.drift", DRIFT_PERIOD_S)   # 7 days
```

- `DbCalibratorRegistry(db: Database)`: `lookup(tenant_id, key)` and `epoch(tenant_id)` are sync dict reads.
  `refresh()` never raises (cancellation propagates). It reads `SystemCalls.calibrator_epochs()`, reloads only the
  changed scopes with `active_calibrators(...)` and swaps the snapshot atomically. A malformed row is skipped, so
  that binding stays uncalibrated. Before the first refresh: `lookup` is None and `epoch` is `""`. An unknown
  tenant's epoch is `""` too. Counters for diagnostics: `refreshes`, `refresh_errors`.
- `listen(redis, channel, *, clock)` runs until cancelled. It subscribes, refreshes once and then on every message,
  and reconnects with backoff from 1 to 30 s on `clock`. Staleness is at most 5 s (the poll); with Redis up it is
  one round trip.
- `Sampler(clock, config, outbox, *, expected_pods=1)`: `offer()` labels final scored answers only (not refusal or
  unavailable, not hierarchical). The draws are deterministic per decision id: `uniform` when
  `draw < uniform_rate`, else `uncertainty` when some labelable answer is in the low band and
  `draw < uncertainty_rate`. The share is `ceil(max_per_day_per_tenant / expected_pods)` per pod and UTC day (the
  `labeling.item` job enforces the cap exactly cluster-wide, see "Daily cap" below). The uniform slice has priority
  and is spread over the day: when the previous day's uniform demand exceeded the share, uniform draws are thinned by
  `share / demand` (a third deterministic draw). Uncertainty offers get only `share - min(share, previous demand)`
  (half the share before the pod's first full day). An offer becomes
  `Outbox.put(labeling_offer{decision_id, turn_id, question_names, sample_reason})`; the counters move only after
  the put succeeded.
- `make_handlers(services: ServiceSet)` reads `clock, ids, config, db, jobs, adapters, admission, estimator, cost,
  files, views, telemetry, redis, keys` (`LabelingDeps`; `redis` and `keys` since C2a: the exact daily cap).
- Route: `noema.api.routes.admin_readonly.router` (already in `API_ROUTERS`) serves `listCalibrators`,
  `listLabelingRuns` and `listLabelingDropped` through `views.list_*`, with `svc.db` and `svc.authz`.

## Jobs

| Kind | Payload | What it does |
|---|---|---|
| `labeling.item` (tenant, prio 200, from the outbox) | `{decision_id, turn_id, question_names, sample_reason}` | span `labeling.item`; on the first attempt counts the offer in the exact daily cap (`RedisKeys.labeling_daily(tenant, YYYYMMDD)`, INCR + EXPIRE 2 days; above `max_per_day_per_tenant` the job ends without labeling); loads the decision row (tenant_uow) and its content; skips rows without stored input; on a retry, skips the answers an earlier attempt stored in any run of their definition (`retries.py`, one `items_for_decision` read) and re-schedules those runs' refresh; opens or reuses the running run of each answer's question definition (`ensure_run`); reserves `labeling` calls with `system_principal(tenant, purpose="labeling")`; asks each labeler (concurrently, `retry="none"`, 120 s deadline); records one `labeling` usage line per call (`h("labeling:{job}:{attempt}:{question}:{model_id}")`, priced with `CostMeter.price(alias, tenant.home_region, usage, snapshot=<job snapshot>)`; the line's `region` is the call region); stores one `label_item` per answer (`ids.new("lab")` kept, `ids.new("ldrop")` dropped) and counts it in `aghoo_labeling_items_total{question, outcome}`; schedules `labeling.run` (run_after +60 s, dedupe = run id) |
| `labeling.run` (tenant) | `{run_id}` | span `labeling.run`; recomputes the counts, consensus rates, `n_by_class` and usage from the items (re-read in the transaction that writes them: the refresh of an item committed after the first read was deduped against this running job; Wave E), and sets `aghoo_label_consensus_ratio` and `aghoo_label_class_share` (each declared option's share of the run's kept uniform labels); closes the run at `fit_min_kept(n_min) = ceil(n_min / 0.5)` kept uniform labels or after 30 days; only a run due by one of those limits is fitted (a run closed early is refreshed, not fitted): each (subset, view) binding over the 90-day window of the same question definition and labelers, one item per decision, fitted in a worker thread, gated, written as the tenant artifact `fitted` -> `accepted`/`rejected` and counted in `aghoo_calibrator_fits_total{status}` |
| `labeling.drift` (`__platform__`, weekly) | `{}` | per active artifact, in a `labeling.drift` span (platform: per tenant served, i.e. not the tenants whose own active artifact of that binding wins), the kept uniform labels of the binding (one `LabelRepo.kept_items` read per tenant, filtered to subset and view, the earliest item per decision) of the last 7 days, widened week by week up to 4 weeks while fewer than 100; still fewer: WARNING "calibrator drift not evaluated", no verdict; `ece > drift_ece_max` -> `drift_ece`, else basic-bootstrap lower bound > `ece_max` -> `drift_ci`; `set_calibrator_status("system:labeling.drift", id, "downgraded")` bumps the epoch and counts `aghoo_calibrator_downgrades_total{reason}`; a refused downgrade (`AdminError` forbidden / not_found / invalid_state) is skipped, any other error is re-raised after the other artifacts were checked (queue retry); schedules `labeling.run` for running runs older than 30 days |

Runs (`runs_open.py`; the revision 0002 `labeling_run_open` index allows one running run per (tenant, profile,
version, question name, question signature)): a run is one question definition (signature, profile version, labeler
set). Definitions that share a name (the facade's `q0`, `q1`, ...; reused tenant names) each keep their own running
run: `ensure_run` calls `open_run(profile, version, name, signature)`. The only remaining conflict is a labeler-set
change (same signature, other labelers): the open run is closed, its `labeling.run` (+60 s) enqueued first when it may
be due, and a new run opens. A race for the slot: the loser re-reads the winner's run, or raises (the job retries).
Runs parked by the C1 rotation (status `completed`, below both limits) are never resumed and never fitted by
themselves; their labels count in the later fit windows of their definition.

Daily cap: `Sampler` keeps its per-pod share `ceil(cap / expected_pods)` (uniform priority, spread over the day) and
the item job enforces the cap exactly cluster-wide. The counter is incremented once per offer (first attempt only,
before the decision row is read: an offer that turns out unlabelable still counts, as in the sampler). A Redis error
or a call slower than 1 s fails open (WARNING "labeling daily cap not checked"); the per-pod share still bounds the
day.

Labelers (`labeling.labelers`, Bedrock ids recorded):
- Prompt `labeler@1`: static system text; `<app_question>` JSON (`<`, `>`, `&` escaped); the input inside
  `<untrusted_input>` with `esc`; document and OCR text datamarked; engine `[role]` labels; `[image-1: view]` lines
  and no filenames.
- Options: choice options permuted per labeler (`sha256("labeling|decision|model_id|question")`); predicates A = true;
  scores in declared order.
- Effort `medium` when the model accepts it (Sol), else none (Sonnet, IMPL-043); never a temperature.
  `max_tokens = 16 + reasoning reserve` (`max(reasoning_reserve_tokens, EFFORT_RESERVE[effort])`, the shared
  `noema.config.models.EFFORT_RESERVE` since C2b; the local copy is gone).
- Answer: exactly one letter. A labeler without a modality gets the derivative view (`InputViewResolver`,
  `derive_for_text_members`); its view kind is recorded per answer.
- Unanimous rule: kept iff every labeler is `ok` with the same option. Drops keep both answers, with precedence
  labeler_error > refused > parse_error > disagreed.
- Retryable adapter errors (throttled, retryable) raise `RetryLabeling` until attempt 5. The other answers are stored
  first, and the last attempt records `labeler_error`.

Content (IMPL-071): the decision row `input`, in the decision/repo.py format; for turns, the turn's `input_parts`
plus up to 10 earlier turns (at most 48,000 characters; turns/context/history.py format) and files by id. The
excerpt is the last user message, passed through `Telemetry.redact` and truncated to 2,000 characters, with
`redacted` set iff a `[REDACTED` marker is present.

## Fitting, metrics and the gate

- Split (06 §2.3 step 1): per class, the most recent `ceil(0.5 n_c)` kept **uniform** labels are the test split.
  Phase 1 has no separate "fit" split; temperature and Platt are fitted on the calibration split. The uncertainty
  slice counts only toward the consensus statistics and the lift.
- Artifact: a per-member temperature on `ln max(p/(1-u), eps)` (eps = 1e-4, T clamped to [0.05, 20]), then the
  linear pool with weights renormalized per row, then the pooled transform. Choice and score use a temperature,
  predicates Platt (`|a|, |b| <= 100`, ridge 1e-6). The unresolved mass is preserved and never spread.
- Metrics (IMPL-073): ECE over `min(15, n)` equal-mass bins (choice: top-label; predicate: P(true); score: the mean
  over the cumulative P(level <= k)). Brier is multiclass, or binary for predicates. NLL is floored at 1e-12.
  Reliability bins are the A1 bins. `reliability.ci` holds percentile 95% bootstrap intervals (200 resamples, seeded
  by dataset, view and subset).
- Holdout: the contract `HoldoutMetrics`, values rounded to 6 decimals, which is what the gate reads.
  `consensus_rate` is kept / sampled. `consensus_rate_by_class[c]` is kept-with-label-c divided by the items where
  some labeler answered c (0.0 when no labeler ever did). The lift is
  `max over members of (max over labelers - min over labelers) of (agree - chance)`, 1.0 without evidence.
  `scope.retained_share` is the kept share of the uniform slice. `scope.slices` holds `input_view:<sig>` and
  `member_subset:<ids>`. `dataset_id` is the id of the run whose job fitted it.
- Window and identity (`window.py`, `runs.py`): the window is every run of the definition started within 90 days
  before the run closed; paging `list_runs` (id DESC, time-ordered ids) stops at the first older run. Each item is
  parsed once into a compact `WindowItem` (example, consensus view, binding) and the row is dropped. A decision
  counts once (the earliest stored item, by ULID). The artifact id is
  `cal_` + `sha256(tenant | binding | choice_method | window end)`, the window end being the binding's newest kept
  uniform label id, so the same labels never mint a second artifact (retries, repeats, overlapping windows).
  `fit_and_evaluate` runs in `anyio.to_thread.run_sync`.
- Artifact rows (`artifact.calibrator_from_row`): `member_t` must cover exactly `member_subset` (ADR-003 §1);
  otherwise the row is malformed and the registry skips it, so the binding stays uncalibrated (no identity
  fallback for a missing member).
- Gate (`acceptance.evaluate_gate`): A1-A8 with contract rule names, thresholds from
  `labeling.acceptance` / `family_overlap_lift_max` (`Thresholds.from_config`). A8 checks the subset against the
  stage's full member set (`m1..mN` of the active profile version) or its share of uniform traffic in the window.
  A missing or ill-typed holdout field fails the rule that reads it.
- Lifecycle (`lifecycle.py`): labeling performs only `fitted -> accepted|rejected` (CalibratorRepo, tenant rows,
  RLS guard) and `active -> downgraded` (SystemCalls). Approval and activation are the two-person CLI steps
  (identity / admin.*); activation retires the previous artifact of the binding. Artifacts whose profile version
  leaves the active config are retired (`profile_version_retired`) by identity's `noema-admin config approve`
  (`identity/cli/platform_cmds.py::retire_left_calibrators`, orchestrator ruling 5); labeling does not fit a retired
  profile version.

## Config keys read

`labeling.labelers`, `labeling.rule`, `labeling.sample.{uniform_rate, uncertainty_rate, max_per_day_per_tenant}`,
`labeling.family_overlap_lift_max`, `labeling.acceptance.{ece_max, n_min, class_n_min, consensus_min,
drift_ece_max, subset_min_share}`, `models.<labeler>.{id, controls.effort, controls.reasoning,
reasoning_reserve_tokens, max_output_tokens_cap, modalities}`, `decision_profiles.<id>.{version, members}`,
`tenants.<id>` (the TenantRef for credentials).

## Metrics, spans, logs, errors

- Spans (C2a): `labeling.item` (one per item-job attempt: `aghoo.tenant`, `aghoo.profile` and `aghoo.member.question`,
  comma-joined when one decision has several labelable answers; a transient retry is not a span error),
  `labeling.run` (one per run job: `aghoo.tenant`, `aghoo.profile`, `aghoo.profile_version`, `aghoo.member.question`)
  and `labeling.drift` (one per artifact checked: profile, version, question, and `aghoo.tenant` for a tenant
  artifact).
- Counters (C2a): `aghoo_labeling_items_total{question, outcome=kept|disagreed|refused|parse_error|labeler_error}`
  (per stored item), `aghoo_calibrator_fits_total{status=accepted|rejected}` (per fitted artifact),
  `aghoo_calibrator_downgrades_total{reason=drift_ece|drift_ci}` (per drift downgrade).
- Gauges: `aghoo_label_consensus_ratio{profile, question, class}` and `aghoo_label_class_share{profile, question,
  class}` (run job; the share of each declared option among the run's kept uniform labels, the 02 §5.5
  label-distribution-shift input), `aghoo_calibration_ece{profile, signature, view_signature}` (fit and drift). Cost
  lines go through `CostMeter.record` (`aghoo_cost_usd_total{tenant, line="labeling"}`).
- Logs: `noema.labeling.registry` (refresh failure and skipped rows, WARNING), `noema.labeling.sampler` (an offer
  failure, logged with the exception), `noema.labeling.items` (INFO: skips, including "the tenant's daily cap is
  reached"; WARNING "labeling daily cap not checked: Redis failed | Redis timed out"), `noema.labeling.runs` (INFO:
  fits, closes, "labeling run closed: the labeler set changed"), `noema.labeling.drift` (WARNING "calibrator
  downgraded by drift" with `n` and `window_days`: the alert; WARNING "calibrator drift not evaluated: too few uniform
  labels" with `calibrator_id`, `scope`, `n`, `min_n`, `window_days`; WARNING "calibrator downgrade refused").
- Errors: `PermanentJobError` (malformed payload, tenant not in config), `RetryLabeling` (transient labeler error),
  `NoemaError(queue_timeout | rpm_exceeded | tpm_exceeded)` from admission (the job retries with backoff),
  `NoemaError(internal_error)` from the database (retry; the drift job re-raises a failed downgrade after checking
  the other artifacts), `AdminError` (`noema.db.system`) from `set_calibrator_status`: forbidden / not_found /
  invalid_state are skipped, invalid_argument propagates. The routes raise `permission_denied` (403, through the
  Authorizer) and `schema_violation` (400, bad `limit` or `after`, from the repos).

## Tests (all deterministic: FakeClock, scripted labelers, seeded synthetic data)

- `tests/unit/labeling/` (198 tests): `test_acceptance.py` and `test_drift.py` are gate **Q6** of spec-10 §3. Every rule
  rejects at its boundary and accepts just inside: ECE 0.0501, NLL above uncalibrated, n 299, a class with n = 29
  flagged, class consensus 0.59, lift 0.1001, subset share 0.0499. Synthetic correctable members are accepted after
  fitting and confidence-inverted members are rejected, for all three question types. A weekly ECE of 0.0801
  downgrades (`drift_ece`), 0.08 does not; the CI rule, the platform-global downgrade, the platform artifact not
  judged where a tenant artifact serves, the widened slice (a thin week, 4-week limit, narrowest sufficient slice),
  the "not evaluated" warning, a transient downgrade failure re-raised after the other artifacts, refused downgrades
  skipped, mismatched slices, the registry epoch bump, the one-read `kept_items` slice (a decision counted once), the
  `labeling.drift` span and the downgrade counter are covered. The other files cover:
  - `test_transforms`, `test_metrics` (the ADR-003 §3 ECE vectors), `test_fitting`, `test_consensus`;
  - `test_sampler` (incl. uniform priority over uncertainty, spreading over the day, a failed put), `test_labelers`,
    `test_content`;
  - `test_items_job`, including the frozen wire rules of `tests/fakes` FakeAdapter, the sampler -> outbox -> job ->
    item path, alternating definitions of one name (2 running runs, no fits), a retry after the run closed (no second
    label, no run scan), a stored item of another definition of the name, a retry that re-schedules a lost refresh,
    and a labeler-set change closing a possibly-due run (fit enqueued first);
  - `test_items_cap_and_telemetry` (C2a): the exact daily cap across pods (INCR + EXPIRE 2 days, per UTC day, a retry
    not counted again, no cap = no counter, Redis failure and timeout fail open), `lab_` / `ldrop_` ids from the
    IdGen, the `labeling.item` span and `aghoo_labeling_items_total`, prices with the job snapshot and the home region;
  - `test_runs_job` (incl. runs closed early never fitted, no duplicate artifact for the same labels, one item per
    decision, the fit in a worker thread, the window's early stop, the `labeling.run` span, the fit counter and the
    uniform class shares), `test_registry` (incl. partial `member_t` skipped), `test_admin_routes` (the real
    middleware and the contract validator), `test_jobs` (incl. the `redis`/`keys` wiring).
- `tests/integration/labeling/`:
  - `test_registry_postgres.py`: two pods; two-person CLI activation; noema_app drift downgrade; Valkey
    `calibrators_changed`.
  - `test_jobs_postgres.py`: the item and run jobs on the real repos and RLS, with the W4 output contract-validated;
    alternating definitions of one name keep two running runs under the revision 0002 `labeling_run_open` index, a
    retry finds its answer through `items_for_decision` and asks no labeler, and the drift slice reads `kept_items`.
  - The live labeler smoke moved out of this tree: it runs under the spec-10 §6 counting registry as
    `tests/live/test_labelers_live.py` (row L9 of spec-10 §6 since C2b; request 10).
  - `conformance_impls.py`: `labeling_sampler/labeling`, `calibrator_registry/labeling-fakedb` and
    `calibrator_registry/labeling-postgres` (integration). The suite's artifacts carry a `T_m` for every member
    (request 11, done), and the kit stores them as given.
- Commands: `make test MOD=labeling` (also `XDIST=4`), `make test-conformance PYTEST_ARGS='-k "calibrator or
  sampler"'`, `make lint MOD=labeling`, `make typecheck MOD=labeling`, `make typecheck-tests`. The Postgres tests need
  revision 0002 (`make db-migrate`; the test templates rebuild from the migration hash).

## Known limits

- The ECE noise floor (request 6, ruling R5): with 15 equal-mass bins a calibrated binding needs about 1,500 or more
  test labels to pass `ece_max = 0.05`. A2 can reject an already-calibrated pipeline. Both are design thresholds for
  Phase 0.
- Daily cap: an offer whose first attempt fails before the counter (only payload and tenant checks precede it, and
  both are permanent) is never counted; a Redis outage fails open, bounded by the sampler's per-pod share.
- Fitting inputs and per-call usage live in the documented jsonb extras of `label_item` (request 3, rejected as
  columns: the layout is final; spec-03 §2 lists the keys).
- The fit window still pages `list_runs` and reads whole runs' items: it needs the dropped items too (consensus, lift,
  subset shares), and `kept_items` returns kept items only. It keeps only compact parsed items and stops paging at its
  start.
- Runs parked by the C1 rotation stay `completed`; one whose definition did not reach a limit is not fitted on its
  own (its labels count in later windows of the definition).
- Drift floor (request 8; ruling R5: `DRIFT_MIN_N = 100` and the ECE noise floor are initial design values, tunable
  in Phase 0, ADR-003): an artifact with fewer than 100 kept uniform labels in 4 weeks gets no verdict (WARNING).
  With the binned-ECE noise floor, `drift_ece` downgrades most calibrated artifacts judged on fewer than about 400
  labels. The drift slice can overlap the artifact's own holdout labels.
- Sampler: on a pod's first day (no forecast) and on a day whose uniform demand jumps above the previous day's, the
  uniform slice still fills the share early in the UTC day; the forecast is per pod and in memory.
- Hierarchical answers are never sampled or fitted (ADR-003 §1). Labeling cost and selection-bias AUROC reporting
  are Phase 0. Platform artifacts (seed-v1) are imported through `PgAdminCalls.upsert_platform_calibrator`
  (`noema_cli`), not produced by these jobs; no `noema-admin` subcommand calls it yet, and the function checks only
  scope, id and status, so an importer must hand it a contract-valid artifact (docs/modules/db.md).
