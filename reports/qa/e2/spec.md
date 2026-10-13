# Wave E2 spec fixer: rulings R6, R7 and the Wave E fixer rulings, recorded as IMPL amendments

Role: spec fixer (Wave E2), 2026-10-11. Edits: `docs/IMPL-DECISIONS.md` and `docs/impl-spec/**` only (no ADR needed a
change). Docs only: no code, no live Bedrock call, no AWS resource, no stack of my own (no gate's behavior depends on
these files). No git write command.

Every recorded rule was checked against the code in the tree and the regression test of the fixer that made it
(column "Backed by"). A spec edit has no "fails before" test of its own. The evidence is that the cited tests pass
on the tree (section 2).

## 1. Items

| # | Item | Recorded in | Backed by (code; regression test) | Result |
|---|---|---|---|---|
| 1 | **R6**: a fallback to a different model is not a retry; `RetryBudget` gates same-model re-sends only; bounded by the attempt cap and admission; F12 still gates | IMPL-016 amendment + index row; spec-04 §4.1 T11 and §6 guards; spec-02 §8 note; spec-10 F12 note; OWNER-CHECKPOINT | `generation/walk.py` `_start` (resilience fixer: `resend` = a model an earlier attempt of the run called); `tests/unit/generation/test_walk.py::test_a_fallback_to_another_model_is_not_a_retry`, `::test_a_re_send_to_a_model_that_already_failed_is_a_retry`, `test_fallback_slo.py` (`resilience-fixer.md`) | recorded |
| 2 | **R7** auth before the body cap (chunked over-cap without a key: 401) | IMPL-064 amendment; spec-04 §1 step 3 | `api/middleware/ingress.py` (edge 7); `tests/unit/api/test_body_cap.py` | recorded |
| 3 | **R7** idempotency stored iff dispatched and the content may be kept; keyed `stream: true` counts as dispatched once started | IMPL-077 amendment; spec-04 §1 step 7; spec-11 §5 (`complete`, streamed turns) | `withhold()` (edge 10), edge 2; `tests/unit/api/test_turn_route_hardening.py`, `test_decision_route_hardening.py` | recorded |
| 4 | **R7** principal-bound fingerprint, `sha256(fingerprint + "\n" + principal_id)` | IMPL-077 amendment; spec-11 §5 formula; spec-09 §7 | `principal_fingerprint` (edge 14); `test_decision_route_hardening.py::test_another_principals_key_is_a_conflict_never_a_replay` | recorded |
| 5 | **R7** malformed 2xx Converse body is retryable `malformed:response`; unparsable or non-string stream text is `malformed:chunk` | IMPL-076 amendment; spec-02 §4.3 note | perf sprint A §3.5, io-fixer 11; `tests/unit/adapters/test_lean_converse_parse.py`, `test_converse_stream.py` | recorded |
| 6 | **R7** U+0000 in stored text becomes U+FFFD | IMPL-022 amendment; spec-04 §10 step 2; spec-06 §16 | `turns/persist.pg_text`, `decision/repo.pg_text` (engines 20); `test_ingress_limits.py::test_nul_in_the_input_or_output_is_stored_as_u_fffd`, `tests/unit/decision/test_inputs.py::test_nul_in_the_input_is_stored_as_u_fffd` | recorded |
| 7 | **R7** NUL or control characters in path (and body) ids: 404, never 500 | IMPL-030 amendment; spec-06 §15 (404 row) | edge fixer finding 17 (Wave E2): `api/middleware/gate.check_params` (path ids with U+0000–U+001F or U+007F–U+009F → 404; query → 400), `db/repos/_base.unstorable` (a body id with U+0000 is never looked up); `tests/qa/contract/test_input_robustness.py::test_nul_in_ids_and_strings_is_a_client_error` | recorded as implemented |
| 8 | **R7** / engines 8: at most 64 media parts per request, 400 `schema_violation`, `details.max_media_parts` | IMPL-064 amendment; spec-04 §1 step 5; spec-06 §15 (400 row); spec-07 §1 | `turns/request.MAX_MEDIA_PARTS`, `decision/inputs.py`; `test_ingress_limits.py::test_more_than_max_media_parts_is_a_400_before_any_lookup`, `test_inputs.py::test_file_parts_are_read_once_per_file_and_capped_before_any_lookup` | recorded (contract declaration: contracts fixer) |
| 9 | IMPL-004 put gate "run not cancelled" | IMPL-004 amendment; spec-06 §14 | `_l2_put` (engines 14); `tests/unit/decision/test_l2.py::test_t_l2_1_a_cancelled_run_is_never_put` | recorded |
| 10 | data class `restricted` on a failed project read | IMPL-022 amendment (IMPL-004 refers to it); spec-06 §14 | `data_class_of` (engines 9); `tests/unit/decision/test_routes.py::test_the_data_class_fails_closed_when_the_project_cannot_be_read` | recorded |
| 11 | turn `FINALIZE_BUDGET_S` = 5 s | IMPL-022 amendment; spec-04 §10 step 2 | `turns/persist.finalize` (engines 2); `tests/unit/turns/test_cancel_and_failures.py::test_hung_finalize_is_bounded_then_outboxed_and_the_turn_completes` | recorded |
| 12 | quota burst: no 60 s window admits more than `rpm`/`tpm` | IMPL-078 amendment; spec-11 §1.2 (`mdl:` buckets are 1 s-slice sliding windows, 62 slices) | `gcra.quota_bucket` and the Lua window (`admission-estimator.md` §1-2, its rulings for the spec agent); `tests/unit/ops_admission/test_quota_window.py`, `tests/integration/ops_admission/test_lua_quota_window.py`. The fixer measured C1's busiest 60 s at 546 against rpm 600, with 0 mock throttles | recorded with the fixer's ruling text |
| 13 | p95-TTFT rule needs >= 2 distinct tenant accounts; TTFT sample rules | IMPL-078 amendment; spec-02 §8 note | `BreakerPolicy.ttft_min_accounts` (resilience fixer); `tests/unit/adapters/test_breakers.py`; engines 10 tests | recorded |
| 14 | `NOEMA_BREAKER_TTFT_RULE`, `NOEMA_CREDENTIALS_BROKER` | spec-05 §5 (two rows) | `composition.ttft_rule_armed`, `broker_kind`; `tests/unit/composition/test_wiring.py` | recorded |
| 15 | per-process overload cap (429 `queue_timeout` + `retry-after: 1`) and 2 api processes per 2-vCPU pod | **IMPL-087** (new; index row); spec-05 §5 (`WEB_CONCURRENCY`, `NOEMA_MAX_TURNS_PER_PROCESS`); spec-11 §7 "Process sizing and overload"; spec-04 §1 step 5; README role rows; 08:L488 patch text in IMPL-087 | `api/overload.py`, `composition.api_workers`; `tests/unit/api/test_overload.py` | recorded (design 08:L488: pending, §3) |
| 16 | GC freeze and thresholds `(700, 10, 40)` | IMPL-087; spec-11 §7 | `composition._freeze_startup_heap`; `tests/unit/composition/test_heap.py` | recorded |
| 17 | lean Converse transport (same wire and errors, botocore `SigV4Auth`, path sent as signed) | IMPL-050 amendment; spec-02 §4.3 note | perf sprint A, io-fixer 1; `tests/unit/adapters/test_lean_converse_{wire,parse,stream}.py`, `tests/integration/adapters/test_unary_transport.py` | recorded |

Also: the IMPL-DECISIONS header and the gap-coverage line (IMPL-047…087), and an OWNER-CHECKPOINT section "Wave E2
rulings" that lists the material items for the owner.

## 2. Checks

| Gate / check | Command | Pass criterion | Measured | Result |
|---|---|---|---|---|
| ownership of the edited paths | `uv run --frozen --no-sync python tools/check_ownership.py --paths docs/IMPL-DECISIONS.md docs/impl-spec/{02,04,05,06,07,09,10,11}-*.md docs/impl-spec/{OWNER-CHECKPOINT,README}.md` | every path owned | 11 paths `ok -> wave-a` | PASS |
| the recorded rules match the code | `uv run --frozen --no-sync pytest -q` over `tests/unit/{decision/test_l2.py::test_t_l2_1_a_cancelled_run_is_never_put, decision/test_routes.py::test_the_data_class_fails_closed_when_the_project_cannot_be_read, turns/test_cancel_and_failures.py::test_hung_finalize_is_bounded_then_outboxed_and_the_turn_completes, turns/test_ingress_limits.py, decision/test_inputs.py, api/test_body_cap.py, api/test_turn_route_hardening.py, api/test_decision_route_hardening.py, api/test_idempotency_hook.py, adapters/test_lean_converse_{parse,stream,wire}.py, api/test_overload.py, composition/test_wiring.py, composition/test_heap.py, decision/test_run.py::test_unary_member_latency_is_not_a_ttft_breaker_sample, generation/test_usage_and_telemetry.py, adapters/test_breakers.py, generation/test_walk.py, generation/test_fallback_slo.py, ops_admission/test_quota_window.py, ops_admission/test_gcra.py}` (22 files or ids) | all pass | **393 passed** (15.5 s), on the tree after the fixers' edits | PASS |
| make check (first, mid-wave) | `NOEMA_AGENT_ID=e2-spec make -k check` | — | red only in other fixers' in-flight files (lint in `tests/integration/infra`, `tests/security/stack`; typecheck-tests `tests/unit/mockbedrock`; unit 11 failed in `ops_admission`, `mockbedrock`, `tools/test_vendor_and_bundle`); ownership green; nothing in `docs/**` | informational |
| **make check (final)** | `NOEMA_AGENT_ID=e2-spec make -k check` | every CHECK_STEP green | lint clean; mypy 323 src / 572 test files 0 issues; import-linter 27 kept / 0 broken; forbidden-scan clean; ownership all `ok`; contracts-check (vendor identical, generated current); unit **3,284 passed**; conformance **757 passed**, 8 skipped; integration **561 passed**, 21 skipped; sdk-check TS 86 / Python 192 | **PASS** ("make check: all green") |
| gates affected by these items | — | — | none: docs only, no runtime behavior. The behavior behind each item is gated by its fixer's runs and the QA lead's re-gate (qa-fault for R6, qa-security SE1 for the fingerprint, qa-contract Q1 for NUL ids, C1 for the quota windows) | NOT-RUN here (no stack; nothing to measure) |

## 3. Not done, discrepancies, hand-offs

- **08:L488 design patch** (design repo, `08-technical-reference.md` §9 sizing row): the text is in IMPL-087 "Patch: doc", for the next design payload (contracts/design-docs owner). The design repo is outside this role's paths.
- **Discrepancy (edge fixer vs R7)**: R7 says "U+0000 in stored text becomes U+FFFD". The edge fixer answers **400 `schema_violation`** for U+0000 in session `metadata` keys or values (`api/middleware/gate._session_rules`). I recorded only the U+FFFD rule, for turn and decision content as implemented (IMPL-022). The orchestrator should confirm the session-metadata 400 or have it changed to U+FFFD.
- **Not in this role's item list, flagged for a later IMPL note** (no spec text changed):
  - defect 32: the admission fixer's per-model `TokenFit` table (IMPL-029c, spec-11 §2, the spec-02 §4.1 `chars_per_token` comment "4.0 for every Phase 1 model"). It asks the spec agent to record it (`admission-estimator.md` §2);
  - the waiver location: SE13 waivers now in `deploy/docker/.trivyignore.yaml`, while spec-09 §9 names `reports/security/waivers.md` (`supply-chain-infra.md` §5 item 1);
  - text-less stops (`reasoning_only`, `empty_text`) recorded as service-breaker failures, except under a client-lowered cap (`resilience-fixer.md`, fault B1);
  - the Wave E edge notes on stale key rows (`STALE_S` 600 s with Postgres and Redis down: spec-09 §2's 60 s revocation bound), the session-metadata bounds, the 10 s / 5 s DB statement bounds, and the operator proof (`0003_operator_proof`);
  - integrator CR 3 (caller deadlines are not breaker signals).
- The lean ConverseStream and the `%3A` path fix are still unverified against live Bedrock (no live calls in Wave E2); IMPL-050's amendment says so.
