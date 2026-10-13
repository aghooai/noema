# QA track io-fixer (Wave E, 2026-10-11)

Scope: the verified io defects (adapters, media, mockbedrock) handed to this role. Edits are limited to
`src/noema/{adapters,media}/**`, `tools/mockbedrock/**` and the matching tests and module docs. Changes outside that
scope are change requests (`.orchestration/requests/io-fixer.md`). This role made no live Bedrock calls and touched no
AWS resource. The shared noema-dev services were not touched. The role's own stack (`noema-e2e-iofix`, port base
29300, images `noema-{api,worker,media}:iofix`) was torn down at the end.

Every fix has a regression test. The "before" column is the measured result of that test on the unfixed code: the
test was written and run before the fix (for finding 1 the fix was also reverted and both tests re-run; for finding
21 the old `noema-media-test:sec` image was used). A test that passed before is a guard for a test gap,
not a regression test, and is marked as such.

## 1. Fixes

| Finding | Fix | Regression test | Before the fix | After |
|---|---|---|---|---|
| 1 / 4 / 8 / 10 (major): the lean path signs `%3A` and sends `:` (yarl re-quotes a str URL) | `src/noema/adapters/_unary.py` `AiohttpUnary._send`: `self._session.post(URL(url, encoded=True), ...)`, as aiobotocore sends it | `tests/integration/adapters/test_unary_transport.py::test_the_wire_path_is_the_signed_path_for_a_colon_model_id` (a `-v1:0` id and an application-inference-profile ARN, unary and stream: request-line path == signed path, and SigV4 recomputed by botocore over the received path == the `Authorization` header); `tests/integration/adapters/test_against_mockbedrock.py::test_a_versioned_model_id_is_sent_as_signed` (the real `ConverseAdapter`, `complete` and `stream`, against the mock) | 4/4 FAIL (wire `/model/arn:aws:bedrock:...%2Fabc123/converse-stream` vs signed `/model/arn%3Aaws%3A...`); the mock test FAIL with `AdapterError: unexpected provider error` (the 403 classified FATAL) | PASS |
| 10 (mock fidelity part) | `tools/mockbedrock/converse.py` + `faults.py`: a raw `:` in the Converse path is answered 403 `InvalidSignatureException`, the mismatch Bedrock would report (the mock cannot verify signatures) | `tests/unit/mockbedrock/test_converse.py::test_a_raw_colon_in_the_model_label_fails_the_signature[False,True]` | 2/2 FAIL (`assert 200 == 403`) | PASS |
| 2 / 7 (minor): `delete_prefix` ignores DeleteObjects `Errors` and re-lists forever (shielded in the worker) | `src/noema/media/quarantine.py` `ObjectStore.delete_prefix`: a per-key `Errors` entry raises `RuntimeError` naming the codes, so the job fails and the queue retries it with backoff | `tests/unit/media_pipeline/test_delete_race.py::test_a_key_s3_keeps_fails_the_delete_instead_of_spinning` (1 list + 1 delete, the deletable key gone) and `::test_a_redelivery_whose_discard_fails_fails_the_job` | 2/2 FAIL (`TimeoutError` after 2 s: still looping) | PASS |
| 3 (minor): every member and key re-read the file row and re-GOT the object | `src/noema/media/pipeline.py`: `SingleFlight`; `read_derivative` shares the row read per (principal, file) and the S3 GET per cache key while calls are in flight | `tests/unit/media_pipeline/test_file_service.py::test_concurrent_derivative_reads_share_the_row_read_and_the_object_get` (3 members x 3 keys at once); `::test_single_flight_shares_results_and_errors_but_not_a_cancelled_leader` | FAIL: 9 units of work for 9 reads (expected 1) | PASS: 1 unit of work, 3 GETs |
| 5 (major/security): one pubsub connection per watcher drains the 100-connection pool | `src/noema/media/pipeline.py` `RedisFileEvents`: one shared pubsub connection per process; channels subscribed while a file has a watcher; a waiting watcher reads it for all (`pump`, one reader at a time), dispatch after the tenant check | `tests/integration/media_pipeline/test_pg_s3_redis.py::test_watchers_share_one_pubsub_connection` (pool of 4, 24 watchers on 8 files: the pool still serves `PING`, every watcher woken by a publish from another client within 1.5 s) | FAIL: `MaxConnectionsError: Too many connections`, "file events unavailable" fallbacks | PASS; `test_foreign_tenant_file_events_are_dropped` and the watch test still PASS |
| 9 (minor, test gap): `_ConverseRaw.finish`, close-on-cancel during a body read | tests only | `tests/unit/adapters/test_converse_stream.py::test_a_body_ending_before_message_stop_is_one_malformed_truncated`; `tests/integration/adapters/test_unary_transport.py::test_cancellation_during_the_body_read_closes_the_connection` | guards: PASS before (behaviour was correct) | PASS; coverage `converse.py` 158 and `_unary.py` 152 now executed (missing only 133, 287 / 123-126, 217-218) |
| 11 (minor): non-string ConverseStream text became `TextDelta(5)` | `src/noema/adapters/converse.py` `_ConverseRaw.feed`: non-string `text` or reasoning `text` raises `StreamFault(malformed:chunk)` | `tests/unit/adapters/test_converse_stream.py::test_non_string_text_is_malformed_chunk_never_a_text_delta[text_int,text_list,text_object,reasoning_text_list]` (the whole `StreamSession`: exactly one `ErrorEvent(retryable, malformed:chunk)`, no non-str `TextDelta`) | 4/4 FAIL (no ErrorEvent) | PASS |
| 12 (minor): a late outcome of a pre-open call decided the half-open probe | `src/noema/adapters/resilience/breakers.py`: half-open counts only as many outcomes as probes were admitted since the transition (`granted`); a probe counts only when both breakers admit the call | `tests/unit/adapters/test_breakers.py::test_a_late_outcome_of_a_call_admitted_before_the_open_never_decides_the_probe[ok,error]` and `::test_an_admitted_probe_closes_on_ok_and_reopens_on_error` (replaces the test that recorded half-open outcomes without `admit`) | 2/2 FAIL (`'closed' == 'half_open'`, `'open' == 'half_open'`) | PASS; `test_state_gauge`, `test_local_transitions_are_published` and `test_resilience_redis.py::test_breaker_transitions_reach_the_other_pod` now admit the probe before recording it |
| 19 (minor/security): damaged PDFs ended `failed{extraction_failed}` | `src/noema/media/extractors/pdf_sanitize.py`: `None` entries of `pdf.objects` skipped; walk/save and the re-open map `PdfError`, `TypeError` (a non-UTF-8 name), `ValueError`, `KeyError`, `SystemError` to `Rejection("corrupt")`; `pdf_render.py`: a per-page `PdfiumError` is `corrupt` | `tests/unit/media_pipeline/test_extractors.py::test_pdf_rejections[truncated (None in pdf.objects), undecodable name key, sanitized output without /Root, pikepdf SystemError on open]` and `::test_a_page_pdfium_cannot_load_is_a_corrupt_rejection` (inputs kept in `tests/unit/media_pipeline/regressions/`, from the QA fuzz corpus) | 5/5 FAIL (`'failed' == 'rejected'`) | PASS; in-process scan (`/tmp/io_fix/sanitize_scan.py`, host libraries) of the QA fuzz corpus (`tests/security/fuzz/test_upload_fuzz.py::corpus`, same seeded RNG): before, 12 of 85 pdf.sanitize inputs and 1 of 85 pdf.render inputs raised unmapped exceptions; after, 0 of 245 and 0 of 165 (a superset of the same inputs) |
| 20 (minor): pikepdf `SystemError` on open | `_open` maps `SystemError` (pikepdf's warning logger fails on a non-UTF-8 qpdf message) to `corrupt` | `test_pdf_rejections[pikepdf SystemError on open]` | FAIL | PASS |
| 21 (minor): the SEC-SBX-08 atheris harness ran uninstrumented | `tests/integration/media_pipeline/fuzz_extractors.py`: the extractor and its lazily imported libraries load under `atheris.instrument_imports(exclude=["pikepdf"])` (with `instrument_all()`, atheris itself aborts on pdf.sanitize within 300 runs: "Unsupported call to As64 in exception handling") | `tests/integration/media_pipeline/test_media_image.py::test_sec_sbx_08_fuzz_harness_smoke[*]` now also asserts no "Is the code instrumented for coverage" warning and `cov: > 0` | 2/2 FAIL in the old image (`noema-media-test:sec`, new test mounted): the warning present | 5/5 PASS (pdf.sanitize 3,000 runs: rc 0, cov 91, corpus 21) |
| 23 (major, partly): `PROBED` claims GLM-5.3 accepts effort `none` | `src/noema/adapters/capabilities.py`: GLM accepted `low..max` incl. `xhigh`, rejected `none` (live cassettes 01/03), so `probe_discrepancies` reports the shipped config (startup WARNING) | `tests/unit/adapters/test_wire_and_registry.py::test_the_example_config_agrees_with_the_probes` now expects exactly that one drift (`KNOWN_DRIFT`, back to `[]` with the config change) | FAIL (`[] == KNOWN_DRIFT`: the drift was invisible) | PASS. The config itself (frozen contracts) and the mock's `_GLM` set wait for the owner: CR item 1 |
| 24 (minor): hidden reasoning reported as 0 reasoning tokens | `src/noema/adapters/wire.py` `reasoning_estimate(..., hidden_text_chars=)`, `converse.py`: when Bedrock hid the reasoning (`redactedContent`, or a reasoning block or signature without text) reasoning is at least `output - ceil(text chars / 4)` | `tests/unit/adapters/test_complete.py::test_hidden_reasoning_is_estimated_from_the_output_left_after_the_text[signature_only,redacted]`, `test_converse_stream.py::test_redacted_reasoning_is_estimated_from_the_output_left_after_the_text` | 3/3 FAIL (reasoning_tokens 0) | PASS (Haiku case: 40 of 40; Sol stream case: 393 - 166 = 227) |
| 25 (minor, partly): mock fidelity | `tools/mockbedrock`: (e) `outputTokens` capped at `maxTokens`; (c) the OpenAI family streams one `reasoningContent.redactedContent` delta; (d) Mantle chat JSON has `service_tier`. (a), (b), (g) are planned by the frozen `tests/fakes/scenario.py` (CR item 2); (f) is spec-10 §2.2 by design | `tests/unit/mockbedrock/test_eventstream.py::test_output_tokens_never_exceed_max_tokens[True]`, `::test_openai_family_reasoning_is_one_redacted_delta`; `test_mantle.py::test_chat_json_has_a_service_tier` | 3/3 FAIL (`410 <= 256`, 3 reasoning-text deltas, no `service_tier`); the JSON case of the cap passed before | PASS |

Already fixed before this role (re-checked, no change): findings 13, 14, 15 and 16. Their cited tests pass in the
`test-unit` and `test-integration` runs below.

## 2. Not fixed here (outside the io scope or waiting for an owner ruling)

Each one is a change request in `.orchestration/requests/io-fixer.md`.

| Finding | Why not here | CR |
|---|---|---|
| 23 config part: `platform.example.yaml` lists GLM effort `none`, and race-sol-glm runs GLM at `none` | frozen contracts (platform) plus an owner decision; changing the mock's `_GLM` first would turn e2e G06 red | item 1 |
| 25 (a), (b), (g): Claude hidden JSON reasoning, Opus/GLM reasoning at `low`, the probe latency table | planned by the frozen `tests/fakes/scenario.py` (fakes-telemetry) | item 2 |
| 5 rest: no watchFile concurrency cap; every watcher re-reads its row each 2 s (spec-07 §3.1); the Redis pool size | `api/routes/files.py` cap through ops/admission, `composition.py` (integrator) | item 3 |
| 3 rest: one `DerivativeReader` per member | `decision/views.py` (decision) | item 4 |
| 22: the retry budget refuses most pre-commit fallbacks, so S1/F1 >= 99 % fails | IMPL-016 as written (10 %/min, floor 10) conflicts with 06 §7 F1; owner ruling | item 5 |
| 6 (core/text.py escaping), 17 (NUL ids -> 500), 18 (openapi upload media type) | frozen core / api, db / contracts | item 6 |

## 3. Gates

The tree was shared with other Wave E fixers during the run. The failures below are all in their files
(api, identity, db, ops_admission and the QA tracks' own paths); every file of this role passes its step.

| Gate (06 §11 / spec-10) | Command | Pass criterion | Measured | Result |
|---|---|---|---|---|
| spec-02 §4.1/§4.2 wire: SigV4 on the wire for every id the schema allows (06 §11 Phase 1 contract tests; L1/L2 for versioned ids and ARNs) | `pytest tests/integration/adapters/test_unary_transport.py tests/integration/adapters/test_against_mockbedrock.py` | request-line path == signed path; botocore SigV4 over the received path == `Authorization`; real adapter vs mock 200 for a `-v1:0` id | 10 + 23 passed (before the fix: 4 + 1 failed) | PASS (not verified against live Bedrock: this role has no live-call budget) |
| F8 (malformed stream, spec-10 §4) for ConverseStream non-string text | `pytest tests/unit/adapters/test_converse_stream.py` | exactly one `ErrorEvent(retryable, malformed:chunk)`, no non-str `TextDelta` | 4 cases passed (before: 4 failed) | PASS |
| F5/F8 truncated stream before `messageStop` | same file | exactly one `ErrorEvent(retryable, malformed:truncated)` | passed (passed before too: guard) | PASS |
| F3 breaker half-open (design 04 §2: the probe decides) | `pytest tests/unit/adapters/test_breakers.py tests/integration/adapters/test_resilience_redis.py` | a pre-open call's late outcome leaves half-open; an admitted probe closes / re-opens | 26 + 3 passed (before: 2 failed) | PASS (F3 fault run itself NOT-RUN here: the fault track's) |
| 06 §9 Uploads: no failed job for a damaged PDF (spec-09 §6.3 `rejected{corrupt}`) | `pytest tests/unit/media_pipeline/test_extractors.py`; `make test-media IMAGE_TAG=iofix` | every damaged input `rejected{corrupt}` | host 59 passed; media image 305 passed, 8 skipped (the Postgres/Redis tests need the network; they ran on the host) | PASS |
| SEC-SBX-08 fuzz harness has coverage feedback | `test_media_image.py::test_sec_sbx_08_fuzz_harness_smoke` in the media image | rc 0, no "Is the code instrumented" warning, `cov: > 0` | 5/5 passed (before: the warning on every kind) | PASS (the nightly fuzz job itself NOT-RUN) |
| 06 §5 Redis/Postgres under watchers: watchers do not drain the Redis pool (finding 5) | `pytest tests/integration/media_pipeline/test_pg_s3_redis.py` (dev Valkey and Postgres) | 24 watchers with a 4-connection pool: `PING` served, all woken within 1.5 s | 8 passed (before: `MaxConnectionsError`) | PASS for the pool; the per-project watchFile cap is NOT-RUN (CR item 3) |
| media decision fan-out (06 §5 decision fan-out; finding 3) | `pytest tests/unit/media_pipeline/test_file_service.py` | 9 concurrent reads: 1 row read, 3 GETs | 1 and 3 (before: 9 row reads) | PASS (efficiency; `decision_ms` not measured) |
| 06 §9 SEC / retention: purge never spins (finding 2) | `pytest tests/unit/media_pipeline/test_delete_race.py` | a key S3 keeps raises within 2 s after 1 list + 1 delete | passed (before: still looping at 2 s) | PASS |
| usage telemetry (finding 24, IMPL-029c estimate) | `pytest tests/unit/adapters/test_complete.py tests/unit/adapters/test_converse_stream.py` | hidden reasoning estimated from the output left after the text | 3 cases passed (before: reasoning_tokens 0) | PASS |
| mock fidelity (spec-10 §2.2-2.3, finding 25 c/d/e) | `pytest tests/unit/mockbedrock` | outputTokens <= maxTokens; Sol one `redactedContent` delta; Mantle `service_tier`; raw `:` -> 403 | 242 passed (before: 5 failed) | PASS for c/d/e; a/b/g NOT-RUN (CR item 2) |
| S1 / F1 fallback success >= 99 % (finding 22) | none (owner ruling needed) | >= 99 % | not changed | NOT-RUN (CR item 5) |
| make check: lint | `make lint` | clean | All checks passed | PASS |
| make check: typecheck | `make typecheck` | 0 errors | 1 error, `src/noema/api/deps.py:249` (api) | FAIL (not this role's file; adapters and media clean) |
| make check: typecheck-tests | `make typecheck-tests` | 0 errors | 7 errors in 4 files: `tests/integration/ops_admission/test_failover.py`, `tests/unit/api/test_ingress_hardening.py`, `tests/integration/identity/test_revocation.py`, `tests/integration/db/test_admin_keys_calibrators.py` | FAIL (not this role's files) |
| make check: importlint, forbidden-scan, contracts-check | the three targets | clean | 27 contracts kept; clean; vendor identical, generated up to date | PASS |
| make check: ownership | `make ownership` | no unowned path | 19 unowned: `make/qa-*.mk`, `tests/qa/contract/*` (QA tracks; qa CR item 1); every path of this role "ok" | FAIL (not this role's files) |
| make check: test-unit | `make test-unit` | green | 3,183 passed, 1 failed: `tests/unit/identity/test_cli_refusals.py::test_operator_keys_on_the_previous_pepper` (identity) | FAIL (not this role's file) |
| make check: test-conformance | `make test-conformance` | green | 757 passed, 8 skipped | PASS |
| make check: test-integration | `make test-integration` | green | 546 passed, 21 skipped, 1 failed: `tests/integration/api/test_uvicorn_server.py::test_a_chunked_body_is_cut_off_at_the_cap` (api) | FAIL (not this role's file) |
| make check: sdk-check | `make sdk-check` | green | TS 86 passed / 6 skipped; Python 192 passed / 1 skipped | PASS |
| e2e (spec-10 §1), run twice | `IMAGE_TAG=iofix NOEMA_API_IMAGE=noema-api:iofix NOEMA_WORKER_IMAGE=noema-worker:iofix NOEMA_MEDIA_IMAGE=noema-media:iofix make e2e E2E_PROJECT=noema-e2e-iofix E2E_PORT_BASE=29300` (second run `E2E_BUILD=0`) | 52/52 both runs, every response contract-validated | 52 passed (155.7 s), 52 passed (154.2 s) | PASS |

## 4. Files

Code: `src/noema/adapters/{_unary.py,converse.py,wire.py,capabilities.py,resilience/breakers.py}`,
`src/noema/media/{quarantine.py,pipeline.py,extractors/pdf_sanitize.py,extractors/pdf_render.py}`,
`tools/mockbedrock/{converse.py,faults.py,mantle.py}`. Tests: `tests/integration/adapters/{test_unary_transport.py,
test_against_mockbedrock.py,test_resilience_redis.py}`, `tests/unit/adapters/{test_converse_stream.py,
test_complete.py,test_breakers.py,test_wire_and_registry.py,test_lean_converse_stream.py}`,
`tests/unit/mockbedrock/{test_converse.py,test_eventstream.py,test_mantle.py}`,
`tests/unit/media_pipeline/{test_delete_race.py,test_extractors.py,test_file_service.py,regressions/*.pdf}`,
`tests/integration/media_pipeline/{test_pg_s3_redis.py,test_media_image.py,fuzz_extractors.py}`. Docs:
`docs/modules/{adapters.md,media_pipeline.md,qa-tools.md}`. Change requests: `.orchestration/requests/io-fixer.md`.

Note for the QA security track: `tests/security/fuzz/harness.py` wraps the same target with
`atheris.instrument_all()`. That call, tried first in the media harness, made atheris abort on pdf.sanitize
("Unsupported call to As64 in exception handling", libFuzzer "fuzz target exited", within 300 runs in
`noema-media-test:sec`). The QA harness itself was not run by this role, but it makes the same call, so it likely
hits the same abort. The media harness now instruments the extractor imports, pikepdf excluded.
