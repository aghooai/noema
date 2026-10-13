# Changelog

## 0.1.0: Phase 1 MVP (2026-10-12)

The first release: the Phase 1 scope of the design's implementation plan (05 §Phase 1), built in waves A to F.
Gate status on the final tree (tag `wave-e3`): **50 PASS, 1 FAIL (K1), 12 NOT-RUN** of the 63 Phase 1 rows; every
NOT-RUN needs Phase 0 data, Bedrock spend, AWS accounts, production or the pre-GA pen test
([reports/qa/wave-e-summary.md](reports/qa/wave-e-summary.md)). Numbers per wave come from the reports named and
from the wave's tagged commit message.

### What it does

- **API**: turns over SSE and JSON with cancel; `/v1/decisions` (native and Jev-shaped) and the OpenAI-compatible
  decisions facade `/openai/v1/decisions`; profiles; sessions with ordering and history; files with sandboxed
  derivatives and events; usage; read-only admin (calibrators, labeling runs, dropped items); health. Contract
  revision 6, `Aghoo-Version: 2026-10-01`.
- **Decisions**: `lightweight-model-decide` (gpt-6-luna, the default, D6) and the `router-default` ensemble (Luna,
  Haiku, DeepSeek); logprob and verbalized members; time-triggered backups; linear pooling; heuristic confidence;
  `router-plus` escalation on `/v1/decisions`; the hierarchical method for more choices than one call carries;
  the L2 decision cache.
- **Generation**: `single`, `fallback` and `race` profiles on Converse/ConverseStream; holdback, invisible restart
  on a changed spec, bindings and conditioning, post-commit verification.
- **Tenancy and security**: API keys and operator keys, a role and scope matrix, RLS, one STS session per (pod,
  tenant) with ExternalIds, spotlighting and escaping, a nsjail media sandbox, a CSP-locked Web UI.
- **Ops**: GCRA admission over the whole work graph, breakers, retry budget, write-behind outbox, jobs, retention,
  price catalog and cost meter, Prometheus metrics, `noema-admin` with two-person approvals.
- **Clients**: the TypeScript (`@aghoo/sdk`) and Python (`aghoo`) SDKs; the Web UI (W1 chat and inspector, W2 uploads,
  W4 calibration, W7 cost).

### Waves

| Wave (tag) | Date | Summary |
|---|---|---|
| A (`wave-a-design`) | 2026-10-09/10 | design analysis: the canonical implementation decisions ([docs/IMPL-DECISIONS.md](docs/IMPL-DECISIONS.md)), the implementation spec ([docs/impl-spec/](docs/impl-spec/README.md)), ADR-001 to ADR-004, the owner checkpoint and answers (hierarchical method built in Phase 1); contract revision 6 applied to the design repo (54 examples, 49 config negatives, 49 audit cases) |
| B (`foundation-freeze`) | 2026-10-10 | frozen foundation: locked toolchain, `core` Protocols, contracts, config, database schema with RLS and definer functions, api skeleton with typed errors and 501 stubs, fakes with conformance suites and golden SSE fixtures, telemetry, compose and the media image |
| C1 (`wave-c1`) | 2026-10-10 | 12 domain modules (identity, adapters, wire mock, decision, generation and streaming, turns, media pipeline and negotiation, admission, ledger, labeling, SDKs), each implemented, independently reviewed and fixed |
| C2a (`wave-c2a`) | 2026-10-10 | change requests applied to the frozen areas (db revision `0002_c2a_requests`), module adaptations, rulings R1-R5 |
| C2b + D (`wave-c2b`) | 2026-10-11 | the real composition, worker entrypoints, `tools/bootstrap_dev.py`, the compose `app` profile, the Web UI, the e2e suite (52 tests over two replicas), Playwright, the load harness and a perf smoke ([reports/perf/c2b-perf-smoke.md](reports/perf/c2b-perf-smoke.md)) |
| E (`wave-e`) | 2026-10-11 | perf sprints A (lean Converse transport) and B (2 api processes per pod, per-process cap with 429 `queue_timeout`, GC tuning); QA tracks contract, security, fault, live smoke; a 4-lens code review and fixes. Gates: 24 PASS, 17 FAIL, 4 PARTIAL, 18 NOT-RUN |
| E2 (`wave-e2`) | 2026-10-12 | remediation: rulings R6 (a fallback is not a retry) and R7 (ingress, idempotency, storage); fixes for slow clients, NUL ids, cross-pod attach, model quota windows, fallback success, breaker rules; base-image waivers and SBOMs; new gate harnesses (L2/L3, L5, R4, M3, SE7, Q9). Gates: 44 PASS, 4 FAIL, 3 PARTIAL, 12 NOT-RUN |
| E3 (`wave-e3`) | 2026-10-12 | perf sprint 2 (object churn and GC thresholds (700, 10, 100); a SigV4 signer and eventstream decoder byte-identical to botocore; the edge shed with max-min fair share); rulings R8 (retry budget) and R9 (U+0000); datastore pool metrics; live re-verification (ledger 85 of 87); sizing from the measured knee ([reports/perf/sizing.md](reports/perf/sizing.md)). Gates: 50 PASS, 1 FAIL, 12 NOT-RUN |
| F | 2026-10-12 | documentation, operability and acceptance: these as-built docs (README, architecture, operations, development, CONTRIBUTING, SECURITY), observability as code (`deploy/observability/`: Prometheus recording and alert rules, Grafana dashboards), the exported streams-per-pod gauge `aghoo_turn_slots_held`, the api image on a python-build-standalone interpreter with the container knee re-measured (125 streams per pod,
[reports/perf/sizing.md](reports/perf/sizing.md)), design revision 7 "as built" (synced to the Wave F sizing; the remaining drift is listed in [reports/mvp-acceptance.md](reports/mvp-acceptance.md) section 7.2), the MVP acceptance report. Working tree, not tagged yet |

### Changed from the design (revision 6)

The deviations, each with its decision record, are listed in [docs/architecture.md](docs/architecture.md)
section 8. The largest: pods are sized at the measured knee (at most 125 streams per 2-vCPU pod with 2 api
processes on the Wave F image, planned at 100: 8 pods for ~800 streams) instead of ~200; a per-process cap answers 429 `queue_timeout` under overload; the body
is read only after authentication; at most 64 media parts per request.

### Known issues and limits

- **K1 fails** (token estimator within 2% per call): 14 of 25 live calls within 2% with the shipped per-model
  table; a refit needs Phase 0 data (`NOEMA_TOKEN_ESTIMATOR_TABLE` loads one without a code change).
- **L1 as written fails** at 200 streams per pod (p50 11.70 ms, E3); it passes at the sized density (Wave F image at
  125 per pod: p50 9.72 and 9.73, p95 14.06 and 13.98 ms, [reports/perf/sizing.md](reports/perf/sizing.md) §6).
- Not run: the 24 h soak (1 h run: RSS +0.76%), multi-pod load on shared datastores, the signer's
  `X-Amz-Security-Token` branch against live Bedrock (every live call used long-term keys,
  [reports/qa/e3/live-smoke.md](reports/qa/e3/live-smoke.md)), the pre-GA penetration test, every Phase 0 gate.
- No calibrator is active: decisions are served `uncalibrated` until Phase 0 labels exist.
- No production infrastructure code (Kubernetes, AWS IaC), no edge or OIDC login, no tenant purge command.

### Not in this release (design Phase 2 and later)

Stream resume (`GET /v1/turns/{id}/events` answers 501), `best_of_n` and `fuse`, `speculative` and `hedge`
strategies, in-turn escalation, end-user delegation (`x-aghoo-end-user` answers 422), OpenAI generation facades,
audio, video, SVG and DOCX inputs, response and semantic caches, budgets.
