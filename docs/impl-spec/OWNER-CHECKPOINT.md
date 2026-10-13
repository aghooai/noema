# Owner checkpoint: Wave A (design analysis) → Wave B (foundation)

Prepared by the lead architect on 2026-10-09 for review before any code is written. Decision ids refer to [`../IMPL-DECISIONS.md`](../IMPL-DECISIONS.md).

**Owner decisions are unchanged.** D1–D6 and R5 hold. Three decisions apply an owner decision in a way you should confirm:
- D2's "per call" wording (§a item 1);
- the D6 default for tenants that don't configure one (§a item 4);
- the R5 hierarchical deferral (§a item 2).

None of them blocks Wave B: each has a default that applies if you don't answer.

## (a) Material decisions to review

| # | Decision | What it means for clients and operators | Why | Alternative |
|---|---|---|---|---|
| 1 | **IMPL-007, STS (D2).** "AssumeRole per call" is read as per-call *tenant credentials* from one STS session per (pod, tenant). The session lasts ≤ 1 h, refreshes 5–10 min before expiry with a single flight, and fails closed | Every model call still runs in the tenant's own account with an asserted account id. The overhead SLO holds | Per-call AssumeRole adds 15+ STS round trips per turn and hits STS rate limits; 02:L203 and 08:L488 already cache | Literal per-call AssumeRole (breaks the 25 ms overhead SLO) |
| 2 | **IMPL-027 / IMPL-036, hierarchical method (R5).** Not built in Phase 1. A choice question with more options than the profile's flat limit (16 on `router-default`, 26 on `lightweight-model-decide`) gets **422 `feature_not_available`** on native `/v1/decisions` and in turns, and **422 `unsupported_parameter`** on the facade and with `output_format: "jev"`. The 2–255 API limit is unchanged | Requests the docs say are answered in two stages are refused with a typed error until the method ships; lifting the 422 later is additive | Both are [X] and outside 05 Phase 1. A half-built path would emit `unresolved_mass` the calibration chain has never exercised | Build the hierarchical method in Phase 1: about 1 engineer-week plus calibration plumbing (owner question 1) |
| 3 | **IMPL-025, no escalation inside turns** with the shipped numbers. `router-plus` needs 2,500 ms, and the turn's decision wait limit is 1,050 ms | A low-confidence `intent` goes to the bindings and conditioning (`cover_top2_and_clarify`). `/v1/decisions` escalates normally | Deadlines stay at their design values until Phase 0. One testable budget inequality | A post-commit escalation that only feeds `decision.updated` and next-turn stickiness, at +3 calls per escalated question (owner question 2) |
| 4 | **IMPL-031, default decision profile (D6).** `profile` is optional on `/v1/decisions`. When a tenant's config omits `defaults.decision_profile`, the loader fills in `lightweight-model-decide`. Unknown and non-allowlisted profiles are both 403 `profile_not_allowed`, on native and facade alike (the facade contract description is patched) | Every tenant gets the D6 default. Profile ids can't be enumerated | The docs already promise the default (03:L221, 08:L33); one error rule for both surfaces | A 400 when no default is configured (the contracts analyst's version): no D6 default for those tenants |
| 5 | **IMPL-002, the `quorum` status.** Inside turns, `quorum` marks the snapshot sent when every commit-critical answer is final but a non-blocking one is still open. Exactly one `decision.updated` follows. Bindings read final answers only | Clients see `quorum`, then one `decision.updated`. Never on `/v1/decisions` | Only commit-critical questions block (02:L214); the enum already describes this. It is **not** early commit (R5 unchanged) | Wait for all questions, which delays the commit by the slowest non-blocking question |
| 6 | **IMPL-001, `completed` vs `partial`.** `completed` = every answer final and none `unavailable`, including "the deadline passed with quorum met". `partial` = at least one `unavailable` | Clients branch on one rule | types.ts is the machine-checked contract; 04:L110 is patched | 04's wording: `partial` whenever the deadline passed |
| 7 | **IMPL-068, which questions escalate.** `escalation.questions` filters platform (config) questions only. Request questions on `/v1/decisions` are always eligible | `router-default` escalates weak request answers to `router-plus`, which costs more calls; the shipped escalated example becomes reachable | Config can't name request questions; a literal filter would leave `router-plus` escalation (a Phase-1 deliverable) unreachable | The literal filter: router-default never escalates request questions with the shipped yaml |
| 8 | **IMPL-061, final-stage rule.** An escalation stage that returns `unavailable` (e.g. a Haiku timeout under `router-plus` quorum 3) **replaces** the scored stage-0 answer, as 02:L422 says | Rarely, an escalation makes an answer `unavailable` | It is the design, applied literally; it keeps the stage-bound calibrator rule simple. Phase 0 measures how often it happens | Keep the earlier scored answer (owner question 3) |
| 9 | **IMPL-047 / IMPL-048, two additive schema changes (CP-DEC-1/2).** (1) A response-side `AnswerName` allows facade-generated `q<digits>` names in stored results; `x_aghoo` is keyed by the client-visible name. (2) `EscalationStage` gains `input_view_signature` and `prompt_templates` | Stored facade decisions validate. Escalated answers can be calibrated correctly | Without (1), every unnamed facade question produces a schema-invalid stored decision. Without (2), an escalated answer's calibrator check uses the wrong stage's view and templates | Reject (2): escalated answers are always `uncalibrated` (owner question 4) |
| 10 | **IMPL-049, Jev and facade values.** Jev `score` and `confidence` are computed from the 2-decimal probability map. The facade's `confidence` uses its own emitted probabilities (n = K). A large censored logprob tail → `unavailable` (Jev) or `refusal` with `mapped_unavailable` (facade) | In rare rounding and censoring cases the Jev score differs from `round(native, 2)` (e.g. 1.93 vs 1.92) | It is the only way the validators pass for every K without making their tolerance meaningless (about 0.6 at K = 16) | Widen the checker tolerance |
| 11 | **IMPL-057, decision outcomes.** Native: every answer unavailable → **200 `partial`**. 503 `tenant_access_misconfigured` only when credentials fail, or when every member failed on tenant access. 504 only when the deadline expired before any member call started. A decision member's model AccessDenied degrades that member (it deviates from 04:L104 for decision members only) | Clients branch on answer types; turns degrade gracefully | Availability; 07:L197 | Fail the request on any member AccessDenied |
| 12 | **IMPL-072, calibrator lifecycle (R5, D4).** Status `fitted → accepted\|rejected → active → downgraded\|retired`; **two-person activation** (approve, then activate by a different operator); a platform artifact that drifts is **downgraded for every tenant** (no per-tenant suppression in Phase 1); pods see changes within ≤ 5 s | `calibrated` is never served outside accepted evidence; the W4 view shows the real lifecycle | 03 W4 specifies a second approver; per-tenant suppression is Phase 2 scope | Single-operator activation (the analyst's ADR draft); per-tenant suppression now |
| 13 | **IMPL-033, end-user delegation deferred.** `x-aghoo-end-user` → 422 `feature_not_available` on every project in Phase 1; no JWKS config | Apps can't scope history to an end user in Phase 1 | Not in 05 Phase 1; OIDC/BFF is Phase 2 (05:L91); the contract is unchanged | Static per-project JWKS verification now (owner question 5) |
| 14 | **IMPL-064, a 50 MiB JSON body cap** (`platform.limits.max_json_body_bytes`) | About 10 maximum-size inline images per request; facade clients with 20 large images must use smaller images or uploads | A hard security control the design lacked; it matches the largest upload cap | Another value (owner question 6) |
| 15 | **IMPL-022 / IMPL-021, persistence and replay.** Ordering-critical writes are synchronous; billing and audit are written behind through an outbox. With Postgres down, `store: true` turns fail fast (500, retryable) and `store: false` continues. An `Idempotency-Key` replay of a streamed turn after the 5-minute window → 410 `resume_window_expired` | Defined fault behavior (06 §7); "no new billing" on replays | `sticky_spec` and `seq` need synchronous commits; the event log lives only for the resume window | A fully asynchronous outbox, which breaks session ordering |

Also material, but mechanical contract work that follows from the above: IMPL-030, 032, 034, 035, 037, 038, 040, 042, 056, 065 and 069. They are listed in §b.

## (b) Contract patch summary (one combined rev-6 payload, IMPL-067)

The base is the verified rev-6 payload in spec-08 (Appendix A + B). The applier merges spec-08 §14 into it, regenerates the examples (48 → 54) and the hashes, and runs `check_docs.sh` plus the audit (41 → 46 cases).
- **ErrorCode**: adds `not_found`, `permission_denied`, `invalid_end_user_token` (reserved), `request_too_large`, `feature_not_available`, `internal_error` (+ type `server_error`) and `not_implemented`. One code → one status, `ERROR_TABLE` (IMPL-030).
- **Responses per operation**: 401 and 500 everywhere, plus per-operation `x-noema-error-codes`. createDecision gains 404, 409 and 504 (pre-dispatch only). The facade gains 403, 409, 413, 415, 422, 503 and 504. createTurn gains 410, 413, 415 and 504. cancelTurn gains 409. `no_capable_member` is removed from createDecision and the facade; `file_not_ready` and `file_rejected` are added to the facade (IMPL-032, IMPL-074).
- **Headers** declared: `x-request-id`, `traceparent`/`tracestate`, `Aghoo-Version` (`2026-10-01`), `Aghoo-Deadline-Ms` (now on the facade too), `Idempotency-Key`, the 6 `x-ratelimit-*`, `retry-after`, `x-aghoo-idempotent-replay`, `x-aghoo-end-user` (422 in Phase 1) (IMPL-033).
- **SSE**: a strict `TurnEvent` discriminator with 15 named closed variants; TS `ErrorCode`; the `file.updated` event; the 15 s ping (IMPL-034).
- **SDK**: generated TS wire types plus hand-written clients; the array form of `q.choice`; new methods; `AghooError`; the Python sync client (IMPL-035).
- **Out-of-phase values**: 422 `feature_not_available` (`speculative`/`hedge`, `best_of_n`/`fuse`, hierarchical on native and turns, `retention: custom`, end-user header); 501 for resume; 422 `unsupported_parameter` for hierarchical on the facade and Jev (IMPL-036).
- **Type fixes**: `ProfileInfo.version` → string; `image_url` `maxLength` removed (the 413 is decided by arithmetic); typed and closed `x_aghoo`; the facade remote image URL → data-URL pattern (400 `schema_violation`); at most 20 images (IMPL-037, IMPL-056).
- **`DecisionCreateParams.profile` optional**, with the D6 default description (IMPL-031).
- **CP-DEC-1** `AnswerName` (IMPL-047) and **CP-DEC-2** `EscalationStage.input_view_signature`/`prompt_templates` (IMPL-048).
- **Sessions, usage, W4**: `SessionUpdate`, `SessionTurn(List)`, `SessionList`, `UsageReport` with pagination; `/v1/admin/{calibrators, labeling/runs, labeling/dropped}` with `CalibratorInfo` aligned to ADR-003 (status enum, `parameters{pooled, member_T, epsilon}`, failed rules A1–A8, two-person `approved_by`/`activated_by`) (IMPL-040, IMPL-042).
- **Transport and derivatives**: the 413 `request_too_large` body cap, 415, 404 for unknown routes, 400 for unknown query parameters; per-kind derivative content types, `?page=`, `pages`, `attachment` with `nosniff` (IMPL-064, IMPL-065).
- **Closed schemas and codegen**: `additionalProperties: false` on every openapi object; named list schemas; the `OADescription` rename; `x-noema-error-codes`/`x-noema-roles`; the `check_docs.sh` bundle step (IMPL-063, IMPL-066, IMPL-067).
- **`config.schema.json` + `check_config.py`** (rev-6 addition): optional `platform` (limits, idempotency, events, sessions, decision cache, cost, media, credentials, data classes), `tenants.<t>.limits`, `labeling.acceptance` bounded by the contract constants, and 4 new checker rules with negative tests. The example yaml is unchanged (IMPL-038, IMPL-039).
- **`check_examples.py`**: importable (`main()` guard, `Violation`); `result_invariants`, `holdout_errors`, `band_of` and `template_limit_errors` parameterized for the active config; the censored-option `.get(key, 0.0)` fix (a crash today); the CP-DEC-2 `answering_stage` 4-tuple; the `x_aghoo` invariant; Jev pairs. `qa/run_design_audit.py` loads it with `runpy` and gains 5 cases (IMPL-041, IMPL-039).
- **types.ts / sdk.ts comments and types**: `DecisionResult.status` text; the `Conditioning.signature` definition (includes the template id); `served_spec_differs` (also on a fallback to `default_profile`); the `HeuristicConfidence` field definitions; the W4 types (IMPL-002, 014, 016, 079, 042).
- **New examples** (6): a censored-tail result and its request; a facade pair with a non-pattern name (`"Severity Level"`); a K = 16 Jev pair.
- **Prose patches** in 01, 02, 03, 04, 05, 07, 08 and README: about 40 sentence-level edits (spec-08 §14.9). They include the reordered 03 SSE example, the hierarchical Phase-1 note, the per-tenant STS wording, the HMAC key hashing, the pikepdf wording, the deadline table and the idempotency storage rule.

## (c) Owner questions (none blocks Wave B; the default applies if unanswered)

| # | Question | Default |
|---|---|---|
| 1 | **R5 / hierarchical** (merges both analysts' question). Accept that Phase 1 answers choice questions above the profile's flat limit with 422: `feature_not_available` on native and in turns, `unsupported_parameter` on the facade and Jev? Or must Phase 1 build the two-stage method (about +1 engineer-week plus calibration plumbing)? | **Defer** (422 as specified) |
| 2 | **Turn escalation**: accept "no escalation inside turns" with the shipped numbers, or add a post-commit escalation that only feeds `decision.updated` and next-turn stickiness (+3 calls per escalated question)? | **Accept** (no in-turn escalation) |
| 3 | **Final-stage rule**: should an escalation stage that returns `unavailable` replace a scored stage-0 answer (the design), or should the earlier scored answer be kept? | **Replace** (design as written) |
| 4 | **CP-DEC-2**: accept the two additive optional `EscalationStage` fields? If not, escalated answers are always served `uncalibrated` | **Accept** |
| 5 | **End-user delegation**: defer `x-aghoo-end-user` verification to Phase 2 with OIDC (422 on every project now)? | **Defer** |
| 6 | **JSON body cap**: 50 MiB (`limits.max_json_body_bytes`)? | **50 MiB** |
| 7 | **`injection_suspected` timing**: it is a non-blocking question, so its `spotlight: strict` applies only to attempts of the same turn that start after it is final (restarts and fallbacks; nothing carries over to later turns, spec-09 §5), and its `verify_answer` applies post-commit. Should it be made commit-critical (a config change only), at the cost of waiting for its slowest member before the commit? | **Keep non-blocking** (as shipped) |

## What happens next

1. You answer §c, or accept the defaults.
2. The contract-patch applier builds and verifies the combined rev-6 payload (§b) and applies it to `docs/noema/contracts` with `check_docs.sh` and the audit green.
3. Wave B1 starts from spec-01, spec-02, spec-05 and spec-08, and freezes `core/` at the interface-freeze tag.

## Wave A2 review (2026-10-10)

An independent review of the backfilled specs (behavior, structure, security, QA) was reconciled into `IMPL-DECISIONS.md` and the spec files. No owner decision (D1–D6, R5) and no material decision above was changed. One wording fix: §c item 7 said strict spotlighting reaches "later turns"; spec-09 §5 defines no carry-over between turns, so the item now says so. Please confirm that reading.

New or changed items you may want to see (none blocks Wave B):
- **IMPL-084 (new, material: config surface).** Optional `models.<alias>.context_window` (default 128,000, a placeholder) for the 04:L32 capability filter and the context budget; `GEN_DEFAULT_MAX_TOKENS = 4096` for models without a cap (Sol, Opus); stream timeouts per model and effort instead of per profile.
- **IMPL-042 (scope).** The W4 admin reads are tenant-wide for every allowed role, so a `reviewer` sees redacted excerpts of other projects' dropped labeling items. Accepted for Phase 1; restricting it needs `label_item.project_id`.
- **IMPL-029a (hardening).** The reserved-tag escape also catches `<` followed by whitespace or invisible format characters before the tag (`< /untrusted_input>`); before the freeze it costs nothing, because no calibrator exists yet.

Challenges to material decisions (recorded, not applied at the time; **both resolved 2026-10-10**: 1 → IMPL-085, 2 → IMPL-086, see "Owner answers (2026-10-10)" below):
1. **[Resolved: IMPL-085] Item 15 / IMPL-077: idempotent replays of large responses.** A response body over 1 MiB is stored as `not_stored`, so a retry with the same `Idempotency-Key` re-executes and bills again. That contradicts "no new billing on replays" for large results (large `/v1/decisions` results with `include: members`, or `stream: false` turns). Options: accept this edge case explicitly, or store large bodies (bytea up to the 50 MiB body cap, or S3) instead of falling back to `not_stored`.
2. **[Resolved: IMPL-086, owner answer 4] §c item 7 / IMPL-029b: `injection_suspected` stays non-blocking.** Strict spotlighting reaches only restarts and fallbacks. The first attempt, which commits in most turns, always gets standard spotlighting, even when `injection_suspected` later fires at p ≥ 0.2, so in practice the strict tightening rarely protects the answer the user receives. A low-cost option that keeps the decision as is: always datamark document and OCR blocks in generation prompts. They sit after the cacheable prefix, so caching is unaffected; the cost is some answer-quality risk on documents (Phase 0 can measure it).


## Owner answers (2026-10-10)

The owner answered §c on 2026-10-10. The answers override the defaults where they differ. D1–D6 and R5 otherwise hold: Bedrock only, never the OpenAI API. The lead architect applied them to `IMPL-DECISIONS.md` and the specs the same day.

| Answer | §c item | Owner answer | Applied as |
|---|---|---|---|
| (1) | 1 | **Build** the hierarchical method in Phase 1, for native `/v1/decisions` and for turn app questions, exactly as 08 §3.4 specifies. The facade and `output_format: "jev"` keep 422 `unsupported_parameter` (R5). This replaces the deferral in IMPL-027/IMPL-036 | **IMPL-027** revised (method built; engineering resolutions (a)–(l)); **IMPL-036** revised (only the facade/Jev 422 remains; `feature_not_available` is never raised for a choice question); spec-06 §19 (algorithm, T-HIER-1…10); spec-04 §3, spec-11 §1.1 (T-WG-5), spec-05 §1.4, spec-10 Q9, spec-02 §5/§8, ADR-003 §1; spec-08 §14.0 |
| (2) | 2 | No in-turn escalation | **IMPL-025** stands (unchanged) |
| (3) | 3 | An escalation stage that returns `unavailable` replaces the stage-0 answer | **IMPL-061** stands (unchanged) |
| (4) | 7 | `injection_suspected` stays non-blocking | default kept; spotlighting is strengthened instead by the orchestrator call (b), **IMPL-086** |
| (5) | 4, 5, 6 and §a item 1 | Defaults accepted: CP-DEC-2 `EscalationStage` fields; `x-aghoo-end-user` deferred (422); 50 MiB JSON body cap; STS cached per (pod, tenant) | **IMPL-048**, **IMPL-033**, **IMPL-064**, **IMPL-007** stand (unchanged) |

Orchestrator engineering calls (recorded as resolutions; not owner decisions):
- **(a) IMPL-085** (and IMPL-077 amended): an idempotent response over 1 MiB is stored in object storage (`s3://noema-<env>/<tenant_id>/idempotency/<record_id>`, SSE-KMS with the tenant key, the record's TTL) instead of `not_stored`. Size alone never makes a replay re-execute or re-bill. The only remaining re-execution after dispatched model calls is an infrastructure failure on the put, which is logged and counted. DDL `idempotency_record.response_body_key` (spec-03 §2); store and sweeper in spec-11 §5–§6; T-IDEM-2.
- **(b) IMPL-086** (supersedes IMPL-029b):
  - `standard` spotlighting always datamarks document- and OCR-derived blocks, in generation prompts (`<untrusted_input>` bodies) and in decision prompts (media view text blocks).
  - `strict` additionally datamarks all other untrusted text in generation prompts: current user text and history user and assistant text.
  - Decision prompts are always `standard`. `PREAMBLE_V1` and the `logprob-codes@2` / `verbalized-dist@1` system text explain the marker; no template version bump (no artifact yet).
  - Tests: spec-09 §5, SEC-INJ-04 (revised), SEC-INJ-06 (new).

### Wave A2 challenges: resolved

1. **Item 15 / IMPL-077, idempotent replays of large responses**: **resolved** by IMPL-085 (orchestrator call (a)), with IMPL-077 amended. Bodies over 1 MiB go to object storage, never `not_stored` for size.
2. **§c item 7 / IMPL-029b, strict spotlighting rarely reaching the committed attempt**: **resolved** by IMPL-086 (orchestrator call (b)), together with owner answer 4 (`injection_suspected` stays non-blocking). The first attempt now always gets datamarked documents and OCR text. The A2 wording point (no carry-over of `strict` to later turns, spec-09 §5) was not answered separately, so the spec keeps no carry-over.

### Engineering resolutions inside IMPL-027 you may want to see (none needs an answer)

- **Deadline.** Both rounds run inside the request's `deadline_ms`; round 1 gets at most half. A round that doesn't fit gives `unavailable / deadline_exceeded` (decision `partial`, still HTTP 200), never a truncated or flat answer.
  - With a profile default of 1,000 ms most hierarchical answers will be `unavailable`. Callers set `deadline_ms` (about 3,000), as 08 §3.4 and the 07 guide already say.
  - There is no new config key and no compile-time latency check.
- **Members across rounds.** A member counts only if both rounds succeeded. If the remaining members would need a group that round 2 didn't expand, the answer is `unavailable / no_quorum`.
- **Censored reads.** A censored logprob read in a hierarchical round makes that member `error / censored_tail`.
- **Calibration.** Hierarchical answers are always `uncalibrated` in Phase 1: labeling fits flat bindings only (ADR-003 §1). The binding already carries `choice_method: "hierarchical"`.
- **Escalation** stays flat: with the shipped config a hierarchical answer never escalates. The example's stage-0 triggers fire, but `router-default` can't carry 32 options flat.
- **Turn platform questions** stay flat (P1-D6). Under a `decisions.profile` override that can't carry one flat, the turn gets 422 `unsupported_parameter` (IMPL-069 amended; it was `feature_not_available`).
- **Admission** reserves 1 + 3 calls per member per hierarchical question (spec-11 §1.1). The L2 key records each question's method (IMPL-004 amended).
- **Confidence.** `normalized_entropy` counts the unresolved bucket, consistent with `normalized_max` (IMPL-079 amended).

### Effect on the §b payload summary

- **Out-of-phase values:** "hierarchical on native and turns" is removed from the 422 `feature_not_available` list. The facade/Jev 422 `unsupported_parameter` stays (spec-08 §7, §14.1 item 2).
- **`check_examples.py`:** the `hierarchy_errors` expansion check uses the server's integer rule, ≥ 9,000 units of the emitted 4-dp masses (spec-08 §14.5).
- **Examples:** the hierarchical examples stay and are now used as the expected output of the Phase-1 code path (spec-06 T-HIER-1).
  - `decision-result.valid-hierarchical.json` is regenerated with `normalized_entropy` 0.7489 (was 0.749). Three derived invalid examples change in the same confidence fields and keep their verdicts.
  - The count stays 54. A scratch run on 2026-10-10 (the rev-5 tree with only the §14.5 and §14.6 hierarchical changes) found 0 unexpected outcomes (spec-08 §14.6).
- **Audit:** 41 → **49** cases (46 before this change). The new ones are `hierarchical_native_answer_valid`, `hierarchical_expansion_exact_threshold` and `hierarchical_round1_only_rejected` (spec-08 §14.8).
- **Prose:** the "Phase 1: hierarchical not available" note for 03 §4.1, 07:L154 and 08 §3.4 is withdrawn. 05:L75 gains the hierarchical method in the Phase 1 decision-engine row (spec-08 §14.9).

## Wave C2a orchestrator rulings (2026-10-10, recorded as IMPL amendments)

- **R1** (IMPL-027 (d), material: changes a decision outcome and the `hierarchy_errors` check): the expanded set holds ≥ 2 options, so a dominant one-option group yields a scored answer instead of `unavailable / no_quorum` (spec-06 §19.6, spec-08 §14.5).
- **R2** (IMPL-012), **R3** (IMPL-075), **R4** (IMPL-078) and **R5** (IMPL-072; ADR-003 §3, §5: the ECE noise floor and `DRIFT_MIN_N = 100`, both tunable in Phase 0) clarify behavior without contract changes.

## Wave E2 rulings (2026-10-11, recorded as IMPL amendments)

Externally visible (material):
- **R6** (IMPL-016): a fallback to a different model is not a retry and never spends the retry budget; the budget gates same-model re-sends only. The attempt cap, admission and the breakers bound fallbacks; F12 (≤ 1.1×) still gates.
- **R7, ingress**:
  - auth before the body is read: a chunked over-cap body without a valid key is 401, not 413 (IMPL-064);
  - at most 64 media parts per request, 400 `schema_violation` (IMPL-064);
  - ids with U+0000 or control characters are 404, never 500 (IMPL-030).
- **R7, idempotency** (IMPL-077):
  - a JSON response is stored only if a call was dispatched and its content may be kept, so a keyed `store: false` retry re-executes and is billed again;
  - the fingerprint is principal-bound: another principal's reuse is 409.
- **R7, storage**: stored U+0000 becomes U+FFFD (IMPL-022). A 2xx Converse body that is not a Converse response is retryable `malformed:response` (IMPL-076).
- **IMPL-087**: 2 api processes per 2-vCPU pod; a per-process cap with 429 `queue_timeout` + `retry-after: 1`; GC freeze and thresholds `(700, 10, 40)`, raised to `(700, 10, 100)` in Wave E3. The 08:L488 patch is applied in design revision 7: 08 §9 sizes the pods from the measured knee (owner decision of 2026-10-12, below).

Clarifications (no contract change): the L2 put gate "run not cancelled" and the fail-closed `restricted` data class (IMPL-004, IMPL-022); `FINALIZE_BUDGET_S` = 5 s (IMPL-022); the quota burst bound and the distinct-tenant p95-TTFT breaker rule (IMPL-078); the lean Converse transport (IMPL-050); `NOEMA_CREDENTIALS_BROKER`, `NOEMA_BREAKER_TTFT_RULE`, `WEB_CONCURRENCY`, `NOEMA_MAX_TURNS_PER_PROCESS` (spec-05 §5).

## Wave E3 rulings (2026-10-12, recorded as IMPL amendments)

- **Owner decision** (IMPL-080): one more performance sprint, then size the pods from the measured L1 knee (08 §9) and report the SLO at that density; the live-smoke hard stop rises from 60 to 87 calls in total (+30 for re-verification).
- **R8** (IMPL-016): the retry budget counts only re-sends the upstream may have received and is 10% of first sends over the sliding 60 s with no floor; a connect retry that never reached the upstream is exempt, once per call. F12 is gated per failing upstream (≤ 1.1×, both rows). Not material for clients: a quiet tenant loses only same-model re-sends, never a fallback (R6).
- **R9** (IMPL-022): U+0000 in stored model or turn text becomes U+FFFD (R7); in a client-supplied structured field (session `metadata`) it is 400 `schema_violation`. Both behaviors shipped in Wave E2; R9 settles which one applies where.
- **IMPL-087 amendment** (Wave E3 perf, recorded in Wave F), externally visible: the edge shed checks the per-process cap before authentication for a caller whose key id the in-process key cache holds. While a process is over its cap, a wrong secret for such a key id therefore gets 429 `queue_timeout`, not 401. Unknown key ids and keyed requests always get the full ingress. The fair share is max-min.

Clarifications (no contract change): the per-model TokenEstimator table and its `NOEMA_TOKEN_ESTIMATOR_TABLE` override (IMPL-029c); text-less stops as breaker failures and the pub/sub heartbeat (IMPL-078); the SE13 waiver file `deploy/docker/.trivyignore.yaml` (IMPL-080, spec-09 §9); one bounded Redis pool (`NOEMA_REDIS_MAX_CONNECTIONS`) and the datastore pool and latency metrics (IMPL-088).

## Wave F (2026-10-12): pending owner acknowledgement

Three material items were recorded after the fact; none has an owner acknowledgement yet (CONTRIBUTING §4:
material, externally visible changes go to the owner first). The acceptance report lists F1 and F2 as open owner
items (`reports/mvp-acceptance.md` §4); F3 was recorded at the Wave F release gate.

| # | Item | What it means for clients and operators | Status |
|---|---|---|---|
| F1 | **IMPL-087 amendment** (Wave E3, above): the edge shed checks the per-process cap before authentication for cached key ids | While a process is over its cap, a wrong secret for a cached key id gets 429 `queue_timeout`, not 401 | pending owner acknowledgement |
| F2 | **IMPL-089**: the images run the python-build-standalone CPython, and 08 §9 sizes the pods from that image's container knee | 125 streams per pod at most, planned at 100: 8 pods for ~800 streams instead of the E3 image's 14; scale out above 100 streams or 0.62 vCPU per pod. Re-gated by the release gate on 2026-10-13 (reports/qa/release-gate.md) | pending owner acknowledgement |
| F3 | **IMPL-091**: object storage as built. Tenant objects are in the platform bucket `noema-<env>` (tenant prefix), encrypted with a per-tenant KMS key `alias/noema-<env>-<tenant_id>` in the platform account, not in the tenant-account media bucket and key of design 08 §5 | Tenant data at rest is in the platform account (D2: one AWS account per tenant). Onboarding creates the tenant key in the platform account. The tenant-account bucket is deferred to Phase 2, where ASR (Transcribe in the tenant account) needs it | pending owner review |
