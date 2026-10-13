# Noema Phase 1: implementation decisions (canonical)

Wave A consolidation by the lead architect, 2026-10-09; Wave A2 independent review, 2026-10-10 (entries amended in place, ids unchanged; IMPL-084 added); owner answers of 2026-10-10 applied the same day (IMPL-027 and IMPL-036 revised: the hierarchical method is built; IMPL-004, 029, 069, 077 and 079 amended; IMPL-085 and IMPL-086 added; see `impl-spec/OWNER-CHECKPOINT.md`); Wave C2a, 2026-10-10: orchestrator rulings R1–R5 recorded as amendments of IMPL-027, IMPL-012, IMPL-075, IMPL-078 and IMPL-072 (marked *Amendment*); Wave E2, 2026-10-11: orchestrator rulings R6 and R7 and the Wave E fixer rulings recorded as amendments of IMPL-004, 016, 022, 030, 050, 064, 076, 077 and 078, and IMPL-087 added (api process sizing and overload); Wave E3, 2026-10-12: rulings R8 and R9 and the owner decision of 2026-10-12 recorded as amendments of IMPL-016, IMPL-022 and IMPL-080 (plus IMPL-029c and IMPL-078), and IMPL-088 added; Wave F, 2026-10-12: IMPL-087 amended (the edge shed, GC thresholds `(700, 10, 100)`, pods sized from the measured knee) and the pending `doc` patches applied in `docs/noema` revision 7 ("as built"); Wave F release gate, 2026-10-12: IMPL-089 amended (the uv 0.13.0 bump), IMPL-090 (operator proof) and IMPL-091 (object storage as built, owner review) added. Design base: `docs/noema` revision 5 (read-only in Wave A).

This file is the **single canonical list** of implementation decisions. Each numbered gap in `.orchestration/context/01-gaps.md` (1–46) has exactly one resolution here, with id `IMPL-0kk` = gap *kk*. `IMPL-047` and later are decisions that are not in the gap list: they came from the analysts while they wrote the specs, or from the two critic passes. The analyst files in `docs/impl-decisions/` are kept for traceability only. Where they differ from this file, this file wins. The "Traceability" section at the end maps their old ids (`IMPL-DEC-n`, `IMPL-CON-n`, `IMPL-SEC-*`) to the ids used here.

The security, architect and QA analyst outputs were never written to disk. The lead architect therefore authored the resolutions for gaps 3, 4 (merged), 6, 8–24, 26, 29, 38, 39 and 43–46 directly; their source is marked "lead architect". ADR-001 and ADR-002 survive from the security analyst and are adopted as written (IMPL-007, IMPL-081), with their references corrected.

Ref keys: `01`…`10` = `docs/noema/0x-*.md`; `README` = `docs/noema/README.md`; `types` = `contracts/types.ts`; `openapi` = `contracts/openapi.yaml`; `dschema` = `contracts/decision.schema.json`; `oaschema` = `contracts/openai-compat.schema.json`; `cschema` = `contracts/config.schema.json`; `ce` = `contracts/check_examples.py`; `cc` = `contracts/check_config.py`; `yaml` = `contracts/examples/platform.example.yaml`. `Lnnn` = line number in revision 5. The spec documents are in `docs/impl-spec/` (`spec-0N` = `impl-spec/0N-*.md`).

**Material** (Y) means the decision changes externally visible API behavior, adds API surface, or touches an owner decision (D1–D6, R5). `OWNER-CHECKPOINT.md` lists the material decisions the owner must see. **Patch**: `none`, `doc` (prose in `docs/noema`), or `contract` (machine-checked files in `docs/noema/contracts`). Every patch is applied through the single combined rev-6 payload (IMPL-067), and only after owner review.

Fixed owner decisions, never changed here: D1 Bedrock only · D2 one AWS account per tenant (AssumeRole + ExternalId) · D3 no regulatory regime · D4 unanimous LLM-consensus labels (gpt-6.1-sol + claude-sonnet-5-5) · D5 DeepSeek in, Kimi out · D6 `lightweight-model-decide` (gpt-6-luna) is the `/v1/decisions` default, plus the Jev-like output · R5 early commit OFF, 255-choice limit with the hierarchical method [X], answers uncalibrated until a per-stage calibrator passes.

## Index

| Id | Gap | Title | Material | Patch | Owning module |
|---|---|---|---|---|---|
| IMPL-001 | 1 | Status at the deadline: `completed` when quorum is met; `partial` iff some answer is `unavailable` | N | doc + contract comment | decision |
| IMPL-002 | 2 | `quorum` is the in-turn snapshot while non-blocking answers are still open; at most one `decision.updated`; bindings read final answers only | Y | doc + contract comment | decision, orchestrator |
| IMPL-003 | 3 | `speculation.holdback_ms` is validated but has no runtime effect in Phase 1 | N | doc | orchestrator |
| IMPL-004 | 4 | L2 decision cache: 8-part key, get/put gates, fail-open, hit semantics | N | doc | decision |
| IMPL-005 | 5 | Config `any_refused` is recorded as `triggered_by: refused` | N | doc | decision |
| IMPL-006 | 6 | Telemetry: the 08 §7 span and metric names win; additions (incl. `aghoo_reservation_overrun_total`, `aghoo.decision.cache_hit`) are registered in B1 | N | doc | foundation (telemetry) |
| IMPL-007 | 7 | One cached STS session per (pod, tenant) with single-flight refresh; botocore windows 600/300 s, no default chain, dev/test-only endpoint overrides; Mantle auth (ADR-001) | Y (D2 wording) | doc | identity, adapters |
| IMPL-008 | 8 | API keys: HMAC-SHA256 with the versioned pepper `noema/api-key-pepper` (row's `pepper_version`), env check, key-id lookup, LRU cache | N | doc | identity |
| IMPL-009 | 9 | RLS: `tenant_id text` on every tenant row; FORCE RLS; SECURITY DEFINER functions (`search_path = pg_catalog, pg_temp`, no PUBLIC EXECUTE) for pre-tenant and cross-tenant paths; platform calibrator rows | N | doc | db (foundation) |
| IMPL-010 | 10 | Phase 1 table set (spec-03), incl. `platform.price_catalog`; `'__platform__'` periodic jobs | N | doc | db (foundation) |
| IMPL-011 | 11 | An attempt is one model call; the outcome enum is mapped from the FSM terminals; deadline cancels emit no frame | N | doc | generation |
| IMPL-012 | 12 | Canonical SSE event order; commit point `max(S1, first_text)`; `decision.completed` always emitted | Y | doc (03 example) | orchestrator, generation |
| IMPL-013 | 13 | `direct`, `none` and `off` render no block; no intent hint in Phase 1 | N | doc | orchestrator |
| IMPL-014 | 14 | spec0 never carries a conditioning block and takes the request pins; `sticky_spec` JSON; the signature includes the template id; `restart_on_conditioning_change` defined | N | doc + contract comment | orchestrator |
| IMPL-015 | 15 | Effort is a profile-level override applied to each member that accepts it | N | none | generation |
| IMPL-016 | 16 | Fallback to `default_profile`: counts toward a turn-wide cap of 2 failure attempts; `RetryBudget` gates same-model re-sends only, never a fallback to another model (R6, Wave E2), and is 10% of first sends with no floor, an unsent connect retry exempt (R8, Wave E3); no fallback on `tenant_access`; the spec restart is counted separately | N | doc + contract comment | generation, orchestrator |
| IMPL-017 | 17 | No `cachePoint` in Phase 1; prefix discipline only | N | doc | orchestrator, adapters |
| IMPL-018 | 18 | `response_cache` and the response purge are recorded no-ops; `log_content` is a real persistence policy | N | doc | orchestrator, ops-ledger |
| IMPL-019 | 19 | Price catalog: `prices.yaml` imported as a `platform.price_catalog` version, pinned by `prices_ref: db:<n>`; key (Bedrock id, region), cache writes at `cache_write_5m`, Decimal math | N | doc | ops-ledger |
| IMPL-020 | 20 | Usage lines: six canonical names, assignment rules, per-line granularity and recorder, cancelled-call estimates | N | doc | ops-ledger, decision, generation |
| IMPL-021 | 21 | Per-turn event log: in-process log plus a Redis Streams mirror; 5-minute window; resume 501; createTurn 410 | Y | none (contract via IMPL-032/036) | generation (streaming) |
| IMPL-022 | 22 | Ordering-critical writes are synchronous (session lock before admission; `session_pending` before acquire); a write-behind outbox handles the rest; Postgres-down behavior | Y | doc | ops-ledger, orchestrator, decision |
| IMPL-023 | 23 | Deadline derivation formulas | N | doc | orchestrator |
| IMPL-024 | 24 | Verification runs if a binding asks for it OR `verify: true`; `verify: false` never skips; text-only verifier input | N | doc | orchestrator |
| IMPL-025 | 25 | Deadlines unchanged; in-turn escalation only inside the wait limit, checked per answer; targets inherit the questions | Y (R5 context) | doc | decision |
| IMPL-026 | 26 | `uncertain_profile` serves a scored low-band intent when no binding sets a profile; decision unavailable → spec0; `default_profile` required (P1 rule) | N | doc | orchestrator, foundation (config rules) |
| IMPL-027 | 27 | The hierarchical method is built in Phase 1 for native `/v1/decisions` and turn app questions, exactly per 08 §3.4, with engineering resolutions (spec-06 §19); other out-of-phase decision mechanisms stay config-rejected (P1-D1…D6) | Y (R5; owner answer 1) | doc + contract (checker, example, audit) | decision, foundation (config rules), ops-admission |
| IMPL-028 | 28 | `ordinal_dispersion` as in types.ts | N | doc | decision |
| IMPL-029 | 29 | The unspecified mechanisms (a)–(g): escaping (tags with whitespace or format characters after `<`; JSON-escaped conditioning values), spotlighting ((b) superseded by IMPL-086), decision TPM reservation, cache-write attribute, PII detection, PDF active content, type-equivalence map | N | doc | orchestrator, decision, ops, media-pipeline, telemetry |
| IMPL-030 | 30 | `not_found` and the complete ErrorCode table (1 code → 1 status) | Y | contract | foundation |
| IMPL-031 | 31 | `profile` optional; the tenant default (filled by the loader, D6); allowlist; resolution errors | Y (D6) | contract + doc | decision |
| IMPL-032 | 32 | Error responses per operation; the 504 pre-dispatch rule; `no_capable_member` dropped from the decision operations | Y | contract | foundation, decision |
| IMPL-033 | 33 | Headers; `x-aghoo-end-user` deferred to Phase 2 (422 everywhere) | Y | contract | foundation (api), identity |
| IMPL-034 | 34 | Strict `TurnEvent` discriminator, `ErrorCode` in TS, file SSE and heartbeat | Y | contract | foundation, generation |
| IMPL-035 | 35 | SDK strategy (generated wire types, hand-written clients) | Y | contract | sdk |
| IMPL-036 | 36 | Out-of-phase values: 422 `feature_not_available` or 501; a question that needs the hierarchical method is 422 `unsupported_parameter` on the facade and with `output_format: "jev"` only | Y (R5) | contract | decision, orchestrator, api |
| IMPL-037 | 37 | Type fixes; inline-image size checked by arithmetic; typed `x_aghoo`; facade rules in code | Y | contract | foundation, decision, media-pipeline |
| IMPL-038 | 38 | Config extensions (`platform`, `tenants.<t>.limits`, `labeling.acceptance`, `models.<alias>.context_window`); projects and keys in the DB; prices as a DB catalog version | Y | contract (cschema, cc) | foundation (config) |
| IMPL-039 | 39 | Constants come from the snapshot; config bounded by the contract constants; parameterized checker | N | contract | foundation, labeling, decision |
| IMPL-040 | 40 | `SessionUpdate`, `SessionTurn`, usage pagination, `key` | Y | contract | identity, ops-ledger |
| IMPL-041 | 41 | Strict server models, request gate, test-mode validator; `check_examples` importable | N | contract | foundation |
| IMPL-042 | 42 | Read-only W4 endpoints (tenant-wide for every allowed role); `CalibratorInfo` aligned with ADR-003 | Y | contract | labeling |
| IMPL-043 | 43 | Sonnet 5.5 media stays UNVERIFIED until the live smoke; negotiation falls back | N | none | labeling, adapters |
| IMPL-044 | 44 | `noema-admin` bootstrap with operator keys and two-person approvals; Phase-1 config integrity (no signed config) | N | doc | identity |
| IMPL-045 | 45 | Context: recent turns within budget, no summary; the compact decision context | N | doc | orchestrator |
| IMPL-046 | 46 | Language ID: none in Phase 1 (`und`) | N | doc | orchestrator |
| IMPL-047 | new | Response-side `AnswerName` (allows `q<digits>`); `x_aghoo` keyed by client-visible names | Y | contract (CP-DEC-1) | decision |
| IMPL-048 | new | Per-stage provenance on `EscalationStage` (CP-DEC-2) | Y | contract | decision |
| IMPL-049 | new | Jev and facade values derived from their own emitted maps; censored-tail edge cases | Y | doc | decision |
| IMPL-050 | new | Member calls: no retries, time-triggered backups, ModelRequest wire mapping; decision owns every prompt byte | N | doc (04) | decision, adapters |
| IMPL-051 | new | Option-order permutation and seed | N | doc | decision |
| IMPL-052 | new | `session_id` on `/v1/decisions`: access-checked, no history | N | none | decision |
| IMPL-053 | new | Exact decision templates (the spec text is canonical `@1`); provider-signalled refusals only | N | doc (08 §3.2) | decision |
| IMPL-054 | new | No automatic `textFormat` fallback | N | none | decision |
| IMPL-055 | new | `include` semantics; `calibrated_distribution` always emitted with members | N | none | decision |
| IMPL-056 | new | Facade remote image URL → 400 `schema_violation`; more than 20 images → 400 | Y | contract | decision |
| IMPL-057 | new | Decision request outcomes: 200 `partial`, the 503 rules, 504 before dispatch | Y | doc | decision |
| IMPL-058 | new | Quorum counting and classification order; `metadata_only` members are skipped | N | doc | decision |
| IMPL-059 | new | Input digest canonicalization; per-tenant HMAC key | N | none | decision |
| IMPL-060 | new | Largest-remainder 4-dp rounding; ordering on emitted values | N | none | decision |
| IMPL-061 | new | The final answer is the last stage's answer, even if `unavailable` | Y | none | decision |
| IMPL-062 | new | Phase-2 upload types → synchronous 415 `unsupported_media_type` before storage | N | doc | media-pipeline |
| IMPL-063 | new | Closed response schemas; named components | N | contract | foundation |
| IMPL-064 | new | Transport rules: 50 MiB JSON body cap, content types, unknown routes, unknown query parameters | Y | contract | foundation (api) |
| IMPL-065 | new | Derivative download: content types, `?page=` (out of range → 400), `pages`; `raster_1568` internal | Y | contract | media-pipeline |
| IMPL-066 | new | `x-noema-error-codes` / `x-noema-roles` plus conformance tests | N | contract | foundation |
| IMPL-067 | new | One combined rev-6 contract payload; `check_docs.sh` bundle step; patch hygiene | N | contract | contract-patch applier |
| IMPL-068 | new | `escalation.questions` filters platform questions only | Y | doc | decision |
| IMPL-069 | new | Turn `decisions.*` overrides (profile, deadline, quorum, include) | Y | doc (03:L117) | orchestrator, decision |
| IMPL-070 | new | DecisionRun lifecycle (async context manager, shielded finalize); protocol fields; snapshot types | N | none | decision, orchestrator |
| IMPL-071 | new | Decision row contents (incl. `principal_id`; questions, output format, input under the content policy); turns insert `DecisionSnapshot.row`; `ContentRef` | N | none | decision, labeling, db |
| IMPL-072 | new | Calibrator scope, lifecycle, two-person activation, registry runtime (ADR-003) | Y (R5, D4) | contract (CalibratorInfo) + doc | labeling, identity |
| IMPL-073 | new | Acceptance ECE defined per question type | N | doc | labeling |
| IMPL-074 | new | Inline-media ingest: the API never decodes; turn waits run in preflight (SSE errors); the decision clock starts once views are ready | N | doc | media-pipeline, decision |
| IMPL-075 | new | Decision view budget (code constant, versioned with the resolver) | N | none | media-negotiation |
| IMPL-076 | new | Error taxonomy (ErrorKind → member code → ErrorCode); adapters fetch credentials | N | none | adapters, decision, generation |
| IMPL-077 | new | Idempotency store: `begin(…, resource_id)`, `await_result`, which responses are stored (bodies over 1 MiB via IMPL-085, never `not_stored` for size), leases, conditional takeover | Y | doc (03:L39-40) | ops-ledger |
| IMPL-078 | new | Hot-path calls (`admit`, `consume`, `lookup`, `estimate`) are sync and in-memory | N | none | ops-admission, adapters, labeling |
| IMPL-079 | new | Heuristic confidence auxiliary fields defined exactly (`normalized_entropy` counts the unresolved bucket when `u > 0`) | N | contract comment | decision |
| IMPL-080 | new | Test strategy: wire mock, scenario DSL, gate map, live-smoke budget, dependency set | N | none | qa-tools, foundation |
| IMPL-081 | new | Media sandbox topology and backends; the Debian media image; no nh3 (ADR-002) | N | doc | media-pipeline |
| IMPL-082 | new | Conditioning placement: end of the final user turn (ADR-004) | N | none | orchestrator |
| IMPL-083 | new | AuthZ roles, scopes and the policy matrix; 403 only where declared, else 404 or an empty page | N | none | identity |
| IMPL-084 | new | Generation model limits: `context_window` (config, optional), `GEN_DEFAULT_MAX_TOKENS`, per-attempt stream timeouts | Y | contract (cschema) | foundation (config), orchestrator, generation, ops-admission |
| IMPL-085 | new | Idempotent responses over 1 MiB are stored in object storage (tenant-scoped key, same TTL), so replays never re-bill | Y | none | ops-ledger, db |
| IMPL-086 | new | Spotlighting levels: `standard` always datamarks document and OCR-derived blocks (generation and decision prompts); `strict` also datamarks user text and history (generation); supersedes IMPL-029b | Y | none | orchestrator (prompt), decision |
| IMPL-087 | new | API process sizing and overload: 2 api processes per 2-vCPU pod; a per-process turn cap with 429 `queue_timeout` + `retry-after: 1` (also checked at the edge, before authentication, since Wave E3); startup `gc.freeze()` and GC thresholds `(700, 10, 100)` (Wave E3; were `(700, 10, 40)`) | Y | doc (08 §9; applied in design rev 7) | api, integration (composition) |
| IMPL-088 | new | Datastore connections: one bounded Redis `BlockingConnectionPool` per process (`NOEMA_REDIS_MAX_CONNECTIONS`, 0.5 s wait); Postgres and Redis pool gauges and sampled acquire / command latency histograms | N | none | integration (composition), telemetry |
| IMPL-089 | new | The api and worker images run CPython 3.12.15 from python-build-standalone (Wave F), installed by uv 0.13.0 (was 0.9.10) in the api build stage; the pods are sized from that image's container knee (125 per pod, planned 100: 8 pods instead of the E3 image's 14) | Y (deployment shape; owner acknowledgement pending) | doc (08 §9; applied in design rev 7) | infra |
| IMPL-090 | new | Operator proof: the database verifies the operator key on each `noema_cli` connection (session setting `noema.operator_key`, migration `0003_operator_proof`); no table, column or grant change | N | none | db, identity |
| IMPL-091 | new | Object storage as built: one platform bucket `noema-<env>` with tenant prefixes and a per-tenant KMS key `alias/noema-<env>-<tenant_id>` in the platform account, instead of 08 §5's tenant-account media bucket and key; the tenant-account bucket deferred to Phase 2 (ASR), pending the owner | Y (owner review; touches D2) | doc (08 §5, §6 as-built notes) | foundation (core, db), media-pipeline, ops-ledger |

Unresolvable gaps: **none**. Two resolutions rest on values that only Phase 0 can measure, and are marked as such: the deadline and quorum values (IMPL-025) and the token estimator's accuracy (IMPL-029c). Both ship with their design values.

---

## A. Architecture and behavior (gaps 1–29)

### IMPL-001 (gap #1): Decision status when the deadline passes with quorum met
**Decision.** types.ts wins.
- `completed`: every answer is final and none is `unavailable`. This includes "the deadline passed with quorum met"; members that missed the deadline are `timeout`.
- `partial`: every answer is final and at least one is `unavailable`, for any reason.
- `quorum`: defined in IMPL-002.

**Rationale.** types and dschema are the machine-checked contract. An answer that met quorum is complete. Keying `partial` to "has an `unavailable` answer" gives clients one test to branch on.
**Refs.** types:L397-402; 04:L110; spec-06 §7.4 (T-POOL-2).
**Patch: doc + contract comment.** 04:L110 "Before commit" cell → "answers that meet quorum are served from the members that answered (missing members `timeout`; decision `completed`); answers below quorum are `unavailable` (decision `partial`); bindings use defaults". The types:L397-401 comment is replaced by the IMPL-002 text. Source: IMPL-DEC-1.

### IMPL-002 (gap #2): `quorum` status with early commit off
**Decision.**
1. `status: "quorum"` marks the snapshot S1 that `DecisionRun.commit_critical()` returns inside a turn while at least one **non-blocking** answer is still open.
   - Every commit-critical answer in S1 is final.
   - The open answers are provisional: computed from the members that answered so far, with pending members `late`.
   - Exactly one `decision.updated` follows, carrying the final result (`completed` or `partial`); its `changed` lists the names that became final.
   - If every answer is final at S1, S1 is already `completed`/`partial` and no `decision.updated` follows.
   - `/v1/decisions`, the facade and stored rows never carry `quorum`.
2. **This is not early commit.** Commit-critical answers still wait for every required member or the deadline. R5 is unchanged.
3. **Bindings read final answers only.**
   - At S1 the orchestrator evaluates `routing.bindings` over `S1.final_names`; a binding whose `answer` is provisional does not match.
   - At the final snapshot, bindings are evaluated again over every answer. Only **post-commit** action keys whose answer became final take effect: `verify_answer`, `response_cache`, `log_content`, and `spotlight: strict` for attempts that start afterwards (IMPL-029b).
   - Pre-commit keys (`generation_profile`, `effort`, `max_output_tokens`, `conditioning`) are fixed at S1 and never change. There is no visible reroute in Phase 1.
4. The S1 snapshot carries:
   - `usage` = the calls completed so far;
   - `completed_at` = the time of S1;
   - members still pending get `latency_ms` = elapsed time since their call started; `skipped` members get `latency_ms` = 0.

**Rationale.** Only commit-critical questions block (02:L214). Waiting for non-blocking ones would delay the commit, and emitting `decision.completed` late would reorder the documented stream (07:L317). The enum value and its definition already describe this snapshot. Point 3 closes a hole: the shipped bindings act on non-blocking answers (yaml:L175-178).
**Refs.** 02:L214, 02:L516, 02:L540; 03:L156, 03:L176, 03:L207; 07:L317; types:L397-402; spec-06 §7.4 and §10; critic findings 23 and 39.
**Patch: doc + contract comment.**
- types:L397-401 comment: `completed`, `quorum` and `partial` exactly as in point 1.
- 07:L317 → "the decision: `completed` or `partial`, or `quorum` while non-blocking answers are still pending (one `decision.updated` then follows)".
- 02 §6.3, after L540: "With early commit off, `quorum` also marks the in-turn snapshot sent when every commit-critical answer is final but a non-blocking answer is still pending."
- The 03:L145-183 example is replaced by the reordered example of IMPL-012.

**Material.** Y: the in-turn meaning of `quorum` is visible to clients. Source: IMPL-DEC-2 plus critic findings 23 and 39.

### IMPL-003 (gap #3): `speculation.holdback_ms` is unused by the timing rule
**Decision.** `holdback_ms` stays required and validated (`holdback_ms ≥ router deadline`, cc:L269), but nothing reads it at runtime in Phase 1. The timing rule is exactly 02 §6.2 with `max_hold_ms`, the router `deadline_ms` and `slack_ms` (IMPL-023).
**Rationale.** The 02:L497-501 rule is complete without it. Every way of folding it in would either do nothing (because of the ≥ check) or cut the decision short of its own deadline. Removing the key would be a breaking config change for no benefit.
**Refs.** yaml:L182; 08:L118; cc:L269; 02:L497-501.
**Patch: doc.** 08:L118 → "`holdback_ms` (≥ router deadline; reserved, no runtime effect in Phase 1)". Source: lead architect.

### IMPL-004 (gap #4): L2 decision-cache key and semantics
**Decision.** The key follows the 8 parts of 04:L147, with exact contents:
1. tenant (key prefix);
2. `project_id`;
3. question-set signature;
4. `input_digest` (IMPL-059);
5. stage-0 `input_view_signature`;
6. `"{id}@{ver};q={min_quorum};d={deadline_ms};e={off | >-joined chain id@ver};m={one letter per question, f|h}"`. The `m=` suffix (owner answers, 2026-10-10) records each question's choice method. The method depends on `templates.*.max_choices`, which a config change can alter without a profile version bump (spec-06 §19.9);
7. `cal:{CalibratorRegistry.epoch(tenant)}`, which stands for `calibrator_id` because the calibrator depends on the answering subset, known only after the call;
8. `policy:{routing.policy_version}`.

The key is `{ns}dec:l2:{tenant_id}:` + sha256(orjson(parts 2–8)).

Gates:
- **Get/put** only if `ctx.store`, `ctx.data_class ∈ {public, internal}` and `purpose ≠ verification`. The data class is the session's; otherwise the project's `default_data_class`; otherwise the platform default `internal` (spec-05).
- **Put** additionally requires all of these:
  - final status `completed`;
  - no `metadata_only` view in any stage;
  - the result is not itself a hit;
  - **no escalation was skipped for budget**;
  - **the stage-0 deadline was not shortened by `ctx.deadline`** (critic 17).
- TTL `platform.decision_cache.ttl_s` (default 600).
- **Fail-open.** Every L2 GET/PUT has a 20 ms timeout. An error or timeout is treated as a miss (`aghoo_cache_misses_total{layer="l2",reason="error"}`). If the epoch is unknown, L2 is bypassed.
- **Hit.**
  - A new `dec_` id, `created_at = now`, the current `trace_id`, `provenance.cache.hit = true`.
  - Usage is zeroed and `members[].usage` removed; `started_at`/`completed_at` keep the fill-time values (ce invariant 8).
  - It is persisted as a new row, and a `cache_savings` usage line records the fill usage and cost as the avoided cost (critic 61).
  - `commit_critical()` returns it immediately as final.
- `purge_tenant` uses `SCAN` on the prefix and `UNLINK`.

**Rationale.** These are the minimum parts that make a hit observationally equal to a fresh computation. The two extra put gates stop a budget-truncated answer from being served to a caller with a full budget, and the timeout keeps a hanging Redis from eating the 1,000 ms decision budget. Tenant isolation comes from the prefix plus per-tenant digests, and is tested with two tenants (IMPL-080).
**Refs.** 04:L147; 02:L241; ce:L378-381; spec-06 §14; critic findings 17, 44, 61.
**Patch: doc.** 04:L147: "the profile part includes request overrides and the escalation chain; the calibrator part is the tenant's calibrator epoch; only `completed` results without metadata-only views, without budget-skipped escalation and without a deadline cap are stored". The 02:L241 diagram label gets "(8-part key, 04 §3.1)". Source: IMPL-DEC-20 plus critic findings.

*Amendment (Wave E fixer rulings, recorded in Wave E2, 2026-10-11; e4-engines-fix 9 and 14):* **Put** also requires that **the run was not cancelled** (`coordinator.cancelled`: answers closed while members were in flight, or an escalation still pending), since a truncated result is not what a fresh computation gives. A failed project read gives the data class **`restricted`**, not the `internal` fallback (IMPL-022 amendment), so L2 is bypassed. spec-06 §14.

### IMPL-005 (gap #5): Escalation trigger names
**Decision.** No rename. The config key `escalation.when.any_refused` is recorded in `EscalationStage.triggered_by` as `refused`; `confidence_below`, `margin_below` and `unavailable` map one-to-one. `triggered_by` is the union over the stage's questions, in canonical order `[confidence_below, margin_below, refused, unavailable]`.
**Rationale.** Both names are machine-checked (cschema with cc negative tests; dschema `EscalationStage`). A mapping costs one table; a rename costs two contract changes.
**Refs.** 08:L96; yaml:L127; types:L283; spec-06 §9.1. **Patch: doc** (08:L96 notes cell). Source: IMPL-DEC-3.

### IMPL-006 (gap #6): Span and metric names
**Decision.**
- 08 §7 is the canonical observability reference (08:L415-453), and Telemetry pre-declares exactly its instruments.
- Span names: `turn`, `preflight`, `history.fetch`, `sts.assume_role`, `decision`, `decision.member` (attribute `aghoo.member.id`, not an index in the name), `decision.escalation`, `bindings.evaluate`, `generation.attempt`, `verification`, `cache.lookup`, `media.extract`, `asr.transcribe` (Phase 2).
- Metric names stay as in 08 §7. Labels are only added:
  - `aghoo_escalations_total{from_profile, trigger, outcome=ran|skipped_budget|skipped_too_many_choices}`;
  - `aghoo_cache_hits_total{layer}`.
- New instruments, registered in B1:

  | Instrument | Labels | Source |
  |---|---|---|
  | `aghoo_cache_misses_total` | layer, reason=miss\|bypass\|error | IMPL-004 |
  | `aghoo_decision_parse_errors_total` | member, code | feeds the 08:L459 alert |
  | `aghoo_decision_quorum_reached_seconds` | profile | the first time `Qcount(ok) ≥ q_req` held for every commit-critical answer; it is the latency gate (critic 49) |
  | `aghoo_tenant_access_failures_total` | tenant, cause | ADR-001 |
  | `aghoo_security_violations_total` | kind | ADR-001 |
  | `aghoo_sandbox_violations_total` | kind | ADR-002 |
  | `aghoo_outbox_backlog` | sink | IMPL-022 |
  | `aghoo_outbox_dropped_total` | — | spec-11 §3 |
  | `aghoo_stuck_attempts_total` | — | spec-11 §6 (soak gate) |
  | `aghoo_idempotency_attach_total` | result | IMPL-077 |
  | `aghoo_reservation_overrun_total` | kind (`CallKind`) | spec-02 §8 `Reservation.consume`, spec-11 §1.2; the C1 load gate (T-ADM-3) requires 0 |
- Span attributes added to the 08 §7 registry (B1 rejects any other key under `NOEMA_ENV=test`, spec-02 §12):
  - `aghoo.member.id` (`decision.member`);
  - `aghoo.decision.cache_hit` (bool, `decision`; spec-06 §16);
  - `aghoo.credentials.source` (`sts` | `ambient`, every span; ADR-001 §7);
  - the cache-write usage attribute of IMPL-029d.

**Rationale.** One reference prevents dashboards and alerts from silently missing metrics.
**Refs.** 04:L282; 08:L415-466; spec-06 §16; critic findings 20 and 49.
**Patch: doc.** 04:L282 span list → the 08 §7 names. Source: lead architect.

### IMPL-007 (gap #7): STS session caching and Mantle authentication
**Decision.** Adopt ADR-001 as written.
- One cached STS session per (pod, tenant), `DurationSeconds` 3600.
- Soft refresh at `expires_at − 600 s` in the background; hard refresh at `expires_at − 300 s`, single-flight.
- A 10 s negative cache; STS calls with no retries; memory-only storage.
- Fail closed with `TenantAccessMisconfigured(cause)` → 503 `tenant_access_misconfigured`.
- The account id is asserted before every adapter call.
- Mantle defaults to `bearer_short_term` (UNVERIFIED for assumed roles; probed in the live smoke). `sigv4` is selectable by config (`platform.credentials.mantle_auth`, spec-05).
- ADR-001 additions (§4, §5, §8), adopted with it:
  - the botocore refreshable credentials use the broker's windows (`_advisory_refresh_timeout` 600 s, `_mandatory_refresh_timeout` 300 s), so botocore never forces a refresh inside the broker's soft window;
  - tenant clients are always built from the broker's credentials with an explicit region and never resolve the default credential chain (only `AmbientBroker` does, once);
  - a failed soft (background) refresh keeps the cached credentials, backs off 10 s (the negative TTL) and never fails callers;
  - `invalidate(tenant_id)` evicts only an entry at least 60 s old;
  - endpoint overrides come only from `NOEMA_AWS_ENDPOINT_OVERRIDES`, honored only when `NOEMA_ENV ∈ {dev, test}` (startup refuses a non-empty value elsewhere);
  - tests SEC-STS-15 (ambient `AWS_*` never signs tenant calls), SEC-STS-16 (STS down in the soft window), SEC-STS-17 (botocore refresh windows).

"Per call" in D2 means **per-call tenant credentials, not a per-call AssumeRole**.
**Rationale.** A per-call AssumeRole adds 15+ STS round trips per turn, breaks the 25 ms overhead SLO, and hits the STS rate limits. 02:L203/L879 and 08:L488 already cache.
**Refs.** README:L38; 01:L71; 04:L191; 02:L136, 02:L203, 02:L879; 08:L488; ADR-001.
**Patch: doc.** 04:L191, 01:L300 and README:L38 → "per-tenant STS session (cached ≤ 1 h, refreshed 5–10 min before expiry) used for every call".
**Material.** Y: a reading of the D2 wording, which the owner acknowledges (OWNER-CHECKPOINT). Source: ADR-001 (security analyst; its IMPL-SEC-07/N6).

### IMPL-008 (gap #8): API key format, hashing and lookup
**Decision.**
- **Format**: `ak_{env}_{key_id}.{secret}`.
  - `env ∈ {live, test}`: the prefix must equal the row's `api_key.env`, and `test` keys are refused when `NOEMA_ENV=prod` (spec-09 §2);
  - `key_id` = 26-char Crockford ULID;
  - `secret` = 43 chars of base64url (32 random bytes).
- **Stored hash**: `hmac-sha256:v{p}:{hex}` = HMAC-SHA256(pepper_v{p}, `key_id + "." + secret`), with `p = api_key.pepper_version`.
  - The pepper lives in Secrets Manager as the platform secret `noema/api-key-pepper` (versioned; the spec-02 §3 `noema/<name>` naming, like IMPL-059's `noema/input-digest-pepper`).
  - Verification uses the pepper version named by the row. Only the current and the previous versions are loaded; any other `p` fails (401). A success with the previous version re-hashes the row (`auth.rehash_api_key`, best effort, never a downgrade).
- **Lookup**: `auth.lookup_api_key(key_id)`, a SECURITY DEFINER function (IMPL-009), returns at most one row. It returns tenant, project, principal, hash, scopes, roles, `expires_at`, `revoked_at` and `ip_allowlist`. The comparison uses `hmac.compare_digest`.
- **Cache**: an in-process LRU of 10,000 entries keyed by `key_id`, TTL 60 s, with a 10 s negative cache. Revocation publishes `RedisKeys.keys_revoked`; pods evict on receipt. Revocation therefore takes ≤ 60 s even if Redis is down.
- **401 hygiene**: 401 messages never echo the key or say which part failed.

Argon2id is rejected: 256-bit random keys gain nothing from slow hashing, and Argon2id costs tens of ms per request against a 10/25 ms overhead SLO.
**Refs.** 03:L32; 04:L199; 06:L225; 08:L467; spec-09 §2.
**Patch: doc.** 04:L199 "an Argon2id/HMAC-SHA256 hash" → "an HMAC-SHA256 hash with a versioned server-side pepper (keys are 256-bit random)". Source: lead architect.

### IMPL-009 (gap #9): RLS coverage and pre-tenant lookups
**Decision.**
1. Every tenant-owned table has `tenant_id text NOT NULL`, the config tenant key such as `t_acme`, not a uuid. This includes `attempt`, `decision` and `derivative`. Child tables use composite foreign keys `(tenant_id, parent_id)`.
2. Every tenant table has `ENABLE` + `FORCE ROW LEVEL SECURITY`, with a policy `USING (tenant_id = current_setting('app.tenant_id', true)) WITH CHECK (same)`. `noema_app` has no BYPASSRLS. `uow(principal)` runs `SET LOCAL app.tenant_id`/`app.project_id`. A missing setting yields NULL, so the query sees zero rows.
3. Pre-tenant and cross-tenant paths go only through SECURITY DEFINER functions owned by the NOLOGIN role `noema_definer` (BYPASSRLS), with `SET search_path = pg_catalog, pg_temp` (`pg_temp` last, so a temporary table can't shadow a relation) and every relation written schema-qualified. Each function gets `REVOKE EXECUTE … FROM PUBLIC` (plus `ALTER DEFAULT PRIVILEGES … REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC`), then EXECUTE for its caller role only (exact list: spec-03 §4):
   - to `noema_app`: `auth.lookup_api_key(text)`, `auth.touch_api_key`, `auth.rehash_api_key`; `jobs.claim(kinds, n, worker, lock_s)`; `jobs.extend` and `jobs.finish`, both fenced by `worker`; `jobs.ensure_periodic`; `ops.outbox_apply`, `ops.outbox_dead`; `ops.reap_stuck(interval)`; `ops.retention_candidates(int)`; `ops.calibrator_epochs()`, `ops.active_calibrators(text[])` (IMPL-072);
   - to `noema_cli`: `auth.lookup_operator` and `admin.*` (operator-checked, IMPL-044). The labeling jobs' `noema_app` grant covers only `admin.set_calibrator_status` (`active → downgraded` only) and `admin.bump_epoch`.

   Each returns the minimum columns, and the caller then opens `uow`/`tenant_uow` for the work.
4. Platform calibrators: `calibrator.tenant_id` is NULL iff `scope = 'platform'`.
   - SELECT policy: `USING (tenant_id = current_setting('app.tenant_id', true) OR scope = 'platform')`.
   - INSERT/UPDATE of platform rows only through `admin.upsert_platform_calibrator()` and `admin.set_calibrator_status()`.
5. RLS tests with a wrong `app.tenant_id` return 0 rows on every table (spec-03 §6).

**Rationale.** Defense in depth (04:L200). Text ids match the config, and SECURITY DEFINER keeps the app role non-privileged.
**Refs.** 02:L719; 08:L473-474; PLAN "API key = HMAC … SECURITY DEFINER"; critic finding 42.
**Patch: doc.** 08:L474 `::uuid` → text `tenant_id` (the config tenant key); 02:L664-715 ER: `tenant_id` on ATTEMPT, DECISION and DERIVATIVE. Source: lead architect.

### IMPL-010 (gap #10): Missing tables and columns
**Decision.** The Phase 1 schema is exactly spec-03:
- `tenant`, `project`, `principal`, `api_key`, `operator`;
- `session` (+ `last_seq`, `sticky_spec`, `active_turn_id`, `lease_expires_at`);
- `turn`, `turn_file`, `attempt`, `decision`;
- `file`, `derivative`;
- `usage_line`, `idempotency_record`, `outbox_dead_letter`, `job`, `audit_event`, `config_version`, `price_catalog` (IMPL-019);
- `label_item`, `labeling_run`, `calibrator`, `calibrator_epoch`.

`jobs.job` rows of periodic platform jobs (`retention.sweep`, `labeling.drift`) carry `tenant_id = '__platform__'`; platform `audit_event` rows use the same value.

Verification results are `decision` rows with `purpose = 'verification'`, referenced from `turn.verification_decision_id`; there is no separate table.

Not created in Phase 1 (phase-gated, added by a migration when built): `session_acl` (ACL sharing is Phase 2, 05:L91), `summary` (IMPL-045), `retention_policy` (W8, Phase 2; Phase 1 retention comes from config, spec-05).

The per-turn event log and the write-behind outbox buffer live in Redis (IMPL-021, IMPL-022), not in Postgres.
**Rationale.** These are the minimum tables the Phase 1 endpoints, the ordering rule, idempotency, billing, jobs and audit need, with no speculative tables.
**Refs.** 02:L649-716; 08:L473; openapi:L883-887 (`last_seq`); critic findings 1, 41, 42.
**Patch: doc.** 08:L473 table list → "see the implementation DDL". Source: lead architect.

### IMPL-011 (gap #11): Attempt model and outcome enum
**Decision.**
- **An attempt is one model call** (one ConverseStream), with its own `attempt_id` and `model`, matching `TurnObject.attempts[].model` and `generation.started.model`.
- The **attempt spec** is profile-level (02:L489). Every attempt of one profile run therefore shares a spec, and a fallback member is a new attempt with the same spec.
- `attempts[].outcome`: exactly one attempt per turn is `committed` (or none, if nothing committed).

| FSM terminal (02:L461-483) | `outcome` | Notes |
|---|---|---|
| Committed → Completed | `committed` | `generation.completed{finish_reason}` |
| Committed → Errored (mid-stream) | `committed` | `error` event carries the code; partial text kept |
| Committed, then client abort | `committed`, `finish_reason = cancelled` | `generation.cancelled{client_abort}` is emitted, the text is kept; `generation.completed{finish_reason: cancelled}` is never sent (IMPL-036) |
| Committed, then `ctx.deadline` | `committed`, `finish_reason = cancelled` | no `generation.*` frame; the turn's `error{deadline_exceeded}` is the terminal |
| Cancelled before commit (`spec_changed`, `lost_race`, `client_abort`) | `cancelled` | `generation.cancelled{reason}` |
| Cancelled before commit by `ctx.deadline` | `cancelled`, `cancel_reason = deadline` | no frame (the closed SSE enum has no `deadline` reason); the turn's `error{deadline_exceeded}` is the terminal (spec-02 §10, spec-04 §4.1 T19) |
| FailedPreCommit (no retry or fallback taken from it) | `failed` | `generation.cancelled{reason: failed}` |
| Rerouted, `not_selected` | — | unreachable in Phase 1 (speculative / `best_of_n`); tests assert they never occur |

**Rationale.** It keeps one model per attempt as the contract says, and makes "the committed attempt" unambiguous for usage lines (IMPL-020).
**Refs.** openapi:L752-757, L837; types:L538; 02:L461-485; 02:L640.
**Patch: doc** (02 §6.1 note: "an attempt = one model call; outcomes per the implementation mapping"). Source: lead architect.

### IMPL-012 (gap #12): SSE event order
**Decision.** The canonical order for a Phase 1 turn (holdback; `id` strictly increasing):
1. `turn.created`, sent once the session lock is held. Errors before it are HTTP errors.
2. `preflight.completed`.
3. `route.selected` for A1 (`speculative: true`), immediately followed by **`generation.started` for A1**. Every attempt, A1 included, gets `generation.started` (02:L789).
4. `decision.completed` (S1), before the bindings are evaluated. `arrived` is one of:
   - `before_generation`: A1 had emitted no stream event;
   - `during_holdback`: A1 was streaming, not committed;
   - `after_commit`: `max_hold_ms` fired first.
   - `after_completion` is never emitted in Phase 1: the decision always settles before `turn.completed` (03:L207). It stays in the enum.
5. The commit, one of:
   - spec agrees: `route.confirmed`, then `generation.committed{spec_agrees}`, then the buffered deltas. The commit point is `max(S1, first_text)`: when S1 comes before A1's first text, the pair is emitted at the first text (spec-04 §3);
   - spec differs: `generation.cancelled{spec_changed}` (A1), then `generation.started` (A2). At A2's first answer text: `route.confirmed`, then `generation.committed{spec_agrees}`;
   - hold cap first: `route.confirmed` (with `served_spec_differs: null`), then `generation.committed{holdback_deadline}`, then deltas. The decision follows later with `arrived: after_commit`;
   - `gate` strategy: `generation.started` only after S1, with `reason: gate`.
6. `generation.delta`…; at most one `decision.updated`, **emitted as soon as the final snapshot exists**, interleaved by time with other events.
7. `generation.completed` (committed attempt).
8. `turn.verified` (if verification runs).
9. `turn.completed`, always last.

A failure after the stream started emits `error`, then `turn.completed`; between the two only decision events (`decision.completed`, `decision.updated`) may appear, because the turn waits for the final snapshot before it completes. `decision.completed` is emitted even when the turn ends before S1: after a cancel or abandon it carries the closed snapshot of `drun.cancel()`; after a failure it carries the normal S1 (spec-04 §4). Fallback attempts each emit `generation.cancelled{failed}` for the failed attempt, then `generation.started` for the next.
*Amendment (orchestrator ruling R2, 2026-10-10, Wave C2a; requests/generation.md item 4):* "hold cap first" also covers a cap that fires while no attempt is buffering (`first_text()` came from an attempt that then failed before any commit): `commit("holdback_deadline")` arms the run, and the fallback attempt commits at its own first text with reason `holdback_deadline` (`holdback_ms` 0). The TTFT bound already fired, so the turn does not wait for S1 (spec-04 §3, spec-02 §10).

**Rationale.** It is one uniform rule for clients and golden fixtures, and emission follows wall-clock order (critic 23). The 03 example contradicted its own timings.
**Refs.** 02:L144-196, 02:L789; 03:L145-183, 03:L192, 03:L207; types:L528-544; critic finding 23.
**Patch: doc.** 03:L145-183 is replaced by the reordered example in spec-08 §14.6 (adds A1 `generation.started`; moves `decision.updated` with `changed: ["sensitive_data"]` before `route.confirmed`; renumbers ids). 02 §3 diagram: `decision.updated` may come before `route.confirmed`. **Material.** Y. Source: lead architect.

### IMPL-013 (gap #13): `direct` conditioning
**Decision.**
- No block is rendered for `direct` (binding), `none` (config binding action) or the request's `conditioning: "off"`.
- `RoutingDecision.conditioning` is `null` whenever no block is rendered, and the conditioning part of the attempt spec is the literal `none`.
- The "one-line intent hint if configured" (02:L585) has no config key and is not built in Phase 1.
- `AnswerMode` stays three-valued. `none` exists only as a config binding action (cschema `Binding.then.conditioning`); `off` exists only as a request value.

| Source | Value | Block rendered | `routing.conditioning` |
|---|---|---|---|
| binding | `direct` | no | null |
| binding | `none` | no | null |
| binding | `cover_top2_and_clarify` / `clarify_first` | yes | `{answer_mode, signature, summary}` |
| request | `auto` | per bindings | per bindings |
| request | `off` | no | null; tightening is unaffected (03:L132) |
| request | an answer mode | as forced | as forced |

**Refs.** 02:L491, 02:L585; 08:L163, 08:L224; types:L462; 03:L123.
**Patch: doc.** 02:L585 "none (no block), or a one-line intent hint if configured" → "none (no block)". Source: lead architect.

### IMPL-014 (gap #14): spec0 conditioning and the conditioning signature
**Decision.**
1. spec0 = `(sticky.generation_profile, sticky.effort, sticky.max_output_tokens, conditioning = none)`; for a first turn, a session without `sticky_spec`, `store: false` without a session, or a modality set different from the previous turn's: `(routing.default_profile, null, null, none)`. Then the request pins (`generation.profile` ≠ `auto`, `effort`, `max_output_tokens`) replace the matching fields, because they also override the decided spec; otherwise a pinned turn would always restart whenever the sticky value differs (spec-04 §3). **A1 never carries a conditioning block**: this turn's block can't be rendered before this turn's decision.
   - `session.sticky_spec` JSON = `{generation_profile, effort, max_output_tokens, answer_mode, modalities}`: the decided spec, the decided `answer_mode` (analytics only), and the turn's sorted modality list, which feeds the modality-change rule above.
2. `conditioning.signature = "sha256:" + sha256(template_id + "\n" + rendered_block)`, with `template_id = "decision-conditioning@1"` (ADR-004); `none` when no block is rendered.
3. `routing.speculation.restart_on_conditioning_change`:
   - `true` (the shipped value): the signature is part of the compared spec, so a decided spec with a block restarts A1 invisibly;
   - `false`: only `(profile, effort, max_output_tokens)` are compared. A1 commits without the block, and `routing.conditioning` reports `null` (the decided mode is logged).

**Rationale.** It removes the impossible match. Conditioning is sparse (02:L491), so restarts happen only on the low-confidence turns that need the block.
**Refs.** 02:L489-491; 04:L86, 04:L93; yaml:L182; ADR-004; critic finding 26.
**Patch: doc + contract comment.**
- types `Conditioning.signature` comment → "sha256 over the template id and the rendered block; part of the attempt spec compared at commit";
- 08 §3.2 one sentence;
- 02:L490: "spec0 never carries conditioning".

Source: lead architect.

### IMPL-015 (gap #15): Effort with multi-member generation profiles
**Decision.** The attempt spec's `effort` is a **profile-level override**:
- `null`: each member uses its own configured `effort`, or the model default;
- value E: every member whose model accepts E uses E. Any other member keeps its configured effort and records a `param_unsupported` degradation on its attempt.

Spec equality compares the override value (`null` vs E), never per-member values. A request `generation.effort` is the same override; with a pinned profile and a primary member that rejects E → 422 `unsupported_parameter` (03:L131).
**Refs.** yaml:L143-145; 08:L137 (rule 12); 02:L489; 03:L131. **Patch: none.** Source: lead architect.

### IMPL-016 (gap #16): Fallback chain and the attempt cap
**Decision.**
- Pre-commit failure handling walks:
  1. a connect, DNS or TLS error (no response byte) is retried once **inside the adapter** (`retry="default"`, RetryBudget-limited, 04:L101). It stays the **same attempt**: no model call happened;
  2. the profile's next members (`fallback` mode);
  3. the first member of `routing.default_profile`, if it differs from the current profile.
- Guards for every failure-driven attempt (steps 2–3 and the throttling retry), all of which must hold, else the turn ends with the last failure's code (IMPL-076):
  - the turn-wide `AttemptBudget` below has an attempt left;
  - `RetryBudget.try_spend(tenant_account, model_id)` is true (10% per (tenant account, Bedrock model id) per minute, 04:L121), so the F12 retry-storm gate (≤ 1.1× amplification at 50% 5xx) holds;
  - the failure kind is not `tenant_access`: a tenant-access generation failure never falls back (fail closed, ADR-001).
- Throttling with no next member: one same-member retry after `retry_after_s` if the provider sent it, else after full jitter `uniform(0, 500 ms · 2^k)` (k = failure attempts so far), only if `ctx.deadline` still leaves the route's `first_answer_s` after the sleep (spec-04 §6).
- **Turn-wide cap: 2 failure-driven attempts** (fallbacks, plus the throttling backoff retry of 04:L102) beyond the first attempt (04:L120). The invisible spec restart (A2) is **not** a failure attempt; at most one restart per turn. Race members are parallel attempts of one profile run and don't count toward the cap. Worst case = A1 + A2 + 2, plus the race width, and admission reserves exactly that (spec-11 §1).
- A fallback to `default_profile` sets:
  - `routing.route_id = default_profile`;
  - `selection = "fallback"`;
  - `served_spec_differs = true`;
  - `reason` naming the failed profile.

  The spec comparison isn't re-run. Stickiness still stores the decided spec (04:L93).

**Rationale.** It bounds the work graph and keeps restarts, which are a correctness mechanism, apart from retries, which are a reliability mechanism.
**Refs.** 04:L48, 04:L101, 04:L120; 02:L489; types `served_spec_differs`.
**Patch: doc + contract comment.** The types `served_spec_differs` comment and the openapi description gain "…or when a pre-commit fallback to `routing.default_profile` served the turn". Source: lead architect.

*Amendment (Wave C2a adapter decisions, adapters request 8; recorded in C2b):*
- **Possibly-delivered retry.** The in-adapter retry covers every connection failure before any response byte (spec-02 §4.1, spec-10 `connect_reset`), but "no model call happened" holds only for connect, DNS, TLS, proxy and write failures. After a reset or disconnect that followed the request write, the request may have reached Bedrock and been billed; the adapters still retry it (the frozen suite and spec-10 require it), log it at WARNING (`possibly_delivered`), and no usage line reports the first dispatch (`ModelResponse` has no field for it).
- **Retry-budget floor** (*superseded by R8, Wave E3: no floor*). `SlidingRetryBudget` keeps a floor of 10 retries per (tenant account, Bedrock model id) per 60 s: the frozen `RetryBudget` contract implies one (its docstring is a token bucket; `test_health_registry` grants a fresh key's first retry, and again after 120 s without traffic), and without it a quiet tenant would get no generation fallback at all. Bound: N first attempts per minute allow max(10, 0.1·N) retries, so the amplification is ≤ 1 + max(10/N, 0.1) (1.1 from N = 100; the F12 gate runs well above that). The owner may still rule on the floor (C1 review minor 8).

*Amendment (orchestrator ruling R6, 2026-10-11, Wave E2; fault B1, io-fixer CR item 5):* **a fallback attempt to a different model is not a retry.**
- It never calls `RetryBudget.try_spend`, neither for the failed model nor for the fallback model. Before this, the budget refused 20–63% of the turns needing a pre-commit fallback in F1 and F4–F8, so S1 (≥ 99%) failed.
- It is bounded by the turn-wide cap of 2 failure-driven attempts and by admission: the worst-case reservation (spec-11 §1.1) and the per-(account, model) buckets and bulkheads (spec-11 §1.2). A breaker-open member is still skipped without an attempt.
- `RetryBudget` gates only **same-model re-sends**: the in-adapter connect retry (step 1), the throttling re-send of the same member, and a failure-driven attempt on a model that an earlier attempt of the run already called (e.g. a `default_profile` member on the failed model). The retry-budget floor above now bounds only those re-sends.
- The F12 retry-storm gate (≤ 1.1× amplification against the failing upstream, spec-10 F12) must still pass.
- spec-04 §4.1 T11 and §6 are amended accordingly.

*Amendment (ruling R8 of the Wave E3 brief, 2026-10-12; supersedes the C2b "retry-budget floor" bullet above; F12 residual of Wave E2):*
- **What the budget counts.** Only re-sends that the upstream may have received: generation's throttling re-send of the same member, generation's failure-driven attempt on a model the run already called (R6), and the in-adapter connect retry after a read or protocol error that followed the request write (`possibly_delivered`).
- **Size.** 10% of first sends per (tenant account, Bedrock model id) over the sliding 60 s, with **no floor**: `try_spend` grants while `retries + 1 ≤ 0.1 × first_sends`, where first sends = noted wire requests − granted re-sends − unsent sends. So a model's requests stay ≤ 1.1 × its first sends at every traffic level. A quiet key has no budget: its first re-send needs 10 first sends in the window. The floor of 10 had allowed 1 + 10/N below N = 100 first sends (F12 at `rate: 0.5`: Sol 1.149 per upstream in Wave E2).
- **Exempt.** The in-adapter connect retry of a send that provably never reached the upstream: a connect, DNS, TLS, proxy or request-write failure (`is_connect_error` and not `possibly_delivered`, `adapters/classify.py`). It spends nothing, and the failed send is not counted as a first send. It stays limited to **one per call** (`_base._send`, step 1 above). `SlidingRetryBudget.try_spend` recognizes it by the exception the adapter is handling when it asks (`sys.exception()`), because the frozen `RetryBudget` protocol passes no cause.
- **F12** is measured per failing upstream X: requests to X at the mock ÷ first sends to X (the turns that started an attempt on X), ≤ 1.1 in both rows (`rate: 0.5` and `0.25`). Requests per turn count every fallback as a first send (R6), so that ratio is recorded but not gated (spec-10 F12).
- **Tests.** `tests/unit/adapters/test_retry_budget.py` (the ratio at N = 0…205, the exempt and the budgeted connect retry, unsent sends not published to other pods; 9 tests fail on the Wave E2 code); `test_complete.py::test_connect_retry_needs_the_retry_budget_unless_nothing_was_sent`; the `RetryBudget` conformance suite notes first sends before it expects a grant; `tests/fault/test_f12_retry_storm.py`.
- spec-02 §8 and spec-10 F12 are amended accordingly. **Material.** N (no API change; a quiet tenant loses only the same-model throttle re-send and the re-send after a possible delivery, never a fallback).

### IMPL-017 (gap #17): Cache points in Phase 1
**Decision.** Phase 1 sends no `cachePoint` blocks and no `prompt_cache_key` (05:L111 defers both to Phase 2). It keeps strict prefix discipline: static preamble → developer instructions → history → current turn, with variable text last. The decision templates keep `B_q` (`<app_question>`) as the first user block, so Phase 2 can add a cache point after it **without** changing prompt bytes or the template version.
**Refs.** 02:L322; 08:L195; 05:L111; 04:L177.
**Patch: doc.** 02:L322 and 08:L195: "(the cache point is added in Phase 2)". Source: lead architect.

### IMPL-018 (gap #18): Phase 1 no-op actions
**Decision.**
- `response_cache: off` and "purge the response cache" (02:L550, 02:L628) target L3, which arrives in Phase 3. In Phase 1 they are OR-combined, recorded in the turn's applied policy and `matched_bindings`, and have no other effect.
- `log_content` **is** effective in Phase 1. It is the persistence policy (02:L566) for the stored turn content (`turn.input_parts`, `turn.output_text`):

  | Value | Stored content |
  |---|---|
  | `full` | as is |
  | `redacted` | PII-redacted with `redactions[]` spans (IMPL-029e) |
  | `none` | metadata only |

  It combines with the data-class `store_content` (spec-05); the most restrictive value wins. Telemetry never carries content in Phase 1.
- `disable_tools` is a no-op (no tools in Phase 1).

**Refs.** 02:L550, 02:L566, 02:L628; 04:L148; 03:L392.
**Patch: doc** (02:L629 note "L3 actions are no-ops until Phase 3"). Source: lead architect.

### IMPL-019 (gap #19): Price catalog
**Decision.**
- A separate versioned catalog in the `prices.yaml` format, validated by `config/prices.schema.json` (foundation). It is not part of the platform config:
  - `noema-admin prices import <file>` validates the yaml and stores it as the next `platform.price_catalog` version (`admin.import_prices`);
  - a config pins one version with `platform.cost.prices_ref: "db:<version>"` (P1-P1: the version must exist at activation). `file:<path>` reads a yaml directly and is accepted only when `NOEMA_ENV ∈ {dev, test}`;
  - `prices_ref` absent → an empty catalog: every price is TBD and every cost `cost_estimated: true`. There is no default path.
  - A turn prices against the catalog of the config snapshot it pinned (`ConfigSnapshot.prices`, spec-05 §2).

  Each entry has:
  - `model` = the **Bedrock id**;
  - `region`;
  - `tier` (default `on_demand`, the only Phase 1 tier);
  - `effective_from`;
  - `usd_per_mtok{input, output, cache_read?, cache_write_5m?, cache_write_1h?}` as decimal strings.
- Lookup key: `models.<alias>.price_ref` if set, else `models.<alias>.id`, plus the tenant's `home_region` and the newest `effective_from ≤ now`.
- Cost uses `Decimal` (28 digits), rounds half-even to 1e-6 USD, and follows 08:L508 with `p_cache_write = cache_write_5m`. Phase 1 sets no cache points, so writes are only implicit and 5-minute.
- A missing entry or field → that component costs 0 and `cost_estimated: true`. Placeholder prices are flagged `cost_estimated` until reconciled (PLAN).

**Refs.** 08:L490-511; 04:L283; types `Usage`. **Patch: doc** (08 §10 key and fields). Source: lead architect.

### IMPL-020 (gap #20): Cost lines and their assignment
**Decision.** The six `UsageLine` names (rev-6 openapi) are canonical: `decision`, `generation`, `labeling`, `verification`, `waste`, `cache_savings`.

| Line | Calls |
|---|---|
| `decision` | Completed decision member calls (primary, backup, escalation stages) for purpose `decision`/`router` |
| `verification` | Completed member calls of verification decisions |
| `generation` | The committed attempt only |
| `waste` | Every other generation attempt (cancelled, failed, lost race), and every cancelled decision or verification member call (lost backup races, deadline or run cancels) |
| `labeling` | Labeler calls (jobs) |
| `cache_savings` | Avoided cost of an L2 hit (IMPL-004); excluded from spend totals |

The lines are disjoint (02:L640). Granularity and recorder (exact ids: spec-11 §2, `h(x) = sha256(x)[:26]`, so outbox replays are idempotent):

| Recorder (when) | Lines | One line per |
|---|---|---|
| `DecisionRun.finalize()` (every origin, turns included) | `decision`, `verification`, `waste` (its cancelled member calls) | (decision, line, Bedrock id) |
| `DecisionRun.finalize()` on an L2 hit | `cache_savings` | decision; `model_id` and `region` NULL |
| the orchestrator at turn completion, from `ProfileRun.result()` | `generation` (the committed attempt), `waste` (every other attempt) | attempt |
| the labeling job | `labeling` | (job attempt, question, labeler) |

The orchestrator never records decision or verification lines, so nothing is counted twice.

Cancelled-call estimates have `tokens_estimated: true` and `cost_estimated: true`:
- input = `TokenEstimator.estimate(...)` of the prompt;
- output, non-streaming decision calls: the call's `max_tokens`. A sent non-streaming call is assumed completed and billed server-side (UNVERIFIED, Phase 0 task 0.7);
- output, streaming attempts: counted text plus reasoning deltas (chars ÷ the model's chars-per-token) plus `platform.cost.cancel_overrun_tokens` (default 64).

**Rationale.** It follows 02:L636-640 and gives conservative waste figures.
**Refs.** 03:L81; 04:L283; 02:L636-640; spec-06 §6.5; critic findings 24 and 53.
**Patch: doc.** 04:L283 line names → the six canonical names. Source: IMPL-DEC-16 plus lead architect.

### IMPL-021 (gap #21): The per-turn event log in Phase 1
**Decision.**
- `TurnEventLog` = an **in-process** append-only log per turn, which is authoritative for same-pod SSE, plus a best-effort **Redis Streams mirror** `RedisKeys.turn_events(turn_id)` (`MAXLEN ~ 20000`).
- `seal()` sets a TTL of `platform.events.resume_window_s` (300) after `turn.completed`.
- Uses:
  - SSE delivery: the writer subscribes to the log;
  - idempotent attach of an in-flight streamed turn on another pod (03:L37);
  - the abandon watchdog: no subscriber for 300 s since the last delivered event → cancel with reason `client_abort`, turn marked `abandoned`.
- `GET /v1/turns/{id}/events` → 501 `not_implemented` (IMPL-036).
- An `Idempotency-Key` replay of a completed streamed turn replays the mirror while it exists. After it expires: 410 `resume_window_expired` with `details.turn_id`.
- `store: false` turns replay the same way within the window and are never written to Postgres.
- Redis down: same-pod SSE is unaffected, and a cross-pod attach waits for the original to finish (IMPL-077), then returns 410.

**Rationale.** 06 §7 requires availability with Redis down. Cross-pod attach needs a shared log. The 5-minute window is 03:L205.
**Refs.** 03:L37, 03:L113, 03:L205-206; 06:L189; 05:L72; critic finding 50.
**Patch: none** (the contract part is IMPL-032/036). **Material.** Y (the 410 replay rule). Source: lead architect.

### IMPL-022 (gap #22): Synchronous ordering vs the asynchronous outbox
**Decision.**
1. **Synchronous Postgres writes**, on the request path:
   - session lock (spec-04 §2): first `EXISTS RedisKeys.session_pending(sid)` → busy (an expired lease whose finalize still waits in the outbox is busy); then `UPDATE session SET active_turn_id, lease_expires_at … WHERE (active_turn_id IS NULL OR lease_expires_at < now()) [AND last_seq = :expected]`. An expired lease is free, so no reaper releases sessions (`ops.reap_stuck` handles jobs only);
   - **ingress order**: the session lock and the history selection run **before** `Admission.reserve` (spec-04 §1 steps 10–11), so the TPM reservation covers the history (06 §5 "reserves the worst case"). A 429 at admission releases the lease without consuming a `seq`;
   - turn finalize: one transaction that inserts `turn` (`seq = last_seq + 1`), `attempt`, the turn's `decision` and verification rows and `turn_file`, then updates `session.last_seq`, `sticky_spec`, `active_turn_id = NULL`, `lease_expires_at = NULL` (`WHERE active_turn_id = :turn AND last_seq = :lease_seq`; 0 rows → `LeaseLost`, spec-04 §10). It runs **before** `turn.completed` is emitted;
   - the `/v1/decisions` decision row, so a GET right after the create succeeds;
   - idempotency records;
   - file metadata.
2. **Write-behind outbox** for everything else (usage lines, audit events, labeling offers):
   - in-process batch writer (every 200 ms or 500 rows);
   - on DB error, append to Redis Stream `RedisKeys.outbox`;
   - the ops-ledger drainer (SECURITY DEFINER insert) applies idempotent upserts keyed by line id;
   - poison records go to `outbox_dead_letter` after 10 attempts;
   - backlog metric and alert (04:L116);
   - the `turn_finalize` apply is guarded by `last_seq` (`UPDATE … WHERE id = :sid AND last_seq = :lease_seq`; a replay whose turn row exists is a no-op, spec-11 §3).
3. **Postgres down** (06:L190):
   - `store: true` turns and session creation fail fast with 500 `internal_error` (retryable);
   - a request that carries `Idempotency-Key` gets 500 `internal_error` (retryable) at `begin`, whatever its kind, so it never risks a double execution (spec-11 §5);
   - otherwise `store: false` turns and `/v1/decisions` continue;
   - a decision row or turn finalize that fails mid-request goes to the Redis outbox as a full record and **never fails the turn**. A `/v1/decisions` call still returns 200; read-after-write is then not guaranteed until the drain;
   - before a `turn_finalize` put, the orchestrator sets `SETEX session_pending(sid) 86400`; the drainer DELs it after the apply **or** the dead-lettering. Until then the next turn on that session waits and then gets 409 `session_busy`, even after the lease expired.

**Rationale.** Ordering and `sticky_spec` need synchronous commits (02:L718); billing and audit need durability, not latency.
**Refs.** 02:L233, 02:L718; 03:L204; 04:L116; 06:L189-190; spec-06 §16; critic finding 44.
**Patch: doc.** 02:L233 "Persist (asynchronous, outbox)" → "Persist: turn, attempts, decision and verification synchronously at completion; usage, audit and labeling write-behind via the outbox".
**Material.** Y (Postgres-down behavior is visible). Source: IMPL-DEC-19 (decision row) plus lead architect.

*Amendment (Wave E fixer rulings and orchestrator ruling R7, recorded in Wave E2, 2026-10-11; e4-engines-fix 2, 9, 20; contract B2):*
- **Finalize budget.** The turn-finalize transaction runs under `FINALIZE_BUDGET_S` = 5 s (a code constant in `turns/persist.py`). Expiry (a hung, not a refused, Postgres) is a DB failure: `session_pending`, then the `turn_finalize` outbox record. The outbox apply is idempotent, so a commit whose reply was lost is a no-op replay. spec-04 §10.
- **Unknown data class fails closed.** When the project read that yields the data class fails (Postgres down, a timeout), a `/v1/decisions` or facade request continues with `restricted`: no L2, `metadata_only` content, `restricted` retention, no labeling offer. A sessionless `store: true` turn already gets 500 at ingress; a `store: false` turn stores nothing and uses no L2. spec-06 §14.
- **U+0000 in stored text becomes U+FFFD** (R7), one code point for one, so redaction spans are unchanged. Postgres `text` and `jsonb` reject U+0000: such a turn or decision answered 200, then its row was lost and its outbox entry could never drain. This covers the stored turn input and output, and the decision row's `questions`, `input`, `result` and `safety_identifier`. Responses and prompts keep the client's text. spec-04 §10, spec-06 §16.

*Amendment (ruling R9 of the Wave E3 brief, 2026-10-12; resolves the Wave E2 discrepancy between R7 and the edge fixer):* U+0000 is handled by where the text goes.
- **Stored model or turn text** (the turn input and output, the decision row's `questions`, `input`, `result` and `safety_identifier`): U+0000 becomes U+FFFD at storage, as above (R7).
- **Client-supplied structured fields** that are stored as given (Phase 1: session `metadata` keys and string values on createSession and updateSession): **400 `schema_violation`**, `param` = `metadata` (a key) or `metadata.<key>` (a value), in the request gate before any SQL (`api/middleware/gate._session_rules`; `tests/unit/api/test_gate_step.py::test_session_metadata_holding_nul_is_400`). A field the client controls is refused, not rewritten.
- spec-04 §10 records both rules; spec-06 §16 keeps the U+FFFD rule for decision rows. **Material.** N (both behaviors shipped in Wave E2).

### IMPL-023 (gap #23): Deadline derivation
**Decision.** Let `D` = `Aghoo-Deadline-Ms` (default 120,000; 100–600,000), `t_in` = ingress (monotonic), and `t_d0` = decision start, once preflight is done and the stage-0 views are ready (IMPL-074).

| Quantity | Formula |
|---|---|
| `ctx.deadline` | `t_in + D` |
| stage-0 decision deadline | `min(t_d0 + deadline_ms_eff, ctx.deadline)` (spec-06 §2.3) |
| decision wait limit `W` (turns) | `min(t_d0 + router.deadline_ms + slack_ms, ctx.deadline)` |
| holdback commit | the earliest of: S1 ready; `t_first_text + max_hold_ms`; `W` (02:L497-501) |
| `escalate_until` (turns) | `W`; for `/v1/decisions` and the facade: `ctx.deadline`; verification: none |
| generation timeouts | per-route connect / first event / first answer / idle (04:L127-132), each capped by `ctx.deadline` |
| verification | start after `generation.completed`; deadline `min(now + verifier.deadline_ms, ctx.deadline)` |
| `stream: false` | 504 `deadline_exceeded` if `ctx.deadline` passes before the response is ready |
| SSE | `error{deadline_exceeded}`, then `turn.completed` |
| decision dispatch | if `ctx.deadline.remaining_ms() < MIN_DISPATCH_MS` (50) when stage 0 would dispatch → `deadline_exceeded` (IMPL-057) |

**Refs.** 03:L41; 02:L497-501; 04:L127-132, 04:L278. **Patch: doc** (03:L41 gets the table). Source: lead architect.

### IMPL-024 (gap #24): Verification precedence
**Decision.**
- Effective verification = (any matching binding sets `verify_answer: true`) OR (`request.verify == true`). `verify: false` is accepted and has no effect: it can never skip a binding's `verify_answer` (03:L132). If `routing.verification` is not configured, `verify: true` → 422 `unsupported_parameter`, `param: verify`.
- Verifier input = `VerificationInput(question = the text parts of the final user message, answers = [("A", committed text)])`. Media parts appear only as their labels (`[doc-1: document]`), not their content.
- Verification runs on `store: false` turns too; nothing is persisted.
- Questions = `routing.verification.questions`, in config order, compiled with `select=` (spec-02 §5).

**Rationale.** It follows the tightening rule. Phase 1 verification is advisory (a badge plus cache policy), so text-only keeps it cheap. Media-aware verification can come later.
**Refs.** 03:L124, 03:L132; 02:L620-632; 08:L117. **Patch: doc** (03:L124 → "force (`true`); `false` cannot skip a binding's `verify_answer`"). Source: lead architect.

### IMPL-025 (gap #25): Latency vs deadlines; how `router-plus` gets its questions
**Decision.**
1. Deadlines stay at their design values (PLAN). A Haiku call still running at 1,000 ms becomes `timeout`; {DeepSeek, Luna} meet `min_quorum: 2`. The answer is then `completed` and `uncalibrated` (no artifact for that subset). The wire mock's latency profile includes a Haiku tail above 1 s.
2. **In-turn escalation** is evaluated **per answer when it closes** (critic 28).
   - If `now + T.deadline_ms > min(escalate_until, ctx.deadline)`, the answer is final immediately (metric `outcome=skipped_budget`). `commit_critical()` therefore never waits on a non-blocking question.
   - The eligible answers that remain when stage *s* has closed form stage *s+1*.
   - With the defaults (2,500 ms > 1,050 ms), **escalation never runs inside turns**. A low-confidence intent goes to the bindings and conditioning instead (02:L423 allows this).
   - On `/v1/decisions` and the facade, `escalate_until = ctx.deadline`.
3. **Question inheritance.**
   - The target re-asks the source stage's compiled questions (same signatures), using its own members, templates, efforts, weights, `min_quorum`, `deadline_ms`, aggregation and modality policy. The target's own `questions` and `commit_critical` are ignored.
   - Request overrides apply to stage 0 only. Chains follow the target's own `escalation`; cycles are rejected by config.

**Rationale.** It is one testable inequality, and it keeps `question_signature` stable across stages for the stage-bound calibrator binding.
**Refs.** 02:L207, 02:L215, 02:L295, 02:L421-424; 04:L110; yaml:L104-127; spec-06 §9.3.
**Patch: doc.** 02:L423 → "In turns, escalation runs only if the target stage fits before the decision wait limit (decision start + router `deadline_ms` + `slack_ms`); with the default `router-plus` (2,500 ms) it never does"; 08:L96: "the target re-asks the source stage's questions with its own members, deadline and quorum".
**Material.** Y: no escalation in turns with the shipped config. Owner question 2. Source: IMPL-DEC-4 plus critic finding 28.

### IMPL-026 (gap #26): `uncertain_profile`
**Decision.**
- The generation profile is resolved as:
  1. the first matching binding that sets `generation_profile`;
  2. else, if the commit-critical `intent` answer is **scored with band `low`**, `routing.uncertain_profile`;
  3. else `routing.default_profile`.
- `intent` `unavailable` or `refusal` → `default_profile` (04:L110).
- **Exception, the decision is unavailable**: if every commit-critical answer in S1 is `unavailable`, the pre-commit keys are not evaluated and decided = spec0, so A1 commits (`selection: sticky` or `default`, `routing.reason = "decision unavailable: <reason>"`; 04:L113, 06:L188; spec-04 §3). Read literally, the rule above would restart a sticky `opus-deep` turn onto `sol-fast` during a decision outage.
- New P1 config rule: `routing.default_profile` is required (cschema leaves it optional; there is no other spec0 source for a first turn).

With the shipped config, binding 2 (yaml:L169) always sets the profile for low-band intents, so behavior is unchanged. The 06:L102 metric ("routed to `uncertain_profile`") becomes meaningful.
**Refs.** yaml:L157, yaml:L169; 08:L113; 06:L102; cc:L44, cc:L178; cschema `Routing.required`.
**Patch: doc** (08:L113 one sentence). Source: lead architect.

### IMPL-027 (gap #27): Hierarchical method and early commit in Phase 1
**Decision (revised 2026-10-10, owner answer 1).** The **hierarchical method is built in Phase 1** for native `/v1/decisions` (`output_format: "native"`) and for turn app questions, exactly as 08 §3.4 specifies. This replaces the earlier deferral. The facade and `output_format: "jev"` keep 422 `unsupported_parameter` (R5, IMPL-036). The exact algorithm and its test vectors are in spec-06 §19 (T-HIER-1 reproduces `decision-result.valid-hierarchical.json` from its request).

Engineering resolutions where 08 §3.4 is silent (lead architect):
- (a) **Method.** Hierarchical iff a choice question has `K > L(profile)` (L over all members). Only choice questions can need it.
- (b) **Deadline.** Both rounds run inside the stage deadline `D`: round 1 closes by `t0 + ⌊(D − t0)/2⌋`, round 2 by `D`. Round 2 dispatches only if at least 50 ms remain.
  - A round that doesn't fit gives `unavailable / deadline_exceeded` (decision `partial`; `/v1/decisions` still 200).
  - The engine never emits a round-1-only, flat, spread or renormalized answer. There is no compile-time latency check.
- (c) **Members across rounds.** A member is `ok` only if its round-1 call and all its round-2 calls are `ok`. Otherwise it takes its first non-ok status, in the order refused > timeout > error > late. A round-1 `ok` member of an answer that closes after round 1 is `late`.
  - Quorum and the pool use only the members `ok` in both rounds. `P(g)` is re-pooled over them.
  - If that pool needs a group that round 2 didn't expand → `unavailable / no_quorum`. Groups expanded but no longer needed are reported unexpanded, and their mass goes to `unresolved_mass`.
- (d) **Exact rounding.** Expansion reads the 4-dp group units (largest remainder; cumulative ≥ 9,000 units; ties by declared order). Each expanded group's candidates are rounded to exactly its group mass. The `ce` expansion check is tightened to the same integer rule (spec-08 §14.5).
- (e) **Censored reads.** A censored logprob read in either round is member `error / censored_tail`. The flat censoring rule can't be represented inside groups.
- (f) **One-option groups.** An expanded group with one option needs no round-2 call.
- (g) **Escalation stays flat.** spec-06 §9.2 rule 4 (`K ≤ L(target)`) is unchanged. With the shipped config no hierarchical answer escalates (IMPL-068).
- (h) **Calibration.** The binding has `choice_method: "hierarchical"`. Phase 1 labeling fits flat bindings only, so hierarchical answers are always `uncalibrated` in Phase 1 (ADR-003 §1).
- (i) **L2 cache.** The profile part gains `;m=` (IMPL-004).
- (j) **Admission.** It reserves 1 + 3 calls per member per hierarchical question (spec-11 §1.1).
- (k) **Platform questions stay flat.** P1-D6 applies. For a turn `decisions.profile` override, a platform question above `L(override)` → 422 `unsupported_parameter` (IMPL-069).
- (l) **`normalized_entropy`** counts the unresolved bucket (IMPL-079).

**Early commit** is not implemented. Every other out-of-phase decision mechanism is config-rejected by the P1 rules in `config/rules.py` (foundation), each with a negative test:
- **P1-D1**: `routing.speculation.commit_on_quorum` must be false;
- **P1-D2**: no `batch_questions: true`;
- **P1-D3**: elicitations ∈ {`token_logprobs`, `verbalized`};
- **P1-D4**: `aggregation = linear_pool`;
- **P1-D5**: templates ∈ {`logprob-codes@2`, `verbalized-dist@1`, `verify-dist@1`};
- **P1-D6**: every question asked by `routing.router_profile` and `routing.verification.decision_profile` has `K ≤ L(profile)`.

*Amendment (orchestrator ruling R1, 2026-10-10, Wave C2a; requests/decision.md item 2):* (d) also requires the expanded set `E` to hold **at least 2 options** in total. `E` grows in the same top-mass order until it covers ≥ 9,000 units **and** holds ≥ 2 options, still at most `HIER_MAX_EXPANDED` (3) groups. Before this, a request-supplied one-option group with ≥ 0.9 of the round-1 mass gave one candidate, which `ScoredBase.candidates` (`minItems: 2`) cannot carry, so Phase 1 answered `unavailable / no_quorum`; it is now scored with ≥ 2 candidates. Admission is unchanged (at most 3 expanded groups). spec-06 §19.6 (T-HIER-3 row), spec-08 §14.5, `ce.hierarchy_errors` and 08 §3.4 carry the same rule.

**Rationale.** The owner chose to build the method (2026-10-10), and the design already specifies it. The resolutions keep every result exact under `check_examples`. They never invent or spread mass, and they make a deadline miss visible as an `unavailable` answer. Early commit stays [X] (R5).
**Refs.** 05:L75; 02:L312-318; 08:L243-295; 03:L220; 07:L154; README:L28; spec-06 §19; OWNER-CHECKPOINT "Owner answers (2026-10-10)".
**Patch: doc + contract.**
- spec-08 §14.5: the `hierarchy_errors` integer expansion rule.
- §14.6: the hierarchical example is regenerated, with `normalized_entropy` 0.7489.
- §14.8: three audit cases.
- §14.9: 05:L75 gains the hierarchical method in the Phase 1 decision engine row.
- The earlier patch "Phase 1: the hierarchical method is not available yet" (03 §4.1, 07:L154, 08 §3.4) is **withdrawn**.

**Material.** Y (R5): owner answer 1 applied. Source: IMPL-DEC-5, revised by the lead architect after the owner answers.

### IMPL-028 (gap #28): `ordinal_dispersion`
**Decision.** As in types:L185 and ce:L49-54: `m = argmax_i p_i` (ties → lowest index); `MAD_u = (1/K)·Σ_i |i − (K−1)/2|`; `value = max(0, 1 − Σ_i p_i·|i − m| / MAD_u)`. Self-tests: (0.1, 0.7, 0.2) → 0.55; (0, .3, .3, .4, 0) → 0.25; K = 2 equals `normalized_max`.
**Refs.** 02:L412; types:L185; ce:L49-67. **Patch: doc** (02:L412 formula text). Source: IMPL-DEC-6.

### IMPL-029 (gap #29): The unspecified mechanisms
**Decision.**
- **(a) Escaping.** The shared `core/text.py` provides `clean_text(s)` (removes C0/C1 controls except `\t\n\r`) and the one-argument `escape_reserved_tags(s)`, which rewrites `<` followed by optional whitespace or format characters, an optional `/`, optional whitespace or format characters again, and then `untrusted_input|app_question|decision_context|question|answer` (case-insensitive, word boundary) to `&lt;…`. So `< /untrusted_input>` and `<` + U+200B + `/untrusted_input` are escaped too. Exact code: spec-06 §5.2.
  - It applies to every untrusted text that enters any prompt (decision or generation): user text parts, document Markdown, OCR text and history.
  - Conditioning option values (08:L228): `json.dumps(clean_text(v)[:64], ensure_ascii=False)` (the 64-character cut for request-supplied values only), then the `app_question` replacement of `&`, `<`, `>` with the JSON escapes `\u0026`, `\u003c`, `\u003e`. JSON-encoding alone would let a value contain a literal `</decision_context>` (spec-04 §5.2 `qv`, SEC-INJ-05).
  - Decision prompts JSON-escape `<`, `>` and `&` inside `<app_question>` the same way.
- **(b) Spotlighting.** *Superseded by IMPL-086 (2026-10-10): `standard` now datamarks document and OCR-derived blocks in every generation and decision prompt, and `strict` additionally datamarks user and history text. The original text follows for traceability.*
  - `standard` (always): document Markdown, OCR text and the text derivatives of history media go in `<untrusted_input source="doc-1" kind="markdown">…</untrusted_input>` blocks, plus (a). History user and assistant **text** is replayed as role messages through `esc` (= `escape_reserved_tags ∘ clean_text`), without a wrapper (spec-04 §9, spec-09 §5).
  - `strict` adds datamarking inside those blocks: `datamark(s) = re.sub(r"\s+", "ˆ", s)`, every whitespace run becomes exactly one U+02C6 with no surrounding spaces. The static system preamble always explains the marker, so the prefix stays byte-stable.
  - `strict` applies only to generation attempts that **start after** the answer that triggered it is final (IMPL-002). Decision prompts are always standard.
- **(c) Decision TPM reservation.** Per call: `TokenEstimator.estimate(model, system, blocks) + max_tokens` (spec-06 §5.4–5.5), reserved for the worst-case graph and reconciled with the actual usage.
  - `TokenEstimator` is sync and owned by ops-ledger: chars ÷ the model's chars-per-token (default 4.0); images `ceil(w·h/750)`, capped at 1,600; documents 1,500 per page.
  - The 2% accuracy gate is Phase 0/live only.
  - *Amendment (Wave E2 defect 32, admission-estimator; recorded in Wave E3, 2026-10-12):* the chars-per-token is **per model alias**, with a per-request overhead: `TOKEN_ESTIMATOR_V1` (`noema/config/models.py`), fitted on the 48 answered Wave E live calls by `python -m noema.ops.cost.estimator_fit` (the three Claude models 3.06 chars per token + 10 tokens, Sol 4.61 + 11, DeepSeek 4.12 + 7, Luna 3.99, GLM 4.0 + 25). An alias not in the table counts chars ÷ 4 and no overhead. Each system string and text block rounds up on its own. K1 (≤ 2% per call) stays live/Phase 0: offline, 33 of 46 calls were within 2%, the max error 5–10% per call (`reports/qa/e2/admission-estimator.md` §4).
  - *Amendment (Wave E3, 2026-10-12):* a Phase 0 refit replaces the table **without a code change**: `NOEMA_TOKEN_ESTIMATOR_TABLE` = an absolute path to the JSON `estimator_fit` writes (`{"models": {alias: {"chars_per_token", "request_overhead_tokens"}}}`). Composition loads it at build (`_estimator_override`; an unreadable or invalid file stops the process with `StartupError`); an alias missing from the refit counts chars ÷ 4. The process variable was chosen over a config field (the admission fixer's two options) because it needs no schema change and a refit is a deployment artifact, not tenant config (spec-05 §5). The generation waste estimate of a cancelled stream (IMPL-020) still divides by `CHARS_PER_TOKEN_V1` (generation owner, open).
- **(d) Cache-write attribute.** The mapping layer emits `gen_ai.usage.cache_creation.input_tokens`. B1 verifies the name against the pinned semconv package; if it is absent there, it uses `aghoo.usage.cache_write.input_tokens`.
- **(e) PII detection.** Phase 1 uses deterministic recognizers, with no Presidio or NER: emails, phone numbers (E.164 and national formats), payment cards (Luhn), IBAN (mod-97), US SSN and the secret patterns (Bearer, AWS key ids, session tokens, JWTs).
  - Used for log and telemetry redaction (always) and for `redacted` content storage (IMPL-018).
  - Egress redaction and automatic up-classification are Phase 2 (Presidio).
- **(f) PDF active content.** 02 wins: a pikepdf rewrite produces `pdf_sanitized`, and page images are rendered **from `pdf_sanitized`** (ADR-002 §5). Removed (exact table: spec-09 §6.3), each recorded as an `active_content[]` fact and reported as `active_content_removed`:
  - `/OpenAction` (`open_action`) and every `/AA` key of the catalog, pages, annotations and form fields (`additional_actions`);
  - JavaScript: `/Names/JavaScript`, every `/JS`, and `/JavaScript` actions anywhere in an `/A` value or a `/Next` chain (`javascript`); `/Launch` actions (`launch`);
  - `/GoToE`, `/GoToR`, `/SubmitForm`, `/ImportData`, `/ResetForm` actions and `/URI` actions outside http, https and mailto (`remote_action`);
  - `/Rendition`, `/Movie`, `/Sound`, `/RichMediaExecute` actions, and `/Screen`, `/Movie`, `/Sound`, `/3D`, `/RichMedia` annotations (`rich_media`);
  - `/FileAttachment` annotations, `/Names/EmbeddedFiles`, every `/EF` key and `/Collection` (`embedded_file`);
  - `/AcroForm/XFA` and `/NeedsRendering` (`xfa`).
  A post-scan of the output re-runs the detector; any hit fails the derivative (`extraction_failed`).
- **(g) Type-equivalence map.** The complete Phase 1 table of (declared types, extensions, Magika labels, libmagic MIME types) is in spec-07 §2. A file is accepted only if all four agree with one row.
  - **Synchronous pre-check** at upload, before any byte is stored (spec-07 §2.1): the normalized declared type (lower-cased, parameters stripped) must be in some row's declared types and the extension in some row's extensions, else 415 `unsupported_media_type` (IMPL-062). Inline images skip the extension check.
  - **Detection** (`detect` in the sandbox): no row for a detector → `rejected{not_allowlisted}`; Magika and libmagic both match rows but disagree, or the declared type or extension is outside the detected row → `rejected{type_mismatch}`.

**Rationale.** Each item is the simplest mechanism that meets the stated control.
**Refs.** 04:L236, 04:L253-256, 04:L265-268; 02:L320-324, 02:L762, 02:L821; 08:L194, 08:L228, 08:L429.
**Patch: doc.** 04:L236 "stripped by rendering pages to images" → "stripped by a pikepdf rewrite (`pdf_sanitized`); page images are rendered from the sanitized file"; 04:L265 "(Phase 1: deterministic recognizers; Presidio NER in Phase 2)". Source: lead architect, plus IMPL-DEC-14 (escaping) and ADR-002 (f).

## B. Contracts (gaps 30–41)

Exact payloads: spec-08 (the verified rev-6 base, Appendix A/B) plus the consolidation addendum spec-08 §14, which supersedes the base where they differ.

### IMPL-030 (gap #30): `not_found` and the complete error table
**Decision.** Add `not_found` to `ErrorCode`. It covers every unknown or foreign id except session ids, which keep `session_not_found`: turn, decision, file, derivative kind, calibrator, `retry_of`, file ids in input parts, and unknown routes.

*Amendment (orchestrator, 2026-10-10, foundation freeze):* a path id that cannot match its pattern is an unknown id, so it is 404 (`not_found`, or `session_not_found` for sessions) on every operation, whether or not the operation declares 400. A derivative `kind` outside the `DerivativeKind` enum is a request-gate failure, 400 `schema_violation` (IMPL-065); an enum kind with no derivative is 404. The nine operations that lacked a 400 (getTurn, cancelTurn, resumeTurnEvents, getDecision, getSession, deleteSession, getFile, deleteFile, watchFile) now declare `400 schema_violation` for header and query failures (IMPL-033/064), and the server-side undeclared-400 allowance is removed.

*Amendment (orchestrator ruling R7, 2026-10-11, Wave E2; contract B1, finding 17):* a path id holding a control character (U+0000–U+001F, U+007F–U+009F) is such a malformed id: 404 (`not_found`, or `session_not_found`), decided in the request gate before any SQL, never 500. An id in a request body (`file_id` parts, `session_id`, `retry_of`) that holds U+0000 is never looked up, so it gets the 404 of an unknown id. Before this, 16 of 18 such requests reached asyncpg and answered 500 `internal_error`. A query parameter holding a control character is 400 `schema_violation`.

Also add `permission_denied` 403, `invalid_end_user_token` 401 (reserved in Phase 1, IMPL-033), `request_too_large` 413, `feature_not_available` 422, `internal_error` 500 (new type `server_error`) and `not_implemented` 501.

**One code → one HTTP status, `type` and `retryable`**, frozen as `core/errors.py ERROR_TABLE` (spec-08 §2). `budget_exhausted` is never raised in Phase 1.
**Refs.** 03:L47-60, 03:L52; openapi:L584-590. **Patch: contract** (openapi, types) + doc (03 §1). Source: IMPL-CON-01.

### IMPL-031 (gap #31): Decision profile resolution
**Decision.**
1. `DecisionCreateParams.profile` is optional. Resolution: request `profile` → `tenants.<t>.defaults.decision_profile`.
2. **The config loader fills `defaults.decision_profile = "lightweight-model-decide"` when a tenant omits it**, and a P1 config rule rejects a config where the resolved default isn't an existing decision profile. The default is therefore always defined, D6 holds for every tenant, and there is **no** "no default" 400 path (critic 5).
3. Allowlist = `{defaults.decision_profile} ∪ request_overrides.decision_profiles`.
4. Errors:

| Case | Native `/v1/decisions` | Facade |
|---|---|---|
| unknown id, or an id outside the allowlist | 403 `profile_not_allowed`, `details.allowed` = the caller's allowlist (both cases look the same, so the config can't be enumerated) | same |
| `model` not of the form `aghoo/<id>[@v]` | — | 400 `model_not_available`, `details.allowed` |
| pin `@v` ≠ the active version | 422 `unsupported_parameter`, `param: profile`, `details {requested, active}` | 400 `model_not_available` |
| `verify-dist` profile with a standard input | 422 `unsupported_parameter`, `param: profile` | same |

5. On `/v1/decisions` and the facade, a profile's built-in `questions` and `commit_critical` are ignored. The facade keeps `model` required (OpenAI shape), so the tenant default never applies there.
6. The idempotency fingerprint hashes the body as sent.
7. Turns: IMPL-069.

**Rationale.** 03:L221 and 08:L33 already promise the default; only the schema contradicted them. One error rule on both surfaces is simpler, and `GET /v1/profiles` already lists what a caller may use. The facade description in the rev-6 base ("an unknown profile is 400") is changed to match (spec-08 §14).
**Refs.** dschema:L272-280; types:L93-95; 03:L221, 03:L303; 08:L33; yaml:L12; critic findings 4, 5, 35.
**Patch: contract + doc.**
- dschema `required`, types `profile?`;
- the facade description;
- 08:L33: "the default applies when `profile` is omitted; it defaults to `lightweight-model-decide` if the tenant config omits it".

**Material.** Y (touches D6: implements it for every tenant). Source: IMPL-CON-02 + IMPL-DEC-9 + critic findings 4/5/35.

### IMPL-032 (gap #32): Error responses per operation
**Decision.**
- Every authenticated operation declares 401 and 500; every rate-limited one declares 429; every operation that accepts `x-aghoo-end-user` declares 422.
- Each operation lists `x-noema-error-codes`. The statuses implied by those codes equal the declared 4xx/5xx statuses (spec-08 §3).
- Shared response components replace the generic `Error`.
- Specific cases:

| Operation | Status | Case |
|---|---|---|
| createTurn | 410 | `Idempotency-Key` replay of a streamed turn whose event log expired (IMPL-021) |
| createTurn | 504 | `stream: false` past the deadline |
| createTurn | 413 / 415 | body cap, inline image, Content-Type |
| createDecision | 404 | session or file ids |
| createDecision | 409 | idempotency, `file_not_ready`, `file_rejected` |
| createDecision, facade | 504 `deadline_exceeded` | **only** if `ctx.deadline.remaining_ms() < MIN_DISPATCH_MS` (50) when stage 0 would dispatch (after admission, credentials and media ingest). No row, no usage. After dispatch the result is 200 (native) or per IMPL-057 (critic 15/36) |
| facade | 403, 409, 413, 415, 422, 503, 504 | — |
| cancelTurn | 409 | `idempotency_key_reused` |

- `no_capable_member` is **removed** from the `x-noema-error-codes` of createDecision and the facade. It is answer-level only in Phase 1 (an `unavailable` answer); no request-level path exists (critic 61).

**Refs.** openapi:L51-473; 03:L47-60, 03:L297, 03:L313; spec-06 §15.
**Patch: contract** (the rev-6 base plus the §14 change to the two code lists). **Material.** Y. Source: IMPL-CON-03 + critic findings 15, 36, 61.

### IMPL-033 (gap #33): Headers
**Decision.** As in IMPL-CON-04 (spec-08 §4):
- `x-request-id` is echoed or replaced, never rejected, and is required on every response;
- `traceparent`/`tracestate`;
- `Aghoo-Deadline-Ms`, now also on the facade;
- `Aghoo-Version` `enum ['2026-10-01']`, default `2026-10-01`; anything else → 400;
- `Idempotency-Key` (1–128 printable ASCII);
- the 6 `x-ratelimit-*` headers from the most constraining bucket;
- `retry-after`;
- `x-aghoo-idempotent-replay: true`.

**Change from IMPL-CON-04:** `x-aghoo-end-user` verification is **deferred to Phase 2**, together with OIDC. In Phase 1 every project answers the header with 422 `feature_not_available`, `param: x-aghoo-end-user`. There is no JWKS config, and `invalid_end_user_token` is reserved. The contract is unchanged (critic 58; contracts open question 3).
**Rationale.** Per-project JWT verification is not in 05 Phase 1 (05:L91 defers OIDC/BFF). The contract already carries the fallback.
**Refs.** 03:L29-41, 03:L62; 04:L201; 05:L91. **Patch: contract.** **Material.** Y. Owner question 5. Source: IMPL-CON-04 + critic finding 58.

### IMPL-034 (gap #34): SSE contracts
**Decision.**
- Named, closed `TurnEvent` variants with `discriminator: type`.
- types.ts `ErrorCode`/`ErrorType`/`ErrorEnvelope`; the SSE `error.code` is `OpenEnum<ErrorCode>`.
- Framing: `id`, `event`, one `data:` line, no `retry:`; a `: ping` comment after 15 s with no frame.
- File events: `event: file.updated` with `FileObject` snapshots, starting with the current one; the stream closes after a terminal status.

**Refs.** openapi:L767-872; types:L528-544; 03:L139-141. **Patch: contract.** **Material.** Y. Source: IMPL-CON-05.

### IMPL-035 (gap #35): SDK strategy
**Decision.**
- **TypeScript** `@aghoo/sdk`:
  - wire types generated by `openapi-typescript@7.13.0` from the bundled OpenAPI;
  - a hand-written client against `contracts/sdk.ts`, with a compile-time wire-compat check (18 assertions);
  - `q.choice(name, {choices: readonly ChoiceOption[]})`;
  - new methods: `sessions.list/update/turns`, `files.derivative`, `usage.list`, `admin.*`;
  - `AghooError`; retries only before the first stream event, `maxRetries` 2 with full jitter; an automatic `Idempotency-Key`.
- **Python** `aghoo`:
  - a sync, hand-written httpx client;
  - tolerant response models (the vendored `python_sdk_types.py` plus the same style for events, turns, sessions, files, profiles and usage);
  - `TypedDict` request params.

Bundling uses `@redocly/cli@2.62.1`, with `$defs.Description` renamed to `OADescription`.
**Refs.** 05:L72; sdk:L36-39; 03:L448-506. **Patch: contract + doc.** **Material.** Y. Source: IMPL-CON-06.

### IMPL-036 (gap #36): Out-of-phase request values
**Decision.** A value assigned to a later phase gets a typed error, never a silent fallback. `param` names the field.

| Input | Phase-1 answer |
|---|---|
| `strategy: speculative \| hedge` | 422 `feature_not_available`, `param: strategy` |
| `generation.profile` with mode `best_of_n` / `fuse` (omitted from `GET /v1/profiles`; config activation rejects bindings or routes to them) | 422 `feature_not_available` |
| Hierarchical method needed (K > L(answering profile)): native `/v1/decisions` and turn app questions | **Not out of phase since 2026-10-10** (owner answer 1): answered hierarchically (IMPL-027, spec-06 §19); no request-level error |
| **Hierarchical method needed: facade, or `output_format: "jev"`** | **422 `unsupported_parameter`**, `param: questions[i].choices`, `details: {max_choices: L, method: "hierarchical"}`: permanent, because these formats can't carry `unresolved_mass` (03:L297, 03:L375) |
| `retention: "custom"` | 422 `feature_not_available`, `param: retention` |
| `x-aghoo-end-user` (any project, IMPL-033) | 422 `feature_not_available` |
| `GET /v1/turns/{id}/events` | 501 `not_implemented` |
| DOCX / SVG / audio / video upload | 415 `unsupported_media_type` |
| a non-API-key bearer token (OIDC) | 401 `invalid_api_key` |

Unreachable in Phase 1, with the enums kept and tests asserting they never occur:
- `budget_exhausted`;
- `generation.rerouted`;
- `generation.committed.reason` `speculative`/`selected`;
- `generation.cancelled.reason` `not_selected`;
- `generation.completed.finish_reason` `cancelled` (a cancelled committed attempt gets `generation.cancelled{client_abort}`, IMPL-011);
- `RoutingDecision.mode` `best_of_n`/`fuse`;
- `selection: expected_utility`;
- `decision.completed.arrived: after_completion`;
- `invalid_end_user_token`.

**This is the single resolution of the hierarchical error code** (critic 2/29): IMPL-027 refers here. Since 2026-10-10 the only such error is the facade/Jev 422 `unsupported_parameter`; the compiler raises it from `DecisionRequest.output_format` (IMPL-070). `feature_not_available` is never raised for a choice question.
**Refs.** 05:L68-91; 03:L122, 03:L220, 03:L297, 03:L375; 07:L154; 08:L288.
**Patch: contract** (descriptions) + doc:
- 07 strategy row, FAQ and errors table;
- 03 §2, §4.1 (without a hierarchical 422 for native requests: withdrawn 2026-10-10);
- ~~08 §3.4: "native: 422 `feature_not_available` in Phase 1"~~ withdrawn (owner answer 1); 08 §3.4 already says Jev and facade get 422 `unsupported_parameter`.

**Material.** Y (R5). Owner question 1, answered 2026-10-10: build the method; only the facade/Jev 422 remains. Source: IMPL-CON-07, adopted over IMPL-DEC-5's code; revised by the lead architect after the owner answers.

### IMPL-037 (gap #37): Type fixes and the inline-image size check
**Decision.**
- `ProfileInfo.version` is `string ^[0-9]+$`. The server emits `str(profile.version)` for decision profiles and `str(config_version)` for generation profiles (critic 55).
- Inline `image_url`:
  - drop dschema `maxLength: 7000000`;
  - the **decoded size is computed arithmetically from the base64 length** at ingress, with no decode: `n = len(b64)*3//4 − padding`. `n > 3,750,000` → synchronous 413 `image_too_large`;
  - the > 8,000 px check runs in the sandbox `image.normalize` job and also gives 413 `image_too_large`, before any model call (IMPL-074). The API never decodes media (ADR-002).
- `x_aghoo` is typed and closed (`decision_id`, `calibration`, `mapped_unavailable?`). Keys are client-visible names (IMPL-047).
- The facade rules JSON Schema can't express (unique explicit names, reserved `q<digits>`, true/"true" collisions, ≤ 20 images) run as imported `check_examples.oa_request_invariants` → 400 `schema_violation` with `param`.

**Refs.** openapi:L942; dschema:L204-208; oaschema:L82; 03:L295-313; critic findings 27, 45, 55. **Patch: contract.** **Material.** Y. Source: IMPL-CON-08 + critic findings.

### IMPL-038 (gap #38): Config for Phase 1 auth and ops
**Decision.** Exact schema text in spec-05.

| Item | Where |
|---|---|
| projects, principals, API keys, operators | **Postgres rows** minted by `noema-admin` (IMPL-044): operational records with a lifecycle, not versioned config. `project.default_data_class` (default `internal`) feeds L2 gating and storage policy |
| rate limits | `tenants.<t>.limits {rpm_per_key, tpm_per_key, rpm_per_project, tpm_per_project, concurrent_streams_per_project, model_quotas{alias: {rpm, tpm}}}`; platform defaults in `platform.limits` |
| budgets | none (Phase 2) |
| data-class policy | `platform.data_classes.<class> {store_content: full\|redacted\|metadata_only, retain_days, l2_cache: bool}`, with design defaults (spec-05) |
| prices | `platform.cost.prices_ref: "db:<version>"` naming a `platform.price_catalog` version imported by `noema-admin prices import` (`file:<path>` in dev/test only; no default, absent = every price TBD) (IMPL-019) |
| model context window | `models.<alias>.context_window` (optional; IMPL-084) |
| per-model region, Mantle endpoint | no key. Region = the tenant's `home_region`; Mantle base URL = `https://bedrock-mantle.{home_region}.api.aws/v1`. Endpoint overrides only via the env var `NOEMA_AWS_ENDPOINT_OVERRIDES` in dev/test (ADR-001 §8, spec-05 §5) |
| idempotency TTL, resume window, JSON body cap, L2 TTL, session queue timeout, cancel overrun, inline media wait, Mantle auth mode | `platform.{idempotency.ttl_s: 86400, events.resume_window_s: 300, limits.max_json_body_bytes: 52428800, decision_cache.ttl_s: 600, sessions.queue_timeout_ms: 2000, cost.cancel_overrun_tokens: 64, media.inline_wait_ms: 3000, credentials.mantle_auth: bearer_short_term}` |
| decision `deadline_ms` maximum | **no key**: the schema bound (100–30,000, dschema `DecisionCreateParams.deadline_ms`) is the limit |
| template prompt text | code (IMPL-053) |
| end-user JWKS | none (IMPL-033) |
| acceptance thresholds | `labeling.acceptance.*` (IMPL-039, ADR-003) |

All new keys are optional with design-value defaults, so the existing yaml stays valid. New check_config negative tests for each new rule (spec-05 §4).
**Refs.** cschema top-level `additionalProperties: false`; 03:L36, 03:L205; 04:L276-278; 08:L22-34; critic findings 1, 22, 51.
**Patch: contract** (cschema, cc and its tests; spec-08 §14.4). **Material.** Y (new config surface). Source: lead architect.

### IMPL-039 (gap #39): Duplicated constants
**Decision.**
- The server reads bands from `snapshot.routing.confidence_bands`, acceptance thresholds from `snapshot.labeling.acceptance` plus `family_overlap_lift_max`, and template limits from `snapshot.templates`. Nothing is hard-coded.
- **Bound rule** (new P1 config rule): the config must be at least as strict as the contract constants, which the tolerant client models enforce:
  - `ece_max ≤ 0.05`, `n_min ≥ 300`, `class_n_min ≥ 30`, `consensus_min ≥ 0.6`, `family_overlap_lift_max ≤ 0.10` (python_sdk_types:L21-23, ce:L26);
  - `drift_ece_max ≥ ece_max`.

  Loosening any threshold needs a contract release.
- `check_examples` is parameterized (rev-6 addendum):
  - `band_of(v, bands=BANDS)`;
  - `holdout_errors(h, classes, *, accept=ACCEPT)`;
  - `template_limit_errors(..., template_max=TEMPLATE_MAX)`;
  - `result_invariants(result, req, *, bands=BANDS, accept=ACCEPT, template_max=TEMPLATE_MAX)`.

  The module defaults still come from the example yaml (design checks unchanged). The server's test-mode `ContractValidator` passes the active snapshot's values.

**Rationale.** It removes the duplication without breaking deployed clients.
**Refs.** python_sdk_types:L21-23; ce:L25-34; ADR-003 §3; critic finding 51.
**Patch: contract** (ce, plus the cc rule). Source: lead architect.

### IMPL-040 (gap #40): Thin schemas
**Decision.** As in IMPL-CON-09:
- `SessionUpdate` (`minProperties: 1`; replace semantics);
- `SessionTurn` / `SessionTurnList` (committed turns, oldest first, code-point redaction spans);
- `SessionList` (newest first);
- `UsageReport` with `limit`/`after`, `group_by` default `line`, and `key` per dimension or null; `from`/`to` default to the last 30 days, at most 366;
- `cache_savings` = avoided cost.

**Refs.** openapi:L280-345, L436-472; 03:L393. **Patch: contract.** **Material.** Y. Source: IMPL-CON-09.

### IMPL-041 (gap #41): Server models and validation
**Decision.** As in IMPL-CON-10:
1. **Request gate**: JSON Schema via `jsonschema-rs`, compiled per operation from the bundle. It is the only accept/reject authority.
2. **Typed access**: strict generated Pydantic v2 models (`datamodel-code-generator==0.83.0`, `StrictBase`), built after the gate.
3. **Semantic rules**: imported `request_invariants` / `oa_request_invariants` → 400 with `param`.
4. **Test-mode `ContractValidator`** (`NOEMA_CONTRACT_VALIDATE=1`, refused in prod) checks:
   - each body and SSE `data` against the schema;
   - status, type and retryable against `ERROR_TABLE`, and the code against the operation's list;
   - `x-request-id`;
   - SSE framing;
   - decision invariants (parameterized, IMPL-039);
   - that the tolerant client models parse every decision body.
5. `python_sdk_types.py` stays client-only.
6. `check_examples.py` is importable (`main()` guard, `Violation`).

**Addition (critic 51):** `ContractValidator.check_sse_frame(operation_id, frame_id, event, data, *, decision_request: Mapping | None = None)`. The turn harness passes the compiled question wire dicts and input, so `decision.completed`/`updated` and `turn.verified` are checked with `result_invariants`. In production the hot path never builds strict models (IMPL-070).
**Refs.** python_sdk_types:L1-31; ce:L479-489; 03:L276; 05:L146. **Patch: contract.** Source: IMPL-CON-10 + critic finding 51.

## C. Media, labeling, UI (gaps 42–46)

### IMPL-042 (gap #42): W4 dashboard endpoints
**Decision.**
- Three read-only operations, tag `admin`, paginated newest first, with roles in `x-noema-roles`:
  - `GET /v1/admin/calibrators` (tenant_admin, reviewer, auditor);
  - `GET /v1/admin/labeling/runs` (same roles);
  - `GET /v1/admin/labeling/dropped` (tenant_admin, reviewer: it shows content).
- **`CalibratorInfo` is aligned with ADR-003 before the freeze** (critic 11/31; exact YAML in spec-08 §14.3):
  - drop `level` and `member_id`, since one artifact holds both parts;
  - `status` ∈ `fitted | rejected | accepted | active | downgraded | retired`;
  - `parameters {pooled: {temperature} | {a, b}, member_T: {m1: T…}, epsilon}`;
  - `acceptance.failed_rules` ∈ the A1–A8 names `[ece_max, nll_not_worse, n_min, flagged_classes, class_consensus_min, family_overlap_lift, label_source, member_subset_share]`;
  - `approved_by` / `activated_by` for two-person activation (IMPL-072);
  - `downgraded_at`, `downgrade_reason` (`drift_ece | drift_ci`);
  - `retired_reason` (`replaced | manual | profile_version_retired`).

  There is no `suppressed` field, because Phase 1 has no per-tenant suppression (IMPL-072).
- Platform artifacts are listed with `scope: platform`. Activation stays in the CLI.
- **Scope**: the three reads are tenant-wide for every allowed role, because labeling runs, label items and calibrators are tenant-level rows with no `project_id` (spec-03 §2, ADR-003). A reviewer therefore sees the content excerpts (≤ 2,000 chars, redacted, null when the data class or log policy forbids stored content) of every project's dropped items. This is accepted for Phase 1: labeling review is a tenant-level function. Restricting excerpts to the reviewer's project would need `label_item.project_id` (Wave A2 note in OWNER-CHECKPOINT).

**Refs.** 05:L33 vs 05:L82; 03:L441, 03:L517, 03:L533; 06:L51-60.
**Patch: contract + doc** (03 §2 catalog). **Material.** Y. Source: IMPL-CON-11 + critic findings 11, 31.

### IMPL-043 (gap #43): Sonnet 5.5 media and effort
**Decision.**
- `sonnet-5.5` keeps `modalities [text, image, document]` and `effort: []` as configured, but its media support is **UNVERIFIED**.
- The gated live smoke spends 2 of its 60 calls on Sonnet: one image (`number.png`) and one PDF (`invoice.pdf`). It writes `reports/live/sonnet-media.json`.
- Until then, and whenever a modality fails, labeling uses the normal negotiation: a labeler whose model lacks a modality gets the derivative view, recorded in the run. If a probe fails, the operator removes the modality in config.
- No `outputConfig.effort` is sent (empty list), and no temperature.

**Rationale.** It doesn't block Phase 1, and it is verified within the existing call budget.
**Refs.** yaml:L58-63; 03-env "Sonnet 5.5 was only probed once". **Patch: none.** Source: lead architect.

### IMPL-044 (gap #44): Phase 1 bootstrap
**Decision.** The `noema-admin` CLI (identity role) is the only admin surface in Phase 1. Commands:
- `bootstrap --operator <name>`: one-time; needs the DB owner DSN; creates the first operator and prints its key once.
- `operators create|revoke`.
- `config validate|propose|approve|rollback`: two-person; the proposer ≠ the approver. Approval activates the version and syncs `tenant` rows.
- `tenants external-id create|rotate` (ADR-001 §3).
- `projects create|list` (`--default-data-class`).
- `principals create` (`--roles`).
- `keys create|list|revoke` (`--scopes`, `--expires`, `--ip-allow`); the key is printed once.
- `calibrators list|approve|activate|retire`: two-person (IMPL-072).
- `prices import <file>`.

Every command authenticates with `NOEMA_OPERATOR_KEY` (an HMAC-hashed `operator` row, same scheme as IMPL-008). It connects as `noema_cli`, calls `admin.*` SECURITY DEFINER functions (exact list: spec-03 §4) and writes an `audit_event`. Besides the mutating functions, the list includes `revoke_operator`, `config_rollback`, `record_audit` (an audit row for CLI actions outside the DB, such as an ExternalId rotation in Secrets Manager) and the read-only `list_projects`, `list_api_keys` (never hashes), `list_calibrators`, `list_config_versions` that the `list` commands need.

**Config integrity in Phase 1** = the two-person DB approval (`config_version.approved_by ≠ proposed_by`) plus full re-validation at load (schema, `check_config`, P1 rules): a pod refuses an invalid snapshot and keeps the previous one (SEC-CFG-01). The "signed, versioned config" of 02:L66 is not implemented in Phase 1 (no signature over the config body).
**Rationale.** 03:L82 makes Phase 1 IaC/CLI-driven. Two-person approval follows 02:L138 and 08:L22.
**Refs.** 03:L82, 03:L439; 02:L138; 08:L22; ADR-001 §3. **Patch: doc** (03:L82 "Phase 1: `noema-admin` CLI"). Source: lead architect.

### IMPL-045 (gap #45): Context selection without a summary model
**Decision.**
- **Generation context**: the static platform preamble, then the developer `instructions`, then the most recent committed turns verbatim, selected newest-first within the budget and emitted in chronological order, then the current turn.
  - Budget = `context_window − max_output_tokens − reasoning_reserve − current-turn estimate − 5%` safety margin, or `history.max_tokens` if smaller. History is selected once per turn at ingress (before admission, IMPL-022), so it uses the worst case over the turn's candidate generation members: the smallest `context_window` (IMPL-084) and the largest `max_tokens` (spec-04 §9, spec-11 §1.1).
  - `history.mode`: `auto` = by budget; `none` = no history; `last_n` = the last *n* turns within the budget.
  - Past media is included as text derivatives only; native media only for the current turn and the previous media-bearing turn (02:L725, 02:L801).
- **Compact decision context**: the text of the last 2 committed turns (user and assistant), at most 2,000 tokens; the oldest is cut first, at a turn boundary, otherwise truncated from its start. It is followed by the current user message with its media views.
- No `summary` row and no summary model in Phase 1.

**Refs.** 02:L229, 02:L720-727; openapi:L652-658; PLAN. **Patch: doc** (02:L723 "Phase 2"). Source: lead architect.

### IMPL-046 (gap #46): Language ID
**Decision.** None in Phase 1. Preflight records `language = "und"`, and labeling slices use `language:und`. No dependency is added.
**Rationale.** Nothing in Phase 1 consumes the language; a library would be speculative.
**Refs.** 02:L115; PLAN "Language ID: a cheap library or 'und'". **Patch: doc** (02:L115 "language ID (Phase 2)"). Source: lead architect.

## D. Decisions not in the gap list

### IMPL-047 (new): Answer names for facade decisions; `x_aghoo` keys
**Decision.**
- The facade names question *i* internally: an explicit name that matches the native `Name` pattern is kept; any other question gets `q{i}` (unnamed, or an explicit name like `"Severity Level"`).
- The stored native result (`GET /v1/decisions/{x_aghoo.decision_id}`) uses the internal names. dschema gains a response-side `AnswerName` (`^[a-z][a-z0-9_]{0,63}$`, without the `q<digits>` reservation), used in `ScoredBase.name`, `RefusalAnswer.name`, `UnavailableAnswer.name` and `EscalationStage.questions.items`. Requests keep `Name`, so a client still can't send `q0`.
- **`x_aghoo.calibration` and `mapped_unavailable` are keyed by the client-visible effective name**: the given name, else `q{i}`. The facade keeps a `given → internal` map for the reverse mapping.
- `calibration` holds the non-refusal answers only. `mapped_unavailable` holds exactly the answers mapped from `unavailable`, including IMPL-049's large-unresolved case.
- New example pair with an explicit non-pattern name, plus a unit test (critic 10/30).

**Refs.** dschema `$defs.Name`; 03:L295; oaschema `OAQuestion.name`; spec-08 §8 and Appendix B `oa_invariants`. **Patch: contract** (CP-DEC-1, folded into rev-6, spec-08 §14.2). **Material.** Y. Source: IMPL-DEC-7 + critic findings 10, 30.

### IMPL-048 (new): Per-stage provenance for escalated answers
**Decision.**
- `EscalationStage` gains two optional fields, always set by the server: `input_view_signature` (that stage's view signature) and `prompt_templates` (`member_id → template`).
- `check_examples.answering_stage` returns a 4-tuple `(profile_id, profile_version, input_view_signature, prompt_templates)`, falling back to `provenance` for stage 0, and the binding check uses it.
- If the owner rejects this, escalated answers skip the calibrator lookup and are always `uncalibrated`.

**Rationale.** Member ids are positional per stage, so stage-0 provenance points at the wrong template and view for an escalated answer.
**Refs.** ce:L151-157, L183-201, L320-330; types:L280-286. **Patch: contract** (CP-DEC-2, rebased onto rev-6 in spec-08 §14.2). **Material.** Y. Owner question 4. Source: IMPL-DEC-8.

### IMPL-049 (new): Jev and facade value mapping
**Decision.** Each format is self-consistent with **its own emitted values** (critic 6/7):
- **Jev**:
  - `probabilities` = `round(native emitted p, 2)` per declared option (a censored option → `0.0`);
  - `choice` = argmax of that map, ties in declared order;
  - `score = round(Σ i·p′_i, 2)` over the **2-dp map**;
  - `confidence = round(f(p′), 2)`, where `f` = `normalized_max` with n = K over the map (choice), or `ordinal_dispersion` over the map (score);
  - `noul = round(P(true), 2)`.

  Every `jev_invariants` check (tolerance 0.02) then holds by construction for every K, with no contract change.
- **Facade**: `probabilities` = the native 4-dp values in declared order; `score` = native `score`; `confidence` = `normalized_max` with n = K over the emitted probabilities (choice/predicate), or `ordinal_dispersion` (score). It matches `oa_invariants`.
- **Censored tail**:
  - a Jev choice or score map violating `|Σ − 1| ≤ max(0.011, 0.005·K)` → `{type: "unavailable", reason: "no_capable_member"}`;
  - a facade answer with emitted `unresolved_mass > 0.001` → `refusal` with `x_aghoo.mapped_unavailable[name] = "no_capable_member"`.

The native confidence keeps n = K + 1 when u > 0 (03:L287).

Test vectors:
- K = 3, native candidates A 0.6, B 0.3999, C censored, u = 0.0001: native confidence 0.4667; Jev map {A 0.6, B 0.4, C 0.0} → confidence 0.40; facade confidence 0.4.
- K = 5, p = (0.1234, 0.2345, 0.3456, 0.1965, 0.1000): native score 1.9152; Jev map (0.12, 0.23, 0.35, 0.2, 0.1) → score 1.93 (not `round(1.9152, 2)` = 1.92); Jev `ordinal_dispersion` 0.28 vs native 0.2685.

**Rationale.** Patching the checker tolerance would need 0.005·(1 + K(K−1)/2), about 0.6 at K = 16, which makes the check meaningless. Deriving from the rounded map keeps the check tight and is what a Jev client can recompute.
**Refs.** 08:L236-241; 03:L312, 03:L375-377; ce:L83-108, L418-442; critic findings 6 and 7.
**Patch: doc.**
- 08:L237-238 → "`confidence` and `score` are computed from the 2-decimal `probabilities` map";
- 03:L312 → "`confidence`: the TypeSafe formula over the emitted probabilities (n = K)";
- 03 §4.2/§4.3: one sentence each on the censored-tail mapping.

**Material.** Y (visible only in rare rounding and censoring cases). Source: IMPL-DEC-10 + critic findings 6, 7.

### IMPL-050 (new): Member calls, retries, backups, wire mapping
**Decision.**
1. **Decision member calls are never retried.**
   - A backup is purely time-triggered: at `backup_after_ms`, if the primary is still in flight, an identical call is sent. A backup applies only if `backup_after_ms + latency_p95_ms ≤` the effective stage `deadline_ms`.
   - The first call to finish `ok` or `refused` wins, and the other is cancelled. An `error` waits for the other call.
   - Backups don't consume `RetryBudget`; admission reserves them.
2. `ModelRequest` carries `call_kind: CallKind` and `retry: Literal["none", "default"]`. Decision, verification and labeling member calls pass `retry="none"`, and the adapter's resilience layer must honor it. The adapter conformance suite asserts that a `retry="none"` call is never retried on connect error or throttling.
3. **The decision module owns every prompt byte.**
   - `InputViewResolver` returns content blocks plus metadata (`label`, `kind`).
   - Decision renders the label lines `[{label}: {kind}]`, the `<untrusted_input>` wrapping and escaping, and builds `ModelRequest`.
   - The adapters map `ModelRequest` to the wire exactly as in the frozen table (spec-02 §4): Converse `system`/`messages`/`inferenceConfig.maxTokens`/`outputConfig.effort`, never `temperature` unless set; Mantle chat with blocks concatenated, `logprobs`/`top_logprobs`, `stream: false`.
   - Golden request fixtures `tests/golden/decision_requests/*.json` are shared by decision, adapters and qa-tools.

**Rationale.** A 10% retry budget would be exhausted by backups. Error retries inside a 1 s deadline add load without much gain. A single owner of prompt bytes protects calibration.
**Refs.** 02:L216; 04:L101-102, L111, L119-123, L132; 08:L95; critic findings 14 and 47.
**Patch: doc.** 04:L101-102 and L132: "decision, verification and labeling member calls: no retries; time-triggered backups only (`backup_after_ms`)". Source: IMPL-DEC-11 + critic findings 14, 47.

*Amendment (Wave E perf sprint A and io-fixer finding 1, recorded in Wave E2, 2026-10-11; `reports/perf/e-sprint-a.md` §3):* **lean Converse transport.** Converse, ConverseStream and Mantle `complete()` no longer go through aiobotocore or httpx:
- the request is built byte for byte as aiobotocore builds it (botocore's endpoint rules, `/model/{percent-encoded modelId}/converse[-stream]`, the rest-json body) and signed with `botocore.auth.SigV4Auth` (signing name `bedrock`, the credentials' region);
- it is sent over one pooled aiohttp client per (tenant, account, region) with aiobotocore's transport settings. The request line carries the path exactly as signed (a `:` in a model id or ARN goes out as `%3A`);
- responses and event streams are parsed with botocore's parsers (`RestJSONParser`, `EventStreamBuffer` with its CRC checks).

The wire mapping (spec-02 §4.2) and the error classification (§4.3) are unchanged. Differential tests against a real aiobotocore client show this (`tests/unit/adapters/test_lean_converse_{wire,parse,stream}.py`, `test_unary_mapping.py`). Each call still signs with its own `broker.get` + `assert_creds_for` credentials (IMPL-076).

Deliberate differences: the malformed-2xx rule (IMPL-076 amendment); a body-read failure after the response headers is never the in-adapter connect retry (IMPL-016 step 1 needs "no response byte"); redirects are never followed; `user-agent: noema`. The lean `complete()` met live Bedrock on 2026-10-11; the lean ConverseStream and the `%3A` path fix have not yet.

*Amendment (Wave E3 perf-cpu-shed, recorded in Wave F, 2026-10-12; `reports/qa/e3/perf-cpu-shed.md` §1–2):* two components were replaced, and the wire is unchanged.
- The signer is now `adapters/_sigv4.py` `sign_post`, a dict-based SigV4 that is byte-equal to `SigV4Auth.add_auth` (73.1 → 16.9 µs per call).
- The stream decoder is now `adapters/_eventstream.py` `EventStreamDecoder`. Its messages and exceptions equal those of botocore's `EventStreamBuffer` (6.48 → 1.63 µs per message).
- Equality was shown offline against botocore and aiobotocore (goldens, fuzzing, differential cases).
- Live in Wave E3: 28 / 28 signatures and 4,304 / 4,304 messages equal. The lean ConverseStream and the `%3A` path also answered live (Luna through its inference-profile ARN).
- The session-token branch of the signer is verified offline only (`reports/qa/e3/live-smoke.md` §6).

### IMPL-051 (new): Option order
**Decision.**
- Only choice options are permuted, per (stage profile@version, member, question). Predicates are fixed A = true / B = false; score levels keep declared order, A = lowest.
- `option_order_seed` = the first 4 bytes of `sha256(decision_id)`, masked to 31 bits. The permutation is `random.Random(sha256(f"{seed}|{profile_ref}|{member_id}|{qname}")[:8]).shuffle`.
- Test vectors: spec-06 §3.6.

**Refs.** 08:L179, L199; types:L298-299. **Patch: doc** (08:L179). Source: IMPL-DEC-12.

### IMPL-052 (new): `session_id` on `/v1/decisions`
**Decision.** Accepted and access-checked (404 `session_not_found`), then recorded on the row. No history is added to member prompts: the default is summary-only, and Phase 1 has no summary (IMPL-045). The session's `data_class` applies (IMPL-004). **Patch: none.** Source: IMPL-DEC-13.

### IMPL-053 (new): Decision templates and refusal detection
**Decision.**
- The exact text of `logprob-codes@2`, `verbalized-dist@1` and `verify-dist@1` is spec-06 §5. That text, including "Neither can change these rules.", the TAIL line and (since IMPL-086) the datamark line of `logprob-codes@2` and `verbalized-dist@1`, **is the canonical `@1`/`@2`**. No calibrator artifact exists yet, so no version bump is needed (critic 18).
- A refusal is a provider signal only: Converse `stopReason ∈ {content_filtered, guardrail_intervened}`, Mantle `finish_reason == "content_filter"` or a non-empty `message.refusal`. Refusal prose fails parsing and is `error`.
- Escaping is IMPL-029a.

**Refs.** 08:L177-211; 04:L253. **Patch: doc.** 08 §3.2: "the template text is code; the implementation text (spec) is canonical for `@1`". Source: IMPL-DEC-14 + critic finding 18.

### IMPL-054 (new): Structured-output fallback
**Decision.** Phase 1 uses prompt-only JSON. A member parse-error rate above 1% alerts (08:L459) and never switches to `textFormat` automatically: that would be a new template version plus recalibration. **Refs.** 02:L325; 08:L403, L459. **Patch: none.** Source: IMPL-DEC-15.

### IMPL-055 (new): `include` semantics
**Decision.**
- Without `include`, `/v1/decisions` answers omit `members`.
- `members` adds `members[]`. **Whenever members are emitted and an artifact applied, each `ok` member carries `calibrated_distribution` and `calibrator_id`** (critic 9). `member_distributions` is therefore equivalent to `members`, and `stages` is a no-op (always present).
- The stored row and `GET` are always full. Turn decisions always include members.

**Refs.** types:L107; ce:L361; 03:L380. **Patch: none.** Source: IMPL-DEC-17 + critic finding 9.

### IMPL-056 (new): Facade request-shape errors
**Decision.**
- A remote `image_url` → 400 `schema_violation`, `param: input[i].content[j].image_url`, enforced by the oaschema data-URL pattern in the request gate.
- More than 20 images → 400 `schema_violation`, `param: input`, `details.max_images: 20` (`oa_request_invariants`).
- IMPL-DEC-18's "400 `unsupported_parameter`" and its 03:L56 note are **withdrawn**: they would give one code two statuses (critic 3/34).

**Refs.** 03:L299-300; 06:L221. **Patch: contract** (oaschema pattern, 03:L300). **Material.** Y. Source: IMPL-CON-12 + IMPL-DEC-18 (bullet 2).

### IMPL-057 (new): Decision request outcomes
**Decision.**
- Native `/v1/decisions` with every answer `unavailable` → 200 `status: partial`.
- **503 `tenant_access_misconfigured`** when credentials fail at `start()` (no row), **or** when every answer is `unavailable` and every member error is `tenant_access_misconfigured`. In the second case the row is stored and `details.decision_id` is set.
- The facade with every answer unavailable → 503 `all_routes_unavailable` (row stored, `details.decision_id`).
- 504 before dispatch: IMPL-032.
- A decision member's model `AccessDeniedException` degrades that member to `error/tenant_access_misconfigured` and the run continues. That deviates from 04:L104's request-level 503, **for decision members only** (critic 21).
- Error mapping: IMPL-076.

**Refs.** 03:L313; 04:L104; 07:L197. **Patch: doc** (04:L104 "decision members degrade; the request fails only if every member fails this way"). **Material.** Y. Source: IMPL-DEC-19 (outcome half) + critic findings 15, 21, 36.

### IMPL-058 (new): Quorum counting, classification, `metadata_only`
**Decision.**
- Quorum counts **distinct models** among `ok`, non-optional members with `weight > 0`. Optional `ok` members join the pool but don't count.
- An answer closes when every required member is terminal, when quorum becomes impossible, at the stage deadline, or on cancel.
- Classification order: spec-06 §7.3.
- **A member whose view of some media modality in the input is `metadata_only` does not run, vote or count** (critic 25): `status: skipped`, `skip_reason: input_view`, `input_view: metadata_only`, counted in `quorum.total`. This is schema-valid (the dschema MemberVote `allOf`).

**Rationale.** A member that saw nothing of the media must not dilute the pool or satisfy quorum (02:L343-345 "neither" column).
**Refs.** 02:L334, L343-351, L362; 08:L95, L129; spec-06 §4.4, §7. **Patch: doc.** 02:L343-345: "`metadata_only` members are skipped (they neither vote nor count)". Source: IMPL-DEC-21 + critic finding 25.

### IMPL-059 (new): Input digest
**Decision.**
- `K_t = HMAC(pepper, "noema/input-digest/v1/" + tenant_id)`.
- digest = `"hmac-sha256:" + HMAC(K_t, canonical_input_bytes)`:
  - strings byte-exact;
  - everything else as canonical JSON (`sort_keys`, `ensure_ascii=False`);
  - inline images as their sha256;
  - file parts as the file sha256 plus the sorted `kind:sha256` of the ready derivatives.
- The pepper comes from the secrets provider (`noema/input-digest-pepper`, 32 bytes).

**Refs.** types:L294; 04:L155; spec-06 §3.4. **Patch: none.** Source: IMPL-DEC-22.

### IMPL-060 (new): Rounding and ordering
**Decision.**
- Candidates and member distributions are emitted at 4 dp with largest-remainder rounding, summing to `1 − unresolved_mass`.
- `raw_score` uses a plain round.
- Ordering, `choice`/`level` and votes use the emitted values, with ties in declared order.
- Confidence fields are computed on unrounded values.

**Refs.** mk:L90-91; ce:L274, L287, L314; spec-06 §8.2. **Patch: none.** Source: IMPL-DEC-23.

### IMPL-061 (new): Final answer per question
**Decision.** A question's final answer is the answer of the **last stage that asked it**, even if that stage returned `unavailable` (02:L422, applied literally). Phase 0 measures how often an escalation stage replaces a scored answer with `unavailable`. **Refs.** 02:L422; ce:L151-157. **Patch: none.** **Material.** Y. Owner question 3. Source: IMPL-DEC-24.

### IMPL-062 (new): Phase-2 upload types
**Decision.** DOCX, SVG, audio and video uploads → 415 `unsupported_media_type` (03:L411-414). The 415 comes from the synchronous pre-check at upload, on the normalized declared type and the extension, before any byte is stored (spec-07 §2.1, IMPL-029g). `not_allowlisted` (no equivalence row) and `type_mismatch` (detectors disagree, or the declared type or extension is outside the detected row) stay `FileObject.rejection.code` values for the asynchronous `detect` result. **Patch: doc** (02:L764). Source: IMPL-CON-13.

### IMPL-063 (new): Closed response schemas
**Decision.**
- Every openapi object schema gets `additionalProperties: false`; `Error.error` requires `param` (nullable) and `request_id`.
- Lists get named schemas.
- In dschema, the answer `required` lists include `type`, and the JSON-state input says `additionalProperties: true`. Validation is unchanged.

**Patch: contract.** Source: IMPL-CON-14.

### IMPL-064 (new): Transport rules
**Decision.**
- JSON bodies over `platform.limits.max_json_body_bytes` (52,428,800 = 50 MiB) → 413 `request_too_large`.
- Multipart uploads are capped per type while streaming (413 `file_too_large`).
- A wrong Content-Type → 415.
- Unknown routes and methods → 404 `not_found` (Starlette's 405 is mapped to 404).
- An unknown query parameter → 400 `schema_violation`; unknown headers are ignored.
- Query and header values are coerced per their schema type.

**Refs.** 02:L132. **Patch: contract.** **Material.** Y. Owner question 6 (the cap value). Source: IMPL-CON-15.

*Amendment (orchestrator ruling R7, 2026-10-11, Wave E2; edge-fixer 7, e4-engines-fix 8):*
- **Authentication runs before the body is read.** Only what the headers declare is decided before authentication: a `Content-Length` over the cap → 413, a wrong `Content-Type` → 415. A body is read only after a valid key (spec-04 §1 step 4), so a body of undeclared length (chunked) over the cap without a valid key is **401**, and 413 only with one. A keyless client never makes the process buffer a body.
- **At most 64 media parts per request** (`MAX_MEDIA_PARTS`, a code constant: `file_id` parts plus inline images) on createTurn and createDecision. Above it: **400 `schema_violation`** (`param: input`, `details.max_media_parts: 64`), checked before any file lookup (createTurn: while the body is read). createDecision also reads each distinct `file_id` once. Per R7 the contract declares it in the `input` descriptions of both requests (openapi `TurnCreateParams`, `decision.schema.json`; contracts owner, Wave E2). spec-04 §1, spec-06 §15, spec-07 §1.

### IMPL-065 (new): Derivative download
**Decision.**
- Content types per kind: `markdown` text/markdown; `text`/`ocr_text` text/plain; `raster`/`thumbnail`/`page_images` image/png or image/jpeg as re-encoded; `pdf_sanitized` application/pdf.
- `?page=` (1-based) is required for `page_images` and rejected for other kinds; a value outside `1..pages` → 400 `schema_violation` (spec-07 §6); `derivatives[].pages` gives the page count.
- `raster_1568` is internal: it is not an openapi `DerivativeKind` value, never listed in `FileObject.derivatives`, and a download request for it fails the request gate (400 `schema_violation`).
- `Content-Disposition: attachment; filename="<file_id>-<kind>[-<page>].<ext>"`, never the uploaded name; `X-Content-Type-Options: nosniff`.
- A missing kind → 404; pending → 409 `file_not_ready`; rejected → 409 `file_rejected`.

**Patch: contract.** **Material.** Y. Source: IMPL-CON-16.

### IMPL-066 (new): Machine-readable extensions
**Decision.** `x-noema-error-codes` on every operation and `x-noema-roles` on every authenticated operation; the security role extends the latter from the W4 operations to all operations for the authz matrix (IMPL-083). The conformance suite asserts:
- `set(ErrorCode)` = the openapi enum = the `ERROR_TABLE` keys;
- the statuses implied by each operation's codes equal its declared 4xx/5xx statuses;
- the routes registered in the app equal the routes in openapi;
- `x-noema-roles` equals the authz policy.

**Patch: contract.** Source: IMPL-CON-17.

### IMPL-067 (new): One combined rev-6 contract payload
**Decision.**
- The contract changes are applied as **one combined rev-6 payload** (critic 16/52):
  1. the verified base (spec-08 Appendix A + B);
  2. the consolidation addendum spec-08 §14, which supersedes the base where they overlap: CP-DEC-1/2 rebased, the CalibratorInfo alignment, the hierarchical and facade descriptions, the check_examples fixes, the config extension, the prose and example patches.
- The contract-patch applier:
  - builds the combined tree in a scratch copy;
  - regenerates the examples with `make_examples.py`;
  - runs `check_docs.sh`, the TS wire-compat check, the jsonschema-rs differential and the 41 audit cases plus the new ones;
  - regenerates the §1 hash table once;
  - publishes the result as `noema-rev6-combined.patch`.
- Rules from then on:
  - examples only via `make_examples.py`;
  - no Redocly warnings;
  - every new error code gets a row in 03 §1, `ERROR_TABLE` and `x-noema-error-codes`;
  - `check_examples.py` stays importable;
  - `check_docs.sh` gains the bundle step.

**Refs.** spec-08 §0–§1, §12, §14. **Patch: contract.** Source: IMPL-CON-18 + critic findings 16, 52.

### IMPL-068 (new): Which questions may escalate
**Decision.**
- `decision_profiles.<id>.escalation.questions` names **platform (config) questions**. It filters only questions whose `source = platform`.
- Request-defined questions (`/v1/decisions`, the facade, turn app questions) are **always eligible**, subject to the triggers, the budget and `K ≤ L(target)`.
- With the shipped config, `router-default` on `/v1/decisions` escalates any weak request question to `router-plus`, which makes `decision-result.valid-escalated.json` reachable (T-ESC-2 end to end). In turns, `intent` is the only platform question listed. Budget permitting it would escalate, but with the defaults it never does (IMPL-025).

**Rationale.** Config can't name request questions; a literal filter would make router-plus escalation (a 05 Phase-1 deliverable) dead on `/v1/decisions`.
**Refs.** 02:L420; 08:L96; yaml:L104-107; 05:L75; critic findings 12, 37.
**Patch: doc.** 08:L96: "`questions?` filters platform questions; request questions are always eligible". **Material.** Y (more escalations, so cost, on `/v1/decisions` with `router-default`). Source: lead architect.

### IMPL-069 (new): Turn `decisions.*` overrides
**Decision.**
- `profile = decisions.profile ?? routing.router_profile`. Allowlist = `{routing.router_profile} ∪ request_overrides.decision_profiles`; otherwise 403.
- **Questions and `commit_critical` always come from `routing.router_profile`'s config**: platform questions in config order, then the app questions. The override changes only members, templates, efforts, weights, aggregation, modality policy, deadline, quorum and the escalation chain.
- Platform question names stay reserved.
- `decisions.deadline_ms` and `decisions.min_quorum` are stage-0 overrides, validated as in spec-06 §2.3.
- `decisions.include` is accepted and ignored: turn decisions always include members.
- `L` is the overriding profile's, at request time. Platform questions are always flat: one above `L(override)` → 422 `unsupported_parameter`, `param: decisions.profile`, `details: {max_choices, question}` (amended 2026-10-10: it was `feature_not_available`, but the case is a profile mismatch, not a phase gate). An app question above `L` is answered hierarchically and is non-blocking unless `decisions.blocking` lists it (IMPL-027).
- Calibrator bindings use the answering profile, so router-default artifacts never apply to an override.

**Refs.** 03:L117; 08:L33; yaml:L12; openapi TurnCreateParams.decisions; critic finding 38.
**Patch: doc.** 03:L117 "default from tenant config" → "default `routing.router_profile`". **Material.** Y. Source: lead architect.

### IMPL-070 (new): DecisionRun lifecycle, protocol fields, snapshots
**Decision.**
- The run is an **async context manager**: `async with engine.run(ctx, req) as run:`. Exiting cancels in-flight calls and awaits the terminal state. `finalize()` records the usage lines, puts to L2 and, only when `ctx.turn_id` is None (`/v1/decisions`, the facade), inserts the row and offers to labeling, all under `anyio.CancelScope(shield=True)` with a 2 s timeout, so a client abort never loses the row or the usage lines. For turns the final snapshot carries `DecisionSnapshot.row`, which the orchestrator inserts in its finalize transaction and offers to labeling after that commits (IMPL-071). `decide(ctx, req)` = run plus `finalize()`. `GenerationEngine.run(...)` follows the same pattern.
- Fields added to the frozen protocols (critic 13):
  - `DecisionContext.reservation: Reservation`;
  - `DecisionContext.content_policy: Literal["full", "redacted", "metadata_only"]`;
  - `DecisionRequest.output_format: Literal["native", "jev", "openai"]`;
  - `DecisionContext.escalate_until: Deadline | None`, replacing the float.
- `QuestionCompiler.compile(..., select: Sequence[str] | None = None)` selects verification questions (critic 60).
- **Snapshots are plain frozen dataclasses** (`AnswerSummary` per answer for bindings and conditioning, plus a JSON-ready `result` dict serialized with orjson). The strict generated model is built only under `NOEMA_CONTRACT_VALIDATE=1` (critic 56).
- Exact text: spec-02 §5.

**Refs.** spec-06 §1.1, §10; 02-interfaces (the plan's proposal); critic findings 13, 40, 56, 60. **Patch: none.** Source: lead architect.

### IMPL-071 (new): Decision row contents and `ContentRef`
**Decision.**
- The `decision` row stores:
  - identity: tenant, project, id, `created_at`, **`principal_id`** (the caller; spec-09 §3 gives `end_user` only its own decisions on getDecision), `turn_id?`, `session_id?`, `purpose`, `origin`;
  - `profile_id`/`profile_version`, the signatures, `input_digest`;
  - `status`, `cache_hit`, `output_format`, `safety_identifier?`;
  - `result jsonb` (full native);
  - **`questions jsonb`** (the compiled wire dicts, in answer order);
  - `input jsonb?`, stored only when `ctx.content_policy != "metadata_only"` (`redacted` stores the redacted text);
  - `expires_at`.
- **Who inserts it**: `/v1/decisions` and the facade: the engine, in `finalize()` (`DecisionRepo.insert`; outbox `decision_row` on a DB failure). Turns (router and verification decisions): the engine doesn't insert; the final snapshot's `DecisionSnapshot.row` (iff `ctx.store`) is inserted by the orchestrator in `SessionRepo.finalize_turn` / `TurnRepo.insert` (spec-04 §10).
- `ContentRef(decision_id, turn_id)` is resolved by labeling under `uow(principal-of-tenant)`: decision row `questions` + `input`, plus for turns the turn's input parts and history up to its `seq`, plus file derivatives by id.
- Rows without stored input are not sampled for labeling.

**Refs.** 02:L402; spec-06 §16; critic finding 41. **Patch: none.** Source: lead architect.

### IMPL-072 (new): Calibrator scope, lifecycle and registry (ADR-003)
**Decision.**
- ADR-003 as amended:
  - one artifact per binding key, holding `member_T` + `pooled` + `epsilon`;
  - lookup precedence tenant → platform → none;
  - the acceptance gate A1–A8 with thresholds from `labeling.acceptance`.
- **Lifecycle**: `fitted → accepted | rejected`; `accepted → active` by **two-person activation**: `noema-admin calibrators approve <id>` by operator A, then `activate <id>` by operator B ≠ A, with `activated_by = [A, B]` (03:L517, L533). `active → downgraded` (drift) or `retired` (replaced, manual, profile version retired).
- **No per-tenant suppression in Phase 1** (critic 58): the drift job downgrades the artifact itself, tenant artifacts per tenant and platform artifacts globally.
- **Registry runtime** (critic 43):
  - each pod holds an in-memory snapshot of active artifacts;
  - `lookup(tenant_id, key)` and `epoch(tenant_id)` are **sync**;
  - the snapshot refreshes on the Redis pub/sub `RedisKeys.calibrators_changed` plus a 5 s poll: `SystemCalls.calibrator_epochs()` (`ops.calibrator_epochs()`, every `scope_key → n`), then `ops.active_calibrators(changed scope keys)` for the scopes whose `n` changed. Both are SECURITY DEFINER functions (IMPL-009), because a tenant-scoped RLS read can't load other tenants' artifacts into the shared per-pod snapshot. The swap is atomic; an error keeps the last snapshot;
  - the staleness bound is ≤ 5 s, replacing ADR-003's "immediately", and is tested with 2 API processes in e2e.
- DDL and RLS: IMPL-009, spec-03.

*Amendment (orchestrator ruling R5, 2026-10-10, Wave C2a; requests/labeling.md items 6 and 8):* two initial design values, tunable in Phase 0, are recorded in ADR-003: the binned-ECE **noise floor** (§3: with `min(15, n)` equal-mass bins a calibrated binding's worst ECE is 0.066 at 600 test labels, so `n_min = 300` / `ece_max = 0.05` reject most honest artifacts until Phase 0 retunes them or adopts a debiased ECE), and the drift floor **`DRIFT_MIN_N = 100`** kept uniform labels (§5: widen week by week up to 4 weeks, else no verdict and a WARNING), with the basic (bias-corrected) bootstrap lower bound `max(0, 2·ECE − q97.5)` for the CI rule.

**Refs.** ADR-003; 02:L364-403; 03:L441, L517, L533; 06:L51-88. **Patch: contract** (CalibratorInfo, IMPL-042) + doc (ADR-003 amended in place). **Material.** Y (R5 uncalibrated rule and D4 labels: governs when `calibrated` can be served). Source: ADR-003 + critic findings 11, 31, 42, 43, 58.

### IMPL-073 (new): Acceptance ECE per question type
**Decision.** Rule A1 uses the ECE that matches the question type, with 15 equal-mass bins:
- choice: top-label ECE;
- predicate: ECE on P(true);
- score: the mean ECE over the K−1 cumulative probabilities P(level ≤ k).

Synthetic test vectors per type are in ADR-003 §3.
**Refs.** 06:§2.2; ADR-003 §3; critic finding 59. **Patch: doc** (ADR-003). Source: lead architect.

### IMPL-074 (new): Inline media ingest and the decision clock
**Decision.**
- `FileService.ingest_inline(principal, data_b64, media_type, *, deadline) -> FileRef`.
  1. Synchronous arithmetic size check (IMPL-037).
  2. Streaming sha256 and quarantine put.
  3. A **high-priority** job (`JobQueue` with LISTEN/NOTIFY wakeup) runs `detect` and `image.normalize` in the sandbox. The API never decodes media (ADR-002).
  4. It waits **only for the re-encoded `raster`**, bounded by `min(deadline, now + platform.media.inline_wait_ms)`. A `raster` failure or a dimension over 8,000 px → 413 `image_too_large` or 409 `file_rejected`, before any model call.
- `ocr_text` and `thumbnail` continue in the background. A member whose view isn't ready gets `metadata_only` and is skipped (IMPL-058); the view signature records it.
- **The stage-0 decision clock `t_d0` starts after the stage-0 views are resolved**, and the ingest wait counts only against `ctx.deadline`.
- Uploaded files must be `ready` or `partial` (409 otherwise, unless `wait_for_files`).
- **Where the errors surface**:
  - at ingress (HTTP status, before `turn.created`): the arithmetic size check (413 `image_too_large`) and the non-waiting status check of uploaded `file_id` parts (409 `file_not_ready` / `file_rejected`);
  - in turns, the waits run in **preflight, after `turn.created`**: the inline-image `raster` wait and the `wait_for_files` wait. Their failures are SSE `error{image_too_large | file_rejected | file_not_ready | deadline_exceeded}` followed by `turn.completed`; HTTP statuses only with `stream: false` (spec-04 §9);
  - `/v1/decisions` and the facade have no SSE: the same failures are their HTTP statuses.
- Media-pipeline owns the latency budget; it is measured in the C2 perf smoke.

**Refs.** 02:L134, L736, L760; 03:L116; ADR-002 §1; critic findings 27, 45. **Patch: doc** (02:L734-740 pipeline note). Source: lead architect (IMPL-SEC-N8 intent).

### IMPL-075 (new): Decision view budget
**Decision.** `InputViewResolver.resolve(..., budget: DecisionViewBudget)` with the code constant `VIEW_BUDGET_V1 = DecisionViewBudget(max_text_tokens=6000, max_images=20, max_documents=5, image_long_edge=1568)`.

Cut rule:
- Markdown and OCR text are truncated to the first N tokens at a page boundary, with a final label line `[doc-1: pages 1–k of n]`;
- images are kept current-turn first;
- every cut is recorded in the member's view metadata, and in turns as a `media_budget` degradation.

The budget is a **code constant versioned with the resolver**, not config: changing it changes the calibrated pipeline and requires new profile versions.
*Amendment (orchestrator ruling R3, 2026-10-10, Wave C2a; requests/media-negotiation.md item 2):* spec-07 §5 wins over the frozen conformance suite: a budget-cut image in a decision view falls back to its `ocr_text` when that is ready and non-empty, otherwise it is omitted (`metadata_only`, no blocks). `test_input_view_resolver.py::test_budget_caps_images` and `noema.media.views` follow it. The versioned view rules (spec-07 §5) also record the C1 review fixes: the written cut order, the binary payload guard (`max_payload_bytes − text_reserve_bytes`), adequacy of fully cut modalities, oversized rasters, and page images within the plan's per-page reservation (spec-07 §4.5).

**Rationale.** It bounds the 1 s deadline, the TPM reservation and the Bedrock limits on the decision path.
**Refs.** 02:L229, L792-806; 06 §11 multimodal gate; critic finding 46. **Patch: none.** Source: lead architect.

### IMPL-076 (new): Error taxonomy and credential flow
**Decision.**
- One core enum `ErrorKind = retryable | throttled | fatal | invalid_request | tenant_access`, with `cause` (the ADR-001 causes) on `tenant_access`.
- The frozen mapping (spec-02 §4.3):

| ErrorKind | `MemberVote.error.code` | Request-level `ErrorCode` when it surfaces |
|---|---|---|
| `throttled` | `throttled` | after fallbacks are exhausted: 503 `all_routes_unavailable` |
| `retryable` | `upstream_error` | 503 `all_routes_unavailable` |
| `fatal` | `upstream_error` | 500 `internal_error` |
| `invalid_request` | `invalid_request` | 500 `internal_error` (our request was wrong) |
| `tenant_access` | `tenant_access_misconfigured` | 503 `tenant_access_misconfigured` (decision: IMPL-057) |

  Decision-only member codes: `circuit_open`, `model_not_allowed`, the parse codes, `reasoning_only`, `empty_output`.
- **Adapters fetch credentials** (ADR-001 §4): `Adapter.complete(req, *, tenant: TenantRef, deadline)` calls `broker.get` (a memory lookup) and `assert_creds_for` on every call. The engine calls `broker.get` once in `start()` only to fail fast.

**Refs.** 02-interfaces; ADR-001 §2, §4; spec-06 §6.2; critic findings 21, 33. **Patch: none.** Source: lead architect.

*Amendment (orchestrator ruling R7, 2026-10-11, Wave E2; perf sprint A §3.5, io-fixer finding 11):* a **2xx Converse body that is not a valid Converse response** (empty, not JSON, not an object, or a shape botocore's parser cannot take) is `retryable` with provider code `malformed:response`, as on Mantle. It is neither an empty `end_turn` answer (a `stream: false` turn would commit empty text) nor §4.3's catch-all `fatal`. Likewise, a ConverseStream payload botocore cannot parse, or a non-string `text` or reasoning `text`, is `malformed:chunk` (spec-10 F8), never a `TextDelta`. spec-02 §4.3.

### IMPL-077 (new): Idempotency store semantics
**Decision.**
- `begin(principal, method, route, key, fingerprint, *, resource_id) -> New(token) | InFlight(token, resource_id) | Replay(StoredResponse) | Conflict` (exact text: spec-02 §7). `resource_id` is the `turn_`/`dec_` id pre-generated for the request (spec-04 §1 step 7); an `InFlight` duplicate uses the original's `resource_id` to attach to its SSE stream.
- `await_result(token, *, deadline) -> StoredResponse`: Redis pub/sub `RedisKeys.idem_done(...)` plus a 250 ms Postgres poll fallback. If the duplicate's own deadline passes → 504 `deadline_exceeded`. If the original's lease lapses or the record turns `not_stored` while waiting → 500 `internal_error` (retryable; the client's retry then takes over as `New`).
- `heartbeat(token) -> bool` renews the lease every 10 s; `False` = the lease was lost (logged; the running request carries on).
- `complete(token, resp | None)`: `None` = not stored.
- Records live in Postgres (`idempotency_record`, TTL 24 h). The scope is `(project_id, method, route template)`. The fingerprint is sha256 of the canonical body (sorted keys) plus `Aghoo-Version`.
- In-flight records hold a 30 s lease renewed by heartbeat. A lapsed lease (pod death), a `not_stored` record and an **expired record** (`expires_at < now()`, which counts as absent) are taken over as `New` by a **conditional UPDATE** that re-checks the condition it read; 0 rows → re-read and re-classify, so exactly one racing taker wins (spec-11 §5).
- **A response is stored iff any model call was dispatched for the request** (2xx, or e.g. 503 with `details.decision_id`). Errors before any model call (4xx, 429, credential failures) are not stored, so a retry re-executes.
- **Body size (amended 2026-10-10, IMPL-085).** A body ≤ 1 MiB is stored inline (`response_body`). A larger one goes to object storage under a tenant-scoped key with the record's TTL (`response_body_key`). Size never makes a record `not_stored`, so a replay never re-executes or re-bills because of size.
- Streamed turns store `{kind: sse, turn_id}`; the replay uses the event-log mirror, else 410 (IMPL-021).
- Conformance test across 2 processes.

**Rationale.** It honors "no new billing" on replays (03:L38) and keeps pre-call failures retryable.
**Refs.** 03:L36-40; spec-02 §7; spec-11 §5; critic finding 50; IMPL-085 (A2 challenge 1).
**Patch: doc.** 03:L39-40 → "Responses are stored once any model call was dispatched; failures before that (including connect errors and 4xx) are not stored, so a retry executes". **Material.** Y. Source: lead architect.

*Amendment (orchestrator ruling R7, 2026-10-11, Wave E2; edge-fixer 2, 10, 14):*
- **Stored iff dispatched and the content may be kept.** A JSON response is stored only if a model call was dispatched **and** the request may keep its content. With `store: false`, or a data class whose `store_content` is not `full`, the record is completed `not_stored`: a retry re-executes, and is billed again. The record never keeps content that the turn or decision itself may not keep.
- **Streamed turns.** SSE records hold only `{sse, turn_id}` and are unaffected. A keyed `stream: true` createTurn counts as dispatched once its turn started (the turn outlives the request), so a retry after a dropped connection attaches to it instead of running a second turn.
- **Principal-bound fingerprint.** The store compares `sha256(fingerprint + "\n" + principal_id)`. Another principal of the project reusing a key with the same body gets 409 `idempotency_key_reused`, never a replay or an attach of someone else's response.
- Keys stay project-scoped (the scope above). So a principal can still squat another principal's predictable key for its TTL: a per-principal scope would change `scope_hash` and is not adopted.
- spec-04 §1 step 7, spec-11 §5, spec-09 §7.

### IMPL-078 (new): Hot-path calls are sync and in-memory
**Decision.**
- These are sync and never touch the network:
  - `HealthRegistry.admit/record/state`: breaker state mirrored via Redis pub/sub; a local window; Redis-down → local only;
  - `RetryBudget.try_spend`: a local token bucket, coarse-synced to Redis every 1 s;
  - `Reservation.consume`: pre-counted at `reserve()`;
  - `Reservation.headers`;
  - `CalibratorRegistry.lookup/epoch`;
  - `TokenEstimator.estimate`;
  - `CostMeter.price`, and `record` (it enqueues).
- Only `Admission.reserve`, `Reservation.reconcile/release` and the Redis-backed stores are async.
- Note (not on the list above, so the hot-path list is unchanged): `MediaNegotiator.negotiate` is **async**, because it reads derivative bytes through `FileService.read_derivative` (spec-02 §6); the PLAN listed it as sync. `MediaBudgetPlanner.plan` stays sync.
- Redis down: admission uses a per-pod local GCRA bucket at `1/pods` of each limit (06:L189).

*Amendment (orchestrator ruling R4, 2026-10-10, Wave C2a; requests/ops-admission.md item 5):* when Redis returns, the local admission state is not merged; it is reused if Redis fails again within τ (60 s), so a flapping Redis cannot hand each pod a fresh local burst per flap. Labeling's 20% headroom also applies to bulkheads (at most `floor(0.8 · cap)` live labeling places), and labeling never waits in the fair queue (`queue_timeout` at once). spec-11 §1.2 is reworded accordingly.

*Amendment (Wave E fixer rulings, recorded in Wave E2, 2026-10-11; qa-lead QL-2, io-fixer finding 10 / e4-engines-fix 10):*
- **Quota burst.** A model-quota bucket (`mdl:{tenant}:{alias}:rpm|tpm`) never admits more than its limit in any 60 s window. Before this, the GCRA with τ = 60 s admitted a full minute of burst on top of the rate (C1: 915 Sol calls in the first minute against `rpm` 600), so the Bedrock quota (the mock's, with one STS session) throttled before admission did. `mdl:` buckets are therefore sliding windows of 1 s slices (a debit of n passes iff the units of the last 62 slices plus n ≤ the limit), not GCRA; `key:`, `prj:` and `authfail:` buckets keep the GCRA. After the change, C1's busiest 60 s window held 546 Sol calls and the mock throttled none (`reports/qa/e2/admission-estimator.md` §1). Exact rule: spec-11 §1.2.
- **p95-TTFT breaker rule.** A service breaker (per Bedrock model id and region, shared by every tenant) opens on TTFT (04:L137) only if both hold:
  - the window's p95 TTFT exceeds `ttft_factor` (3) × the baseline;
  - the samples above that threshold came from at least `BreakerPolicy.ttft_min_accounts` (default **2**) distinct tenant accounts.

  So one tenant's slow calls never open a breaker that every tenant shares. A breaching window never feeds the baseline.
- **TTFT samples.** Only generation attempts give TTFT samples. Decision members are unary calls and record their outcome with no TTFT. A generation attempt gives none when its prompt estimate exceeds `TTFT_SIGNAL_MAX_INPUT_TOKENS` (8,192), or when it reasoned before its first text.
- The failure-ratio rule is unchanged. `NOEMA_BREAKER_TTFT_RULE=off` (dev/test only, spec-05 §5) disarms the TTFT rule for stacks on the wire mock.
- **Text-less stops** (Wave E2 fault B1, resilience fixer; recorded in Wave E3): a generation attempt that stopped with `reasoning_only` or `empty_text` records `error` for its service breaker, so a model that keeps answering without text is skipped by later turns (F7). Exception: a reasoning-only stop under a `max_tokens` the request lowered (`Attempt.client_capped`) is tenant-shaped and records nothing (like the TTFT exclusions). Code: `generation/run.py` `_record_health`.
- **Pub/sub liveness** (Wave E3): the long-lived listeners of `turn_cancel` and `session_released`, `breaker_events`, `calibrators_changed` and `config_changed` read through `noema.core.pubsub.heartbeat_messages` (the loop `keys_revoked` has had since Wave E, `identity/revocation.py`; the shared idempotency and file-event connections ping on their own): a PING after 5 s without traffic on the subscribed connection, and `ConnectionError` (close, resubscribe) after 15 s without any message or PONG. A connection that died without FIN/RST therefore stops a listener for at most ~15 s, not until the TCP keepalives notice it. The time a listener spends on a message is not silence.

**Refs.** 06:L189; 04:L134-137; critic finding 57. **Patch: none.** Source: lead architect.

### IMPL-079 (new): Heuristic confidence auxiliary fields
**Decision.** Follow the types.ts comments exactly (critic 19):
- `normalized_entropy = −Σ_b p_b ln p_b / ln n` over the same n buckets as `normalized_max`: the candidates, plus the unresolved bucket when the emitted `u > 0` (amended 2026-10-10 for the hierarchical method; with `u = 0` it is `ln K` over the K candidates, as before). The hierarchical example gives 0.7489 (17 buckets, spec-06 §19.11 T-HIER-1);
- `agreement` = the fraction of `ok` members whose argmax equals `candidates[0].value`, for every answer type;
- `member_divergence` = the mean base-2 Jensen–Shannon divergence between each `ok` member's (calibrated if an artifact applied) distribution plus its `unresolved_mass` bucket, and the **linear pool** (`raw_score` plus the pool's `u`); null if only 1 member answered;
- `margin = p(top1) − p(top2)` over the emitted candidates.

T-POOL-1 values are unchanged (u = 0). The types.ts comments are clarified to "member → linear pool" and, for `normalized_entropy`, "H/ln(n) over the candidates plus the unresolved bucket when unresolved_mass > 0" (spec-08 §14.7).
**Refs.** types:L180-198; spec-06 §11. **Patch: contract comment** (types `HeuristicConfidence`). Source: lead architect.

### IMPL-080 (new): Test strategy, wire mock, gate map, dependencies
**Decision.** spec-10 is the QA design:
- the wire mock, by qa-tools:
  - Converse JSON **and** ConverseStream eventstream;
  - Mantle chat `stream: false` with `logprobs`/`top_logprobs` (split-token, whitespace-variant and censored-tail scenarios), plus Mantle SSE;
  - refusal variants; malformed and truncated bodies;
  - per-model latency distributions (Haiku tail > 1 s, DeepSeek 4–12 s outliers);
  - barrier scripting;
- the **scenario DSL** (spec-10 §2.4, `tests/fakes/scenario.py`), one loader shared by the fakes under `FakeClock` and `tools/mockbedrock`; the golden SSE fixtures embed it (spec-04 §4.2);
- the map from each 06 §11 gate to a test, an owner and fixtures;
- the fault and load tooling;
- the **live smoke** (spec-10 §6): expected 48 calls, worst case 57 ≤ 60. The pinned e2e comparison runs under a `smoke-router` config (1 commit-critical question, 1 member), with `labeling.sample` rates 0 and escalation off; the API runs in-process so `CountingAdapterRegistry(limit=60)` sees every call and aborts at the 61st;
- the decision-subset additions: a two-tenant L2 isolation test, a golden "no filename in any member request" test, the K = 3 censored Jev/facade test, the K = 16 Jev test, and the non-pattern facade name test.

B1 pre-adds `openai` (PyPI and npm), `schemathesis`, `pytest-socket`, `hypothesis`, `respx`, `moto[server]` and `pytest-timeout`. The openai clients use `base_url=http://127.0.0.1:<port>/openai/v1`. The CI `api.openai.com` grep is scoped to `src/`, `tests/`, `sdk/` and `web/`, because the openai SDK embeds that URL.
**Refs.** 06 §11; PLAN Wave A "Test strategy"; critic findings 48, 54. **Patch: none.** Source: lead architect.

*Amendment (Wave E2 rulings, recorded in Wave E3, 2026-10-12; supply-chain-infra defect 29, QA lead H1/H2):*
- **SE13 waivers** live in `deploy/docker/.trivyignore.yaml` (infra), not `reports/security/waivers.md`: one entry per CVE id with a statement and a review date (`expired_at`, 2027-01-09 for the Wave E2 set). `make image-scan` passes it to trivy (`--ignorefile`; an expired entry no longer waives, so the gate fails until the entry is reviewed) and `make qa-security-sca` passes it to `tests.security.sca_gate`. spec-09 §9.
- **F12 per failing upstream** (H2, made the gate for both rows by R8; IMPL-016 amendment): requests to model X at the mock ÷ the turns that started an attempt on X, ≤ 1.1.

*Amendment (owner decision, 2026-10-12, Wave E3):* one more performance sprint, then the pods are sized from the **measured knee** (the highest streams per 2-vCPU pod at which L1's p50 ≤ 10 ms and p95 ≤ 25 ms both hold, 08 §9), with the SLO reported at that density. The live-smoke hard stop rises from 60 to **87** ledger entries in total (+30 calls for re-verification: the lean ConverseStream path, a `:` model id through SigV4, the CR #26 verification quorum, the GLM race at low, L6 pairs and K1 token records). spec-10 §6.

### IMPL-081 (new): Media sandbox (ADR-002)
**Decision.** Adopt ADR-002:
- a networked `media-worker` (no parser libraries) and a network-less `media-sandbox`, joined by a UDS on a shared tmpfs spool;
- one nsjail per job with the kafel deny-list;
- per-kind limits;
- strict manifest validation by trusted code;
- backends `RemoteSandboxRunner`/`NsjailRunner` (staging and prod), `BwrapRunner` (dev), `InprocRlimitRunner` (unit tests only), each refusing a disallowed environment;
- the **Debian 12 media image** `noema-media` (ADR-002 §8) carries nsjail, tesseract 5, libmagic and qpdf; the host has none of them, so every test that needs them runs in that image;
- **nh3 is not a Phase-1 dependency**: Markdown is re-serialized by the spec-09 §6.5 renderer;
- `ocr` input: `raster` for images, or `pdf_sanitized` with `params.pages` = the pages without a text layer (ascending);
- `markdown` and PDF `ocr_text` hold one `\f`-separated segment per page (ADR-002 §5, spec-07 §1);
- sandboxd's ownership walk of `out/` never follows a link: `os.scandir(follow_symlinks=False)`, deletion of every non-regular or multiply-linked entry, then `O_NOFOLLOW` opens with `fchown`/`fchmod` (ADR-002 §6).

**Refs.** ADR-002; 02:L36-37, L134, L732-754; 04:L193. **Patch: doc** (08 §12 ADR status). Source: ADR-002 (IMPL-SEC-N7).

### IMPL-082 (new): Conditioning placement (ADR-004)
**Decision.** Adopt ADR-004:
- the block is the last content block of the final user message;
- the static preamble declares it on every turn;
- the block is never stored in history;
- option values are rendered as IMPL-029a says: `json.dumps(clean_text(v)[:64])` plus the `&`/`<`/`>` JSON-escape replacement (spec-04 §5.2);
- decision members never see it;
- switching to system placement is a versioned release (`decision-conditioning@2` plus a `policy_version` bump) after Phase 0 task 0.6.

**Refs.** ADR-004; 02:L573; 08:L213. **Patch: none.** Source: ADR-004.

### IMPL-083 (new): AuthZ roles, scopes and policy
**Decision.**
- Roles `tenant_admin`, `developer`, `reviewer`, `end_user`, `auditor` (04:L200).
- Key scopes: `turns`, `decisions`, `sessions`, `files.read`, `files.write`, `usage.read`, `admin.read`.
- The operation × role × ownership matrix is spec-09 §3, enforced in one `Authorizer.check(principal, action, resource)`; RLS gives defense in depth.
- A role or scope failure (checked before any lookup) → 403 `permission_denied` **only on operations whose rev-6 contract declares 403** (spec-08 §3). The operations without a declared 403 answer as follows (spec-09 §3): single-resource reads and cancel (getTurn, cancelTurn, resumeTurnEvents, getDecision, getSession, listSessionTurns, getFile, watchFile, getFileDerivative) → their 404 code (`not_found`, or `session_not_found` for session ids); list reads (listSessions) → an empty page; listProfiles needs no scope and lists only the caller's allowlist.
- A resource the caller **cannot see** (another tenant, or outside the spec-09 §3 visibility) → 404, never 403, so existence doesn't leak. A resource the caller **can see** but may not act on → 403: tenant_admin and a session in another project → `session_forbidden` on createTurn, `permission_denied` on createDecision and uploadFile; deleteFile by a non-uploader → `permission_denied`. cancelTurn declares no 403, so a visible turn the caller didn't create → 404 `not_found`.
- `Authorizer` (spec-02 §11): `check(principal, action, resource)` raises `permission_denied`, `not_found`, `session_not_found` or `session_forbidden`; list operations call `list_scope(principal, action)` and filter (`none` → empty page).
- `x-noema-roles` on every operation is generated from the same table, and the authz matrix test enumerates it.

**Refs.** 04:L190, L200; 03:L51-52; spec-08 §3; spec-09 §3. **Patch: none.** Source: lead architect; 403/404 rule amended in Wave A2.

### IMPL-084 (new): Generation model limits: context window, default `max_tokens`, per-attempt stream timeouts
**Decision.**
- **`models.<alias>.context_window`**: an optional integer (≥ 1,024) in the rev-6 config schema (spec-05 §1.5, spec-08 §14.4). Absent → `DEFAULT_CONTEXT_WINDOW = 128_000`, a code constant in `config/models.py` (UNVERIFIED; operators set the real value per model). Exposed as `ModelCapabilities.context_window`. Used by:
  - the capability-fit hard filter (04:L32): a member fits iff `est_input_tokens + max_tokens(attempt) ≤ context_window`; a profile with no fitting member is filtered like a non-allowlisted one, and a non-fitting member is skipped at attempt start like a breaker-open one (spec-04 §5.1, §4.1 T2);
  - the history budget (IMPL-045) and the preflight derived-text allowance `MediaCaps.max_text_tokens` (spec-04 §9).
- **`GEN_DEFAULT_MAX_TOKENS = 4096`** (code constant): `ModelRequest.max_tokens = min(cap, (spec.max_output_tokens ?? member.max_output_tokens ?? cap ?? 4096) + reasoning_reserve_tokens)`, with `cap = models.<alias>.max_output_tokens_cap` (no cap = no `min`). Sol and Opus have no cap, so without this constant they had no value. Admission reserves the same formula's worst case (spec-11 §1.1).
- **Stream timeouts are per attempt**, keyed by (model alias, effective effort) instead of the profile id: `ConfigSnapshot.route_timeouts(model_alias, effort)` over the code table `ROUTE_TIMEOUTS_V1` (spec-05 §1.4). The Sol member of `opus-deep` (effort high) gets the permissive row, not `sol-fast`'s 8 s first-answer timeout, and each `race-sol-glm` member gets its own model's row.

**Rationale.** 04:L32 and IMPL-045 need a context window that no config key carried; the 04:L127-132 rows describe a model at an effort, so a profile-keyed table mis-times fallback and race members.
**Refs.** 04:L32, 04:L127-132; 08:L48-49; cschema `Model`; IMPL-038, IMPL-045. **Patch: contract** (cschema `Model.context_window`, spec-08 §14.4). **Material.** Y (a new optional config key; the example yaml is unchanged). Source: Wave A2 review.

### IMPL-085 (new): Idempotent responses over 1 MiB go to object storage
**Decision** (orchestrator engineering call (a), 2026-10-10; resolves Wave A2 challenge 1).
- `IdempotencyStore.complete(token, resp)` stores a body ≤ `IDEM_INLINE_MAX_BYTES` (1 MiB, a code constant) inline in `idempotency_record.response_body`, as before. A larger body goes to object storage:
  - first `PutObject s3://noema-<env>/<tenant_id>/idempotency/<record_id>`, encrypted with SSE-KMS under the tenant key (as derivatives, spec-09 §6.7);
  - then the conditional record UPDATE (`WHERE owner = :me`) sets `state = completed`, `response_body = NULL` and `response_body_key = <tenant_id>/idempotency/<record_id>`.
  - Object first, then the row, so a `completed` record never points at a missing object.
- **Same TTL.** The object lives exactly as long as its record (`expires_at`, `platform.idempotency.ttl_s`):
  - the retention sweeper deletes the object with the row (a `DeleteObject` for every swept record id; a no-op when absent);
  - a take-over clears `response_body_key`, and the new owner's `complete` overwrites the same key;
  - tenant deletion purges the `<tenant_id>/` prefix.
- **Replay.** `begin` reads the record under RLS. For a `completed` record with a key, it `GetObject`s the body and returns `Replay(StoredResponse)`, which the client can't tell from an inline replay (`x-aghoo-idempotent-replay: true`).
  - A GET failure → 500 `internal_error` (retryable). The record stays `completed`, so the client's retry replays; it doesn't re-execute.
  - `await_result` reads the body the same way.
- **Put failure.** One retry, inside the 2 s shielded `complete`. Then the record becomes `not_stored`, with `aghoo_idempotency_store_errors_total` and an ERROR log.
  - This infrastructure failure is the only case where a request whose model calls were dispatched can re-execute. Losing the Postgres write in `complete` already has the same effect.
  - Size alone never causes it.
- DDL: spec-03 §2 (`response_body_key`, at most one of the body and the key). Store: spec-11 §5. Security: spec-09 §7.

**Rationale.** Replays must never bill again, whatever the size (03:L38). Multi-MiB `bytea` rows would bloat the hot idempotency table. Object storage with per-tenant keys and tenant KMS keys already exists for media.
**Refs.** OWNER-CHECKPOINT Wave A2 challenge 1; IMPL-077; IMPL-064 (the 50 MiB request cap is unrelated: it bounds requests); spec-03 §2, §5; spec-11 §5; spec-09 §7.
**Patch: none** (internal; the IMPL-077 wording for 03:L39-40 still holds). **Material.** Y: no re-billing on large replays, and response content is now stored in S3 under the tenant's key. Source: orchestrator engineering call, recorded by the lead architect.

### IMPL-086 (new): Spotlighting levels (supersedes IMPL-029b)
**Decision** (orchestrator engineering call (b), 2026-10-10; resolves Wave A2 challenge 2; `injection_suspected` stays non-blocking, owner answer 4).
- `datamark(s) = re.sub(r"\s+", "ˆ", s)` is unchanged: every whitespace run becomes exactly one U+02C6 (spec-09 §5).
- **`standard`** (every prompt) always datamarks **document- and OCR-derived text blocks**:
  - generation: every `<untrusted_input>` body (document Markdown, OCR text of images and scanned pages, past-media text derivatives) is `datamark(esc(text))`; the tags themselves are not datamarked;
  - decision: every text block of a media view part (spec-06 §5.3) is `datamark(esc(text))`.
  - User text, history text, JSON input and verification input get `esc` only.
- **`strict`** additionally datamarks **all other untrusted text** in generation prompts: the current user text parts and the history user and assistant text (`datamark(esc(text))`). The system role, the request `instructions` and the conditioning block are never datamarked.
- **Where each level applies.**
  - Decision prompts (router, `/v1/decisions`, facade, verification) are always `standard`. A per-turn level would change calibrated prompt bytes.
  - `strict` applies to generation attempts that start after the triggering answer is final (IMPL-002), as before. Nothing carries over to later turns.
- **Prompt text.**
  - `PREAMBLE_V1` explains that `<untrusted_input>` text is datamarked, and that user and earlier conversation text may be (spec-04 §9).
  - `logprob-codes@2` and `verbalized-dist@1` gain the system line "In document and image text, every run of whitespace is replaced by ˆ." (spec-06 §5).
  - No version bump: no calibrator artifact exists yet (IMPL-053).
- **Caching.** `standard` output is a pure function of the content, so history prefixes stay byte-stable across turns. A `strict` attempt rewrites the history bytes (a prefix miss for that attempt only; Phase 1 has no `cachePoint`, IMPL-017).

**Rationale.** Under IMPL-029b, the first attempt, which commits in most turns, never got datamarking. Making the highest-risk carriers (documents and OCR) always datamarked protects the committed answer without making `injection_suspected` blocking. It also matches 04:L253 ("delimited, datamarked blocks"). The cost is some answer-quality risk on documents, which Phase 0 measures.
**Refs.** OWNER-CHECKPOINT Wave A2 challenge 2 and §c item 7; IMPL-029b; IMPL-002; 04:L253-256; spec-04 §9; spec-06 §5; spec-09 §5.
**Patch: none** (04:L253 already describes datamarked blocks). **Material.** Y: the prompt bytes and the injection posture change for every document turn. Source: orchestrator engineering call, recorded by the lead architect.

### IMPL-087 (new): API process sizing and overload
**Decision** (Wave E perf sprint B and edge fixer, recorded in Wave E2, 2026-10-11; `reports/perf/e-sprint-b.md` §4–6, edge-fixer 11, 19, 25).
- **Two api processes per 2-vCPU pod.** The api runs uvicorn `--workers` = `WEB_CONCURRENCY` (2 per pod; allowed 1–64, default 1).
  - One process is one event-loop thread. At the L1 workload it is CPU-bound from about 110–150 streams, so 08:L488's ~200 streams per pod means 2 × 100.
  - Each worker is its own pod for leases, owners and consumer names (`pod_id` = `<NOEMA_POD_ID or host name>-<pid>`).
  - The Redis-down local divisor is `NOEMA_EXPECTED_PODS × WEB_CONCURRENCY`.
  - Worker *i* serves `/metrics` on `NOEMA_METRICS_PORT + i` (one scrape target per worker).
- **Per-process cap.** `NOEMA_MAX_TURNS_PER_PROCESS` (default 120; 0 = off). A running turn (from createTurn's check to its end), and a createDecision or facade request for its whole duration, count against it.
  - Above the cap: **429 `queue_timeout`** (`rate_limit_error`, retryable) with `retry-after: 1`, counted in `aghoo_turns_shed_total`.
  - The check runs after authentication and the request gate (their 401/400s keep precedence) and before any Postgres, Redis or model work.
  - The code is the one spec-11 §1.2 gives per-pod capacity exhaustion (stream slots, the fair queue), already declared on these operations. A 503 would claim an upstream failure.
- **Tenant-fair slots.** A tenant's share is the cap ÷ the tenants holding slots. A tenant below its share is admitted up to cap + cap ÷ 10, so one tenant alone still gets the whole cap but never keeps a newcomer out.
- **Keyed requests at the cap.** They reach step 7 first: a duplicate attaches or replays without a slot. A new keyed request is shed there, and its record is not stored.
- **Measured.** Without the cap, one process past its knee answered 1,672 × 500 in 150 s (pool timeouts). With it: 0 × 500 at unchanged goodput.
- **GC.** Outside test, `composition.start` ends with `gc.collect(); gc.freeze()`, then sets `gc.set_threshold(700, 10, 100)` (`GC_THRESHOLDS`) unless the thresholds were already changed. Wave E2 shipped `(700, 10, 40)`: at 100 streams per process a full collection every ~5.3 s instead of ~1.45 s, TTFT overhead p95 124 → 72 ms. Wave E3 (perf-gc-objects item 10) raised the third threshold to 100: at 200 streams per pod a full collection every ~21 s instead of 8.6 s at about the same pause, TTFT overhead p95 25.8 → 23.7 ms, RSS +0.5% (`reports/qa/e3/perf-gc-objects.md` §2). QL-5 is closed: at the E3 sizing (75 streams per pod, E3 image) a full collection took 20.1 ms p50 / 22.6 ms max (`reports/qa/e3/qa-lead.md` §3.1); on the Wave F image at its 125-stream knee 31–38 ms p50 (51 ms max) every 30–38 s (`reports/perf/sizing.md` §2.1 and §7 item 7).

*Amendment (Wave E3, 2026-10-12, recorded in Wave F; `reports/qa/e3/perf-cpu-shed.md` §4, `src/noema/api/shed.py`):*
- **The cap is checked twice.** First at the edge, before the middleware stack (`noema.api.shed`, ~10–17 µs per refusal), for an unkeyed createTurn, createDecision or facade `POST` whose key id the in-process key cache holds; that tenant is not verified there. Then authoritatively in the route, after authentication and the request gate, as above.
  - A key id the cache does not hold always gets the full ingress, so its 401 keeps precedence. While the process is over the cap, a wrong secret for a cached key id gets 429, not 401, and the gate's 400/413/415 are not reached.
  - A keyed request is never shed at the edge.
  - Edge refusals get no access line: one `edge shed` summary line per second at most. `aghoo_turns_shed_total` counts every refusal.
- **Max-min fair share** replaces `cap ÷ tenants`. A tenant whose turn timed out in the admission queue is saturated for 2 s (`SATURATED_S`). While saturated, it holds at most `cap ÷ 10` waiting turns per process, which do not count against other tenants. Measured: C5 victim p95 ratio 1.021 / 1.022, where Wave E2 had 1.226 / 1.199 (`reports/qa/wave-e-summary.md` C5).
- **Sizing** (owner decision 2026-10-12): the pods are sized from the measured L1 knee, not from 08:L488's ~200 streams per pod. The knee of the shipped Wave F image in the container is 125 streams per 2-vCPU pod with 2 processes, planned at 100: 8 pods for ~800 streams (`reports/perf/sizing.md` §5; IMPL-089; the E3 image's knee was 75, planned at 60, 14 pods). The 120 cap is an overload guard, 1.9× the knee, not the SLO.

**Rationale.** One process per 2-vCPU pod left a vCPU idle and collapsed past ~150 streams. A typed, retryable shed before the pools time out keeps goodput and gives clients a retry hint.
**Refs.** 08:L488; spec-11 §1.2, §7; spec-05 §5; `reports/perf/e-sprint-b.md`; `reports/qa/qa-lead.md` §3.1–3.2.
**Patch: doc**, applied in design revision 7 (Wave F): 08 §9's `api` row now carries the measured-knee sizing of `reports/perf/sizing.md` §5, which supersedes the Wave E2 text "4 × (2 vCPU, 4 GiB), 2 api processes each (`WEB_CONCURRENCY=2`), ~200 concurrent streams per pod (2 × 100)". 08 §14 carries the overload and GC notes. **Material.** Y: a new 429 condition under overload, and the deployment shape changes. Source: perf sprint B and the edge fixer; recorded by the spec fixer.

### IMPL-088 (new): Datastore connections: one bounded Redis pool, pool and latency metrics
**Decision** (Wave E3, 2026-10-12; C2b perf smoke R4, edge-fixer CR item 3b; `reports/qa/e3/misc-fixes.md`).
- **One bounded Redis pool per process.** The process's Redis client (every module shares it, pub/sub included) uses a `redis.asyncio.BlockingConnectionPool` of `NOEMA_REDIS_MAX_CONNECTIONS` connections (default 256, 8–10,000). Past the bound a caller waits up to `REDIS_POOL_TIMEOUT_S` = 0.5 s for a released connection, then gets the `ConnectionError` of an unreachable Redis, which every caller already handles fail-soft. Before, the pool was unbounded, and an overload opened connections until Valkey or the descriptors ran out. A free connection is taken without the blocking pool's condition and timer, which cost ~9 µs of CPU per command. The bound must exceed the long-lived holders: up to 7 listener and shared pub/sub connections plus `MAX_FOLLOWERS` (128) mirror readers per process.
- **Metrics** (registered in `telemetry/registry.py`, observed in composition; spec-10 C2's pool saturation and datastore latency):
  - gauges, sampled every second: `aghoo_db_pool_size{pool}` / `aghoo_redis_pool_size` (the pool's maximum) and `aghoo_db_pool_in_use{pool}` / `aghoo_redis_pool_in_use` (connections checked out, pub/sub ones included);
  - histograms: `aghoo_db_pool_acquire_seconds{pool}` and `aghoo_redis_pool_acquire_seconds` (the wait for a pooled connection, connecting included), `aghoo_db_command_seconds{pool}` (one unit of work, acquire to release) and `aghoo_redis_command_seconds{command}` (one command or pipeline, `PIPELINE`/`MULTI`, the wait included).
  - `pool` is `app` (the `noema_app` pool) in Phase 1.
  - Histogram observations are a **random 1 in 16** (`DATASTORE_SAMPLE_RATE`): one OTel record costs ~15 µs, a Redis command ~100 µs of CPU, and timing every one added 34 µs per command. Counts are therefore 1/16 of the operations; means and quantiles are unbiased. The seconds buckets start at 5 ms, so a sub-ms command shows in the sum and count, not in a bucket.
**Rationale.** A bounded pool turns an overload into short waits and typed failures instead of unbounded connections. The metrics make C2's pool saturation and datastore latency measurable at a CPU cost below 0.3% of a turn.
**Refs.** spec-10 §5 C2; spec-05 §5; spec-02 §12; IMPL-006, IMPL-087. **Patch: none.** **Material.** N. Source: Wave E3 misc fixer.

### IMPL-089 (new): The image interpreter (python-build-standalone, uv 0.13.0) and the sizing basis
**Decision** (Wave F, 2026-10-12; recorded by the final docs fixer from `reports/perf/sizing.md` §2.1, §5, §8).
- **Interpreter.** `deploy/docker/api.Dockerfile` (and `noema-worker`, which derives from it) installs CPython 3.12.15 from python-build-standalone with `uv python install` into `/opt/python` on `debian:trixie-slim`, instead of the official Python image's CPython. The venv holds the same 66 distributions; uid 10001, read-only root, no dev or media group and no tests, as before.
- **Why.** In E3 the image's interpreter cost +11–13% CPU per turn and +0.7–1.5 ms of p50 against the dev venv (also python-build-standalone), which put the image's knee at 75 and the venv's at 125 streams per pod. Rebuilt, the image matches the venv: knee 125 in two runs, 9–11% less CPU per turn than the E3 image.
- **Sizing basis.** Per the owner decision of 2026-10-12 (size from the measured knee), 08 §9 now uses the Wave F image's container knee: at most 125 streams per pod, planned at 100, 8 pods for ~800 streams, scale out above 100 streams or 0.62 vCPU per pod. The alert rules and dashboards (`deploy/observability/`) use the same thresholds.
- **Supply chain.** The interpreter's statically bundled OpenSSL, SQLite, zlib and other libraries are invisible to trivy's Debian scan; a CVE in them needs a `PYTHON_VERSION` / `UV_IMAGE` bump (sizing §7 risk 6). trivy on `:wfinfra`: 0 CRITICAL; 43 HIGH, all waived (sizing §8).
- **Re-gated.** The release gate (2026-10-13, reports/qa/release-gate.md) re-ran the full regression on images built from the final tree and measured C2, C3, C5 and L1 at 100 streams/pod (p50 9.21, p95 12.64 ms) in the container; C4 (1 h soak) stands from E3.
- **uv 0.13.0** (recorded at the Wave F release gate). The api build stage's `UV_IMAGE` moves from uv 0.9.10 to `ghcr.io/astral-sh/uv:0.13.0@sha256:cdc60931...`. uv's embedded, checksum-pinned download list selects and verifies the interpreter build, and uv 0.9.10's list ends at CPython 3.12.12 (checked offline, 2026-10-12), so 3.12.15 (the patch level the E3 image already shipped) needs the newer uv. The `UV_IMAGE` pin is therefore also the interpreter's supply-chain pin. `uv.lock` and `pyproject.toml` are unchanged (`git diff wave-e3 wave-f`). The media image keeps `python:3.12-slim-bookworm` and uv 0.9.10, and the dev venv keeps CPython 3.12.12 from uv 0.9.10: the dev interpreter is three patch releases behind the image's.
**Rationale.** The owner chose to size from the knee; the knee was the interpreter's, and the shipped image now has the venv's.
**Refs.** 08 §9; IMPL-087; `reports/perf/sizing.md` §2, §5, §8; `reports/qa/e3/qa-lead.md` §3.1. **Patch: doc**, applied in design revision 7 (08 §9, 06 §11 note, README). **Material.** Y: the deployment shape changes (8 pods instead of 14); owner acknowledgement pending (OWNER-CHECKPOINT, Wave F). Source: Wave F infra-perf.

### IMPL-090 (new): Operator proof: the database verifies the operator key (migration `0003_operator_proof`)
**Decision** (Wave E edge fixer item 8, SE15 / SEC-CFG-01, 2026-10-11; recorded at the Wave F release gate; `reports/qa/edge-fixer.md` item 8 and §3 item 2; `docs/modules/db.md` "Revision 0003").
- **Before.** `admin._operator`, which every operator-facing `admin.*` function runs, checked only that `p_operator_id` named a live operator; the key was verified inside the `noema-admin` client. Anyone holding the shared `noema_cli` DSN could therefore act as any live operator id (the ids are readable in audit rows and `list_config_versions`): propose as A and approve as B, mint keys, or re-enroll after a revocation.
- **Now.** Revision `0003_operator_proof` replaces the internal `admin._operator`. The connection must carry the proof in its session setting `noema.operator_key` (`{"v": pepper version, "p": pepper, "k": key_id, "s": secret}`), which `PgAdminCalls.prove_operator` sets after the CLI's own check and again after a pepper re-hash. `_operator` recomputes `hmac-sha256:v<v>:<hex>` with the new internal helper `admin._hmac_sha256` and compares it with `platform.operator.key_hash`; otherwise `operator_invalid` ("unknown, revoked or unproven operator"). A session setting is private to its connection, so one connection acts only as the operator whose secret it holds, and the two-person rules (config approval, calibrator activation) need two keys.
- **Unchanged.** No table, column, grant or granted function changes (IMPL-010; spec-03 §4's function list). `NOEMA_OPERATOR_KEY` and the IMPL-044 commands are as before.
- **Operations.** The proof reaches Postgres as a bind parameter: keep statement logging with parameters (`log_statement=all`) off on production databases, as for every secret-bearing call. The migration head is `0003_operator_proof` (`docs/operations/upgrades-and-migrations.md`).
**Rationale.** The two-person rule has to hold against a holder of the `noema_cli` DSN, not only inside the CLI.
**Refs.** IMPL-044, IMPL-008, IMPL-009, IMPL-010; spec-03 §4; SE15 (SEC-CFG-01); `tests/integration/db/test_admin_config.py::test_a_connection_acts_only_as_the_operator_whose_key_it_proved`. **Patch: none.** **Material.** N: no API change; the operator commands are unchanged, and a `noema_cli` connection without a valid operator key is now refused. Source: Wave E edge fixer.

### IMPL-091 (new): Object storage as built: the platform bucket `noema-<env>` with per-tenant KMS keys in the platform account
**Decision** (as built since Wave B, `src/noema/core/objects.py`; spec-03 §2, spec-09 §6.7; recorded at the Wave F release gate, 2026-10-12).
- **Design** (08 §5, 08 §6 onboarding row, 02 §8.5). Each tenant account holds a media bucket (`aghoo-media-<tenant-id>`) and a KMS key. The tenant's `AghooBedrockInvoke` role may put, get and delete objects in it and use the key, and onboarding creates both through StackSets.
- **As built (Phase 1).** One bucket per environment in the **platform account**, `noema-<env>` (`NOEMA_S3_BUCKET`, `default_bucket`). Every object is under its tenant's prefix: `<tenant_id>/quarantine/<file_id>`, `<tenant_id>/derivatives/<file_id>/<kind>[/page-NNNN.png]` and `<tenant_id>/idempotency/<record_id>` (IMPL-085).
  - In staging and prod every put is SSE-KMS with the tenant's own key, `alias/noema-<env>-<tenant_id>`, also in the platform account (`sse_args`, `tenant_kms_key_alias`). Dev and test use SSE-S3, since moto has no tenant keys.
  - The processes reach the bucket with their pod credentials, not with the tenant's STS session. Tenant isolation rests on the prefix, the RLS-scoped row that holds the object key, and the per-tenant KMS key.
  - The platform workload roles therefore need S3 object access on `noema-<env>` and `kms:GenerateDataKey` / `kms:Decrypt` on the tenant keys. 08 §5's data-plane policy does not list them, and no document gives the exact action list yet (IaC item).
- **Why it holds in Phase 1.** No Phase 1 path needs an object inside the tenant account. Audio, video and ASR through Transcribe are Phase 2 (05; 02 §8.5). Uploads, derivatives and large idempotent bodies are read only by the platform.
- **Deferred to Phase 2, pending the owner.** ASR needs the tenant-account bucket: Transcribe runs with the tenant's credentials in the tenant account and reads the normalized audio there (02 §8.5). Until the owner decides, 08 §5's `MediaBucket` / `MediaKey` statements and the onboarding row's tenant media bucket and key are Phase 2 items. Phase 1 onboarding creates the tenant key in the platform account (`docs/operations/tenant-onboarding.md` step 4).
- **For the owner.** (a) Accept for Phase 1 that tenant objects rest in the platform account under a per-tenant key there, not in the tenant's own account (D2: one AWS account per tenant). (b) For Phase 2, choose a tenant-account bucket for ASR only, or for every object, which means migrating the existing prefixes.
**Rationale** (not recorded when spec-03 §2 chose the layout in Wave B; stated here at the release gate). One platform bucket keeps Phase 1 onboarding to the invoke role and one key, and leaves one set of access paths to secure and test. A per-tenant key still separates the tenants' data at rest.
**Refs.** 08 §5, §6, §8; 02 §8.5; 05 Phase 2 modalities; spec-03 §2; spec-09 §6.7; IMPL-085; `docs/architecture.md` §8; `docs/operations/tenant-onboarding.md`. **Patch: doc**, applied at the Wave F release gate as as-built notes in 08 §5 and §6 (design revision 7). **Material.** Y, **owner review**: it changes where tenant data rests and which account holds its key, which touches D2. It is not visible in the API. Source: Wave B foundation (spec-03 §2), first listed as a deviation in Wave F (`reports/mvp-acceptance.md` §6).

---

## Traceability

| Old id | Canonical id | Note |
|---|---|---|
| IMPL-DEC-1 | IMPL-001 | |
| IMPL-DEC-2 | IMPL-002 | plus the binding rule (critic 39) |
| IMPL-DEC-3 | IMPL-005 | |
| IMPL-DEC-4 | IMPL-025 | plus the per-answer budget check (critic 28) |
| IMPL-DEC-5 | IMPL-027 | the error code moved to IMPL-036 (critic 2/29) |
| IMPL-DEC-6 | IMPL-028 | |
| IMPL-DEC-7 | IMPL-047 | `x_aghoo` keys changed to client-visible names (critic 10/30) |
| IMPL-DEC-8 | IMPL-048 | |
| IMPL-DEC-9 | IMPL-031 | "no default" 400 removed; 403 on both surfaces |
| IMPL-DEC-10 | IMPL-049 | Jev/facade values from their own maps (critic 6/7) |
| IMPL-DEC-11 | IMPL-050 | patch changed to `doc` (critic 14) |
| IMPL-DEC-12 | IMPL-051 | |
| IMPL-DEC-13 | IMPL-052 | |
| IMPL-DEC-14 | IMPL-053 (templates), IMPL-029a (escaping) | |
| IMPL-DEC-15 | IMPL-054 | |
| IMPL-DEC-16 | IMPL-020 | the gap-20 resolution |
| IMPL-DEC-17 | IMPL-055 | `calibrated_distribution` always with members (critic 9) |
| IMPL-DEC-18 | IMPL-056 | bullet 1 and the 03:L56 patch withdrawn (critic 3/34) |
| IMPL-DEC-19 | IMPL-022 (persistence), IMPL-057 (outcomes) | |
| IMPL-DEC-20 | IMPL-004 | the gap-4 resolution; put gates added (critic 17) |
| IMPL-DEC-21 | IMPL-058 | `metadata_only` skipped (critic 25) |
| IMPL-DEC-22 | IMPL-059 | |
| IMPL-DEC-23 | IMPL-060 | |
| IMPL-DEC-24 | IMPL-061 | |
| IMPL-CON-01 … 11 | IMPL-030 … IMPL-042 | CON-01→030, 02→031, 03→032, 04→033, 05→034, 06→035, 07→036, 08→037, 09→040, 10→041, 11→042 |
| IMPL-CON-12 | IMPL-056 | |
| IMPL-CON-13 … 18 | IMPL-062 … IMPL-067 | |
| IMPL-SEC-07, IMPL-SEC-N6 (ADR-001) | IMPL-007 | security.md was never written; ADR-001 is the source |
| IMPL-SEC-N7, IMPL-SEC-29f/29g (ADR-002) | IMPL-081, IMPL-029f/g | |
| IMPL-SEC-N8 (ADR-002 "API never decodes media") | IMPL-074 | |
| CP-DEC-1, CP-DEC-2 | IMPL-047, IMPL-048 | rebased into the rev-6 payload (spec-08 §14.2) |

Gap coverage check: gaps 1–46 → IMPL-001…IMPL-046, one each. No gap has a second resolution: the new decisions IMPL-047…091 refer back to their gaps instead of resolving them again (IMPL-086 supersedes the spotlighting item 29b in place; gap 29 keeps IMPL-029 as its resolution).
