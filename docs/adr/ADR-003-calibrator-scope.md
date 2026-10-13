# ADR-003: Calibrator scope, binding, acceptance and drift

- **Status**: Accepted for Phase 1 implementation (Wave A). **Amended in the Wave A consolidation**: IMPL-072 (lifecycle, two-person activation, no per-tenant suppression in Phase 1, registry runtime) and IMPL-073 (ECE per question type). The thresholds stay at their initial design values until Phase 0 measures them. **Amended in Wave C2a** (ruling R5, 2026-10-10): the ECE noise floor (§3) and the drift floor `DRIFT_MIN_N = 100` with the basic-bootstrap CI (§5), both initial design values tunable in Phase 0.
- **Deciders**: decision-engine architect; labeling owner implements; owner review at the Wave A checkpoint.
- **Planned in**: 05 task 0.9, 08 §12 (ADR-003, "Calibrator scope: per tenant vs platform default").
- **Related**: [`../impl-spec/06-decision-engine.md`](../impl-spec/06-decision-engine.md) §8, [`../IMPL-DECISIONS.md`](../IMPL-DECISIONS.md) IMPL-048 (per-stage provenance), IMPL-039 (thresholds from config), IMPL-042 (W4 `CalibratorInfo`), IMPL-072, IMPL-073; DDL and RLS in [`../impl-spec/03-data-model.md`](../impl-spec/03-data-model.md); owner decisions D2 and D4, and R5 ("answers stay uncalibrated until a per-stage calibrator passes").

## Context

- **Calibration is a property of the whole pipeline.** It depends on the members, the templates, the option-order policy, the pooling, the question signature, the member subset and the input-view signature. Changing any of these invalidates a calibrator (02 §5.5, L366).
- **Labels are LLM consensus.** `gpt-6.1-sol` and `claude-sonnet-5-5` must agree, or the item is dropped (D4). Calibration is therefore against consensus labels on the retained population, with a selection bias and a labeler-family overlap that are measured and exposed (02 L395-403; 06 §2.2).
- **A calibrator applies only to the stage it was fitted for.** `lightweight-model-decide` answers and escalated answers stay `uncalibrated` until a calibrator fitted for their own stage passes (02 L376; 06 §2.3 step 5).
- **Labels stay in the tenant's scope.** Labeling runs in the tenant's AWS account (D2). A platform-default calibrator built from platform-owned seed data applies until the tenant has n ≥ 300 (02 L377).
- **Monitoring:** weekly ECE on the uniform random slice; drift (ECE > 0.08, or a CI that excludes the acceptance level) means auto-downgrade to `uncalibrated` plus an alert (06 §2.3 step 6).
- **Phase 1 reality:** there are no real consensus labels yet. The machinery is built and tested on synthetic labels, and live traffic serves `uncalibrated` (PLAN, "Honest scope labels").

## Decision

### 1. One artifact per binding key

A **calibrator artifact** is fitted, accepted, activated and applied as a single unit for exactly one binding:

```python
@dataclass(frozen=True, slots=True)
class CalibratorBindingKey:               # frozen text: spec-02 §9. Scope and tenant are NOT part of the key;
                                          # they are attributes of the artifact, and lookup(tenant_id, key) applies the precedence
    profile_id: str                       # the ANSWERING stage, e.g. "router-default"
    profile_version: str                  # "1"
    question_signature: str               # per-question sha256 (spec §3.3)
    member_subset: tuple[str, ...]        # sorted ok member ids of that stage, e.g. ("m1","m2","m3")
    input_view_signature: str             # that stage's view signature (spec §3.5)
    choice_method: Literal["flat", "hierarchical"]   # the answer's method; Phase 1 fits "flat" artifacts only, so hierarchical
                                                     # answers (built in Phase 1, IMPL-027) stay uncalibrated (spec-06 §19.8)
```

| Artifact field | Contents |
|---|---|
| `id` | `cal_<ulid>` |
| `binding` | the key above |
| `method` | `temperature` (choice/score) or `platt` (predicate); this is the pooled transform |
| `member_T` | `{member_id: T_m}` for every member of the subset (per-member temperature on log-probabilities) |
| `pooled` | `{T}` or `{a, b}` |
| `epsilon` | log-space floor for stated zeros, default `1e-4` |
| `holdout` | the full `HoldoutMetrics` of the contract: `dataset_id`, `label_source: "llm_consensus"`, `labelers`, `consensus_rate(_by_class)`, `n(_by_class)`, `ece`, `brier`, `nll`, `nll_uncalibrated`, `flagged_classes`, `scope{target, retained_share, slices}`, `family_overlap{max_member_lift, threshold}`, `evaluated_at` |
| `scope`, `tenant_id` | `tenant` (with the owner tenant) or `platform` (`tenant_id` NULL) |
| `status` | `fitted → accepted \| rejected`; `accepted → active` (two-person activation, §3); `active → downgraded` (drift, §5) or `retired` |
| `fitted_at`, `accepted_at`, `approved_by`, `activated_by[2]`, `activated_at`, `downgraded_at?`, `downgrade_reason?` (`drift_ece` \| `drift_ci`), `retired_at?`, `retired_reason?` | audit trail (also the W4 `CalibratorInfo`, IMPL-042) |

The per-member and pooled parts are never applied separately. An answer is either fully calibrated by one artifact or `uncalibrated`. There is no partial calibration and no fallback to a "nearby" binding. In particular, none of these ever transfer: another member subset, another view signature, another stage, another profile version, the hierarchical method. Phase 1 labeling fits flat bindings only (amended 2026-10-10, IMPL-027). A hierarchical artifact needs its own transform, because each expanded group's calibrated candidates must still sum to its group mass; it comes with that fitter later. Until then hierarchical answers are `uncalibrated`.

### 2. Scope and lookup precedence

`CalibratorRegistry.lookup(tenant_id, binding)` returns the first match:
1. a **tenant** artifact for the exact binding with `status = active`;
2. a **platform** artifact for the exact binding with `status = active`;
3. otherwise `None`, and the answer is `uncalibrated`.

- **Tenant artifacts** are fitted only on that tenant's consensus labels, stored with that tenant's `tenant_id` under RLS, and produced by labeling jobs that run in the tenant's AWS account (D2).
- **Platform artifacts** are fitted on the platform-owned `seed-v1` (06 §2.1) and apply only to platform question signatures (the router and verifier questions in config). Request-defined questions have tenant-specific signatures and can only ever get tenant artifacts.
- "Platform default until the tenant has n ≥ 300" is implemented by precedence: a tenant artifact needs `n ≥ 300` to be accepted (§3), and once it is active it wins.
- `CalibratorRegistry.epoch(tenant_id) -> str` = `f"{platform_epoch}.{tenant_epoch}"`, from the `calibrator_epoch` rows (spec-03). A counter increases on any activation, downgrade or retirement in its scope. It is part of the L2 key (spec §14), so a calibrator change invalidates cached decisions.
- **Runtime** (IMPL-072):
  - each process holds an in-memory snapshot of the active artifacts and the epochs, so `lookup` and `epoch` are **sync** with no I/O;
  - the snapshot refreshes on the Redis pub/sub `RedisKeys.calibrators_changed` (published by the `admin.*` and labeling definer functions) and on a 5 s poll of `ops.calibrator_epochs()`;
  - the staleness bound is **≤ 5 s**;
  - an unknown epoch (snapshot not loaded yet) → `epoch() = ""` → L2 bypass, and `lookup()` returns `None`.

### 3. Acceptance gate (thresholds read from config, never hard-coded)

Config extension (IMPL-038/039; exact schema in [`../impl-spec/05-config-extensions.md`](../impl-spec/05-config-extensions.md) §1.3), with the initial design values as defaults:

```yaml
labeling:
  family_overlap_lift_max: 0.10          # exists today
  acceptance:                            # new
    ece_max: 0.05
    n_min: 300
    class_n_min: 30
    consensus_min: 0.6
    drift_ece_max: 0.08
    subset_min_share: 0.05
```

An artifact moves `fitted → accepted` only if **every** rule holds on the time-ordered, most recent test split:

| # | Rule |
|---|---|
| A1 | `holdout.ece ≤ ece_max`, with the ECE that matches the question type (IMPL-073): **choice** = top-label ECE; **predicate** = ECE of P(true); **score** = the mean over k = 0…K−2 of the ECE of the cumulative P(level ≤ k) against 1[y ≤ k]. In every case `bins = min(15, n)` equal-mass bins by sorted confidence, and `ECE = Σ_b (n_b/n)·\|mean conf_b − mean outcome_b\|` |
| A2 | `holdout.nll ≤ holdout.nll_uncalibrated` (both recorded) |
| A3 | `holdout.n ≥ n_min`, with `n_by_class` summing to `n` and covering exactly the declared values |
| A4 | `flagged_classes` = exactly the classes with `n_by_class < class_n_min` (listed, not a failure) |
| A5 | `consensus_rate_by_class[c] ≥ consensus_min` for **every** class, and the overall `consensus_rate ≥ consensus_min` (so every rate is > 0) |
| A6 | `family_overlap.max_member_lift ≤ labeling.family_overlap_lift_max`, with `threshold` = that config value. The lift is defined in 06 §2.2. This blocks `lightweight-model-decide` (Luna, which shares Sol's family) until the lift is small |
| A7 | `label_source = "llm_consensus"` and ≥ 2 distinct labelers |
| A8 | `member_subset` is the full required set, or a partial subset whose share of the stage's answered traffic over the label window is ≥ `subset_min_share` (02 L371, 06 §2.3 step 5). Rarer subsets are not fitted |

- ECE test vectors (n = 3, so 3 bins of one item each):

  | Type | Inputs | Outcomes | ECE |
  |---|---|---|---|
  | choice | top-label confidences (0.9, 0.6, 0.8) | correct (1, 0, 1) | (0.1 + 0.6 + 0.2)/3 = **0.3** |
  | predicate | P(true) (0.9, 0.2, 0.7) | labels (T, F, F) | (0.1 + 0.2 + 0.7)/3 = **0.3333** |
  | score | one item, K = 3, p = (0.2, 0.5, 0.3), y = 1 | — | P(≤0) = 0.2 vs 0 → 0.2; P(≤1) = 0.7 vs 1 → 0.3; mean = **0.25** |

- **ECE noise floor** (labeling C1 evidence; orchestrator ruling R5, 2026-10-10: the thresholds stay initial design values, tuned in Phase 0). With `bins = min(15, n)` equal-mass bins, the binned ECE of a perfectly calibrated binding is biased upward by bin noise: on synthetic 4-option choice data (`tests/unit/labeling/synth.py`) the worst ECE over 9 calibrated or correctable sets was 0.066 at 600 test labels, 0.052 at 1,200 and 0.037 at 2,000. At `n_min = 300` most honest artifacts would therefore fail `ece_max = 0.05`, and A2 can reject an already calibrated pipeline when the fitted transform is marginally worse on the test split. Phase 0 sets `n_min` / `ece_max` with this bias in mind, or adopts a debiased ECE for both the gate and drift (§5).
- The gate uses the same predicates as `contracts/check_examples.py::holdout_errors`, so the served artifact and the response validator can't disagree. It reads its thresholds from the config snapshot instead of the module constants. The config is bounded by the contract constants, so it can only be stricter (IMPL-039).
- **Activation** (`accepted → active`) is **two-person** (03:L517, L533; IMPL-072):
  1. `noema-admin calibrators approve <id>` by operator A;
  2. `noema-admin calibrators activate <id>` by operator B ≠ A.

  Both steps are audited, and `activated_by = [A, B]`. The DB enforces A ≠ B (spec-03 §2 CHECK). Nothing activates automatically.
- Activating an artifact for a binding retires the previously active artifact of the same scope and binding.

### 4. Application (decision engine, per answer)

1. The answering stage closes the answer. The binding is `(stage profile id@version, question signature, sorted ok member ids, the stage's view signature, "flat")`.
2. `cal = lookup(...)`. If it is `None`, the answer is `uncalibrated`: `probability = raw_score = the linear pool of the raw member distributions`.
3. If `cal` exists:
   - per-member temperature: `q_m = Temp(raw_m, T_m, ε)`;
   - linear pool over the `q_m` gives `raw_score`;
   - the pooled transform gives `probability`;
   - `Calibration{status: "calibrated", method, calibrator_id, member_subset, binding, holdout}`.
   - Confidence and band are then computed on the calibrated probabilities; the band uses `p_max` (spec §11).

Transforms (spec §8.3). `O` = the measured (non-censored) options; `u` = unresolved mass, which is preserved:
- `Temp(p, T, ε)_o = (1−u) · exp(ln max(p_o/(1−u), ε)/T) / Σ_{o'∈O} exp(ln max(p_o'/(1−u), ε)/T)`;
- `Platt(p_true) = σ(a·logit(clamp(p_true, ε, 1−ε)) + b)`, and `P(false) = 1 − P(true)`, both scaled by `(1−u)` when `u > 0`.

The stage rule falls out of the binding:
- a `router-default` artifact never applies to a `router-plus` answer, or to the `router-default` stage of a cascade whose views differ;
- an escalated answer binds to the stage that produced it, using that stage's `EscalationStage.input_view_signature` (CP-DEC-2);
- if CP-DEC-2 is rejected, escalated answers skip the lookup and stay `uncalibrated`.

### 5. Drift and downgrade (labeling job, weekly)

- For every `active` artifact, and for every tenant that a platform artifact serves, compute the ECE (and a bootstrap CI) on that tenant's **uniform random slice** of consensus-labeled decisions from the last 7 days that match the artifact's binding.
- **Minimum slice** (`DRIFT_MIN_N = 100` kept uniform labels; initial design value, tunable in Phase 0, ruling R5): the job judges the last 7 days when they hold at least 100 labels, otherwise widens the slice week by week up to 4 weeks; when even 4 weeks hold fewer, it gives no verdict and logs WARNING `calibrator drift not evaluated: too few uniform labels` (`calibrator_id`, `scope`, `n`, `min_n`, `window_days`) once per artifact (a platform artifact: only when no served tenant reached the floor). Without a floor the binned-ECE noise (§3) would downgrade almost every low-volume artifact on its first check (on calibrated 4-option slices, `P(ECE > 0.08)` is 0.98 at n = 100 and 0.83 at n = 200).
- The CI is the **basic (bias-corrected) bootstrap** interval; its lower bound is `max(0, 2·ECE − q97.5)`. With the percentile interval, every well-calibrated weekly slice under about 1,000 labels would be downgraded with `drift_ci`.
- If `ece > drift_ece_max`, or the CI lower bound exceeds `ece_max`:
  - a tenant artifact goes to `status = downgraded`;
  - **a platform artifact is downgraded globally** in Phase 1. Per-tenant suppression is deferred to Phase 2, when the drift jobs are live (06 §2.4; critic finding 58). The cost is that one tenant's drift turns the platform default off for every tenant, which is the conservative failure (R5);
  - the epoch increases, an alert fires (`aghoo_calibration_ece` gauge plus a rule), and answers serve `uncalibrated` within the registry staleness bound (≤ 5 s).
- Re-entry needs a new fit and the full gate. A downgraded artifact is never re-activated.
- Profile and model changes need no job: a new profile version means a new binding (02 L287), so old artifacts simply stop matching. They are marked `retired` when their profile version leaves the active config.
- Silent provider changes are caught only by the drift job and the capability probes (02 L848).

### 6. Phase 1 scope (honest labels)

- Implemented: the binding, lookup precedence, epoch, the per-process registry snapshot, the gate (A1–A8 with per-type ECE), two-person activation in the CLI, the drift job, application in the engine, and the W4 read-only views (IMPL-042).
- Tested on synthetic label sets, including a negative case per gate rule, the stage-rule negatives, and a drift-downgrade case.
- No artifact is fitted on live data in this build. `06 §11` "first calibrated signature" is reported **not run (needs Phase 0 data and spend)**.

## Consequences

- **Positive**: one artifact gives one auditable unit. `calibrated` can't be served outside the evidence it was accepted on. The stage rule and the subset rule need no special code. Tenant data never trains another tenant's artifact.
- **Negative**: there are many bindings. Every (stage, question, subset, view signature) combination needs its own 300+ labels, so most traffic stays `uncalibrated` for a long time, especially image turns, pair subsets and request-defined questions. That is the intended conservative outcome (R5).
- **Operational**: activation is a two-person human step (audited). The weekly drift job needs a per-tenant uniform slice, so sampling (`labeling.sample.uniform_rate`) must stay > 0 for calibrated tenants.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Pooled-only calibrators (no per-member T) | 06 §2.3 step 2 requires per-member temperature for the verbalized members. One artifact can hold both at the same cost |
| Per-profile calibrators shared across questions | Violates the per-question binding (`CalibrationBinding.question_signature`, `ce` invariant 5) |
| Fall back to a superset or subset artifact when a member is missing | Wrong distribution: the pooled transform was fitted on a different pool. 02 L371 explicitly says `uncalibrated` |
| Automatic activation on gate pass | No human check on a status that licenses probability thresholds in client automation (07 §4.3). The CLI step is cheap |
| Single-operator activation | 03 W4 specifies a second approver (03:L517, L533); the contract's `activated_by` carries both |
| Per-tenant suppression of platform artifacts in Phase 1 | Needs suppression records, per-tenant epochs and drift jobs per tenant on platform artifacts; Phase 2 (06 §2.4). Phase 1 downgrades globally |
| Isotonic or Dirichlet in Phase 1 | Needs ≥ 1–2k labels per binding (02 L373). Not before Phase 2 |
| Platform artifacts for request questions | Their signatures are tenant-specific, and the seed set doesn't cover them |

## References

02 §5.5 (L364-403), 02 §5.6 (L405-414), 06 §2.1–2.4 (L19-88), 03 §4.1 invariant 5 (L281), `contracts/types.ts` L118-178, `contracts/check_examples.py` L160-180 and L320-330, `contracts/python_sdk_types.py` L21-23 (the constants to be replaced by config), README D2/D4.
