# Wave E summary: every 06 §11 gate (QA lead, 2026-10-11; re-gated after Wave E2 and after Wave E3, 2026-10-12; release gate 2026-10-13)

**Release gate (2026-10-13; section 8).** The release QA lead re-gated HEAD `10aa2d4` (tag `wave-f`) on images
`noema-*:rgate` built from it, with 2 api processes per pod ([release-gate](release-gate.md)). Every row it re-ran
kept its status, so the tally stays **50 PASS, 1 FAIL (K1), 0 PARTIAL, 12 NOT-RUN**. The release status and evidence
of each row are in [../mvp-acceptance.md](../mvp-acceptance.md) §3. The E3 text below is kept as the E3 record.

Status of every gate of 06 §11 (design repo `06-validation-plan.md`) in spec-10 §3's ids. **Wave E3 re-gate**: HEAD
`1a28933` (tag wave-e2) plus the whole E3 working tree (perf-gc-objects, perf-cpu-shed, misc-fixes, live; last
product edit 08:36 UTC; tree digest `5e4146f4ff4cf8d7` over `src deploy tools`, unchanged through every run of the
re-gate), measured by the QA lead on fresh stacks and images (`:qae3`) built from it, 2 api processes per pod
([e3/qa-lead](e3/qa-lead.md); sizing: [../perf/sizing.md](../perf/sizing.md)). Where a gate was re-run in E3 that
run decides ("(E3)"); otherwise the E2 or Wave E measurement stands ("(E2)", "(Wave E)"). Nothing here is claimed
without a measurement in the linked report. Statuses: **PASS**, **FAIL**,
**PARTIAL** (some criteria measured and passed, others failed or not measured), **NOT-RUN** (with the reason;
**[P0]** = needs Phase 0 data, spend or AWS accounts, by design not runnable in this build; **[prod]** = production
only). Rulings R6 (a fallback to another model is not a retry), R7 (control characters, idempotency, auth before
the body cap), R8 (the retry budget counts only possibly-delivered re-sends, 10% of first sends, no floor; F12 per
failing upstream) and R9 (U+0000: U+FFFD in stored model / turn text, 400 in client-supplied structured fields) are
applied as binding, and the owner decision of 2026-10-12 (size the pods from the measured knee and report the SLO at
that density; live cap 87).

Reports: Wave E3 [e3/qa-lead](e3/qa-lead.md) (re-gate), [e3/perf-gc-objects](e3/perf-gc-objects.md),
[e3/perf-cpu-shed](e3/perf-cpu-shed.md), [e3/misc-fixes](e3/misc-fixes.md), [e3/live-smoke](e3/live-smoke.md),
[../perf/sizing.md](../perf/sizing.md). Wave E2 [e2/qa-lead](e2/qa-lead.md) (re-gate), fixers [e2/edge-fixer](e2/edge-fixer.md),
[e2/admission-estimator](e2/admission-estimator.md), [e2/resilience-fixer](e2/resilience-fixer.md),
[e2/contracts-config-mock](e2/contracts-config-mock.md), [e2/supply-chain-infra](e2/supply-chain-infra.md),
[e2/qa-harness](e2/qa-harness.md), [e2/spec](e2/spec.md). Wave E: [qa-lead](qa-lead.md) (load gates, final regression),
[perf-decision-ingress](perf-decision-ingress.md), [perf-streaming-runtime](perf-streaming-runtime.md),
[contract](contract.md), [security](security.md), [fault](fault.md), [live-smoke](live-smoke.md),
[edge-fixer](edge-fixer.md), [io-fixer](io-fixer.md), [e4-engines-fix](e4-engines-fix.md).

## 1. Headline

- Phase 1 (MVP) gates after Wave E3: **50 PASS, 1 FAIL, 0 PARTIAL, 12 NOT-RUN** over the 63 rows of section 2 (F rows
  and S1's two criteria counted one by one; a row with a [P0] half counts by its measured half). Wave E2: 44 PASS,
  4 FAIL, 3 PARTIAL, 12 NOT-RUN; Wave E: 24 / 17 / 4 / 18. All 12 NOT-RUN are [P0] / [prod] / pre-GA by design.
  Phase 0 gates: all NOT-RUN [P0] (section 3).
- The only failing row is **K1** (estimator tokens <= 2%, live-n: 14 of 25 calls within 2% with the shipped table,
  out of sample; a Phase 0 refit). **L1 passes at the sized density** (owner decision 2026-10-12: size from the knee):
  the shipped image holds p50 <= 10 / p95 <= 25 ms up to **75 streams per 2-vCPU pod** (p50 9.65 [9.63, 9.67], p95
  12.38 [12.26, 12.50]); the gate as written at 200 per pod still misses p50 (11.70 ms; p95 20.1 ms now passes).
  Recommended 08 §9: N x (2 vCPU, 4 GiB) api pods, 2 processes each, <= 75 streams per pod planned at 60 (14 pods for
  ~800 streams; 8 with the venv's faster interpreter in the image) ([../perf/sizing.md](../perf/sizing.md)).
- Fixed and measured in Wave E3: L1 at the knee (GC re-tune and object churn, SigV4 / eventstream / pool-release CPU),
  C5 (edge shed: 1.021 / 1.022, was 1.20-1.23), C3 (QL-3: queue high-water 306 B under 1 MiB), F12's residual (R8:
  Sol 1.038 per upstream at rate 0.5, was 1.149), C2's pools and datastores (R4 metrics), M2 (one per-reason test),
  and live: R5 (CR #26 quorum 2/2), L6 (0.899, 6 interleaved pairs), the `:` model id and the GLM race at low.
- Regression on the final tree: `make check` all green (twice), `make e2e` 52/52 twice, `ui-check`, `ui-e2e` 12/12
  (fixture and stack), `qa-contract` 131/131, `qa-security` all green, `qa-fault` 22/22, `qa-gates` all green,
  `test-media` 307 passed, `image-scan` exit 0, `check_docs.sh` exit 0; live budget 85 of 87.
- **Release gate (2026-10-13, `wave-f` on `:rgate`)**: every suite above green again. The first runs of
  `qa-security-sca` (PyPI unreachable) and `qa-fault` (F10's same-pod probe on 2-process pods) were red, and the
  harness was fixed. C2, C3 and C5 pass in the container, and L1 passes at the planned 100 streams per pod (p50 9.21,
  p95 12.64 ms). No product code changed and no live call was made (section 8).

## 2. Phase 1 gates (06 §11 row 1; spec-10 §3)

"(E3)" = re-run by the QA lead on the final Wave E3 tree ([e3/qa-lead](e3/qa-lead.md)); "(E2)" / "(Wave E)" = not
re-run in E3, that measurement stands. "was X" marks a status that changed in Wave E3 (section 7); E2's changes are in
section 6.

| Id | Gate (06 ref) | Status | Evidence (measured) | Blocking item / owner |
|---|---|---|---|---|
| Q1 | 100% schema-valid responses; schemathesis 0 failures on every operation (§2.4, §11) | **PASS** | (E3) `make qa-contract` 131/131: the 18 NUL-id reproducers 404 / 400, input robustness 67/67; schemathesis stateless 6,160 calls (25 operations) and stateful 1,664 calls, 0 failures (only `resumeTurnEvents` 501 x65, allowed); server-side ContractValidator 0 violations; every e2e / UI / security / fault / gate / load response validated, 0 violations; R9 probe on the e2e stack: U+0000 in session metadata 400 `schema_violation` (`param` `metadata.k` / `metadata`, create and update), in turn text stored as U+FFFD ([e3/qa-lead §4](e3/qa-lead.md)) | - (generated uploads mostly 415, no accepted extension: schemathesis coverage gap B3, contracts owner) |
| Q2 | OpenAI facade conformance 100% (§2.2) | **PASS** | (E3) `make qa-contract`: openai Python 14/14, retries on 429/503 3/3, openai npm 12/12 | - |
| Q3 | Jev conformance 100% (§2.2) | **PASS** | (E3) `make qa-contract` Jev 8/8 | - |
| Q9 | (supporting) hierarchical method (08 §3.4, IMPL-027) | **PASS** | (E3) `make qa-hier-deadline`, probe latencies, n 200 per case: `deadline_exceeded` share **1.0 at 1,000 ms, 0.0 at 3,000 ms** on lightweight-model-decide and router-default, no other outcome (nothing truncated silently); `test_hierarchical.py` and the e2e 32-option golden pass | - |
| Q4 | ensemble vs lightweight-model-decide vs cascade (§2.4 (1)-(3)) | **NOT-RUN [P0]** | needs seed-v1 and paired Phase 0 runs on Bedrock | Phase 0 |
| Q5 | first `calibrated` signature (§2.3) | **NOT-RUN [P0]** | no consensus-labeled data yet (machinery = Q6) | Phase 0 |
| Q6 | calibration machinery A1-A8, binding, drift downgrade (§2.3) | **PASS** | (E3) `test_acceptance.py`, `test_drift.py`, `test_calibration_binding.py` in make check (unit 3,447 passed) | - |
| Q7 | selection-bias AUROC (§2.2) | **NOT-RUN [P0]** | needs labeled `dropped` / kept items | Phase 0 |
| Q8 | holdback reporting (§2.2; meas) | **PASS** | (E3) `make qa-load-holdback`: `holdback_deadline` share 0 in every agree / disagree cell except where the hold cap fires before S1 (L2 900/300 and the Q8 late-disagreement cell: 1.0); stored `served_spec_differs` true in **200/200** Q8 turns, false at every `spec_agrees` commit | - |
| R1 | conditioning effect (§3.1) | **NOT-RUN [P0]** | needs D4 labelers on the ambiguous subset | Phase 0 |
| R2 | conditioning placement (§3.1, ADR-004) | **NOT-RUN [P0]** | Phase 0 task 0.6 | Phase 0 |
| R3 | effort binding non-inferiority (§3.1) | **NOT-RUN [P0]** | judged quality needs Phase 0 data | Phase 0 |
| R4 | spec restarts <= 10%, waste <= 5% (§3.1, §8; meas, gate P0) | **PASS** (meas) / **NOT-RUN [P0]** (thresholds) | (E3) `make qa-replay`: seeded replay set 40 sessions x 5 turns, restarts 46 = the script's 46, share 0.23, waste 0.40 of generation cost (synthetic agreement 0.77: reported against the P0 10% / 5%, not gated); idempotent replay 60/60 identical, 0 model calls, usage unchanged | thresholds: Phase 0 (real agreement rate) |
| R5 | verification machinery; AUROC / precision (§3.1) | **PASS** (machinery, live; was PARTIAL) / **NOT-RUN [P0]** (quality) | (E3) live (live-smoke): CR #26 verification on the GLM race turn, `turn.verified` completed, `answers_question` and `unsupported_claims` **quorum 2/2** (Haiku at max_tokens 600: 85 / 270 output tokens, `end_turn`, text present; Wave E: `no_quorum`); mock: e2e G08 in 52/52 twice ([e3/live-smoke](e3/live-smoke.md)) | quality Phase 0 |
| L1 | proxy overhead p50 <= 10 / p95 <= 25 ms at 200 streams per pod (§4); owner decision 2026-10-12: at the sized density | **PASS** (at the sized density; was FAIL) / as written at 200 per pod: **FAIL** (p50) | (E3) knee search, n >= 10,000 per valid window, upper 95% CI: **image (deployment) knee 75 streams per pod: p50 9.65 [9.63, 9.67], p95 12.38 [12.26, 12.50]** (100: p50 10.35 [10.32, 10.38] fails); venv knee 125 (9.60 / 9.65 and 13.9, two runs; 150: 10.03 [9.99, 10.07] and 10.16 fail p50); **200 per pod (venv): p50 11.70 [11.64, 11.76] FAIL, p95 20.13 [19.77, 20.54] PASS** (E2 14.76 / 49.79); full GC 64 ms every ~20 s at 200 (E2 118-130 ms every ~6 s); sizing: 08 §9 at <= 75 streams per pod ([e3/qa-lead §3.1](e3/qa-lead.md), [sizing](../perf/sizing.md)) | the 200-per-pod form: p50 = ingress floor + per-loop queueing (api, perf); image interpreter +11-13% CPU per turn (infra) |
| L2 | holdback agree <= max(T_d, TTFA) + 50 ms (§4) | **PASS** | (E3) `make qa-load-holdback`, 6 cells T_d {300, 900} x TTFA {300, 720, 2400}, n 200 each at concurrency 50, contract-validated: every p95 within its bound; tightest 300/300: 322.2 vs 350 ms and 900/720: 923.9 vs 950 ms; 3 windows flagged invalid by mock lateness met their bounds ([e3/qa-lead §4.2](e3/qa-lead.md)) | - |
| L3 | holdback disagree <= T_d + TTFT(r*) + 100 ms (§4) | **PASS** | (E3) same harness, 6 forced-restart cells: tightest 300/300: 633.6 vs 700 ms and 900/2400: 3,328.4 vs 3,400; premise 1.0 in every cell; 3 windows flagged invalid by mock lateness met their bounds | - |
| L4 | decision quorum p95 <= 1,000 ms; quorum met >= 99% (§4) | **PASS** | (E3) re-run on the mock's probe table (decision run changed in E3): n 2,381 turns, quorum reached in **2,815 of 2,817** decisions (99.9%), all within 1.0 s, 97.6% within 0.75 s; Haiku timeouts 2.5% ([e3/qa-lead §3.7](e3/qa-lead.md)) | the probe rows rest on few live samples; 06 §4's Phase 0 re-set of the deadline stays (QL-6, Phase 0) |
| L5 | DeepSeek p99 with backups <= 1.5 s (§4) | **PASS** | (E3) `make qa-load-backup-tail`, DeepSeek-only profile on the probe tail, n 5,000: p99 **929.5 ms**; every primary in the 2% tail answered by its backup | - |
| L6 | E2E <= 5% over direct Bedrock at p50, pinned (§4; live-n, gate P0) | **PASS** (live-n; was FAIL) / gate proper **NOT-RUN [P0]** | (E3) live L8, **6 interleaved pairs**: p50 e2e ratio **0.899**, first delta 1.003 (Wave E: 5 pairs, not interleaved, 1.093); gateway overhead on the first delta 5-22 ms when the router answers before Sol's first text ([e3/live-smoke §3](e3/live-smoke.md)) | real n: Phase 0 |
| L7 | first answer per route Opus / GLM p50/p95 (§4) | **NOT-RUN [P0]** | (E3) single live samples only (Opus low 1,117 ms, GLM low 397 ms; Wave E 1,078 / 358) | Phase 0 |
| C1 | 200 streams/pod with fan-out; admission counts every call, reserves the worst case, throttles before Bedrock (§5) | **PASS** | (E3) 2 processes, 2 AKIDs against ONE account-scoped mock window, rpm 600: busiest 60 s **546** Sol calls (E2 562), per minute 546, 484, 431, 413, 445, 448; **0 mock throttles** (44,272 mock requests all `ok`); overrun counter 0; 25,579 admission 429s; no fallback to the local buckets (E2: one) ([e3/qa-lead §3.5](e3/qa-lead.md)) | - (throughput cost ~440 of 600 calls/min: accepted, spec-11 §1.2) |
| C2 | ramp 0 -> 1,000; loop lag p99 < 50 ms up to 200 streams; the rest measured (§5) | **PASS** | (E3) `e3-ramp`, every window valid up to 1,000: lag p99 **2.1 / 3.4 / 4.2 / 5.1 ms** at 50 / 100 / 150 / 200 per pod; capacity 34.6-34.7 turns/s from 300 streams, 429 `queue_timeout` to 1,000 streams with 0 x 5xx (TTFT ovh p50 16.1 ms and lag p99 6.7 ms at 1,000; Wave E 276 / 48.9); **now measured (R4 metrics)**: Postgres <= 1 of 20 connections in use, acquire 0.03-0.06 ms, unit of work 2.3 -> 5.1 ms mean; Redis 5-7 of 256, acquire 0.02 ms, command 0.29 -> 0.85 ms mean ([e3/qa-lead §3.2](e3/qa-lead.md)) | - |
| C3 | slow clients -> coalescing -> `client_too_slow`, others unaffected (§5) | **PASS** (was PARTIAL) | (E3) 20 slow among 180: others' p95 ratio **0.998** [0.994, 1.002]; coalescing 15,660 frames; detection 60/60 at 30.000-30.029 s; 60/60 slow turns stored `failed` at 64.3-64.6 s; **queue high-water 1,048,270 B <= 1 MiB** (E2 1,052,270 B, QL-3 fixed), 253 events ([e3/qa-lead §3.3](e3/qa-lead.md)) | - |
| C4 | soak 24 h at 60%: memory growth <= 5%, no stuck attempts (§5) | **PASS** (1 h, shortened) | (E3) 150 streams per pod (0.6 x the ramp's 250), 1 h after 10 min warm-up: RSS **+0.76%** (698.3 -> 703.6 MiB), `aghoo_stuck_attempts_total` 0, **91,050 turns `completed`, 91,050 attempts `committed`**, 0 non-terminal, 0 errors; full GC flat over the hour; the 24 h form NOT-RUN ([e3/qa-lead §3.6](e3/qa-lead.md)) | - |
| C5 | a flooding tenant raises another's p95 TTFT by <= 10% (§5) | **PASS** (was FAIL) | (E3) flooder at 5x its concurrency limit (gated form, 2 runs): victim p95 ratio **1.021** [1.019, 1.026] and **1.022** [1.019, 1.026], 0 victim errors (E2 1.226 / 1.199); victim ingress p95 under the flood 16 ms (E2 61-70), decision p95 75-76 ms (E2 137-152): ~37.5k per-process-cap sheds per run, the unkeyed ones refused by the edge shed before the middleware stack ([e3/qa-lead §3.4](e3/qa-lead.md)) | - |
| F1 | throttling: immediate fallback, >= 99% pre-commit; no cross-tenant impact (§7) | **PASS** | (E3) `make qa-fault` 22/22 (2 workers per pod): fallback **52/52** (gap p95 5.2 ms); tenant B TTFT p95 635.1 vs 642.3 ms clean ([e3/qa-lead §4.1](e3/qa-lead.md)) | - |
| F2 | AssumeRole failure / AccessDenied: typed 503, no retry, 100% classified (§7) | **PASS** | (E3) 503 `tenant_access_misconfigured`, not retryable, 0 mock requests (3 variants x 20); F2b 0 Sol requests (no fallback); F2c members degraded, 100/100 completed | - |
| F3 | 5xx burst: circuit opens in the 30 s window, half-open, recovers (§7) | **PASS** | (E3) open 4.29 s, recovered 5.13 s after the window (E2 7.93 / 9.05 s); F3b p95-TTFT rule: open 7.83 s after the slowdown, closed 0.23 s after the restore | - |
| F4 | hang before first byte: TTFT timeout -> fallback; no turn > deadline + 1 s (§7) | **PASS** | (E3) fallback **100/100**; e2e max 11.3 s at deadline 30 s; F4b (no member left) 100/100 typed, max 5.4 s at 10 s | - |
| F5 | stall mid-stream: post-commit typed error, partial text kept (§7) | **PASS** | (E3) pre-commit fallback **100/100**; post-commit 100/100 `upstream_stalled` retryable, partial rendered and stored (20/20 checked) | - |
| F6 | exception event in ConverseStream: post-commit typed, pre-commit fallback, 100% classified (§7) | **PASS** | (E3) pre-commit fallback **60/60**; post-commit 5 types x 20 `all_routes_unavailable` retryable | - |
| F7 | reasoning-only output: nothing committed, fallback, no empty answer (§7) | **PASS** | (E3) **50/50 + 50/50** served (reasoning-only, empty text), 0 committed from GLM, 0 empty answers | - |
| F8 | malformed SSE / invalid decision JSON: member error / fallback, no 500 (§7) | **PASS** | (E3) attempt fallback **30/30 + 30/30** (CRC, truncation), 0 x 500; members get their parse codes, 100/100 completed | - |
| F9 | all decision members down: 100% of turns served, flagged (§7) | **PASS** | (E3) 200/200 served, every answer `unavailable`, reason `decision unavailable: ...` | - |
| F10 | Redis down: availability unaffected, local buckets, resume disabled (§7) | **PASS** | (E3) down and hung: 125/125 + 125/125 completed during the outages, `admission_degraded` 4, L2 bypassed; same-pod cancel 202, cross-pod cancel 500 (as specified); cross-pod attach 410 `resume_window_expired` after 2.5 s / 3.3 s (IMPL-021) | - |
| F11 | Postgres down 60 s: fail fast, ephemeral turns continue, no data loss (§7) | **PASS** | (E3) `store: true` 500 `internal_error` retryable at ingress; `store: false` turns completed through the outage (0 failures); 80/80 finalized turns stored exactly once with one usage line | - |
| F12 | retry storm: upstream amplification <= 1.1x (§7; R6, R8: per failing upstream) | **PASS** | (E3) both rows gated per failing upstream (requests to X / X's first sends, every operation; misc-fixes): **rate 0.5: Sol 1.038**, Opus 1.000, GLM 1.000; **rate 0.25: Sol 1.070**, Opus 1.000, GLM 1.000 (generation-only Sol 1.100 / 1.086; requests per turn 0.216 / 1.208); R8 budget (10% of first sends, no floor) | - (E2 residual Sol 1.149 closed by R8) |
| S1a | SLO: zero duplicated client-visible text across retries (§7) | **PASS** | (E3) asserted in every fault row before its other assertions: 0 problems; P-SSE-1 in e2e | - |
| S1b | SLO: pre-commit fallback success >= 99% (§7) | **PASS** | (E3) 100% in F1, F4-F8: 472 of 472 failed-first turns served (R6: a fallback spends no retry budget) | - |
| S2 | turn API availability 99.9% monthly (§7) | **NOT-RUN [prod]** | production SLO | - |
| K1 | estimator: tokens <= 2% (incl. reasoning), cost <= 5% (§8) | **FAIL** (tokens, live-n) / **NOT-RUN [P0]** (cost) | (E3) live, the shipped per-model table out of sample (25 answered E3 calls): **14 of 25 within 2%** (chars/4: 5 of 25); mean input error Haiku 1.1%, Opus 3.2%, GLM 6.2%, Sol 6.9%, Luna 8.5% (max 42.9% on a 59-char prompt); refit on all 71 live calls, leave-one-out 2.1-5.8% per model ([e3/live-smoke §4](e3/live-smoke.md)); the table override is wired (`NOEMA_TOKEN_ESTIMATOR_TABLE`, misc-fixes); cost needs real prices and billing | Phase 0 refit per call kind with more data per model; generation's waste estimate still chars/4 (generation owner, misc-fixes §9) |
| SE1 | AuthZ matrix: zero leaks (§9) | **PASS** | (E3) `make qa-security` (2 workers per pod): **356 cells, 356 equal to the oracle, 0 leaks**, the 18 `resumeTurnEvents` cells included; SEC-AZ-03 PASS; SEC-AZ-02/04 unit-covered | - |
| SE2 | RLS with a wrong `app.tenant_id` (§9) | **PASS** | (E3) `test_rls_all_tables.py` 2/2; `tests/integration/db/test_rls.py` and the new `test_pool_reset.py` (no `app.tenant_id` / `app.project_id` on a reused backend after commit, rollback, cancel, savepoint) in make check | - |
| SE3 | cache isolation (§9, §6) | **PASS** (L2) / **NOT-RUN [P0]** (Bedrock side) | (E3) `test_cache_isolation.py` 3/3 (other project / tenant miss; forged cross-tenant `turn_cancel` dropped and signalled, counters summed over all 4 worker processes); Bedrock half needs two AWS accounts | Phase 0 for the Bedrock half |
| SE4 | cross-account access (§9) | **PASS** (moto) / **NOT-RUN [P0]** (AWS, CloudTrail) | (E3) SEC-STS-01...17 in `qa-security-local` 72/72 | Phase 0 accounts |
| SE5 | labeler poisoning (§9) | **NOT-RUN [P0]** | `adversarial-v1` does not exist yet | Phase 0 dataset |
| SE6 | `<decision_context>` forgery (§9) | **PASS** | (E3) `test_injection.py` 11/11: 52 captured requests, 51 canaries, none unseen, 0 unescaped reserved tags | - |
| SE7 | media injection per input view (§9) | **PASS** (policy) / **NOT-RUN [P0]** (`injection_suspected` recall) | (E3) `test_media_injection.py` in `qa-security-stack` and `qa-gates`: one corpus in PNG (OCR), PDF text layer, scanned PDF, Markdown; 44 captured model requests, 0 policy violations (SEC-INJ-02/-03/-04/-06), every canary reached a model | recall: Phase 0 |
| SE8 | filenames and SSRF (§9) | **PASS** | (E3) 0 filename substrings in captured requests (SEC-INJ-03 in SE7's capture as well); remote-URL forms 400 `schema_violation` (security stack tests 31/31) | - |
| SE9 | upload fuzz: no escape, no egress (§9) | **PASS** | (E3) `qa-security-fuzz` 6/6 in 12 min 22 s (mutation corpus + atheris through nsjail; one atheris `pdf.sanitize` crash input -> jail `rejected:corrupt`, typed), contained, no egress | - |
| SE10 | prompt injection vs decisions: flip rate (§9) | **NOT-RUN [P0]** | `adversarial-v1` | Phase 0 dataset |
| SE11 | output handling, CSP (§9) | **PASS** | (E3) ui-e2e 12/12 (SEC-UI-01..03) three times (fixture, e2e stack, security stack); `test_ui_csp.py` 2/2 | - |
| SE12 | secrets in telemetry; 401s never echo (§9) | **PASS** | (E3) secrets scan (capture incl. `metrics-<replica>-<worker>.txt` of all 4 processes, 46 secret values checked), trace scan (49 spans), authn hygiene pass; gitleaks on the capture: 3,720 `aws-access-token` in the mock's request records (moto STS AKIDs) and 19 `generic-api-key` in `db.sql` (idempotency owner ids), the triaged false-positive classes; repo scan 77 hits, the 30 new ones in commit `1a28933` = moto AKIDs in committed fault results and strings in committed trivy JSON | - |
| SE13 | dependencies: no unaddressed critical CVE (§9) | **PASS** | (E3) pip-audit 0/243, npm 0/0; trivy api/worker 44 HIGH, media 4 CRITICAL + 94 HIGH, all Debian OS packages without a fix, **every id waived** with a statement and a 2027-01-09 review (`deploy/docker/.trivyignore.yaml`, 51 ids, 0 unwaived); `make image-scan` exit 0 (images `:qae3`); SBOMs (CycloneDX) 155 / 155 / 331 components | review at 2027-01-09; media to trixie once libmagic >= 5.45 is handled (media-pipeline); spec-09 §9 now names the waiver file (misc-fixes) |
| SE14 | pre-GA penetration test (§9) | **NOT-RUN** | pre-GA by definition | - |
| SE15 | (supporting) config integrity, edge caps (§9) | **PASS** | (E3) `test_edge_and_config.py` 4/4 | - |
| M1 | negotiation matrix 100% (§10) | **PASS** | (E3) MED-08 / views matrix in make check; (Wave E) live Sonnet image + PDF native | - |
| M2 | degradation reporting (§10) | **PASS** (was PARTIAL) | (E3) `tests/unit/turns/test_degradation_reporting.py` (misc-fixes; in make check, and re-run alone: 1 passed): one turn through the orchestrator with the real generation engine, MediaNegotiator and MediaBudgetPlanner; all 8 reasons with a Phase 1 producer on the right event (`preflight.completed`, each attempt's `generation.started` / `route.selected`) with the right `attempt_id` and model (a mutation check fails it without the `attempt_id`); e2e `test_files.py` (`active_content_removed`) in 52/52 | - (`policy` has no Phase 1 producer) |
| M3 | OCR CER (§10; meas) | **PASS** (meas) | (E3) `make qa-ocr-cer` in the media image (Tesseract + pypdfium2, the shipped extractor): CER **0.0** on the probe fixtures and clean renders (aggregate 0.0 <= 0.05) | - |
| M4 | decision accuracy on media turns within 3 pts (§10) | **NOT-RUN [P0]** | seed image/PDF turns | Phase 0 |
| M5 | > 20 images / > 5 PDFs / 600-page PDFs: no Bedrock limit errors (§10) | **PASS** | (E3) MED-09 tests in make check; the 64-media-part cap is a declared 400 `schema_violation` (R7) | - |
| X1 | contract tests green; check_docs; OpenAPI <-> 07 drift (§11) | **PASS** | (E3) `contracts-check` in make check; `docs-drift` 8/8, `sdk-compat` 65/65; `check_docs.sh` exit 0 | - |

## 3. Phase 0 gates (06 §11 row 0)

All **NOT-RUN [P0]**: they need the Phase 0 tenant accounts, Bedrock spend or labeled data, which this build does not
have by design (Bedrock-only, the live smoke capped at 87 calls since the owner decision of 2026-10-12, 85 used;
owner decisions D1-D6/R5).

| Gate | Status | Reason |
|---|---|---|
| Tenant accounts vended; cross-account tests passing | NOT-RUN [P0] | no tenant AWS accounts; the moto/STS form passes (SE4) |
| Labeler consensus report | NOT-RUN [P0] | needs seed-v1 labeled by both labelers (live L9 only proved both labelers parse, n = 2) |
| Member benchmark with CIs (5-question layout, backup requests) | NOT-RUN [P0] | Bedrock spend; the mock's L4 run is not a benchmark |
| Race measurement and spec-agreement rate | NOT-RUN [P0] | needs real routes and traffic |
| Conditioning placement A/B (R2) | NOT-RUN [P0] | Phase 0 task 0.6 |
| Bedrock cancellation-billing table | NOT-RUN [P0] | needs billed cancelled streams |
| Account cache-isolation test (§6) | NOT-RUN [P0] | two tenant accounts (SE3's Bedrock half) |
| Verification spikes 0.8 answered | NOT-RUN [P0] | the machinery now answers live (E3: quorum 2/2 on both questions, n = 1 turn); the spike needs Phase 0 data |
| ADRs signed off | NOT-RUN (owner) | an owner sign-off, not a test |

Phase 2 and 3 gates (economic proof, per-tenant calibrators, routing frontier, best_of_n, prefix-cache A/B, other
modalities, hedge strategies, failover drill) are out of this wave's scope and were not run.

## 4. Supporting checks (not 06 §11 rows)

| Check | Status | Evidence |
|---|---|---|
| `make check` (final tree) | **PASS** | (E3) all green at 11:32 and 18:49 UTC: ruff (916 files), mypy 327 / 590, import-linter 27 / 0, forbidden-scan, ownership (0 BAD), contracts-check, unit 3,447, conformance 757, integration 573, sdk-check TS 86 / Python 192; `make qa-ownership-all`: 0 of 1,707 tracked paths unowned ([e3/qa-lead §4](e3/qa-lead.md)) |
| `make e2e` twice (final tree, 2 workers per api pod) | **PASS** | (E3) 52/52 (155.6 s), 52/52 (153.9 s); plus 52/52 inside `qa-security` |
| `make ui-check`, `make ui-e2e` | **PASS** | (E3) tsc, vitest 75/75, build; Playwright 12/12 on the fixture API and on the e2e stack, 55 proxied responses contract-valid (+12/12 in `qa-security`) |
| `make test-media` | **PASS** | (E3) 307 passed, 8 skipped (no network in the media image) |
| `make image-scan` (infra) | **PASS** | (E3) exit 0 on `:qae3`: no unwaived CRITICAL; CycloneDX SBOMs 155 / 155 / 331 components |
| `check_docs.sh` (design repo) | **PASS** | (E3) exit 0 |
| spec-10 §5 load-run validity | **PASS** for every gated window | (E3) every L1, C2 (to 1,000 streams), C3, C5, C1, C4 window valid; L4 invalid by the scenario's design (holdback, counts unaffected); 7 of 13 holdback cells flagged invalid by the compose mock's lateness met their bounds; 1-minute loadavg < 8 in every load run (max 6.8), 0 foreign load processes |
| Overload past the knee (06 §5 ramp; C2b risk 4) | **PASS** | (E3) typed 429 `queue_timeout` + `retry-after: 1` (edge shed), 0 x 5xx up to 1,000 streams per pod, served turns' TTFT overhead p50 16.1 ms there (Wave E 276 ms) |
| Deployment: 2 api processes per 2-vCPU pod (08 §9, IMPL-087) | **PASS** | (E3) every stack ran 2 workers per api pod; 4 processes per pod measured as a diagnostic: no knee gain, +12-14% CPU per turn, ~+50% RSS ([e3/qa-lead §3.1](e3/qa-lead.md)) |
| Sizing from the knee (owner decision 2026-10-12) | **DONE** | (E3) image knee 75 streams per pod, venv 125; recommended 08 §9 `api` row and scale signals in [../perf/sizing.md](../perf/sizing.md) |
| spec-02 §4.2 / §4.3 frozen wire mapping and error classification, §14 conformance | **PASS** | (E3) conformance 757 passed in make check; the new SigV4 signer and eventstream decoder byte-equal to botocore offline (perf-cpu-shed) and live (28 / 28 requests, 4,304 / 4,304 messages) |
| Live budget (spec-10 §6, hard stop; 87 since 2026-10-12) | **PASS** | (E3) 85 of 87 ledger entries; 28 calls by the live role on the final tree |
| Live L1-L9 items | **PASS** (the items re-run in E3) | (E3) L2 Opus low and GLM low (lean stream), the `:` id call, the verified GLM race turn, L8 (6 pairs) all PASS live; L1's Wave E failure (GLM at effort `none`) was fixed in config in E2 and GLM low passes on ConverseStream and in the race; the non-streaming GLM call was not re-run; mock fidelity E3: 8 pairs, 4 identical, the differences explained (the `p` member, GLM reasoning at low, ARN ids unknown to the mock) ([e3/live-smoke §5](e3/live-smoke.md)) |
| SigV4 fix for `:` model ids against live Bedrock (io-fixer finding 1) | **PASS** (was NOT-RUN) | (E3) live: Luna through its inference-profile ARN, `%3A` / `%2F` in the path, signature equal to `SigV4Auth`, answered in 375 ms |

## 5. What blocks the Phase 1 exit (by owner; status after Wave F and the release gate)

| Owner | Items | Gates | Status (2026-10-13) |
|---|---|---|---|
| live-smoke / spec owner, Phase 0 | estimator refit per call kind with more data per model (one (cpt, overhead) line per model cannot reach 2% per call: Luna and Sol mix short adapter prompts and templates) | K1 | **open** (no live calls at the release gate) |
| infra / supply-chain | the image's CPython (official image, shared libpython) costs 11-13% CPU per turn and 0.7-1.5 ms p50 against the venv's python-build-standalone build: knee 75 vs 125 streams per pod, 14 vs 8 pods for ~800 streams | L1 sizing (cost) | **closed in Wave F** (IMPL-089: python-build-standalone CPython in the image, knee 125 per pod, 8 pods; confirmed at the release gate: L1 at 100 per pod p50 9.21 ms) |
| api, perf | L1 as written at 200 streams per pod: p50 11.7 ms = ingress floor (~8-9 ms) + per-loop queueing; any ingress-path cost lowers the knee | L1 at 200 per pod | **open** (the release ramp's 200 step: p50 11.20 ms, indicative) |
| telemetry, api | no exported streams-per-pod gauge for the 08 §9 scale signal (`TurnSlots.held` exists, not exported) | sizing / ops | **closed in Wave F** (`aghoo_turn_slots_held`; release gate: the per-pod sum equals the streams per pod, e.g. 50 + 50) |
| spec owner, integrator (docs) | stale text: GC thresholds (700, 10, 40) -> (700, 10, 100) in IMPL-087, spec-11, OWNER-CHECKPOINT, `docs/modules/integration.md` §5; the cap's place (now before authentication and the request gate: edge shed) in spec-11 §7 / spec-04 §1 step 5 / IMPL-087; spec-04 §3 "A1 starts at the same point"; spec-10 §6's 256-token live cap (now 1,024); `integration.md` lacks the `telemetry.pool_gauges` task and the 2 new variables; 08 §9 / 08:L488 patch with the sizing | docs only | **closed** in design revision 7 (Wave F) and by the release gate's docs edits (uncommitted); what is still stale is in [../mvp-acceptance.md](../mvp-acceptance.md) §7.2 |
| generation | waste estimate still chars/4 (`generation/run.py _usage_of`; exact patch in misc-fixes §9) | K1 (output side) | **closed in Wave F** (`TOKEN_ESTIMATOR_V1` in `generation/run.py`) |
| api, spec owner (release gate) | with 2 processes per pod a cancel during a Redis outage can reach the pod's other process and get 500; spec-04 §12 and spec-10 F10 still say "pod" ([release-gate §3.3](release-gate.md)) | F10 wording | **open** |
| qa-security / platform (release gate) | `make/qa-security.mk` runs pip-audit with `\|\| true`, so a network failure surfaces as `sca_gate` `FileNotFoundError` ([release-gate §3.1](release-gate.md)) | SE13 tooling | **open** |

## 6. Wave E2 delta

Re-gate by the QA lead on the final E2 tree (2026-10-11 23:38 to 2026-10-12 03:55 UTC; images `:qae2`; every stack
its own project and port base, all torn down). Every status change, with the run that decided it:

| Gate | Wave E | Wave E2 | Decided by (E2) | Fix behind it |
|---|---|---|---|---|
| Q1 | FAIL | **PASS** | `make qa-contract` x2: 18/18 NUL-id reproducers 404/400, schemathesis 7,861 calls 0 failures, 0 violations | edge finding 17 (R7: path ids 404, query 400, repo `get` None) |
| Q8 | PARTIAL | **PASS** | `make qa-load-holdback`: `served_spec_differs` 200/200, `holdback_deadline` shares | qa-harness `test_holdback.py` |
| Q9 | PARTIAL | **PASS** | `make qa-hier-deadline`: share 1.0 at 1,000 ms, 0.0 at 3,000 ms | qa-harness `test_hier_deadline.py` |
| R4 | NOT-RUN | **PASS** (meas) / [P0] | `make qa-replay`: restarts = script, idempotent replay 60/60, 0 model calls | qa-harness `test_replay.py` |
| L2 | NOT-RUN | **PASS** | 6 agree cells within max(T_d, TTFA) + 50 ms | qa-harness |
| L3 | NOT-RUN | **PASS** | 6 forced-restart cells within T_d + TTFT(r*) + 100 ms | qa-harness |
| L4 | FAIL | **PASS** | `run.sh e2-l4`: quorum 2,816 / 2,816, p95 <= 0.75 s | contracts-config-mock: probe table from the live cassettes |
| L5 | NOT-RUN | **PASS** | `make qa-load-backup-tail`: p99 941.5 ms, n 5,000 | qa-harness `test_backup_tail.py` |
| C1 | FAIL | **PASS** | `run.sh e2-c1`, account-scoped mock window: busiest 60 s 562 <= 600, 0 mock throttles, overrun 0 | admission-estimator: sliding-window `mdl:` buckets (QL-2); contracts-config-mock: `akid: "*"` (QL-4) |
| C3 | FAIL | **PARTIAL** | `run.sh e2-c3`: 60/60 slow turns `failed` at ~64 s, others 0.976; queue 1 MiB + one frame | edge #24 (QL-1: `on_close` before a bounded closing send); QL-3 open |
| C5 | PASS | **FAIL** | `run.sh e2-c5` x2: 1.226, 1.199; A/B shows the Wave E code fails too (1.155 / 1.291), E2 image once 1.063 | none (not an E2 regression; spread 1.06-1.29) |
| F1, F4, F5, F6, F7, F8 | FAIL | **PASS** | `make qa-fault` 22/22: fallback 60/60, 50/50, 100/100, 80/80, 100/100, 80/80 | resilience: R6 in `generation/walk.py` (fallbacks spend no retry budget); B1 text-less stops open GLM's breaker |
| F10 | FAIL | **PASS** | cross-pod attach waits, then 410 (2.5 s / 3.3 s) | edge #25 (IMPL-021) |
| S1b | FAIL | **PASS** | 470 / 470 failed-first turns served in F1, F4-F8 | R6 |
| SE1 | FAIL | **PASS** | `make qa-security`: 356 / 356 cells, 0 leaks | edge #22 (V(turn) before the 501) |
| SE7 | NOT-RUN | **PASS** (policy) / [P0] | 0 policy violations over 44 captured requests | qa-harness `test_media_injection.py` |
| SE13 | FAIL | **PASS** | sca_gate with the waiver file: 51 waived, 0 unwaived; `make image-scan` exit 0 | supply-chain-infra: trixie api/worker, apt upgrade, `.trivyignore.yaml`; QA lead H1 (the gate reads it) |
| M3 | NOT-RUN | **PASS** (meas) | `make qa-ocr-cer`: CER 0.0 on probe + clean fixtures | qa-harness `test_ocr_cer.py` |
| make check (supporting) | FAIL (ownership) | **PASS** | all green twice | qa-harness OWNERS.yaml rules |

Unchanged but re-measured in E2: Q2, Q3, Q6, F2, F3 (+F3b), F9, F11, F12, S1a, SE2, SE3, SE4, SE6, SE8, SE9, SE11, SE12,
SE15, M1, M5, X1 (PASS); L1 (FAIL: p95 at 200/pod 71.5 -> 49.8 ms, p50 15.2 -> 14.8 ms), R5 and M2 (PARTIAL).
Not re-run (Wave E measurement stands): C2, C4 (PASS), L6, K1 (FAIL, live-n), the live items.

Harness changes by the QA lead (each with before / after in [e2/qa-lead §2](e2/qa-lead.md)): H1 `make/qa-security.mk`
passes `deploy/docker/.trivyignore.yaml` to the SE13 gate (before: 0 waived, SE13 FAIL); H2 F12's `rate: 0.25` row
asserts R6's per-upstream amplification (before: requests per turn 1.221 > 1.1 by construction under R6; the gated
`rate: 0.5` row is unchanged); H3 the stateful schemathesis test lets accepted uploads settle and gives keyed calls
fresh keys (before: `FlakyStrategyDefinition` in 2 of 6 runs; after: 8 of 8 pass).

## 7. Wave E3 delta

Re-gate by the QA lead on the final E3 tree (2026-10-12 11:30 to 19:00 UTC; tree digest `5e4146f4ff4cf8d7`; images
`:qae3`; every stack its own project and port base, all torn down; load runs pinned, serialized, 1-minute loadavg
< 8 throughout). Every status change, with the run that decided it ([e3/qa-lead §5](e3/qa-lead.md)):

| Gate | Wave E2 | Wave E3 | Decided by (E3) | Fix behind it |
|---|---|---|---|---|
| L1 | FAIL | **PASS** at the sized density / FAIL as written (200 per pod, p50) | knee search 50-200 streams per pod, n >= 10,000 per window: image knee 75 (p50 9.65 [9.63, 9.67], p95 12.38 [12.26, 12.50]), venv knee 125 (two runs); 200 per pod p50 11.70, p95 20.13 | perf-gc-objects (task-free deadline scopes, one turn watcher, decision cycle break, untracked frames, GC (700, 10, 100)); perf-cpu-shed (SigV4 signer, eventstream decoder, no pool reset); owner decision: size from the knee |
| C3 | PARTIAL | **PASS** | `e3-c3`: queue high-water 1,048,270 B (<= 1 MiB), others 0.998 | perf-gc-objects: QL-3 frame admission in `sse_writer` |
| C5 | FAIL | **PASS** | `e3-c5` x2: 1.021, 1.022 (upper CI 1.026) | perf-cpu-shed: edge shed before the middleware stack, max-min fair share, saturated tenants |
| R5 | PARTIAL | **PASS** (machinery, live) / [P0] | live: CR #26 quorum 2/2 | live-smoke: harness cap 1,024 for decision / verification members |
| L6 | FAIL (live-n) | **PASS** (live-n) / [P0] | live: 6 interleaved pairs, p50 ratio 0.899 | live-smoke (interleaved method) |
| M2 | PARTIAL | **PASS** | `test_degradation_reporting.py` | misc-fixes |
| F12 residual | Sol 1.149 per upstream at rate 0.5 | **closed**: 1.038 (0.5), 1.070 (0.25) | `make qa-fault` 22/22, both rows gated per upstream | misc-fixes: R8 in `SlidingRetryBudget` |
| C2's unmeasured part | not measurable | **measured** | `e3-ramp` with the R4 metrics | misc-fixes: pool gauges and datastore histograms |
| SigV4 `:` ids live (supporting) | NOT-RUN | **PASS** | live: inference-profile ARN, signature equal | live-smoke |

Unchanged but re-measured in E3: Q1, Q2, Q3, Q6, Q8, Q9, R4, L2, L3, L4, L5, C1, C2, C4, F1-F11, F12, S1a, S1b,
SE1-SE4, SE6-SE9, SE11-SE13, SE15, M1, M3, M5, X1 (PASS); K1 (FAIL, improved: 14 of 25 within 2% live).

Harness changes by the QA lead in E3: none (the E3 roles' test edits are theirs: misc-fixes §8, perf-gc-objects,
perf-cpu-shed, live).

## 8. Release gate delta (2026-10-13)

The release QA lead re-gated the release tree from 2026-10-12 23:54 to 2026-10-13 03:48 UTC
([release-gate](release-gate.md)).
- **Tree and images.** HEAD `10aa2d4` = tag `wave-f`, product digest `678b7d68ced8ea33`. The images
  `noema-*:rgate` were built from that tree, and all 331 `noema/**/*.py` files in the api and media images equal
  the tree's.
- **Stacks.** Each stack had its own project, with port bases 21100-21600 and the load stack at 22000+. All were torn
  down at the end; `noema-dev` was kept.
- **No status changed.** The rows below were re-measured, and the release values are in
  [../mvp-acceptance.md](../mvp-acceptance.md) §3.

| Gate | Wave E3 | Release gate | Decided by (release gate) |
|---|---|---|---|
| L1 at the planned density | knee 75 (E3 image) | **PASS**: 100 per pod, p50 9.21 [9.18, 9.23], p95 12.64 [12.45, 12.81] ms; the Wave F knee 125 stands | `rg-l1-100` in the container |
| C2 | PASS (venv) | **PASS** in the container: lag p99 1.9 / 3.5 / 4.1 / 4.9 ms; 0 x 5xx and 0 transport errors to 1,000 | `rg-ramp` |
| C3 | PASS (venv) | **PASS** in the container: others 1.001 [0.997, 1.006]; 60/60 `client_too_slow`; 1,048,270 B | `rg-c3` (with the `--sse-probe` harness fix) |
| C5 | PASS (venv; 1 and 4 `ReadError`s) | **PASS** in the container: 1.020, 1.031; 0 flooder transport errors | `rg-c5`, `rg-c5-run2` |
| F1-F12, S1a, S1b | PASS 22/22 | **PASS** 22/22 on a fresh stack (first run 21/22, F10's probe; harness fix) | `make qa-fault`, `fault-results-final.json` |
| SE13 | PASS | **PASS** on the re-run (the first try could not reach PyPI) | `make qa-security-sca` |
| Q1, Q2, Q3, Q8, Q9, R4, L2, L3, L5, SE1-SE4, SE6-SE9, SE11, SE12, SE15, M1-M3, M5, X1, Q6 | PASS | **PASS** | `qa-contract`, `qa-gates`, `qa-security`, `make check`, `test-media` |

**Not re-run at the release gate**, so the E3 measurement stands:
- K1, L6, R5 live and the live items: these need live calls;
- L4, C1, C4: not in the release brief;
- L1 as written at 200 streams per pod. The ramp's 200 step is indicative only: p50 11.20 [11.13, 11.28] ms.

**Harness changes by the release QA lead** (before / after evidence in [release-gate §3.3, §6](release-gate.md)):
- `tests/load/run.py`: `--sse-probe` now reaches the container pod, with one probe file per process. A regression
  test was added.
- `tests/fault/test_f10_redis_down.py`: the same-pod cancel probe now retries a sibling-process 500 on fresh
  connections and asserts the owning process's 202.

**New findings:** the F10 cancel semantics with 2 processes per pod, and the `|| true` in `qa-security-sca`
(section 5). The release gate also re-confirmed that mock lateness in `qa-gates` is unchanged: 9 of 13 cells.
