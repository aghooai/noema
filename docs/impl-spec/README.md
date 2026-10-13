# Noema Phase 1 implementation spec: index

Wave A output (design only), consolidated 2026-10-09. The design base is `docs/noema` revision 5 (read-only). The canonical decision list is [`../IMPL-DECISIONS.md`](../IMPL-DECISIONS.md) (IMPL-001…084; one resolution per gap 1–46; amended in the Wave A2 review). What the owner must review before Wave B is in [`OWNER-CHECKPOINT.md`](OWNER-CHECKPOINT.md).

Precedence when documents disagree:
1. owner decisions D1–D6 and R5;
2. `IMPL-DECISIONS.md`;
3. spec-02 (frozen interfaces);
4. the other spec documents;
5. the ADRs;
6. `docs/noema` rev 5 (rev 6 once the combined patch is applied; rev 7, "as built", since Wave F, 2026-10-12);
7. the analyst files in `../impl-decisions/` (traceability only).

## Documents

| # | Document | Contents | Status |
|---|---|---|---|
| 01 | [module map](01-module-map.md) | packages, import layers, composition root, processes | final for B1 |
| 02 | [frozen interfaces](02-interfaces.md) | every `core/` value type and Protocol, the adapter wire and error mapping, RedisKeys, the conformance suites | **frozen at the B tag** |
| 03 | [data model](03-data-model.md) | roles, schemas, DDL, RLS, SECURITY DEFINER functions, retention, db tests | final for B2 (db) |
| 04 | [turn lifecycle](04-turn-lifecycle.md) | ingress order, session ordering, timing and commit rule, event sequence, bindings, conditioning, attempts, verification, persistence, cancel/abandon, failures | final for C1 |
| 05 | [config extensions](05-config-extensions.md) | rev-6 config schema additions (incl. `Model.context_window`, §1.5), the price catalog, loader and P1 rules, process environment (§5) | final for B1 (config) |
| 06 | [decision engine](06-decision-engine.md) | the complete decision spec (compiler, templates, members, pool, calibration, escalation, run, Jev, facade, L2, errors, tests) | final for C1 |
| 07 | [media](07-media.md) | types and limits, the type-equivalence map, upload and inline pipelines, the `FileObject` mapping (§3.3), negotiation, decision views, derivative download | final for C1 |
| 08 | [contract patches](08-contract-patches.md) | the verified rev-6 base payload (Appendix A/B) plus **§14, the consolidation addendum** | applied by the contract-patch applier after the checkpoint (IMPL-067) |
| 09 | [security](09-security.md) | authn, the authz matrix, secrets and redaction, injection controls, media hardening, UI, supply chain, the security gate map | final for C1 |
| 10 | [QA and test strategy](10-qa-test-strategy.md) | test layers, the wire mock, the scenario DSL (§2.4), the 06 §11 gate map and the module test ids behind it (§3.1), the fault suite, load tooling, the live-smoke budget, dependencies | final for C1 (qa-tools) and E |
| 11 | [ops](11-ops.md) | work graph and admission, cost and usage, outbox, jobs, idempotency, retention, reapers, health and shutdown | final for C1 |
| ADR-001 | [tenancy and credentials](../adr/ADR-001-tenancy-credentials.md) | STS session cache, fail-closed causes, ExternalIds, account assertion, Mantle auth, moto tests | adopted (IMPL-007) |
| ADR-002 | [media sandbox](../adr/ADR-002-sandbox.md) | spooler/jailer topology, privileges, nsjail, seccomp, per-kind limits, IPC, backends, tests | adopted (IMPL-081) |
| ADR-003 | [calibrator scope](../adr/ADR-003-calibrator-scope.md) | binding key, artifact, precedence, gate A1–A8, two-person activation, drift, runtime | adopted, amended (IMPL-072/073) |
| ADR-004 | [conditioning placement](../adr/ADR-004-conditioning-placement.md) | end of the final user turn; signature; switch procedure | adopted (IMPL-082) |

## Reading order per role

Every role reads first: `IMPL-DECISIONS.md` (index table plus the entries listed for the role), spec-01, and spec-02 §1. Then:

| Role (wave) | Read, in order | Key IMPL ids |
|---|---|---|
| **foundation / platform engineer (B1)** | spec-02 (all), spec-01, spec-05, spec-08 §2–§4, §10.4 and §14, spec-03 §1, spec-10 §7, ADR-002 §7, spec-09 §4 | 030, 038, 039, 041, 063, 064, 066, 070, 076, 078, 080, 084 |
| **db (B2)** | spec-03 (all), spec-02 §7, ADR-003 §1–§2, spec-11 §3–§6 | 009, 010, 022, 071, 072, 077 |
| **api skeleton (B2)** | spec-04 §1, spec-08 §2–§5 and §14.1, spec-09 §2–§3, spec-02 §11 and §13 | 030–034, 041, 064, 066, 083, 087 |
| **fakes + telemetry (B2)** | spec-02 (all fakes and suites §14), spec-04 §4.2 (golden SSE fixtures), spec-10 §2.4 (scenario DSL), spec-09 §4 (redaction), 08 §7 | 006, 012, 029d, 029e |
| **identity** | spec-09 §2–§4, ADR-001 (all), spec-03 §2–§4 (auth, operator, admin functions), spec-05 §3 (activation), spec-04 §2 (sessions), spec-08 §9 | 007, 008, 044, 083, 040, 072 (CLI part) |
| **adapters** | spec-02 §3–§4 (the wire mapping and error table are binding), ADR-001 §4–§8, spec-06 §5 (the request shapes they must honor), spec-10 §2 (mock), §6 (live smoke) | 050, 053, 054, 076, 078, 017, 043 |
| **qa-tools** (wire mock; not the adapter author) | spec-10 §2 (all), spec-02 §4.2 (the wire format to emulate), spec-06 §5.4–§5.7 and T-LP-1, T-VB-1, spec-07 §4 (Bedrock limits), `03-env-probes-gates.md` quirks | 080, 050 |
| **decision** | spec-06 (all), spec-02 §5, §6, §9, ADR-003 §1–§4, spec-08 §14.2–§14.5, spec-07 §5, spec-11 §1.1 | 001, 002, 004, 005, 025, 027 (hierarchical, spec-06 §19), 028, 031, 036, 047–061, 068–072, 074–076, 079, 086 (decision prompts) |
| **generation** (+ streaming) | spec-04 §3, §4, §6, §10, §11, spec-02 §4 (stream), §7 (event log, SSE writer), §10, spec-11 §6 (watchdog), 02 §6.1–§6.8 | 011, 012, 015, 016, 020, 021, 034 |
| **orchestrator** | spec-04 (all), spec-06 §1.1, §2.4, §10, ADR-004, spec-09 §5, spec-07 §3.2 and §4, spec-02 §5 and §10 | 002, 003, 012–014, 018, 022–024, 026, 029a, 045, 046, 069, 082, 084, 086 |
| **media-pipeline** | ADR-002 (all), spec-07 §1–§3 and §6–§7, spec-09 §6, spec-03 (file, derivative, job), spec-11 §4 | 029f/g, 037, 062, 065, 074, 081 |
| **media-negotiation** | spec-07 §4–§5, spec-02 §6, spec-06 §4 and §5.3, 02 §8.3–§8.4 | 058, 075, 074 |
| **ops-admission** | spec-11 §1, spec-02 §8, spec-05 §1.1–§1.2, 04 §5 | 016, 027 (1 + 3 hierarchical calls), 029c, 078, 084 |
| **ops-ledger** | spec-11 §2–§7, spec-03 (usage, idempotency, jobs, outbox), spec-02 §7–§8, spec-05 §2 | 019, 020, 022, 040, 077, 085 |
| **labeling** | ADR-003 (all), spec-03 (labeling and calibrator tables, RLS, definer functions), spec-02 §9, spec-08 §14.1 item 5 (W4 types), IMPL-071 (ContentRef) | 039, 042, 043, 071, 072, 073 |
| **sdk** | spec-08 §10, §14.1, §14.7, IMPL-035, spec-10 §3 (facade conformance with the openai clients) | 033–036, 040, 042, 047 |
| **ui** (from C2) | spec-09 §2 (dev login), §6.8, §8, spec-08 §9 and §14.1 item 5 (W4 types), 03 §10 (W1/W2/W4/W7) | 042, 065, 083 |
| **integrator (C2/D)** | spec-01 §3–§4, spec-02 §14, spec-04 (all), spec-10 §1 and §3 | 070, 078, 087 |
| **contract-patch applier (after the checkpoint)** | spec-08 §0, §12, §14 (procedure, incl. the §14.0 owner-answer deltas), OWNER-CHECKPOINT.md §b and "Owner answers (2026-10-10)" | 067, 027, 036 |
| **QA (E)** | spec-10 (all), spec-09 §10, ADR-001 tests, ADR-002 tests, spec-03 §6 | 080 |

## Frozen interfaces (spec-02; frozen at the Wave B tag; changes only by change request at C2)

| Module (`src/noema/core/…`) | Interfaces | Spec |
|---|---|---|
| `ids.py`, `clock.py` | `IdGen`, `Clock`, `Deadline` | 02 §1 |
| `errors.py` | `ErrorType`, `ErrorCode` (36), `ErrorSpec`, `ERROR_TABLE`, `NoemaError`, `ErrorKind`, `ClassifiedError`, `TenantAccessMisconfigured` | 02 §1, 08 §2 |
| `values.py`, `principal.py`, `text.py`, `secret.py` | `UsageRecord`, `Cost`, `Degradation`, content blocks, `Message`, `Principal`, `system_principal`, `TenantRef`, `clean_text`, `escape_reserved_tags` (one argument), `datamark`, `SecretStr` | 02 §1 |
| `protocols/config.py` | `ConfigRegistry`, `ConfigSnapshot` (`route_timeouts(model_alias, effort)`, `prices: PriceCatalog`; + `*Cfg` from `noema.config`) | 02 §2, 05 |
| `protocols/credentials.py` | `CredentialBroker`, `TenantCredentials`, `AwsCredentials`, `MantleAuth`, `assert_creds_for`, `SecretsProvider` | 02 §3, ADR-001 §4 |
| `protocols/adapters.py` | `Adapter`, `AdapterRegistry`, `ModelRequest`, `ModelResponse`, `TokenLogprob`, `StreamEvent` union, `StreamTimeouts`, `ModelCapabilities`, `AdapterError`; **the wire-mapping and error-classification tables** | 02 §4 |
| `protocols/decision.py` | `DecisionEngine` (`run` async CM, `decide`), `DecisionRun`, `DecisionContext`, `DecisionRequest`, `ProfileRef`, `DecisionSnapshot` (incl. `row`), `AnswerSummary`, `QuestionCompiler`, `DecisionCache`, `CompiledQuestion(Set)`, `OptionSpec`, `DecisionInput` | 02 §5, 06 §3.2, §4.1 |
| `protocols/media.py`, `protocols/sandbox.py` | `ContentPart`, `InputViewResolver`, `InputView`, `ViewPart`, `DecisionViewBudget` + `VIEW_BUDGET_V1`, `FileService` (incl. `ingest_inline`, `read_derivative`), `FileRef`, `DerivativeRef`, `MediaNegotiator` (async), `MediaBudgetPlanner`, `MediaCaps` + `CONVERSE_CAPS_V1`, `PlannedPart`, `MediaPlan`, `SandboxRunner` + job/limits/result types | 02 §6, 07 §4.4, ADR-002 §7 |
| `protocols/storage.py`, `protocols/events.py` | `Row`, `Database` (`uow`, `tenant_uow`, `system_uow`), `UnitOfWork` + the §7.1 repo Protocols (`SessionRepo.acquire/renew/finalize_turn/release`, `TurnRepo`, `AttemptRepo`, `DecisionRepo`, `FileRepo`, `DerivativeRepo`, `UsageRepo`, `LabelRepo`, `CalibratorRepo`, `IdempotencyRepo`, `AuditRepo`, `ProjectRepo`, `JobRepo`), `SessionLease`/`Busy`/`LeaseLost`, `SystemCalls`, `Page`, `JobQueue`, `Job`/`Worker`/`PermanentJobError`, `IdempotencyStore` (`begin`/`await_result`/`heartbeat`/`complete`) + `StoredResponse`/`IdemToken`/`New`/`InFlight`/`Replay`/`Conflict`, `Outbox` + `OutboxRecord`, `TurnEventLog`, `SseFrame`, `SseWriter` | 02 §7 |
| `protocols/ops.py` | `HealthRegistry`, `RetryBudget`, `DecisionWork`, `GenerationWork`, `WorkGraphPlanner`, `WorkGraph`, `Admission`, `Reservation`, `TokenEstimator`, `CostMeter`, `UsageLine` | 02 §8 |
| `protocols/labeling.py` | `CalibratorBindingKey`, `Transform`, `Calibrator`, `CalibratorRegistry`, `LabelingSampler`, `ContentRef` | 02 §9 |
| `protocols/generation.py` | `AttemptSpec`, `Conditioning`, `AssembledPrompt`, `PromptBuilder`, `AttemptBudget`, `TurnContext`, `AttemptRecord`, `GenEvent`, `ProfileRun`, `GenerationEngine` (`run` async CM) | 02 §10 |
| `protocols/auth.py` | `Authenticator`, `Authorizer` (`check`, `list_scope`), `Action`, `ResourceRef`, `ListScope` | 02 §11, 09 §3 |
| `protocols/telemetry.py` | `Telemetry` (`span`, `instruments`, `redact`), `SpanName`, `Span`, `Instruments` | 02 §12, IMPL-006 |
| `redis_keys.py` | `RedisKeys` | 02 §13 |

Also frozen at the B tag (not Protocols):
- `contracts/` (the vendored rev-6 tree, generated strict models, `RequestGate`, `ContractValidator`);
- `config/` (models, loader, P1 rules);
- `db/` (migration of spec-03, roles, `uow`);
- `api/` middleware and SSE framing;
- `tests/{fakes,golden,contract_internal}`.
