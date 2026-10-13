# Noema Phase 1 MVP acceptance report (Wave F, 2026-10-12; release gate 2026-10-13)

**Basis.** Commit `10aa2d4`, tag `wave-f`. The Wave F work is committed and tagged: the api image rebuilt on a
python-build-standalone interpreter, the `aghoo_turn_slots_held` gauge, the per-model waste estimate, load-tool
fixes, observability as code, the as-built docs and design revision 7. The Wave F draft of this report was written
before that commit, so it called the work "uncommitted"; that is no longer true. The release commit (tag `phase1-mvp`) adds
on top of it:
- two test-harness fixes (`tests/fault/test_f10_redis_down.py`, `tests/load/run.py` with its test);
- the release-gate reports;
- another role's docs edits and a `tools/bootstrap_dev.py` docstring change (section 7.2).

**Where the statuses come from.** Gate statuses are the **release gate**'s:
[qa/release-gate.md](qa/release-gate.md), the release QA lead's re-gate of that tree on the images
`noema-{api,worker,media}:rgate`, built from it (product digest `678b7d68ced8ea33`). They are marked "(RG)". A row the
release gate did not re-run keeps its E3 measurement from [qa/wave-e-summary.md](qa/wave-e-summary.md) (digest
`5e4146f4ff4cf8d7`, images `:qae3`), marked "(E3)". Wave F numbers come from [perf/sizing.md](perf/sizing.md) (image
`noema-api:wfinfra`) and section 3.5.

**Statuses.** PASS, FAIL or NOT-RUN. **[P0]** = needs Phase 0 data, Bedrock spend or tenant AWS accounts, so it
cannot run in this build by design. **[prod]** = production only.

**Design sources.** Design documents are cited by number (05, 06, 08) and live in
[`../../docs/noema/`](../../docs/noema/). Owner decisions (section 4) are kept apart from orchestrator rulings and
engineering calls (section 5).

## 1. Executive summary

**What was built.** Noema 0.1.0 is a multi-tenant LLM gateway that steers chat turns with decisions. Every model
call goes to Amazon Bedrock (D1) inside the tenant's own AWS account (D2). The build covers every area of the 05
Phase 1 table:
- the turn API (SSE and JSON) with decision fan-out;
- `/v1/decisions` and its OpenAI-compatible facade, plus sessions, files and usage;
- the `bedrock-converse` and `bedrock-mantle-chat` adapters;
- the decision engine, including the hierarchical method;
- bindings, conditioning, bounded holdback and verification;
- `single`, `fallback` and `race` generation;
- the labeling and calibration machinery and the sandboxed media pipeline;
- admission, breakers, the cost meter and telemetry;
- the TypeScript and Python SDKs and the Web UI.

10 of the 12 areas are delivered and 2 are partial (section 2). It was built in waves A to F, 2026-10-09 to
2026-10-12 (`git log`), and re-gated on 2026-10-13. It was tested against a wire mock, moto STS and a live Bedrock
smoke of 85 ledger entries (`.orchestration/reports/live-calls.jsonl`: 85 lines, cap 87). The release gate made no
live call.

**Release gate (section 3.6).** Every gate that ran passed on images built from the `wave-f` tree, with 2 processes
per api pod:
- `make check`, twice;
- `make e2e` on 2 fresh stacks, 52/52 each;
- UI, contract, security (including SE13), fault (22/22), the qa-gates, media and observability suites;
- the image scan and `check_docs.sh`;
- C2, C3 and C5 in the container, and L1 at the planned 100 streams per pod.

The first runs of two suites were red. Both had a root cause outside the product: PyPI was unreachable for pip-audit, and the F10
test assumed 1 process per pod. The release QA lead fixed the test harness, and no product code changed. The
release gate did not run K1, the [P0] rows or L1 as written at 200 streams per pod. K1 and the [P0] rows need live
calls or Phase 0 data, and L1 at 200 was out of the brief.

**Gates (section 3).** The 63 Phase 1 rows of 06 §11 stand at **50 PASS, 1 FAIL, 0 PARTIAL and 12 NOT-RUN**, the
same tally as E3. The FAIL is K1. Of the 12 NOT-RUN rows, 10 are [P0], 1 is [prod] and 1 is pre-GA. All 9 Phase 0
rows are NOT-RUN.

**As written, the 06 §11 Phase 1 exit gate is not met:**
- K1 fails: 14 of 25 live calls were within 2% on estimator tokens (E3, live).
- L1 fails at 200 streams per pod: p50 is 11.70 [11.64, 11.76] ms against a bound of 10 ms (E3, venv). The release
  gate's ramp agrees in the container: 11.20 [11.13, 11.28] ms at that step. That step is a 120 s hold (n 3,454),
  not a gated L1 window.
- 12 rows were not run.

L1 passes at the sized density, which the owner chose on 2026-10-12:
- The knee of the Wave F image in the container is 125 streams per pod: p50 9.72 [9.69, 9.75] and 9.73 [9.71, 9.77]
  ms, p95 14.06 and 13.98 ms ([perf/sizing.md](perf/sizing.md) §2.1).
- On the release image, the release gate re-measured the planned density of 100 streams per pod: p50 9.21 [9.18, 9.23] ms, p95 12.64
  [12.45, 12.81] ms (RG).

**Verdict (recommendation; the acceptance decision is the owner's).** Accept tag `wave-f`, with the release gate's
harness fixes, as the Phase 1 engineering baseline that Phase 0 runs against. It is not a production release.
- **Condition 1 of the previous draft is met.** It asked for a re-gate of the Wave F tree on its own images, and
  the release gate did that.
- Three conditions remain:
  1. **The owner reviews the items in section 4.1**: the 8-pod sizing, the 429-before-401 trade-off and the
     as-built object storage, which touches D2.
  2. **Phase 0 must run the [P0] rows and the K1 refit** (section 8).
  3. **The knee must be re-measured on the target instance type, with shared datastores, before N is fixed**
     ([perf/sizing.md](perf/sizing.md) §7 risks 2-4).
- **Housekeeping.** Done: the release gate's changes are committed and tagged `phase1-mvp`.

**Top risks (each measured):**

| # | Risk | Measured | Source |
|---|---|---|---|
| 1 | Token estimates are off: admission reservations and cost estimates rest on them | K1: 14 of 25 live calls within 2%. Mean input error per model: Luna 8.5%, Sol 6.9%, GLM 6.2%, Opus 3.2%, Haiku 1.1%; max 42.9% | [qa/e3/live-smoke.md](qa/e3/live-smoke.md) §4 |
| 2 | The product's value is unproven | Decision quality, calibration, conditioning and injection robustness have no data: Q4, Q5, Q7, R1-R3, SE5, SE10 and M4 are [P0]. Live traffic is served `uncalibrated` | summary §2; [../docs/modules/labeling.md](../docs/modules/labeling.md) |
| 3 | Sizing rests on one pod with dedicated cores, dedicated datastores and a plain-HTTP mock | knee 125 per pod; the p50 floor is 8.74 of the 10 ms budget at 75 per pod, +0.4-0.6 ms per 25 streams | [perf/sizing.md](perf/sizing.md) §2.1, §7 |
| 4 | Tenancy is untested on real AWS | STS was tested on moto only (SE4). The Bedrock half of cache isolation was not run (SE3). The SigV4 session-token branch was verified offline only | summary SE3, SE4; [qa/e3/live-smoke.md](qa/e3/live-smoke.md) §6 |
| 5 | Supply chain | 51 waived vulnerability ids (review 2027-01-09). The interpreter's statically bundled OpenSSL, SQLite, zlib and other libraries are invisible to trivy | `deploy/docker/.trivyignore.yaml`; [perf/sizing.md](perf/sizing.md) §7 risk 6 |
| 6 | Operations signals are proxies | There is no gateway-overhead histogram: the overhead alerts use a mean proxy that includes the designed holdback wait (live E3: 282 and 407 ms in 2 of 6 turns), so the burn alert is a warning, not a page. Counters appear only after their first add, so the "any increase" alerts need a first-sample clause. The rules ran against a compose stack only, not a cluster | [../docs/operations/observability.md](../docs/operations/observability.md) §6; [qa/wave-f/prometheus-e2e.json](qa/wave-f/prometheus-e2e.json) |

## 2. Scope delivered vs the 05 Phase 1 table

Each row of the 05 Phase 1 "In scope" table
([`05-implementation-plan.md`](../../docs/noema/05-implementation-plan.md)), with its status and evidence. "Gate"
ids are the rows of section 3. "Delivered" means built and gated on the mock or moto. The real-AWS parts are
listed under section 7.

| Area | Status | Evidence (code, test, report) | Gap |
|---|---|---|---|
| API | **Delivered** | `src/noema/api/routes/` (turns, decisions, openai_compat, profiles, sessions, files, usage, health, admin_readonly). Q1: schemathesis over 25 operations, 0 failures. Q2 facade 14/14 + 12/12; Q3 Jev 8/8. SDK wire types generated from OpenAPI (`sdk/typescript`, `sdk/python`); `sdk-check` TS 86 / Python 192 (RG, section 3.6) | Resume (`GET /v1/turns/{id}/events`) is 501 and the generation facades are absent, both Phase 2 per 05 |
| Tenancy | **Delivered** (moto) | One cached STS session per (pod, tenant) (`src/noema/identity/sts.py`, IMPL-007). Quota buckets per (tenant account, model) (`src/noema/ops/admission/`). C1: 0 mock throttles. F2: typed 503 `tenant_access_misconfigured`, fails closed. SE4 72/72 on moto | No tenant AWS accounts, so SE4 on AWS is [P0] |
| Adapters | **Delivered** | `src/noema/adapters/converse.py` (Converse / ConverseStream, effort, `textFormat`, `reasoningContent` filtering) and `mantle.py` (logprobs). The probe matrix is checked at startup and in tests (`PROBED`, `probe_discrepancies`; [../docs/modules/adapters.md](../docs/modules/adapters.md)). Conformance 757. Live: 28/28 signatures and 4,304/4,304 eventstream messages equal to botocore (summary §4) | SigV4 session-token branch not verified live (section 7) |
| Decision engine | **Delivered** (machinery) | `src/noema/decision/` (profiles, flat and hierarchical, jev, facade, escalation, cache, confidence, views, members). Q9 hierarchical: `deadline_exceeded` 1.0 at 1,000 ms, 0.0 at 3,000 ms. L4 quorum 2,815/2,817 (E3). L5 DeepSeek p99 927.5 ms with backups (RG). SE3 L2 cache isolation 3/3. Early commit is off and enforced (`commit_on_quorum` false, `src/noema/config/rules.py`) | Quality vs `lightweight-model-decide` and cascade (Q4) is [P0]. No escalation inside turns (owner answer 2) |
| Decisions → generation | **Delivered** | `src/noema/turns/bindings.py`, `conditioning.py`, `spec.py`. R4: restarts equal the script's 46, idempotent replay 60/60. Q8 `served_spec_differs` 200/200. R5: verification quorum 2/2 live, `turn.verified` | Conditioning placement uses the ADR-004 default (IMPL-082). Its A/B (R2, task 0.6) is [P0] |
| Generation profiles | **Delivered** | `src/noema/generation/engine.py` `RUNNABLE_MODES` = single, fallback, race. `best_of_n` / `fuse` are refused by config rule (`OUT_OF_PHASE_MODES`). Fallback 100% in F1, F4-F8 (RG: 493/493). GLM race turn verified live (summary §4) | - |
| Labeling + calibration | **Partial** | `src/noema/labeling/`. `labeling.item` and `labeling.run` are per-tenant jobs; `labeling.drift` is a weekly platform job ([../docs/modules/labeling.md](../docs/modules/labeling.md)); the example config sets `runs_in: tenant_account`. Per-member and pooled temperature/Platt calibrators; gate A1-A8; drift downgrade; two-person activation (IMPL-072). Q6 PASS (`tests/unit/labeling/test_acceptance.py`, `test_drift.py`, `tests/integration/decision/test_calibration_binding.py`) | **No platform-default calibrator**: seed-v1 does not exist (Phase 0 task 0.2), Q5 is [P0], and no `noema-admin` subcommand imports a platform artifact ([../docs/modules/labeling.md](../docs/modules/labeling.md), known limits). Every answer is served `uncalibrated`. In-account labeling was not exercised (no tenant accounts) |
| Media | **Delivered** | `src/noema/media/`: nsjail sandbox; Magika + libmagic (`extractors/detect.py`); HEIC → PNG (`intake.py`); OCR; PDF → `pdf_sanitized` + Docling Markdown (`extractors/pdf_markdown.py`) + page images; Markdown sanitization; budget planner (`budget.py`). M1, M2, M3 (CER 0.0), M5 PASS. SE9 fuzz 6/6. `make test-media` 307 passed (RG) | OCR page chunking is not implemented: a scanned 600-page PDF may end `ocr_text` `failed{timeout}` ([../docs/modules/media_pipeline.md](../docs/modules/media_pipeline.md)). M4 is [P0] |
| Security | **Delivered** | API keys hashed with HMAC-SHA256 and a pepper (`src/noema/identity/apikeys.py`, IMPL-008). Tenant/project model with RLS (SE2). ExternalIds (`identity/sts.py`). Redaction (`src/noema/telemetry/redact.py`, SE12). Spotlighting (IMPL-086, SE7). Decision-context escaping (SE6 11/11). Sanitized UI rendering and CSP (SE11). SE1: 356/356 authz cells, 0 leaks | SE5 and SE10 are [P0]; SE14 (pen test) is pre-GA |
| Ops | **Delivered** | GCRA limits that count decision calls (`src/noema/ops/admission/gcra.py`, C1). Per-route timeouts (`adapters/resilience/timeouts.py`, F4). Breakers (`breakers.py`, F3). OTel GenAI names through `src/noema/telemetry/genai.py` (pinned `opentelemetry-semantic-conventions` 0.66b1). Price catalog (IMPL-019) and cost meter with the line names decision, generation, labeling, verification, waste and cache_savings (`src/noema/core/values.py`). Wave F adds 25 alerts, 19 recording rules and 7 dashboards (`deploy/observability/`) | Real prices are [P0]. No real Bedrock prices are in the repo: the placeholder catalog prices every call at 0 with `cost_estimated: true` (`src/noema/ops/prices/placeholder.py`), and the dev stack imports fake dev prices (`tools/bootstrap_dev.py`). K1 cost is [P0]. The correspondence to GenAI semconv v1.41.0 is not checked here |
| Web UI | **Delivered** | `web/src/`: chat + inspector (W1), files (W2), calibration (W4, read-only), usage (W7). `make ui-e2e` 12/12 (W1, W2, W4, W7 + SEC-UI-01..03); vitest 75/75 (summary §4) | W4 and W7 have no paging (100 / 1,000 rows; [../docs/modules/ui.md](../docs/modules/ui.md)) |
| Docs | **Partial** | The CI drift check exists and passes: X1, `docs-drift` 8/8, `check_docs.sh` exit 0 (RG, twice). Design revision 7 "as built" (08 §14), synced to the Wave F sizing (125 / 100 / 8 pods) by the final docs fixer. The four Wave F drift places were fixed during the release gate (committed with tag `phase1-mvp`, not reviewed by QA). Fixed at release: the "not re-gated" text in IMPL-089 and OWNER-CHECKPOINT F2, the 08 revision line, the 08 §8 retention bullet, the `DEFAULT_MAX_TURNS` comment | Remaining drift: the undocumented platform-role S3 / KMS permissions (an IaC item, IMPL-091). Details in section 7.2 |

**Out of scope (05), as built (per the IMPL records).**
- `best_of_n` / `fuse` are config-rejected (`OUT_OF_PHASE_MODES`).
- `speculative` / `hedge` return 422 `feature_not_available` (IMPL-036).
- Audio, video, SVG and DOCX uploads return 415 before storage (IMPL-062).
- The response cache is a recorded no-op (IMPL-018).
- `x-aghoo-end-user` returns 422 (IMPL-033).
- Resume returns 501 (IMPL-021).
- The UI uses API keys through a dev cookie proxy ([../docs/modules/ui.md](../docs/modules/ui.md)).

## 3. Acceptance gates (06 §11, with the §4, §5, §7, §9 and §10 rows)

Every status below is the release status. "(RG)" marks a row re-run by the release gate on the `:rgate` images
([qa/release-gate.md](qa/release-gate.md), section 3.6). In that case the evidence is the release gate's. Every
other row keeps its E3 measurement and is marked "(E3)": it needed live calls or Phase 0 data, or it was not in the
release brief. That applies to K1, L4, L6, R5 live, C1 and C4. Statuses and the counting rule follow
[qa/wave-e-summary.md](qa/wave-e-summary.md) §2 and §3, and nothing is softened. A row with a [P0] half is counted by
its measured half. The release gate changed no status. The Wave F measurements are in section 3.5.

### 3.1 Tally

| Scope | PASS | FAIL | PARTIAL | NOT-RUN | Source |
|---|---|---|---|---|---|
| Phase 1, 63 rows (each F row and both S1 criteria counted), release gate | **50** | **1** (K1) | 0 | **12**: 10 [P0] (Q4, Q5, Q7, R1, R2, R3, L7, SE5, SE10, M4), 1 [prod] (S2), 1 pre-GA (SE14) | [qa/release-gate.md](qa/release-gate.md) §1; summary §1, §8 |
| Phase 0, 9 rows | 0 | 0 | 0 | 9 (8 [P0], 1 owner sign-off) | summary §3 |
| History: Wave E / Wave E2 / Wave E3 | 24 / 44 / 50 | 17 / 4 / 1 | 4 / 3 / 0 | 18 / 12 / 12 | summary §1 |

### 3.2 Phase 1 rows: decision quality, routing, latency, load (06 §2-§5)

| Id | Gate (06 ref) | Status | Evidence (measured) | Detail |
|---|---|---|---|---|
| Q1 | 100% schema-valid; schemathesis 0 failures (§2.4, §11) | **PASS** | (RG) `qa-contract` 131 passed; schemathesis stateless 6,160 calls over 25 operations, stateful 1,735 calls over 19 operations, 0 failures (the only 5xx: `resumeTurnEvents` 501, allowed); server-side ContractValidator 0 violations | [release-gate §3](qa/release-gate.md) |
| Q2 | OpenAI facade conformance 100% (§2.2) | **PASS** | (RG) openai Python 14/14, retries 3/3, openai npm 12/12 | same |
| Q3 | Jev conformance 100% (§2.2) | **PASS** | (RG) Jev 8/8 | same |
| Q9 | (supporting) hierarchical method (08 §3.4) | **PASS** | (RG) `qa-hier-deadline`, n 200 per case: `deadline_exceeded` 1.0 at 1,000 ms, 0.0 at 3,000 ms; no other outcome | [release-gate §3.4](qa/release-gate.md) |
| Q4 | ensemble vs `lightweight-model-decide` vs cascade (§2.4) | **NOT-RUN [P0]** | needs seed-v1 and paired runs on Bedrock | - |
| Q5 | first `calibrated` signature (§2.3) | **NOT-RUN [P0]** | no consensus-labeled data | - |
| Q6 | calibration machinery A1-A8, binding, drift (§2.3) | **PASS** | (RG) `test_acceptance.py`, `test_drift.py`, `test_calibration_binding.py` in `make check` (all green twice; unit 3,588, integration 573) | [release-gate §3, §5](qa/release-gate.md) |
| Q7 | selection-bias AUROC (§2.2) | **NOT-RUN [P0]** | needs labeled `dropped` / kept items | - |
| Q8 | holdback reporting (§2.2) | **PASS** | (RG) `qa-load-holdback`: `holdback_deadline` share 0 except where the hold cap fires before S1; `served_spec_differs` 200/200 | [release-gate §3.4](qa/release-gate.md) |
| R1 | conditioning effect (§3.1) | **NOT-RUN [P0]** | needs D4 labelers on the ambiguous subset | - |
| R2 | conditioning placement (§3.1, ADR-004) | **NOT-RUN [P0]** | Phase 0 task 0.6 | - |
| R3 | effort binding non-inferiority (§3.1) | **NOT-RUN [P0]** | judged quality needs Phase 0 data | - |
| R4 | spec restarts <= 10%, waste <= 5% (§3.1, §8) | **PASS** (meas) / **NOT-RUN [P0]** (thresholds) | (RG) `qa-replay`: restarts 46 = the script's 46, share 0.23, waste 0.401 at a synthetic agreement of 0.77 (reported, not gated); replay 60/60 identical, 0 model calls | [release-gate §3.4](qa/release-gate.md) |
| R5 | verification machinery; AUROC / precision (§3.1) | **PASS** (machinery, live) / **NOT-RUN [P0]** (quality) | (E3) live: CR #26 `turn.verified`, quorum 2/2 on both questions. (RG) mock: e2e G08 in 52/52 on 2 fresh stacks | [e3/live-smoke](qa/e3/live-smoke.md); [release-gate §3](qa/release-gate.md) |
| L1 | overhead p50 <= 10 / p95 <= 25 ms at 200 streams per pod (§4) | **PASS** at the sized density / **FAIL** as written (p50) | (RG) container, release image, the planned 100 per pod: p50 9.21 [9.18, 9.23], p95 12.64 [12.45, 12.81], n 10,696, valid; the Wave F knee of 125 stands (section 3.5). As written at 200 per pod: (E3, venv) p50 **11.70** [11.64, 11.76] FAIL, p95 20.13 PASS; (RG) the ramp's 200 step reads 11.20 [11.13, 11.28], a 120 s hold, not a gated window | [release-gate §4.1, §4.2](qa/release-gate.md), [perf/sizing.md](perf/sizing.md) |
| L2 | holdback agree <= max(T_d, TTFA) + 50 ms (§4) | **PASS** | (RG) 6 cells, n 200 each: tightest 326.4 vs 350 ms, 920.3 vs 950 ms. 9 of 13 holdback cells flagged invalid by mock lateness, each within its bound (section 7.1 item 10) | [release-gate §3.4](qa/release-gate.md) |
| L3 | holdback disagree <= T_d + TTFT(r*) + 100 ms (§4) | **PASS** | (RG) 6 forced-restart cells, premise 1.0: tightest 1,650.0 vs 1,720 ms (900/720), 3,323.0 vs 3,400 ms (900/2400) | same |
| L4 | decision quorum p95 <= 1,000 ms; quorum >= 99% (§4) | **PASS** | (E3; not in the release brief) n 2,381 turns: quorum 2,815 of 2,817 (99.9%), all within 1.0 s | [e3/qa-lead §3.7](qa/e3/qa-lead.md) |
| L5 | DeepSeek p99 with backups <= 1.5 s (§4) | **PASS** | (RG) `qa-load-backup-tail`, n 5,000: p99 927.5 ms, p95 609.8 ms, valid | [release-gate §3.4](qa/release-gate.md) |
| L6 | E2E <= 5% over direct Bedrock at p50 (§4) | **PASS** (live-n) / **NOT-RUN [P0]** (gate proper) | (E3, live; no live calls at the release gate) 6 interleaved pairs: p50 ratio 0.899 | [e3/live-smoke §3](qa/e3/live-smoke.md) |
| L7 | first answer per route p50/p95 (§4) | **NOT-RUN [P0]** | single live samples only (Opus low 1,117 ms, GLM low 397 ms) | - |
| C1 | 200 streams/pod with fan-out; admission throttles before Bedrock (§5) | **PASS** | (E3; not in the release brief) busiest 60 s: 546 Sol calls against rpm 600; 0 mock throttles in 44,272 requests; overrun 0 | [e3/qa-lead §3.5](qa/e3/qa-lead.md) |
| C2 | ramp 0 -> 1,000; loop lag p99 < 50 ms to 200 streams (§5) | **PASS** | (RG) container, `noema-api:rgate`, 2 processes: lag p99 1.9 / 3.5 / 4.1 / 4.9 ms at 50-200 per pod; every window valid to 1,000; capacity 34.5-34.7 turns/s; 429 `queue_timeout` past the cap, 0 x 5xx and 0 transport errors; Postgres <= 2 of 20 connections, Redis 5-7 of 256 | [release-gate §4.2](qa/release-gate.md) |
| C3 | slow clients -> `client_too_slow`, others unaffected (§5) | **PASS** | (RG) container: others' p95 ratio 1.001 [0.997, 1.006]; 60/60 `client_too_slow` at 30.000-30.059 s; 60/60 stored `failed`; queue high-water 253 events, 1,048,270 B <= 1 MiB | [release-gate §4.4](qa/release-gate.md) |
| C4 | soak 24 h at 60%: memory <= +5%, no stuck attempts (§5) | **PASS** (1 h, shortened) | (E3; not in the release brief) 150 per pod for 1 h: RSS +0.76%, 91,050 turns completed, 0 stuck. **The 24 h form was not run** | [e3/qa-lead §3.6](qa/e3/qa-lead.md) |
| C5 | flooding tenant raises another's p95 TTFT <= 10% (§5) | **PASS** | (RG) container, 2 runs: victim p95 ratio 1.020 [1.017, 1.022] and 1.031 [1.026, 1.036]; 0 victim errors; 0 flooder transport errors (E3: 1 and 4 `ReadError`s) | [release-gate §4.3](qa/release-gate.md) |

### 3.3 Phase 1 rows: reliability, SLO, estimator, security, media, contracts (06 §7-§11)

Fault rows: (RG) the final `make qa-fault` passed 22/22 on a fresh stack, with 2 workers per api pod
([release-gate §5](qa/release-gate.md), `reports/qa/release-gate/fault-results-final.json`). The first run passed
21/22. F10's own same-pod cancel probe failed, because the test assumed 1 process per pod. Every spec-10 F10
criterion passed in that run too, and the harness was fixed ([release-gate §3.3](qa/release-gate.md)).

| Id | Gate (06 ref) | Status | Evidence (measured) |
|---|---|---|---|
| F1 | throttling: immediate fallback, >= 99% pre-commit; no cross-tenant impact (§7) | **PASS** | (RG) fallback 53/53, gap p95 45.4 ms; tenant B TTFT p95 633.4 ms under the fault and 633.4 ms clean |
| F2 | AssumeRole failure: typed 503, no retry (§7) | **PASS** | (RG) 503 `tenant_access_misconfigured`, not retryable, 0 mock requests (3 variants x 20); F2b 0 Sol requests; F2c members degraded, 100/100 completed |
| F3 | 5xx burst: circuit opens in 30 s, half-open, recovers (§7) | **PASS** | (RG) open 3.85 s, recovered 4.94 s after the window; F3b TTFT rule open 8.09 s, closed 0.06 s |
| F4 | hang before first byte: fallback; no turn > deadline + 1 s (§7) | **PASS** | (RG) fallback 100/100; max 11.17 s at deadline 30 s; F4b 100/100 typed, max 5.35 s at 10 s |
| F5 | stall mid-stream: post-commit typed error, partial kept (§7) | **PASS** | (RG) pre-commit fallback 100/100; post-commit 100/100 `upstream_stalled`, retryable, partial stored (20/20 checked) |
| F6 | stream exception event: typed, fallback, 100% classified (§7) | **PASS** | (RG) pre-commit 60/60; post-commit 5 types x 20 `all_routes_unavailable`, retryable |
| F7 | reasoning-only output: nothing committed, fallback (§7) | **PASS** | (RG) 50/50 + 50/50 served, 0 committed from GLM, 0 empty answers |
| F8 | malformed SSE / decision JSON: no 500 (§7) | **PASS** | (RG) attempt fallback 40/40 + 40/40 (CRC, truncation), 100/100 status 200; members get `upstream_error` / `parse_error` / `missing_options`, 100% completed |
| F9 | all decision members down: 100% served, flagged (§7) | **PASS** | (RG) 200/200 served, every answer (1,000) `unavailable` |
| F10 | Redis down: availability unaffected (§7) | **PASS** | (RG) down and hung: 125/125 + 125/125 completed during the outage, 40 + 40 after; cross-pod cancel 500 (as specified); cross-pod attach 410 after 2.5 / 3.3 s. A same-pod cancel can reach the pod's other process and get 500 (`500,500,202` once in the final run; section 7.1 item 12) |
| F11 | Postgres down 60 s: fail fast, ephemeral continue, no loss (§7) | **PASS** | (RG) `store: true` 500 `internal_error`, retryable; `store: false` 0 failures; 80/80 stored exactly once, 0 missing |
| F12 | retry storm: amplification <= 1.1x per failing upstream (§7; R6, R8) | **PASS** | (RG) rate 0.5: Sol 1.034; rate 0.25: Sol 1.042; Opus and GLM 1.000 |
| S1a | zero duplicated client-visible text (§7) | **PASS** | (RG) asserted in every fault row: 0 problems |
| S1b | pre-commit fallback success >= 99% (§7) | **PASS** | (RG) 493 of 493 failed-first turns served |
| S2 | availability 99.9% monthly (§7) | **NOT-RUN [prod]** | production SLO |
| K1 | estimator: tokens <= 2%, cost <= 5% (§8) | **FAIL** (tokens, live-n) / **NOT-RUN [P0]** (cost) | (E3, live; not re-run: needs live calls) shipped table out of sample: **14 of 25 within 2%** (chars/4: 5 of 25); refit leave-one-out 2.1-5.8% per model ([e3/live-smoke §4](qa/e3/live-smoke.md)) |
| SE1 | AuthZ matrix: zero leaks (§9) | **PASS** | (RG) 356 cells, 356 equal to the oracle, 0 leaks (the 18 `resumeTurnEvents` cells included) |
| SE2 | RLS with a wrong `app.tenant_id` (§9) | **PASS** | (RG) `test_rls_all_tables.py` 2/2 (24 tables); `tests/integration/db/test_rls.py`, `test_pool_reset.py` in `make check` |
| SE3 | cache isolation (§9, §6) | **PASS** (L2) / **NOT-RUN [P0]** (Bedrock) | (RG) `test_cache_isolation.py` 3/3; the Bedrock half needs two AWS accounts |
| SE4 | cross-account access (§9) | **PASS** (moto) / **NOT-RUN [P0]** (AWS, CloudTrail) | (RG) SEC-STS-01..17 in `qa-security-local` 72/72 |
| SE5 | labeler poisoning (§9) | **NOT-RUN [P0]** | `adversarial-v1` does not exist |
| SE6 | `<decision_context>` forgery (§9) | **PASS** | (RG) `test_injection.py` 11/11: 52 requests, 51 canaries, none unseen |
| SE7 | media injection per input view (§9) | **PASS** (policy) / **NOT-RUN [P0]** (recall) | (RG) 44 captured model requests, 0 policy violations (in `qa-security` and `qa-gates`) |
| SE8 | filenames and SSRF (§9) | **PASS** | (RG) `tests/security/stack` 31/31, which includes the filename check and the remote-URL 400 cases (`test_injection.py`) |
| SE9 | upload fuzz: no escape, no egress (§9) | **PASS** | (RG) `qa-security-fuzz` 6/6 in the media image: `escaped: []`, every egress family `EAFNOSUPPORT`; one atheris `pdf.sanitize` input -> `rejected:corrupt`, typed |
| SE10 | prompt injection vs decisions (§9) | **NOT-RUN [P0]** | `adversarial-v1` |
| SE11 | output handling, CSP (§9) | **PASS** | (RG) ui-e2e 12/12 three times (fixture API, e2e stack, security stack); `test_ui_csp.py` 2/2 |
| SE12 | secrets in telemetry; 401s never echo (§9) | **PASS** | (RG) secrets scan (46 values, all 4 api processes' metrics), trace scan (49 spans); gitleaks hits are the triaged false-positive classes, no real secret |
| SE13 | no unaddressed critical CVE (§9) | **PASS** (re-run; the first try failed because PyPI was unreachable) | (RG) pip-audit 243 packages / 0 vulnerable, npm 0/0; trivy 0 unwaived ids (51 waived); SBOMs 148 / 148 / 331; `make image-scan` exit 0, no unwaived CRITICAL |
| SE14 | pre-GA penetration test (§9) | **NOT-RUN** | pre-GA by definition |
| SE15 | (supporting) config integrity, edge caps (§9) | **PASS** | (RG) `test_edge_and_config.py` 4/4 |
| M1 | negotiation matrix 100% (§10) | **PASS** | (RG) MED-08 in `make check`; (Wave E) live Sonnet image + PDF native |
| M2 | degradation reporting (§10) | **PASS** | (RG) `tests/unit/turns/test_degradation_reporting.py` in `make check`: all 8 reasons with a Phase 1 producer, on the right event and `attempt_id` |
| M3 | OCR CER (§10) | **PASS** (meas) | (RG) `qa-ocr-cer` in `noema-media-test:rgate`: CER 0.0 |
| M4 | decision accuracy on media turns within 3 pts (§10) | **NOT-RUN [P0]** | seed image/PDF turns |
| M5 | > 20 images / > 5 PDFs / 600 pages: no Bedrock limit errors (§10) | **PASS** | (RG) MED-09 in `make check`; the 64-media-part cap is a declared 400 (R7) |
| X1 | contract tests; `check_docs`; OpenAPI <-> 07 drift (§11) | **PASS** | (RG) `contracts-check` (70 files identical) in `make check`, `docs-drift` 8/8, `sdk-compat` 65/65, `check_docs.sh` exit 0 twice |

### 3.4 Phase 0 rows (06 §11 row 0) and supporting checks

| Phase 0 gate | Status | Reason (summary §3) |
|---|---|---|
| Tenant accounts vended; cross-account tests | NOT-RUN [P0] | no tenant AWS accounts; the moto form passes (SE4) |
| Labeler consensus report | NOT-RUN [P0] | needs seed-v1 labeled by both labelers |
| Member benchmark with CIs | NOT-RUN [P0] | Bedrock spend |
| Race measurement and spec-agreement rate | NOT-RUN [P0] | needs real routes and traffic |
| Conditioning placement A/B | NOT-RUN [P0] | Phase 0 task 0.6 |
| Bedrock cancellation-billing table | NOT-RUN [P0] | needs billed cancelled streams |
| Account cache-isolation test (§6) | NOT-RUN [P0] | two tenant accounts |
| Verification spikes 0.8 answered | NOT-RUN [P0] | the machinery answers live (n = 1 turn); the spike needs data |
| ADRs signed off | NOT-RUN (owner) | an owner sign-off, not a test |

Supporting checks at the release gate (RG; section 3.6):
- `make check` all green twice;
- `make e2e` 52/52 on 2 fresh stacks;
- `ui-check` and `ui-e2e` 12/12, twice;
- `test-media` 307 passed;
- `image-scan` exit 0;
- every gated load window valid (spec-10 §5);
- overload past the knee: typed 429, 0 x 5xx and 0 transport errors to 1,000 streams per pod, in the container;
- 2 api processes per pod everywhere;
- the frozen wire mapping, 757 conformance tests;
- the live budget untouched at 85 of 87.

### 3.5 Wave F measurements and re-runs (working tree, 2026-10-12; history)

This is a record of Wave F. The release gate (section 3.6) re-ran these checks on the committed tree, and its
results decide the statuses above.

**L1 in the container on the rebuilt image** (`noema-api:wfinfra`, CPython 3.12.15 from python-build-standalone):
- 2 api processes on CPUs 46-47, pinned, on a quiet host;
- n >= 10,000 per window, every window valid;
- results in `reports/perf/wf-infra/load/wf-l1-{a,b,c}.json` ([perf/sizing.md](perf/sizing.md) §2.1, §6).

| Streams per pod | Run | TTFT overhead p50 [95% CI], ms | p95 [95% CI], ms | Pod CPU (vCPU) | CPU per turn, ms | L1 |
|---|---|---|---|---|---|---|
| 75 | c | 8.74 [8.72, 8.76] | 11.40 [11.27, 11.54] | 0.467 | 43.1 | PASS |
| 100 (planned) | a | 9.30 [9.27, 9.32] | 13.20 [13.02, 13.43] | 0.617 | 42.7 | PASS |
| **125 (knee)** | b / c | **9.72 [9.69, 9.75] / 9.73 [9.71, 9.77]** | 14.06 [13.88, 14.23] / 13.98 [13.83, 14.12] | 0.750 / 0.753 | 41.6 / 41.7 | **PASS** |
| 150 | b | 10.21 [10.18, **10.26**] | 15.41 [15.22, 15.62] | 0.894 | 41.4 | FAIL (p50) |

What these runs show:
- **The knee rose from 75 (E3 image) to 125 streams per pod.** CPU per turn is 9-11% lower than on the E3 image and
  within 2% of the venv.
- **`aghoo_turn_slots_held`, summed per pod, equalled the streams per pod in every step**, e.g. 59 + 66 = 125
  ([perf/sizing.md](perf/sizing.md) §2.1; the step-end scrapes are in
  `reports/perf/wf-infra/load/wf-l1-{a,b,c}/metrics-step*.txt`, the loadavg monitors in
  `reports/perf/wf-infra/load/<run>.monitor`, the runner in `reports/perf/wf-infra/load/run.sh`).
- **The L1 form at 200 per pod was not run on this image.**
- **Not re-run in the container:** C2 ramp, C3, C4 and C5. They stand from the E3 venv, which this image matches on
  CPU and p50 (sizing §6).

**Other Wave F evidence.**

| Check | Result | Source |
|---|---|---|
| SE13 image half: trivy on `:wfinfra` | api and worker: 0 CRITICAL, 43 HIGH, all waived (8 ids), 0 Python package findings. media: 4 CRITICAL and 94 HIGH, all waived. CycloneDX SBOMs: 148 / 148 / 331 components (E3: 155 / 155 / 331) | `reports/perf/wf-infra/image-scan/` (counts by this author with `jq`) |
| SE13 SCA gate (`tests.security.sca_gate`) | **not run in Wave F** | infra-perf |
| L2 / L3 on `:wfinfra` (`make qa-load-holdback`, 3 runs, mock-pinning experiment) | The 12 L2/L3 cells met their TTFT bounds in each run. Cells invalid from mock lateness: 3 of 13 unpinned, 10 and 6 of 13 pinned | `reports/perf/wf-infra/gates/holdback-*.json`, the mock CPU samples `reports/perf/wf-infra/gates/mockcpu-*.txt`; [perf/sizing.md](perf/sizing.md) §10 |
| Waste estimate per model (generation) | Uses `TOKEN_ESTIMATOR_V1` chars per token instead of chars/4 (`src/noema/generation/run.py`). This closes the "K1 output side" item of summary §5. K1's status does not change | infra-perf; unit tests in `make check` |

**Re-run by this author on the Wave F working tree (2026-10-12):**

| Command | Result |
|---|---|
| `NOEMA_AGENT_ID=acceptance make -k check XDIST=4` | **exit 2: one step fails, `ownership`**: "15 changed path(s) without an owner": CHANGELOG.md, CONTRIBUTING.md, SECURITY.md, docs/architecture.md, docs/development/{README,setup,testing}.md, and 8 files under docs/operations/ |
| (same run, the other steps) | All green: ruff (920 files), mypy (327 src / 594 test files), import-linter (27 kept / 0 broken), forbidden-scan (clean), contracts-check (70 files identical), unit (3,588 passed, 1 skipped), conformance (757 passed, 8 skipped), integration (573 passed, 21 skipped), sdk-check (TS 86 + 6 skipped, Python 192 + 1 skipped) |
| `make observability-check` | exit 0. promtool: 6 + 8 + 11 alerting rules, 19 recording rules, 84 dashboard queries, config syntax valid. pytest 139 passed |
| `make qa-security-local` | 72 passed |
| `make docs-drift` / `make sdk-compat` | 8 passed / 65 passed |
| `cd /codes/aghooai/docs/noema && ./check_docs.sh` | exit 0 (36 s) |

**Re-run by the final docs fixer after its fixes (2026-10-12; the critic's findings, OWNERS.yaml `repo-docs` rule):**

| Command | Result |
|---|---|
| `make ownership` | exit 0: every changed path has an owner (before the rule: exit 1, "15 changed path(s) without an owner") |
| `NOEMA_AGENT_ID=wffix make check XDIST=4` | **`make check: all green`**: ruff (920 files), mypy (327 src / 594 test files), import-linter (27 kept, 0 broken), forbidden-scan clean, ownership, contracts-check (70 files identical), unit 3,588 passed / 1 skipped, conformance 757 / 8 skipped, integration 573 / 21 skipped, sdk-check (TS 86 + 6 skipped, Python 192 + 1 skipped) |
| `make observability-check` | exit 0, "observability-check: all green": promtool check rules (6 + 8 + 11 alerting, 19 recording), 4 test files SUCCESS (53 alert cases, 3 recording-rule cases), example config valid, 84 dashboard queries, pytest 139 passed |
| `cd /codes/aghooai/docs/noema && ./check_docs.sh` | exit 0 (37 s) after the revision 7 sizing sync |
| README quickstart on its own stack (`noema-wffix`, port base 36300, `:wfinfra` images, `E2E_BUILD=0`, a dev-only venv from `uv sync --frozen`) | `make e2e-up` 29 s; the curl turn returned the documented event list and text; `make e2e-smoke` 8 passed; `make e2e-down` left 0 containers, volumes and networks |
| Prometheus v3.5.0 with `deploy/observability/prometheus/prometheus.compose.yml` on that stack | 6 targets up, 44 of 44 rules healthy, `noema:pod_turn_slots_held:sum` and the SLO recording rules returned data, no alert fired ([qa/wave-f/prometheus-e2e.json](qa/wave-f/prometheus-e2e.json)) |

**Not run on the Wave F tree in Wave F**, all run since by the release gate (section 3.6), except where noted:
- `make e2e`, `qa-contract` (stack), `qa-security` (stack, fuzz, SCA), `qa-fault`, the full `qa-gates`,
  `test-media`, `ui-check` and `ui-e2e`;
- L4 and L5 (the release gate ran L5; L4 stands from E3);
- C1-C5 (the release gate ran C2, C3 and C5 in the container; C1 and C4 stand from E3);
- the live smoke (not run since; no live calls were allowed).

The Wave F code changes are `api/app.py`, `api/overload.py`, `telemetry/registry.py`, `generation/run.py`, the
load driver and the mock CLI. In Wave F, the new interpreter's in-image behaviour was measured only by these:
- the L1 runs and the L2/L3 holdback runs above;
- the trivy scan;
- infra-perf's image checks ([perf/sizing.md](perf/sizing.md) §8): uid 10001, read-only root, the same 66
  distributions as `:qae3`.

The release gate then ran every suite on the image (section 3.6).

### 3.6 Release gate on the `wave-f` tree (2026-10-12 23:54 to 2026-10-13 03:48 UTC)

Run by the release QA lead ([qa/release-gate.md](qa/release-gate.md)).

**Setup.**
- HEAD `10aa2d4` = tag `wave-f`.
- Images built from the tree: `make e2e-images image-media-test IMAGE_TAG=rgate`, plus `E2E_BUILD=1` on the first
  e2e run. The image ids are api `sha256:3863d2741b72`, worker `40b5e78dc0e2` and media `8f27a96517b5`.
- All 331 `noema/**/*.py` files in the api and media images are byte-equal to the tree.
- `WEB_CONCURRENCY=2` in every stack.
- Each stack had its own compose project, with port bases 21100-21600 and the load stack at 22000+. All are below
  32768.
- No live Bedrock call was made.
- `src/` and `deploy/` did not change during the gate.

| Gate | Release result | Evidence (release-gate section) |
|---|---|---|
| Images from the current tree | **PASS** | 331/331 files equal to the tree; CPython 3.12.15 (python-build-standalone), static (§2) |
| `make check` (incl. ownership), start and end | **PASS** | All green at 23:54-23:57 and at 03:03-03:05. The counts: ruff 920 files; mypy 327 / 594; import-linter 27 kept / 0 broken; unit 3,588; conformance 757; integration 573; sdk TS 86 / Python 192. Ownership: Wave F set 108/108; all 1,989 tracked paths owned; end-state changes 38/38 (§3, §5) |
| `make e2e`, run 1 / run 2 (fresh stacks `noema-rg-e2e` 21100, `noema-rg-e2e2` 21200) | **PASS** | 52 passed in 154.9 s / 155.7 s on the `:rgate` images (§3) |
| `make ui-check` | **PASS** | `tsc --strict`, vitest 75/75, vite build, ruff and mypy on `web/e2e` (§3) |
| `make ui-e2e` | **PASS** | Playwright 12/12 on the fixture API (port 21290) and 12/12 against `noema-rg-e2e` (21295); 55 proxied responses contract-valid in each run (§3) |
| `make qa-contract` (Q1, Q2, Q3, X1 part) | **PASS** | 131 passed. Schemathesis: stateless 6,160 calls / 25 operations, stateful 1,735 / 19, 0 failures. openai Python 14/14, retries 3/3, npm 12/12, Jev 8/8 (§3) |
| `make qa-security` (SE1-SE12, SE9 fuzz) | **PASS** | local 72; stack: e2e 52, UI 12, `tests/security/stack` 31; SE1 356/356 cells, 0 leaks; SE6 51 canaries; SE7 44 captured requests; SE12 46 secret values, 49 spans; fuzz 6/6, nothing escaped (§3.1) |
| `qa-security-sca` (SE13: waivers, SBOMs) | **PASS** on the re-run | The first try failed: pip-audit could not reach PyPI (`RemoteDisconnected`), so `sca_gate` hit `FileNotFoundError`. The re-run passed: pip-audit 243 packages / 0 vulnerable, npm 0/0, trivy 0 unwaived ids (51 waived), SBOMs 148 / 148 / 331 (§3.1) |
| `make qa-fault` (F1-F12, S1a, S1b) | **PASS**, 22/22 on a fresh stack | First run 21/22: the F10 [hung] same-pod cancel probe got 500, while every spec-10 F10 criterion passed. Root cause: the turn registry is per process, and the pods run 2 processes. After the harness fix, F10 passed 3 times, then the full suite passed 22/22 (§3.2, §3.3, §5) |
| `make qa-gates` | **PASS** | L2/L3 13 passed, every bound met (tightest 326.4 vs 350 ms); Q8 `served_spec_differs` 200/200; L5 p99 927.5 ms; R4 restarts 46, replay 60/60; Q9 1.0 / 0.0; M3 CER 0.0; ownership-all 0 unowned. 9 of 13 holdback cells were invalid by mock lateness (§3.4) |
| `make test-media` | **PASS** | 307 passed, 8 skipped (the media image has no network) (§3) |
| `make observability-check`, also with `NOEMA_OBS_RUNBOOKS=1` | **PASS** | promtool: 6 + 8 + 11 alerting and 19 recording rules; 4 rule-test files SUCCESS; example config valid; 84 dashboard queries; pytest 139 passed, including the runbook-anchor test. Run at 23:55 and at 03:05 (§3, §5) |
| `make image-scan` | **PASS** | exit 0, no unwaived CRITICAL. api / worker: 43 HIGH waived (8 ids); media: 4 CRITICAL + 94 HIGH waived (51 ids); SBOMs 148 / 148 / 331, identical to Wave F (§3) |
| C2 ramp in the container | **PASS** | `rg-ramp`, `noema-api:rgate`, 2 processes on CPUs 46-47: lag p99 1.9 / 3.5 / 4.1 / 4.9 ms at 50-200 per pod; every window valid to 1,000; 0 x 5xx, 0 transport errors (§4.2) |
| C3 slow clients in the container | **PASS** | `rg-c3`, with the `--sse-probe` harness fix: others' p95 ratio 1.001 [0.997, 1.006]; 60/60 `client_too_slow`; 60/60 stored `failed`; queue high-water 253 events, 1,048,270 B (§4.4) |
| C5 flooding tenant in the container | **PASS** | 1.020 [1.017, 1.022] and 1.031 [1.026, 1.036] against the 1.10 bound; 0 victim errors; 0 flooder transport errors (§4.3) |
| L1 at the planned 100 streams per pod (container) | **PASS** | `rg-l1-100`, n 10,696: p50 9.21 [9.18, 9.23], p95 12.64 [12.45, 12.81] ms, valid; 41.9 ms CPU per turn; `aghoo_turn_slots_held` 50 + 50 = 100. The Wave F knee of 125 stands (§4.1) |
| `check_docs.sh` (design repo) | **PASS** | exit 0 at 23:56 (37.3 s) and at 03:05 (35.5 s), after the other role's 08 edit (§3, §5) |
| K1 estimator (06 §11) | **NOT-RUN** | needs live Bedrock calls, which were not allowed. The E3 FAIL stands (14 of 25 within 2%) |
| L1 as written at 200 per pod | **NOT-RUN** | not re-run, per the brief. The ramp's 200-per-pod step is indicative only: p50 11.20 [11.13, 11.28] ms (n 3,454). That fits the standing FAIL |
| Teardown | **PASS** | `noema-rg-e2e`, `-e2e2`, `-fault`, `-contract`, `-sec`, `-gates` and `-perf`: 0 containers, volumes and networks each. `noema-dev` kept (4 healthy). The `:rgate` images are kept as the gated artifacts (§8) |

**Changes made by the release gate** (tests only, committed with tag `phase1-mvp`; no product bug found and no product code changed):
- At release, the orchestrator also fixed a startup race in the wire-mock CLI (`tools/mockbedrock/__main__.py`: the ready line was printed before the SIGTERM/SIGINT handlers were installed, so a stop signal right after it killed the process; the CPU-pinning unit test failed 4 of 72 stressed runs before and 0 of 72 after), made `qa-security-sca` fail loudly when pip-audit writes no report, and fixed the stale text listed in section 7.2. Final checks: `make check` all green, `make e2e` 52/52 on the `:rgate` images, `check_docs.sh` exit 0.
- `tests/load/run.py`: `--sse-probe` was silently dropped with `--api-image`. The fix adds `container_serve_args()`
  and gives each pod process its own probe file. The regression test
  `test_run_api_image.py::test_the_container_pod_gets_one_sse_probe_file_per_process` failed before the fix and
  passes after. The fix made C3 measurable in the container.
- `tests/fault/test_f10_redis_down.py`: the same-pod cancel probe now retries a sibling process's 500 on fresh
  connections (up to 8 tries) and asserts the owning process's 202. The access-log 500 took 303 ms, the Redis
  timeout path, and the sequence `500,500,202` was seen twice.

## 4. Owner decisions log

Only the owner's decisions are listed here. Sources:
- [`01-feasibility-study.md` §1.1](../../docs/noema/01-feasibility-study.md);
- [../docs/IMPL-DECISIONS.md](../docs/IMPL-DECISIONS.md), line 13 ("Fixed owner decisions");
- [../docs/impl-spec/OWNER-CHECKPOINT.md](../docs/impl-spec/OWNER-CHECKPOINT.md) ("Owner answers (2026-10-10)" and the
  Wave E3 section);
- IMPL-080, amendment of 2026-10-12.

| Date | Id | Decision | As built (evidence) |
|---|---|---|---|
| 2026-10-09 | D1 | Every decision, generation and labeling model runs on Amazon Bedrock; no OpenAI API calls | Only the `bedrock-converse` and `bedrock-mantle-chat` adapters exist (`src/noema/adapters/`). `make forbidden-scan` is clean (RG, section 3.6) |
| 2026-10-09 | D2 | One AWS account per tenant | Cross-account AssumeRole with ExternalId; one cached STS session per (pod, tenant), confirmed by answer (5) below (IMPL-007). Tested on moto only (SE4) |
| 2026-10-09 | D3 | No regulatory regime | No ZDR or residency routing (01 §1.1) |
| 2026-10-09 | D4 | Labels come from `gpt-6.1-sol` + `claude-sonnet-5-5`, kept only when both agree | `labelers: [sol-6.1, sonnet-5.5]`, `rule: unanimous` (`src/noema/contracts/_vendor/examples/platform.example.yaml`) |
| 2026-10-09 | D5 | Kimi K3 out, DeepSeek in; the holdback default stands | `router-default` members are DeepSeek 3.2, Luna 6 and Haiku 5.5 (same file) |
| 2026-10-09 | D6 | gpt-6-luna is the Jev-like decision model behind `/v1/decisions` | `lightweight-model-decide` (Luna) is the default profile, filled by the loader (`src/noema/config/loader.py`, IMPL-031). Jev output Q3 PASS |
| 2026-10-09 | R5 (project rule, revision 5) | Early commit is OFF; the 255-choice limit stays, with the hierarchical method [X]; answers are `uncalibrated` until a per-stage calibrator passes | `commit_on_quorum` is always false (`src/noema/config/models.py`, rule in `config/rules.py`). The hierarchical method is built (Q9). No calibrator is active (section 2) |
| 2026-10-10 | Answer (1) | **Build the hierarchical method in Phase 1** for native `/v1/decisions` and turn app questions. The facade and `output_format: "jev"` keep 422 `unsupported_parameter` | IMPL-027 and IMPL-036 revised; `src/noema/decision/hierarchical.py`; Q9 PASS |
| 2026-10-10 | Answer (2) | No escalation inside turns | IMPL-025 unchanged; `/v1/decisions` escalates |
| 2026-10-10 | Answer (3) | An escalation stage that returns `unavailable` replaces the stage-0 answer | IMPL-061 unchanged |
| 2026-10-10 | Answer (4) | `injection_suspected` stays non-blocking | Kept; spotlighting strengthened instead by the orchestrator call IMPL-086 (section 5) |
| 2026-10-10 | Answer (5) | Defaults accepted: the CP-DEC-2 `EscalationStage` fields (IMPL-048); `x-aghoo-end-user` deferred, 422 (IMPL-033); a 50 MiB JSON body cap (IMPL-064); STS cached per (pod, tenant) (IMPL-007) | As listed |
| 2026-10-12 | Perf and live | **One more performance sprint (E3), then size the pods from the measured L1 knee** (08 §9), reporting the SLO at that density. **+30 live calls**: the live-smoke hard stop rises from 60 to 87 ledger entries | IMPL-080 amendment, IMPL-087 sizing bullet. Knee: E3 image 75, Wave F image 125 per pod (section 3.5). Ledger at 85 of 87 |

### 4.1 Owner review items

Items 1-3 are material engineering decisions that were recorded after the fact. CONTRIBUTING §4 says material,
externally visible changes go to the owner first. Items 4-6 are owner sign-offs or approvals that are still open.
None of the six has an owner answer yet. Sources:
[../docs/IMPL-DECISIONS.md](../docs/IMPL-DECISIONS.md) and
[../docs/impl-spec/OWNER-CHECKPOINT.md](../docs/impl-spec/OWNER-CHECKPOINT.md), "Wave F".

| # | Item (record) | What the owner is asked | Evidence |
|---|---|---|---|
| 1 | **Sizing: 8 pods instead of 14** (IMPL-089; OWNER-CHECKPOINT F2). The images run CPython 3.12.15 from python-build-standalone, installed by uv 0.13.0. That raised the knee from 75 to 125 streams per pod. The plan is 100 per pod, so ~800 streams need 8 pods. Scale out above 100 streams or 0.62 vCPU per pod | Acknowledge the sizing basis. Also acknowledge the uv 0.9.10 -> 0.13.0 bump in the api build stage: it also pins the interpreter build | Re-gated at the release gate: L1 at 100 per pod p50 9.21, p95 12.64 ms; C2, C3 and C5 pass in the container (section 3.6). Caveats: section 7.1 items 4-5 |
| 2 | **Overload: 429 before 401** (IMPL-087 amendment; OWNER-CHECKPOINT F1). The edge shed checks the per-process cap before authentication for key ids in the process's key cache. While a process is over its cap, a wrong secret for such a key id gets 429 `queue_timeout`, not 401. Unknown key ids and keyed requests always get the full ingress | Accept the trade-off, or ask for 401 first. A 401-first order would put authentication back in front of the shed | The edge shed is part of the fix that moved C5 from 1.226 / 1.199 (E2) to 1.021 / 1.022 (E3). The release gate measured 1.020 / 1.031. It shipped in E3 and was recorded in Wave F |
| 3 | **Object storage as built** (IMPL-091, material, touches D2; OWNER-CHECKPOINT F3). Tenant objects live in one platform bucket `noema-<env>` under tenant prefixes. Each tenant has its own KMS key `alias/noema-<env>-<tenant_id>`, held in the platform account. Design 08 §5 puts the media bucket and key in the tenant account | (a) Accept, for Phase 1, that tenant data rests in the platform account under a per-tenant key. (b) For Phase 2 (ASR through Transcribe in the tenant account), choose a tenant-account bucket for ASR only, or for every object, which means migrating the existing prefixes | `src/noema/core/objects.py`; spec-03 §2. The platform roles' S3 and KMS permissions are not documented yet (section 7.2) |
| 4 | **ADRs signed off** (the 06 §11 Phase 0 row) | Sign off ADR-001 to ADR-004 (security) | NOT-RUN. The ADRs are adopted by engineering (impl-spec README) |
| 5 | **The Wave A2 wording point**: `strict` spotlighting does not carry over to later turns | Confirm that reading | The spec keeps no carry-over (OWNER-CHECKPOINT, "Wave A2 challenges: resolved") |
| 6 | **Live-call budget.** 2 of the 87 ledger entries are left | Approve a budget for the live SigV4 session-token check, with AssumeRole credentials, and more live data for the K1 refit | section 7.1 items 1 and 6 |

No other IMPL record carries a pending owner flag. This was checked with `grep` for "owner review", "pending" and
"acknowledg" on 2026-10-13. IMPL-007's reading of D2 was acknowledged in owner answer (5). IMPL-090 (operator proof)
is not material.

## 5. Orchestrator rulings and engineering calls (not owner decisions)

**These are implementation rulings, recorded as IMPL amendments.** Each is checked against
[../docs/IMPL-DECISIONS.md](../docs/IMPL-DECISIONS.md). The canonical table is 08 §14.4.

**Name clash:** ruling **R5** (2026-10-10, ECE noise floor) is unrelated to the owner's **project rule R5**
(revision 5, section 4).

| Ruling | Date, wave | IMPL | Ruling | Visible to clients |
|---|---|---|---|---|
| R1 | 2026-10-10, C2a | 027 (d) | The hierarchical expansion must hold >= 2 options, so a dominant one-option group yields a scored answer, not `unavailable / no_quorum` | yes (outcome) |
| R2 | 2026-10-10, C2a | 012 | A hold cap that fires with nothing buffered arms the run; the fallback commits at its own first text with `holdback_deadline`, `holdback_ms` 0 | event reason |
| R3 | 2026-10-10, C2a | 075 | A budget-cut image in a decision view falls back to its ready `ocr_text`, else it is omitted (`metadata_only`) | degradation |
| R4 | 2026-10-10, C2a | 078 | When Redis returns, the local admission state is not merged (it is reused within 60 s); labeling keeps 20% headroom in the bulkheads | no |
| R5 | 2026-10-10, C2a | 072, ADR-003 | Two values are initial and tunable in Phase 0: the binned-ECE noise floor (`n_min` 300 / `ece_max` 0.05) and `DRIFT_MIN_N` = 100 | no |
| R6 | 2026-10-11, E2 | 016 | A fallback to a different model is not a retry and spends no retry budget | no |
| R7 | 2026-10-11, E2 | 064, 030, 077, 022, 076 | Ingress: authentication before the body is read; at most 64 media parts (400); control-character ids give 404. Idempotency: a keyed `store: false` retry re-executes and bills; another principal's reuse is 409. A malformed 2xx Converse body is retryable | yes |
| R8 | 2026-10-12, E3 | 016 | The retry budget counts only possibly-delivered same-model re-sends: 10% of first sends, no floor. F12 is gated per failing upstream | no |
| R9 | 2026-10-12, E3 | 022 | U+0000 becomes U+FFFD in stored model and turn text; in session `metadata` it is 400 `schema_violation` | yes |

**Engineering calls (orchestrator or wave engineers; not owner decisions):**

| Call | Date | What | Evidence |
|---|---|---|---|
| IMPL-085 (call (a)) | 2026-10-10 | Idempotent responses over 1 MiB go to object storage (tenant prefix, SSE-KMS, same TTL), so size never causes a re-bill | OWNER-CHECKPOINT; spec-11 §5-§6 |
| IMPL-086 (call (b), supersedes IMPL-029b) | 2026-10-10 | `standard` spotlighting always datamarks document and OCR blocks (generation and decision prompts); `strict` also datamarks user text and history | SE7 0 policy violations; SEC-INJ-04/-06 |
| IMPL-087 | 2026-10-11 (E2), amended E3 / Wave F | 2 api processes per 2-vCPU pod. A per-process cap of 120 gives 429 `queue_timeout` + `retry-after: 1`. An edge shed runs before authentication for cached key ids. Max-min fair share. `gc.freeze()` and thresholds (700, 10, 100) | C5 1.021 / 1.022; overload 0 x 5xx to 1,000 streams per pod |
| IMPL-088 | 2026-10-12 (E3) | One bounded Redis pool per process (`NOEMA_REDIS_MAX_CONNECTIONS`, default 256); pool gauges and sampled datastore latency histograms | C2's datastore metrics (summary C2) |
| IMPL-050 amendment | Wave E, E3 | Lean Converse transport on aiohttp, with its own SigV4 signer and eventstream decoder, byte-equal to botocore | 28/28 live signatures, 4,304/4,304 messages |
| IMPL-029c | E2, E3 | Per-model chars-per-token table plus overhead; `NOEMA_TOKEN_ESTIMATOR_TABLE` override | K1 14 of 25 (still FAIL) |
| IMPL-078 clarifications | E2, E3 | Text-less stops count as breaker failures; the p95-TTFT rule needs distinct tenants (`NOEMA_BREAKER_TTFT_RULE`) | F3 / F3b PASS |
| IMPL-089 | Wave F; amended at the release gate | The api and worker images run python-build-standalone CPython 3.12.15, installed by uv 0.13.0 in the api build stage. The media image keeps uv 0.9.10, and the dev venv stays on 3.12.12. The pods are sized from that image's knee. Material: owner acknowledgement (section 4.1 item 1) | [perf/sizing.md](perf/sizing.md) §8; release gate L1, C2, C3, C5 (section 3.6) |
| IMPL-090 | recorded at the release gate (Wave E change) | Operator proof: the database verifies the operator key on each `noema_cli` connection (migration `0003_operator_proof`). No table, column or grant changes | `tests/integration/db/test_admin_config.py` in `make check` |
| IMPL-091 | recorded at the release gate (Wave B layout) | Object storage as built: the platform bucket `noema-<env>` with per-tenant KMS keys in the platform account. Material, touches D2: owner review (section 4.1 item 3) | `src/noema/core/objects.py`; spec-03 §2 |
| Not recorded (test tooling) | 2026-10-12 | The qa-gates mock stays unpinned by default (no IMPL record; it is not a product decision) | [perf/sizing.md](perf/sizing.md) §10 |

## 6. Deviations from design revision 6

Revision 6 (2026-10-10) is the design the build started from. The revision-6 cells were checked against the rev-6
archive in `/codes/aghooai/.backups/docs-noema-rev6-2026-10-10.tgz` where noted. The as-built record is 08 §14 and
[../docs/architecture.md](../docs/architecture.md) §8.

| Area | Revision 6 | As built | Record |
|---|---|---|---|
| api pod sizing | 08:L488: "4 × (2 vCPU, 4 GiB), ~200 concurrent streams each"; scale out above 150 per pod (rev-6 archive) | 2 api processes per 2-vCPU pod. Knee 125 streams per pod (Wave F image; E3 image: 75), planned at 100: 8 pods for ~800 streams. Scale out above 100 streams per pod (summed `aghoo_turn_slots_held`) or 0.62 vCPU. Release gate: L1 at 100 per pod p50 9.21 / p95 12.64 ms | IMPL-087, IMPL-089 (owner acknowledgement pending); owner decision 2026-10-12; [perf/sizing.md](perf/sizing.md) §5 |
| L1 gate | p50 <= 10 / p95 <= 25 ms at 200 streams per pod | Assessed at the sized density (owner decision); the 200-per-pod form fails p50 | 06 §11 note (rev 7); section 3 |
| Overload | no per-process limit | 429 `queue_timeout` + `retry-after: 1` above 120 turns per process. Checked at the edge before authentication for cached key ids, so a wrong secret then gets 429, not 401. Max-min fair share | IMPL-087 and its amendment |
| Ingress order | spec-04 §1 (tag `wave-c2b`): step 3 body cap → 413, before step 4 authentication | Only the header-declared size and type are checked before authentication; the body is read after it, so an over-cap chunked body without a key is 401 | R7, IMPL-064 |
| Media parts | no count limit | At most 64 media parts per request: 400 `schema_violation` | R7, IMPL-064 |
| Malformed ids | not specified | Control characters give 404 in paths and 400 in queries, never 500 | R7, IMPL-030 |
| U+0000 | not specified | U+FFFD in stored model and turn text; 400 in client-supplied structured fields | R9, IMPL-022 |
| Retry budget | The budget also gated fallbacks to another model (it refused 20-63% of fallback turns in F1, F4-F8); a floor of 10 retries per 60 s (C2b) | Fallbacks spend none (R6); 10% of possibly-delivered first sends, no floor (R8) | IMPL-016 |
| Idempotency | stored responses replay | Stored only if a call was dispatched and the content may be kept, so a keyed `store: false` retry re-executes and bills. The fingerprint is principal-bound (409) | R7, IMPL-077 |
| Converse transport | "aiobotocore + httpx" (rev-6 02, technology line) | Lean aiohttp transport with its own SigV4 signer and eventstream decoder, byte-equal to botocore; same wire | IMPL-050 amendment; 08 §14.3 |
| Malformed 2xx body | not specified | Retryable `malformed:response` | R7, IMPL-076 |
| Breakers | failure-ratio and p95-TTFT rules | Text-less stops count as failures; the p95-TTFT rule needs distinct tenants and can be turned off in dev/test (`NOEMA_BREAKER_TTFT_RULE`) | IMPL-078 |
| Redis outage | resume disabled | A cross-pod attach waits, then returns 410 `resume_window_expired` (F10: 2.5 / 3.3 s). A cancel that reaches a process other than the turn's own returns 500 (retryable), even on the same pod (section 7.1 item 12) | IMPL-021 |
| Hierarchical, one dominant group | `unavailable / no_quorum` | Scored answer (the expanded set holds >= 2 options) | R1, IMPL-027 (d) |
| Token estimator | chars ÷ the model's chars-per-token, default 4.0 | Per-model table plus a per-request overhead (`TOKEN_ESTIMATOR_V1`), replaceable by `NOEMA_TOKEN_ESTIMATOR_TABLE`. Since Wave F, the generation waste estimate uses the shipped table, not the override | IMPL-029c; `src/noema/generation/run.py` |
| GC | not specified | `gc.freeze()` at start; thresholds (700, 10, 100) | IMPL-087 |
| Object storage | a tenant media bucket and KMS key per tenant account (08 §6 onboarding row; 02 Transcribe path) | One platform bucket `noema-<env>` with a tenant prefix and a per-tenant KMS key `alias/noema-<env>-<tenant_id>` in the platform account (`src/noema/core/objects.py`). The tenant-account bucket is deferred to Phase 2 (ASR) | **IMPL-091** (material, owner review: section 4.1 item 3); spec-03 §2; as-built notes in 08 §5 and §6 (uncommitted, section 7.2) |
| Operator proof | operator key checked in the CLI | The database verifies the operator key per connection (migration `0003_operator_proof`) | **IMPL-090**; [../docs/modules/db.md](../docs/modules/db.md) |
| Live smoke cap | 60 calls | 87 ledger entries in total, 85 used | owner decision 2026-10-12, IMPL-080 |
| Process environment | spec-05 §5 list | Adds `NOEMA_CREDENTIALS_BROKER`, `NOEMA_BREAKER_TTFT_RULE`, `WEB_CONCURRENCY`, `NOEMA_MAX_TURNS_PER_PROCESS`, `NOEMA_REDIS_MAX_CONNECTIONS` and `NOEMA_TOKEN_ESTIMATOR_TABLE` | spec-05 §5; IMPL-087, IMPL-088, IMPL-029c |
| api image interpreter (Wave F) | not specified (E3: the official Python image) | python-build-standalone CPython 3.12.15 through `uv python install` (uv 0.13.0) on `debian:trixie-slim`; 372 MB. Re-gated at the release gate (section 3.6) | **IMPL-089** (amended: uv 0.13.0); [perf/sizing.md](perf/sizing.md) §8 |

## 7. Known limitations and open items

### 7.1 Gates and measurements

| # | Item | Measured state | Source | Owner |
|---|---|---|---|---|
| 1 | **K1 FAIL**, estimator tokens <= 2% | 14 of 25 live calls within 2% with the shipped table (E3). One (chars per token, overhead) line per model cannot reach 2% per call: Luna and Sol mix short adapter prompts with templates. Not re-run at the release gate (no live calls) | [qa/e3/live-smoke.md](qa/e3/live-smoke.md) §4; summary §5 | Phase 0 refit per call kind (`NOEMA_TOKEN_ESTIMATOR_TABLE`) |
| 2 | **L1 as written** (200 streams per pod) | p50 11.70 [11.64, 11.76] ms FAIL, p95 20.13 ms PASS (E3, venv). Not run as an L1 window on the Wave F or release image. 150 per pod already misses p50 there (10.21 [10.18, 10.26] ms, Wave F), and the release ramp's 200 step reads 11.20 [11.13, 11.28] ms (indicative). The p50 is the ingress floor plus per-loop queueing | [perf/sizing.md](perf/sizing.md) §2, §7 risk 1; [qa/release-gate.md](qa/release-gate.md) §4.2 | api, perf; owner decision accepts the knee |
| 3 | **The 24 h soak was not run** | C4 ran for 1 h at 150 streams per pod (venv): RSS +0.76%, 0 stuck attempts | summary C4 | Phase 0/1 load test |
| 4 | **No multi-pod load on shared datastores** | Every load gate used one pod with its own Postgres and Valkey. Estimated for 800 streams: ~1.7k Redis commands/s and ~290 Postgres units/s. The Redis-down divisor (`NOEMA_EXPECTED_PODS × WEB_CONCURRENCY`) at N = 8 was not exercised | [qa/e3/qa-lead.md](qa/e3/qa-lead.md) §7 item 8; [perf/sizing.md](perf/sizing.md) §7 risks 4, 7 | Phase 0/1 load test |
| 5 | **Hardware and workload of the knee** | Two dedicated physical cores, quiet host, plain-HTTP mock, and the L1 scenario only: no escalation, verification, media or race | [perf/sizing.md](perf/sizing.md) §7 risks 2, 3 | infra, perf |
| 6 | **SigV4 session-token branch not verified live** | All 28 live signer records used IAM-user keys (`session_token: false`). Tenant AssumeRole credentials always carry a token. Verified offline only (`test_sigv4.py` goldens and fuzzing). The Mantle short-term bearer from assumed roles is also UNVERIFIED | [qa/e3/live-smoke.md](qa/e3/live-smoke.md) §6 item 3; [../docs/modules/adapters.md](../docs/modules/adapters.md) | live smoke with Phase 0 accounts |
| 7 | **Phase 0 gates** | All 9 NOT-RUN, plus the 10 [P0] Phase 1 rows | section 3.4 | Phase 0 |
| 10 | Mock lateness in `qa-gates` | 9 of 13 holdback cells invalid at the release gate (unpinned mock, shared host), every bound met. Wave F: 3 of 13 unpinned, 10 and 6 of 13 pinned. The mock used <= 0.13 core | [qa/release-gate.md](qa/release-gate.md) §3.4; [perf/sizing.md](perf/sizing.md) §10 | qa-tools (multi-process compose mock) |
| 11 | L4 rests on few live samples | The probe-table rows come from few live calls; 06 §4's Phase 0 re-set of the deadline stays (QL-6). Live: Luna hit its 1,000 ms deadline in 2 of 7 router calls | summary L4; [qa/e3/live-smoke.md](qa/e3/live-smoke.md) §6 item 1 | Phase 0 task 0.4 |
| 12 | **A cancel during a Redis outage can return 500 on the same pod** (docs resolved; behavior documented) | With 2 processes per pod (IMPL-087), the running-turn registry is per process. A cancel that reaches the pod's other process asks Redis for the owner, and with Redis down that is 500 (retryable). 3 of 9 same-pod first tries reached the sibling process. spec-04 §12 (`docs/impl-spec/04-turn-lifecycle.md:354`) and spec-10 F10 still say "pod". Resolved for the docs: spec-04 §12 and spec-10 F10 now say "another process" (a retryable 500). Forwarding cancels between a pod's processes stays a Phase 2 option | [qa/release-gate.md](qa/release-gate.md) §3.3, §7 item 1 | api (Phase 2 option) |
| 13 | **`qa-security-sca` hid a pip-audit failure** (resolved) | `make/qa-security.mk` runs `uvx pip-audit ... \|\| true`. When PyPI was unreachable, `tests.security.sca_gate` crashed with `FileNotFoundError`. Resolved at release: the target now deletes any stale report and exits 2 with an explicit message when pip-audit writes no report; both paths were tested | [qa/release-gate.md](qa/release-gate.md) §3.1, §7 item 2 | resolved |
| 14 | **The release gate's changes were uncommitted** (resolved: committed, tag `phase1-mvp`) | On top of `wave-f`, the tree carries uncommitted changes: the two harness fixes (`tests/fault/test_f10_redis_down.py`, `tests/load/run.py`, `tests/load/test_run_api_image.py`), `reports/qa/release-gate*`, this report, the summary, and the docs edits of section 7.2. The F10 and C3 PASS results depend on the harness fixes. `make check` (ownership 38/38, then 84/84) and `check_docs.sh` pass on this tree | [qa/release-gate.md](qa/release-gate.md) §2, §5 | orchestrator (commit; the docs owners review section 7.2 first) |

Items 8 (the Wave F tree had not been re-gated) and 9 (`make check` failed at ownership) are **closed**: see section 3.6 and [qa/release-gate.md](qa/release-gate.md) §7.1, which also covers the C5 repeat after the keep-alive fix, with 0 transport errors. The numbers are kept, so references stay valid.

### 7.2 Documentation and configuration drift (Wave F and the release gate)

The Wave F sizing is in [perf/sizing.md](perf/sizing.md) §5: knee 125, planned 100, 8 pods, scale out above 100
streams or 0.62 vCPU per pod. The final docs fixer brought these places to it (2026-10-12): the alert rules
(`noema-pod-streams-high` > 100, `noema-pod-cpu-high` > 0.62, the lag annotation 4.0-4.1 ms), the dashboard
builder and JSON (`thr=100`, `thr=0.62`), the promtool cases,
[../docs/operations/observability.md](../docs/operations/observability.md) and the runbooks, 08 §9, the 08 §6
scale-out row, 08 §14.2 (the Wave F GC figures, sizing §2.1 and §7 item 7), the 06 §11 L1 note, the design
README, IMPL-087 (sizing bullet, cap 1.9x the knee, the QL-5 GC citation), spec-11 §7 and
[../docs/modules/integration.md](../docs/modules/integration.md) §5.1 (the density sentence). The interpreter
change is recorded as IMPL-089. `check_docs.sh` and `make observability-check` pass (section 3.5; again at the
release gate, section 3.6).

**Fixed during the release gate** by another role, committed with tag `phase1-mvp`. `make check` and
`check_docs.sh` pass with these edits, but QA did not review their content; the docs owners review it. Checked with
`grep` on 2026-10-13:
- [../docs/modules/infra.md](../docs/modules/infra.md) §1: Debian 13 slim + python-build-standalone CPython 3.12.15,
  372 MB, the split base pins (api / worker on trixie and uv 0.13.0; media on bookworm and uv 0.9.10).
- [../docs/modules/integration.md](../docs/modules/integration.md) §1, §3, §5.1: the bounded Redis pool, the
  `telemetry.pool_gauges` task, the edge shed, the max-min fair share, saturated tenants and `aghoo_turn_slots_held`.
- The `tools/bootstrap_dev.py` docstring: `alembic upgrade head` (to `0003_operator_proof`).
- IMPL-089 (uv 0.13.0), the new IMPL-090 and IMPL-091, OWNER-CHECKPOINT F3, and `docs/architecture.md` §8.
- Design 08 §5 and the 08 §6 onboarding row: "As built in Phase 1" notes (IMPL-091). The design README also got an
  as-built bullet.

**Still stale** (checked with `grep` on 2026-10-13; none is in this report's edit scope):

| Location | Stale content | Owner |
|---|---|---|
| [../docs/IMPL-DECISIONS.md](../docs/IMPL-DECISIONS.md) IMPL-089, "Not done" bullet | "The QA lead has not re-gated the Wave F tree; C2 ramp, C3, C4 and C5 stand from the E3 venv". The release gate re-gated the tree and ran C2, C3 and C5 in the container | spec owner |
| [../docs/impl-spec/OWNER-CHECKPOINT.md](../docs/impl-spec/OWNER-CHECKPOINT.md) "Wave F", F2 | "Not yet re-gated by the QA lead" | spec owner |
| 08 revision line (`08-technical-reference.md:5`) | "tag wave-e3; §9 and §14.2 carry the Wave F image measurements, untagged". Wave F is tag `wave-f` | design docs |
| 08 §8 retention bullet (line 492) | "tenant-bucket lifecycle (media 1 day)" is the Phase 2 ASR audio lifecycle. No lifecycle rule on `noema-<env>` is documented as built | design docs, ops-ledger |
| `src/noema/api/overload.py:34` comment | `DEFAULT_MAX_TURNS` "about the knee of one process at the L1 workload". Under Wave F the cap is an overload guard at 1.9x the pod knee | api |
| 08 §5, `docs/operations`, IaC docs | The S3 permissions of the platform data-plane and job roles on `noema-<env>`, and `kms:GenerateDataKey` / `kms:Decrypt` on the tenant keys, are not written down (IMPL-091, an IaC item) | infra |
| spec-04 §12, spec-10 F10 | "cross-pod cancel -> 500" should say "another process" (section 7.1 item 12) | spec owner |
| Interpreter versions | The dev venv runs CPython 3.12.12 (uv 0.9.10) and the image runs 3.12.15 (uv 0.13.0). Recorded in IMPL-089, not resolved | infra |
| [../docs/modules/infra.md](../docs/modules/infra.md) §1 | The api cold-build time (20 s) dates from 2026-10-10. The doc marks it "not re-measured on the Wave F base", so it is a missing measurement, not wrong text | infra |

### 7.3 Observability gaps (registry is frozen; [../docs/operations/observability.md](../docs/operations/observability.md) §6)

- **Overhead alerts.** There is no gateway-overhead histogram, so both overhead alerts use a mean proxy (mean TTFT
  per turn minus mean model first text per attempt). It can miss a p95 breach, and it **over-reads**: TTFT includes
  the designed holdback wait, max(S1, first text) - first text, up to the router's 1,000 ms deadline (live E3: the
  first delta came 282 and 407 ms after Sol's first text in 2 of 6 turns, [qa/e3/live-smoke.md](qa/e3/live-smoke.md)
  §3), plus spec-restart and failover time. With real decision latency the mean can exceed 25 ms on a healthy
  gateway, so `noema-overhead-slo-burn` was downgraded from critical to warning. The fix is a gateway-overhead
  histogram (spec-10's L1 quantity) in a fakes-telemetry change request.
- **Counters appear only after their first add.** OpenTelemetry exports no counter series before its first `add`
  (an idle api process exposed 13 metric families, no counter), and `increase()` cannot count the step from
  absent to the first value. The four "any increase" alerts (`noema-turns-shed`, `noema-stuck-attempts`,
  `noema-outbox-records-dropped`, `noema-calibrator-ece-drift`) therefore carry a first-sample clause; their
  promtool series start absent. The cleaner fix, every label-less counter exported at 0 at start, is a
  fakes-telemetry change request.
- **ECE drift alert.** It now fires on the drift job's downgrades (`aghoo_calibrator_downgrades_total`), not on the
  `aghoo_calibration_ece` gauge, which a rejected fit also sets.
- **Availability SLI.** There is no request counter by status, so the SLI counts only 429 sheds. 5xx answers and
  failed turns are not counted.
- **Job metrics.** There are none, so the 08 §7 "deletion job > 24 h" and the 08 §9 jobs scale signal
  (queue age > 5 min) are not alertable.
- **Per-tenant throttling.** There is no per-tenant call counter. The throttling alert fires on any sustained
  throttling instead of "> 2% per tenant".
- **Dead letters.** Outbox dead-letters and dead jobs appear only as ERROR logs.
- **Inert DeepSeek alert.** `noema-deepseek-p99-high` never fires: `aghoo_decision_seconds` is observed with
  `member=""` (`src/noema/decision/run.py` line 169).
- **Verification flags.** `aghoo_verification_flags_total` is declared but not emitted.
- **Cost alert.** `noema-cost-per-turn-high` uses a placeholder budget of 0.05 USD.
- **Slot gauge scope.** `aghoo_turn_slots_held` also counts createDecision and facade calls in flight. The two
  processes of a pod split the load unevenly (e.g. 27 + 48), so scale on the per-pod sum.
- **Test coverage.** Every one of the 25 alerts has a firing promtool case (53 alert cases, 27 of them assert no
  alert). Rules were tested with Prometheus v3.5.0 only, and evaluated once against a compose stack's scraped
  metrics (all 44 rules healthy, [qa/wave-f/prometheus-e2e.json](qa/wave-f/prometheus-e2e.json)), never against a
  cluster; Grafana 12.1.1 was checked once. `make check` does not run the promtool tests or the runbook-anchor
  check: `make observability-check` is now in the wave gate list (CONTRIBUTING §6).

### 7.4 Functional limits as built (by design or recorded)

- **Calibration.** Every answer is served `uncalibrated` until an operator activates an artifact. Hierarchical
  answers are always `uncalibrated` in Phase 1 (ADR-003 §1).
- **Escalation.** There is none inside turns (owner answer 2).
- **Unavailable features.** `x-aghoo-end-user` returns 422 and resume returns 501.
- **Re-billed retries.** A keyed `store: false` retry re-executes and is billed again (R7).
- **401 vs 429 under overload.** While a process is over its cap, a wrong secret for a cached key id gets 429, not
  401 (IMPL-087 amendment).
- **Redis down.** A cancel that reaches a process other than the turn's own returns 500 (retryable). That includes
  the sibling process on the same pod (F10; section 7.1 item 12). A retry on a fresh connection can reach the owner.
- **Large scanned PDFs.** A scanned 600-page PDF may exceed the OCR limits (no page chunking). A 90 s Markdown job
  holds a short-lane sandbox slot ([../docs/modules/media_pipeline.md](../docs/modules/media_pipeline.md)).
- **UI.** W4 and W7 have no paging. UI sessions live in proxy memory ([../docs/modules/ui.md](../docs/modules/ui.md)).
- **W4 visibility.** W4 admin reads are tenant-wide for every allowed role (IMPL-042), so a reviewer sees other
  projects' redacted dropped items.

### 7.5 Supply chain and security

- **Waivers.** 51 vulnerability ids are waived with statements, for review on 2027-01-09
  (`deploy/docker/.trivyignore.yaml`). The media image carries 4 waived CRITICAL findings (in the E3, Wave F and
  release-gate scans).
- **Media image pip.** The media image ships the base image's pip 25.0.1: 5 MEDIUM and 1 LOW findings, not gating.
  The api and worker images carry no pip (owner: infra / media-pipeline; [qa/release-gate.md](qa/release-gate.md)
  §7 item 6).
- **Static libraries.** The api interpreter's bundled OpenSSL 3.5.9, SQLite 3.53.1, zlib 1.3.2, expat, ncurses,
  libedit, libffi, xz and bzip2 are invisible to trivy. Patching them needs a `PYTHON_VERSION` / `UV_IMAGE` bump in
  `deploy/docker/api.Dockerfile` ([perf/sizing.md](perf/sizing.md) §7 risk 6, §8).
- **pip-audit coverage.** pip-audit cannot audit torch or torchvision ([qa/e3/qa-lead.md](qa/e3/qa-lead.md) §4).
- **Pen test.** SE14 has not been done; it is pre-GA.
- **Reporting channel.** SECURITY.md can only say "report privately to the project owner": no security contact or
  channel exists (repo-docs open item).
- **Not run.** No IaC is in the repo. Not run while writing the operations docs: `noema-admin bootstrap`,
  `operators revoke` and `calibrators approve / activate / retire` (marked in
  [../docs/operations/configuration.md](../docs/operations/configuration.md)).

## 8. Phase 0 prerequisites and recommended next steps

### 8.1 Phase 0 prerequisites

These are the 05 Phase 0 tasks and the Phase 1 rows each one unblocks.

| 05 task | Needed for | Rows it unblocks |
|---|---|---|
| 0.1 Two vended tenant accounts; `AghooBedrockInvoke` role (7 model/profile ARNs + Mantle) with ExternalId; quotas | Real cross-account tests and CloudTrail evidence | SE4 (AWS), SE3 (Bedrock half), the Phase 0 tenant row. Also the live check of the SigV4 session-token branch and of the Mantle short-term bearer from assumed roles (section 7.1 item 6) |
| 0.2 Seed set `seed-v1` (600-1,000 queries; 100 image and 50 PDF turns) | Benchmarks, the platform-default calibrator | Q4, Q5, M4; the platform-default calibrator (section 2) |
| 0.3 D4 labeling of the seed set (Sol + Sonnet, unanimous) | Calibration and its caveats | Q5, Q7, the labeler consensus report |
| 0.4 Member benchmark with CIs (5-question layout, backups) | Deadline, quorum, `commit_critical` set, `backup_after_ms` | L4's deadline re-set (QL-6), L5 on real tails |
| 0.5 Race measurement and spec-agreement rate `a` | Holdback parameters, per-route timeouts | L7, the R4 thresholds (10% restarts, 5% waste) |
| 0.6 Conditioning A/B | Placement (ADR-004 default in use) | R1, R2 |
| 0.7 Cancellation billing table | Overrun allowance per route | K1 cost half |
| 0.8 Verification spikes (Mantle SigV4 with assumed-role credentials, cross-account cache scope, Anthropic use-case, GLM/DeepSeek caching) | Open questions 2, 3, 8 | Phase 0 rows "cache isolation" and "verification spikes" |
| 0.9 ADR sign-off by security (ADR-001..004) | Governance | Phase 0 row "ADRs signed off" |
| 0.10 Real Bedrock prices and the break-even report | Cost lines (only the TBD placeholder and fake dev prices exist today) | K1 cost; **Phase 2 does not start without it** (06 §11 row 2) |
| Datasets for Phase 0 | `adversarial-v1` | SE5, SE10, SE7 recall |
| K1 refit | A refit per call kind, with more live data per model, shipped through `NOEMA_TOKEN_ESTIMATOR_TABLE` (no code change) | K1 tokens |

### 8.2 Recommended next steps (engineering; ordered)

The previous draft listed two steps that are now done. The Wave F tree is tagged (`wave-f` = `10aa2d4`), and the
release gate re-gated it on its own images (section 3.6).

1. **Done: the release gate's changes are committed** (tag `phase1-mvp`). The docs owners should still review the
   docs edits of section 7.2. The OWNERS.yaml `repo-docs` rule of Wave F is platform-owned, and the orchestrator
   confirms it.
2. **Owner review** of the items in section 4.1: IMPL-089 (8 pods, uv 0.13.0), the IMPL-087 amendment (429 before
   401), IMPL-091 (platform-account storage, D2), the ADR sign-off and the live-call budget.
3. **Fix the remaining drift** (section 7.2). Decide F10's cancel semantics with 2 processes per pod: either reword
   spec-04 §12 and spec-10 F10, or forward cancels between a pod's processes (section 7.1 item 12). Make
   `qa-security-sca` report a pip-audit that did not run (section 7.1 item 13).
4. **Write down the platform roles' S3 and KMS permissions** on `noema-<env>` and the tenant keys (IMPL-091, IaC).
5. **Before fixing N, measure the knee on the target instance type** with N pods on shared Postgres and Valkey,
   TLS to the upstream, and the production mix. Run the 24 h soak (section 7.1 items 3-5).
6. **Verify the SigV4 session-token branch live, on Bedrock, with AssumeRole credentials from Phase 0 accounts.**
   The E3 live role tried to spend the 2 remaining calls of the 87 on it, but writing the session credentials to a
   file was denied ([qa/e3/live-smoke.md](qa/e3/live-smoke.md) §6). More calls need an owner budget.
7. **Change requests to fakes-telemetry for the registry gaps** (section 7.3). The most important are a
   gateway-overhead histogram (then `noema-overhead-slo-burn` can page again), a request counter by status, and
   every label-less counter exported at 0 at start (the first and the last are filed in
   `.orchestration/requests/observability.md`). Fix the `member=""` observation in
   `src/noema/decision/run.py`.
8. **qa-tools:** a multi-process compose mock for the gates stack (section 7.1 item 10).
9. **Security:** a reporting channel for SECURITY.md; a pen test before GA (SE14); the waiver review on
   2027-01-09; track the interpreter's bundled libraries.

## 9. How to reproduce

Run everything from `/codes/aghooai/noema` unless noted.

**The release gate (section 3.6)** has its full command list in [qa/release-gate.md](qa/release-gate.md) §9. It
builds the `:rgate` images, runs every suite on its own compose project with a port base below 32768 (21100-21600,
load stack 22000+) and runs the load plans `rg-l1-100`, `rg-ramp`, `rg-c5` / `rg-c5-run2` and `rg-c3`. Its
results are in `reports/qa/release-gate/`, and its analysis helpers are in `/tmp/noema-rg/bin/` on the build host.
Each stack is brought down with its `*-down` target afterwards. The `:rgate` images and the
`~/.noema/stacks/noema-rg-*` state files were kept as the gated artifacts; remove them with
`docker rmi noema-api:rgate noema-worker:rgate noema-media:rgate noema-media-test:rgate`.

The commands below are history: Wave F and E3.

**Commands this author ran on the Wave F tree (section 3.5):**

```
NOEMA_AGENT_ID=acceptance make -k check XDIST=4
make ownership
make observability-check
make qa-security-local
make docs-drift
make sdk-compat
cd /codes/aghooai/docs/noema && ./check_docs.sh
wc -l .orchestration/reports/live-calls.jsonl          # 85 ledger entries
```

**Commands the final docs fixer ran after its fixes (section 3.5):**

```
make ownership
NOEMA_AGENT_ID=wffix make check XDIST=4
make observability-dashboards && make observability-check
cd /codes/aghooai/docs/noema && ./check_docs.sh
UV_PROJECT_ENVIRONMENT=/tmp/wffix-venv uv sync --frozen        # = make sync-dev, outside the shared .venv
export UV_PROJECT_ENVIRONMENT=/tmp/wffix-venv NOEMA_API_IMAGE=noema-api:wfinfra \
    NOEMA_WORKER_IMAGE=noema-worker:wfinfra NOEMA_MEDIA_IMAGE=noema-media:wfinfra
make e2e-up E2E_BUILD=0 E2E_PROJECT=noema-wffix E2E_PORT_BASE=36300
# the README curl turn; then Prometheus on the stack network (docs/operations/observability.md §2.6)
make e2e-smoke E2E_BUILD=0 E2E_PROJECT=noema-wffix E2E_PORT_BASE=36300
make e2e-down E2E_PROJECT=noema-wffix E2E_PORT_BASE=36300
```

**The E3 re-gate commands** (QA lead, [qa/e3/qa-lead.md](qa/e3/qa-lead.md) §4; all are Makefile targets; each
stack uses its own project and port base; for `make e2e` the QA lead set the `NOEMA_*_IMAGE` variables to the
`:qae3` images):

```
NOEMA_API_IMAGE=noema-api:qae3 NOEMA_WORKER_IMAGE=noema-worker:qae3 NOEMA_MEDIA_IMAGE=noema-media:qae3 \
    make e2e E2E_PROJECT=noema-qae3-e2e E2E_PORT_BASE=34200 E2E_BUILD=0
make ui-check
NOEMA_UI_PORT=34290 make ui-e2e
make qa-contract QA_CONTRACT_PROJECT=noema-qae3-contract QA_CONTRACT_PORT_BASE=34400 QA_CONTRACT_BUILD=0
make qa-security QA_SEC_PROJECT=noema-qae3-sec QA_SEC_PORT_BASE=34500 QA_SEC_TAG=qae3 QA_SEC_BUILD=0 \
    QA_SEC_UI_PORT=34590 QA_SEC_OUT=~/.noema/qae3-security QA_SEC_REPORTS=reports/qa/e3/qa-lead/security
make qa-fault FAULT_PROJECT=noema-qae3-fault FAULT_PORT_BASE=34300 FAULT_IMAGE_TAG=qae3 FAULT_BUILD=0 \
    FAULT_RESULTS=reports/qa/e3/qa-lead/fault-results.json
make qa-gates QA_GATES_PROJECT=noema-qae3-gates QA_GATES_PORT_BASE=34600 QA_GATES_TAG=qae3 QA_GATES_BUILD=0 \
    QA_GATES_RESULTS=reports/qa/e3/qa-lead/gates-results.json QA_GATES_OUT=~/.noema/qae3-gates
make test-media IMAGE_TAG=qae3
make image-scan IMAGE_TAG=qae3 IMAGE_SCAN_OUT=~/.noema/qae3-image-scan
```

Before the release gate, the plan for re-gating the Wave F tree was to run `make images IMAGE_TAG=wfinfra`, then
point each target at those images. The release gate did this with `IMAGE_TAG=rgate` instead:
- `qa-security`: `QA_SEC_TAG=wfinfra`;
- `qa-gates`: `QA_GATES_TAG=wfinfra`;
- `qa-fault`: `FAULT_IMAGE_TAG=wfinfra`;
- `test-media` and `image-scan`: `IMAGE_TAG=wfinfra`;
- `e2e` and `qa-contract`: these read the compose variables, so set `NOEMA_API_IMAGE=noema-api:wfinfra`,
  `NOEMA_WORKER_IMAGE=noema-worker:wfinfra` and `NOEMA_MEDIA_IMAGE=noema-media:wfinfra`, as the QA lead did for
  `:qae3`.

Bring every stack down afterwards with its `*-down` target: `make e2e-down`, `make qa-contract-down`,
`make qa-security-down`, `make qa-fault-down` and `make qa-gates-down`.

**The Wave F images, scan and container knee** (infra-perf; full commands in [perf/sizing.md](perf/sizing.md) §9):

```
make images IMAGE_TAG=wfinfra
make image-scan IMAGE_TAG=wfinfra IMAGE_SCAN_OUT=reports/perf/wf-infra/image-scan
UV_NO_SYNC=1 uv run --frozen --no-sync python -m tests.load.run --state ~/.noema/stacks/noema-wfinfra-perf.json \
    --out /tmp/noema-wf-infra/runs/wf-l1-b --api-port 35090 --mock-port 35081 --api-cpus 46-47 --api-procs 2 \
    --stack-cpus 44-45 --mock-cpus 16,40,17,41,18,42,19,43 --driver-cpus 4-15,28-39 \
    --api-image noema-api:wfinfra --plan 125:600,150:500 --warmup-s 60 --settle-s 30 \
    --results /tmp/noema-wf-infra/results/wf-l1-b.json
```

That run needs its own seeded stack first (`docker compose -p noema-wfinfra-perf ... up`, `tools.bootstrap_dev`,
`tests.load.stack activate`; sizing §9). The runner script, the loadavg monitors and the step-end metric scrapes of
these runs are copied to `reports/perf/wf-infra/load/`; the `/tmp/noema-wf-infra/` paths above were the originals.

**Not to be run in Wave F or at the release gate:** the live smoke. The ledger is at 85 of 87, and no live Bedrock
calls are allowed.
