# 08 — Contract patches for `docs/noema` (rev 5 → rev 6)

> **Consolidation status (2026-10-09).** §14 (consolidation addendum) supersedes §1–§13 and both appendices where they overlap. The applier merges §14 into the payloads and regenerates the §1 hashes (IMPL-067). Decision ids: IMPL-CON-n → canonical IMPL-NNN per [`../IMPL-DECISIONS.md`](../IMPL-DECISIONS.md) (Traceability).

These are the exact, mechanically applicable patches behind decisions IMPL-CON-01…18 ([`../impl-decisions/contracts.md`](../impl-decisions/contracts.md)). They target `/codes/aghooai/docs/noema`, which stays read-only until the owner checkpoint. The contract-patch applier applies them afterwards. Nothing here calls a model or AWS.

The payload is in two parts:
- **Appendix A**: the complete new `contracts/openapi.yaml`. The file is replaced whole, because its unified diff (1,924 lines) is longer than the file (1,905 lines).
- **Appendix B**: one unified diff (`patch -p1`, paths relative to `docs/noema`) for every other changed file: contracts, `qa/`, `check_docs.sh` and the prose docs.

The 5 new example files are not in the diff. `contracts/make_examples.py`, as patched, generates them; step 4 below does that, and the hashes in §1 identify them.

## 0. How to apply (and the evidence that it works)

```bash
set -euo pipefail
MD=/codes/aghooai/noema/docs/impl-spec/08-contract-patches.md
cd /codes/aghooai/docs/noema
# 1. extract the two payloads
python3 -I - "$MD" <<'EOF'
import pathlib, re, sys
md = pathlib.Path(sys.argv[1]).read_text()
def block(name):
    m = re.search(r"<!-- BEGIN %s -->\n````[a-z]*\n(.*?)````\n<!-- END %s -->" % (re.escape(name), re.escape(name)), md, re.S)
    return m.group(1)
pathlib.Path("/tmp/noema-rev6.patch").write_text(block("noema-rev6.patch"))
pathlib.Path("/tmp/noema-rev6-openapi.yaml").write_text(block("contracts/openapi.yaml"))
EOF
# 2. check the base, then patch
patch -p1 --dry-run < /tmp/noema-rev6.patch
patch -p1 < /tmp/noema-rev6.patch
# 3. replace openapi.yaml
cp /tmp/noema-rev6-openapi.yaml contracts/openapi.yaml
# 4. regenerate the examples (adds 5 files; the 43 existing files come out byte-identical)
(cd contracts && python3 -I make_examples.py)        # -> "examples written: 48"
# 5. verify
sha256sum contracts/openapi.yaml                       # ea30e652e17eed6fdb22a9f6ed7fbebafd8560e7066a57ea69e3d9c64cf15a56
./check_docs.sh                                        # every line PASS, exit 0
```

Verified on 2026-10-09 in scratch copies under `/tmp` (deleted afterwards):
- **The procedure reproduces the verified tree.** The extraction and steps 2–4 run on a fresh copy of `docs/noema` produce a tree byte-identical to the one that passed every check (`diff -r` empty).
- **`check_docs.sh` passes every step** on the patched tree: `mermaid PASS` ×11, `tsc PASS`, `schema PASS` (48 examples), `pydantic PASS`, `config PASS`, `audit PASS` (41/41), `openapi PASS` (Redocly 2.62.1 = `@latest`, 0 warnings), `bundle PASS` (new step, 0 warnings), `prose PASS`, `links PASS`. Exit 0.
- **Codegen works on the bundle:**
  - `openapi-typescript@7.13.0` output is assignable to the contract TS types (§10.3: 18 of 18 assertions compile). A mutation test (a wrong `last_seq` type) is caught.
  - `datamodel-code-generator==0.83.0` with the §10.4 flags plus pydantic 2.14.0 accepts and rejects the 48 examples exactly as `jsonschema` does. The one exception is the `if/then` "calibrated ⇒ holdout" rule, a known model looseness (IMPL-CON-10); the request gate runs first. The `TurnEvent` discriminator resolves to the named class.
  - `jsonschema-rs` agrees with `jsonschema` 4.23 on all 48 examples plus null, duplicate, type and reserved-name mutations.
- **Per-operation consistency:** for each of the 23 authenticated operations, the statuses implied by `x-noema-error-codes` equal the declared 4xx/5xx responses.

## 1. Files touched

The hashes are the first 16 hex characters of SHA-256. Step 2 fails on a base mismatch: if `patch --dry-run` rejects a hunk, `docs/noema` changed after this patch was cut.

**Regenerated 2026-10-10 for the combined rev-6 payload (§14.0 step 5).** "Before" is the rev-5 base (`/codes/aghooai/.backups/docs-noema-rev5-2026-10-09.tgz`); "After" is the combined tree that passed every check: `check_docs.sh` exit 0 (`mermaid PASS` ×11, `tsc`, `schema` 54 examples, `pydantic`, `config` 49 negative + 1 positive, `audit` 49/49, `openapi` 0 warnings, `bundle`, `prose`, `links`), the §10.3 wire-compat check (18 of 18 assertions; a `last_seq` mutation is caught) and the `jsonschema-rs` differential (1,280 documents: the 54 examples plus null, type, duplicate and reserved-name mutations, 0 disagreements). Full `openapi.yaml` sha256: `2ea1c827a5ad6534d0bbe8e7bbd38ff917d33c2759d8c195a0c50d48e61811ca`. Appendix A and B are the pre-§14 payloads; their own hashes (§0) are unchanged.

| File | Before | After | Change |
|---|---|---|---|
| `contracts/openapi.yaml` | `9fdff40204346eae` | `2ea1c827a5ad6534` | Replaced (Appendix A) plus §14.1: facade and `createDecision` descriptions, facade 409 codes, `served_spec_differs`, `CalibratorInfo`/`listCalibrators`; `x-noema-roles` on all 23 authenticated operations (IMPL-066). §2–§9 |
| `contracts/decision.schema.json` | `e0b2858967ef8fea` | `52503903ca9f59b0` | Appendix B plus §14.2: `AnswerName` (CP-DEC-1), `EscalationStage.input_view_signature`/`prompt_templates` (CP-DEC-2), `profile` description |
| `contracts/openai-compat.schema.json` | `2feefa8950b19aaa` | `c5ce2e84d44104d6` | Appendix B plus §14.3 `x_aghoo` description |
| `contracts/types.ts` | `bb67df73cd8b8a05` | `a2942a2e52af9c1e` | Appendix B plus §14.7 comments (`status`, `signature`, `served_spec_differs`, `HeuristicConfidence`, `name`, `profile`) and the CP-DEC-2 `EscalationStage` fields |
| `contracts/sdk.ts` | `bef898ea82cf67b2` | `44d91ecab0a0e7ac` | Appendix B plus §14.7 `CalibratorStatus`/`CalibratorInfo` |
| `contracts/sdk-usage.check.ts` | `d9563d79065ce131` | `f5e05a89e866cf25` | Appendix B (no §14 change) |
| `contracts/config.schema.json` | `448b74cc9ea1f91b` | `288e01d3b82206ea` | §14.4: `platform`, `Platform`, `DataClassPolicy`, `Tenant.limits`, `Labeling.acceptance`, `Model.context_window`; `family_overlap_lift_max` maximum 0.1 |
| `contracts/check_config.py` | `e63e47b4909b0879` | `cea6addbf2270f43` | §14.4: P1-C1…P1-C4 with negative tests (45 → 49); P1-C2 tightens the rev-5 band rule |
| `contracts/check_examples.py` | `35aa4fa70696f9a3` | `34a8a885fbc17535` | Appendix B plus §14.5 (config pass-through, censored `.get`, CP-DEC-2 `answering_stage`, `JEV_PAIRS`, `OA_PAIRS`, exact hierarchical expansion); `PAIRS` censored entry; `jev_invariants` numeric key-order fix |
| `contracts/make_examples.py` | `09c27bb3b893aec8` | `5a9d73ae57dce468` | Appendix B plus §14.6: writes 11 new examples; hierarchical confidence from unrounded values; CP-DEC-2 stage fields |
| `contracts/python_sdk_types.py` | `53cf68f7bbdaebd7` | `82c06aaac8b0ac2c` | `ChoiceAnswer` group check uses the exact integer expansion rule (IMPL-027 (d)) |
| `qa/run_design_audit.py` | `5e5ea59c5d843a72` | `069b3b057554f363` | Appendix B loader plus §14.8 cases 1–8 (41 → 49) |
| `check_docs.sh` | `d11a6cc13cee0d58` | `d295d980421d9d77` | New `bundle` step |
| `README.md` | `aae01681a561adcc` | `e99b7c73df4f4589` | Status line and Revision 6 note; verify comment: lint + bundle |
| `01-feasibility-study.md` | `dde35763c9f71615` | `e1651eed55de968d` | Revision 6 note; counts; per-tenant STS wording |
| `02-architecture.md` | `49f243c452865f84` | `8bee9efaf7e71106` | Revision 6 note; DOCX 415 code; §14.9 rows for 02 (ER `tenant_id`, diagrams, formula, persistence, Phase 2 items) |
| `03-contracts.md` | `4f3990eb29dc9bdb` | `8d52cb504ec1aa0b` | Revision 6 note; Appendix B text; §14.9 rows for 03; the §14.6 SSE example |
| `04-caching-routing-security.md` | `95881287cee9133d` | `d29c0e9a34e8157c` | Revision 6 note; §14.9 rows for 04; config negative-test count |
| `05-implementation-plan.md` | `c785553ecf0b64bc` | `efcb79e6165818e9` | Revision 6 note; SDK wording; Phase 1 hierarchical method (L75) |
| `07-api-user-guide.md` | `866ceb37d6435f51` | `4a07f968929f542c` | Revision 6 note; Appendix B text; `decision.completed` status wording; event order |
| `08-technical-reference.md` | `8d7ebec89da5bd5e` | `ce150495f901820a` | Revision 6 note; Appendix B text; §14.9 rows for 08; §13 counts |
| `contracts/examples/decision-request.valid-default-profile.json` | new | `f5050d037be956bc` | No `profile` (IMPL-CON-02) |
| `contracts/examples/openai-request.invalid-21-images.json` | new | `68391ab170cd65e7` | 21 images (IMPL-CON-08) |
| `contracts/examples/openai-request.invalid-remote-image-url.json` | new | `2d1ca83f77e59397` | http(s) image (IMPL-CON-12) |
| `contracts/examples/openai-response.valid-ensemble.json` | new | `2b4e7b7803b2d813` | Typed `x_aghoo` with `mapped_unavailable` |
| `contracts/examples/openai-response.invalid-x-aghoo-mapped-not-refusal.json` | new | `f673783218f42f33` | `mapped_unavailable` names a non-refusal |
| `contracts/examples/decision-request.valid-censored.json` | new | `8b504b661a3c29e4` | Request for the censored result (§14.6) |
| `contracts/examples/decision-result.valid-censored.json` | new | `0d4f5c36cc93199a` | T-LP-1 member with `censored_options` (§14.6) |
| `contracts/examples/decision-request.valid-jev-k16.json` | new | `3bc03d27eeeee7c9` | 16-option choice, 16-level score (IMPL-049) |
| `contracts/examples/decision-jev-response.valid-k16.json` | new | `b2c754d8439fc3f0` | Jev answer from the 2-dp map (IMPL-049) |
| `contracts/examples/openai-request.valid-non-pattern-name.json` | new | `43e79583f96f32b3` | Name `"Severity Level"` (IMPL-047) |
| `contracts/examples/openai-response.valid-non-pattern-name.json` | new | `667453953bffa424` | `x_aghoo` keyed by `"Severity Level"` (IMPL-047) |
| `contracts/examples/decision-result.valid-escalated.json` | `f19b58d7e5094288` | `479a35a35317ba02` | CP-DEC-2 stage fields (§14.6) |
| `contracts/examples/decision-result.invalid-image-no-adequate-view.json` | `d8b9f7481c085c19` | `4e957278a43e2662` | Derived from the escalated result: same stage fields, same rejection |
| `contracts/examples/decision-result.valid-hierarchical.json` | `bbb10b6729a9abe3` | `68a92bd8005455a1` | `normalized_entropy` 0.749 → 0.7489 (§14.6) |
| `contracts/examples/decision-result.invalid-hierarchical-unresolved-renormalized.json` | `45675a38ac5997e7` | `e894b2254f09bb13` | Same field (§14.6) |
| `contracts/examples/decision-result.invalid-flat-over-member-limit.json` | `d6c67acf0ecdfd3b` | `88bdea11e1f98f30` | Same field (§14.6) |
| `contracts/examples/decision-result.invalid-hierarchical-coverage.json` | `d188df1843512115` | `2ee0c2e1f3170ebb` | `normalized_entropy` 0.6271 → 0.627, `margin` 0.1919 → 0.192 (§14.6) |

Not touched:
- `openai-compat.ts`, `tsconfig.json`, `examples/platform.example.yaml`;
- the other 37 existing example files;
- `06-validation-plan.md`, `09-qa-report.md`, `10-design-qa-api-ensemble.md`;
- `qa/audit-results.json` (the recorded rev-5 run, 41/41).

## 2. ErrorCode → HTTP status → `error.type` → retryable (complete)

Each code has exactly one row. `x-noema-error-codes` on each operation lists which codes that operation may return.

| code | HTTP | error.type | retryable | new in rev 6 |
|---|---|---|---|---|
| schema_violation | 400 | invalid_request_error | no | |
| unsupported_question_type | 400 | invalid_request_error | no | |
| reserved_question_name | 400 | invalid_request_error | no | |
| model_not_available | 400 | invalid_request_error | no | |
| invalid_api_key | 401 | authentication_error | no | |
| invalid_end_user_token | 401 | authentication_error | no | yes |
| permission_denied | 403 | permission_error | no | yes |
| profile_not_allowed | 403 | permission_error | no | |
| session_forbidden | 403 | permission_error | no | |
| not_found | 404 | not_found_error | no | yes |
| session_not_found | 404 | not_found_error | no | |
| idempotency_key_reused | 409 | conflict_error | no | |
| session_conflict | 409 | conflict_error | no | |
| session_busy | 409 | conflict_error | yes | |
| file_not_ready | 409 | conflict_error | yes | |
| file_rejected | 409 | conflict_error | no | |
| resume_window_expired | 410 | invalid_request_error | no | |
| request_too_large | 413 | invalid_request_error | no | yes |
| file_too_large | 413 | invalid_request_error | no | |
| image_too_large | 413 | invalid_request_error | no | |
| unsupported_media_type | 415 | invalid_request_error | no | |
| no_route_satisfies_constraints | 422 | capability_error | no | |
| no_capable_member | 422 | capability_error | no | |
| unsupported_parameter | 422 | capability_error | no | |
| feature_not_available | 422 | capability_error | no | yes |
| rpm_exceeded | 429 | rate_limit_error | yes | |
| tpm_exceeded | 429 | rate_limit_error | yes | |
| queue_timeout | 429 | rate_limit_error | yes | |
| budget_exhausted | 429 | rate_limit_error | no | |
| internal_error | 500 | server_error | yes | yes (+ type `server_error`) |
| not_implemented | 501 | invalid_request_error | no | yes |
| all_routes_unavailable | 503 | upstream_error | yes | |
| tenant_access_misconfigured | 503 | upstream_error | no | |
| deadline_exceeded | 504 | upstream_error | yes | |
| upstream_stalled | SSE only | upstream_error | yes | |
| client_too_slow | SSE only | upstream_error | no | |

Before → after excerpt (`components.schemas`; the full text is in Appendix A):

```yaml
# before
    ErrorCode:
      type: string
      enum: [schema_violation, ..., resume_window_expired, session_conflict, session_busy]
# after: the same 29 values, in the same order, then
             not_found, permission_denied, invalid_end_user_token, request_too_large, feature_not_available, internal_error,
             not_implemented]
# Error.error.type enum gains server_error; Error and Error.error gain additionalProperties: false;
# Error.error.required: [type, code, message, retryable] -> [type, code, message, param, request_id, retryable]
```

types.ts adds the same union (`export type ErrorCode = ...`), `ErrorType`, and `ErrorEnvelope { error: { type: OpenEnum<ErrorType>; code: OpenEnum<ErrorCode>; message; param: string | null; request_id; retryable; details? } }`.

## 3. Error responses per operation

The old generic `components.responses.Error` is replaced by per-status responses, each carrying `x-request-id`:

| Response | Status | Extra headers |
|---|---|---|
| `BadRequest` | 400 | — |
| `Unauthorized` | 401 | `www-authenticate` |
| `Forbidden` | 403 | — |
| `NotFound` | 404 | — |
| `Conflict` | 409 | — |
| `Gone` | 410 | — |
| `PayloadTooLarge` | 413 | — |
| `UnsupportedMediaType` | 415 | — |
| `Unprocessable` | 422 | — |
| `RateLimited` | 429 | `retry-after` + the 6 rate-limit headers |
| `InternalError` | 500 | — |
| `NotImplemented` | 501 | — |
| `Unavailable` | 503 | `retry-after` |
| `GatewayTimeout` | 504 | — |

Header column: rl = the six `x-ratelimit-*` headers on 2xx; replay = `x-aghoo-idempotent-replay`; idem = accepts `Idempotency-Key`; deadline = accepts `Aghoo-Deadline-Ms`; enduser = accepts `x-aghoo-end-user`. Every authenticated operation also accepts `Aghoo-Version`, `x-request-id`, `traceparent` and `tracestate`, and returns `x-request-id`.

| Operation | operationId | Responses (rev 6) | # codes | Headers |
|---|---|---|---|---|
| `POST /v1/turns` | createTurn | 200, 400, 401, 403, 404, 409, 410, 413, 415, 422, 429, 500, 503, 504 | 31 | rl replay deadline idem enduser |
| `GET /v1/turns/{turn_id}` | getTurn | 200, 401, 404, 422, 429, 500 | 6 | enduser |
| `POST /v1/turns/{turn_id}/cancel` | cancelTurn | 202, 401, 404, 409, 422, 429, 500 | 7 | replay idem enduser |
| `GET /v1/turns/{turn_id}/events` | resumeTurnEvents | 200 (Phase 2), 401, 404, 410, 422, 429, 500, **501** | 8 | enduser |
| `POST /v1/decisions` | createDecision | 200, 400, 401, 403, 404, 409, 413, 415, 422, 429, 500, 503, 504 | 25 | rl replay deadline idem enduser |
| `GET /v1/decisions/{decision_id}` | getDecision | 200, 401, 404, 422, 429, 500 | 6 | enduser |
| `POST /openai/v1/decisions` | createDecisionOpenAICompat | 200, 400, 401, 403, 409, 413, 415, 422, 429, 500, 503, 504 | 21 | rl replay deadline (new) idem enduser |
| `GET /v1/profiles` | listProfiles | 200, 400, 401, 422, 429, 500 | 6 | enduser |
| `POST /v1/sessions` | createSession | 201, 400, 401, 403, 409, 413, 415, 422, 429, 500 | 10 | replay idem enduser |
| `GET /v1/sessions` | listSessions | 200, 400, 401, 422, 429, 500 | 6 | enduser |
| `GET /v1/sessions/{session_id}` | getSession | 200, 401, 404, 422, 429, 500 | 6 | enduser |
| `PATCH /v1/sessions/{session_id}` | updateSession | 200, 400, 401, 403, 404, 413, 415, 422, 429, 500 | 11 | enduser |
| `DELETE /v1/sessions/{session_id}` | deleteSession | 202, 401, 403, 404, 422, 429, 500 | 8 | enduser |
| `GET /v1/sessions/{session_id}/turns` | listSessionTurns | 200, 400, 401, 404, 422, 429, 500 | 7 | enduser |
| `POST /v1/files` | uploadFile | 202, 400, 401, 403, 404, 409, 413, 415, 422, 429, 500 | 12 | replay idem enduser |
| `GET /v1/files/{file_id}` | getFile | 200, 401, 404, 422, 429, 500 | 6 | enduser |
| `DELETE /v1/files/{file_id}` | deleteFile | 200, 401, 403, 404, 422, 429, 500 | 7 | enduser |
| `GET /v1/files/{file_id}/events` | watchFile | 200, 401, 404, 422, 429, 500 | 6 | enduser |
| `GET /v1/files/{file_id}/derivatives/{kind}` | getFileDerivative | 200, 400, 401, 404, 409, 422, 429, 500 | 9 | enduser |
| `GET /v1/usage` | getUsage | 200, 400, 401, 403, 422, 429, 500 | 7 | enduser |
| `GET /v1/admin/calibrators` | listCalibrators (new) | 200, 400, 401, 403, 422, 429, 500 | 7 | enduser |
| `GET /v1/admin/labeling/runs` | listLabelingRuns (new) | 200, 400, 401, 403, 422, 429, 500 | 7 | enduser |
| `GET /v1/admin/labeling/dropped` | listLabelingDropped (new) | 200, 400, 401, 403, 422, 429, 500 | 7 | enduser |
| `GET /healthz`, `GET /readyz` | healthz, readyz | unchanged (no auth, no envelope) | — | — |

The gap-32 items specifically:

| Operation | Added statuses |
|---|---|
| createDecision | 404 (`session_not_found`, `not_found` for file ids), 409 (`idempotency_key_reused`, `file_not_ready`, `file_rejected`), 504 (deadline expired before any member call started) |
| facade | 403 (`profile_not_allowed`, `permission_denied`), 409 (`idempotency_key_reused`), 422 (hierarchical → `unsupported_parameter`), 503 (every answer unavailable → `all_routes_unavailable`), 504 |
| cancelTurn | 409 (`idempotency_key_reused`) |
| createTurn | 410 (an Idempotency-Key replay after the event log expired), 413, 415, 504 |

All operations above also gained 401 and 500.

## 4. Header declarations (`components.parameters` / `components.headers`)

| Name | Where | Declaration (exact) |
|---|---|---|
| `Aghoo-Version` (request) | every authenticated operation | `schema: { type: string, enum: ['2026-10-01'], default: '2026-10-01' }`. Current = default = the only supported value; other values are 400 `schema_violation` (`param: Aghoo-Version`). `info.description` lists the versions |
| `Aghoo-Deadline-Ms` (request) | createTurn, createDecision, **facade (new)** | `{ type: integer, minimum: 100, maximum: 600000, default: 120000 }` |
| `Idempotency-Key` (request) | every POST | `{ type: string, minLength: 1, maxLength: 128, pattern: '^[\x21-\x7E]+$' }` |
| `x-request-id` (request, `RequestIdHeader`) | every authenticated operation | `{ type: string }`. A valid value (1–128 of `[A-Za-z0-9._:-]`) is echoed; any other value is replaced, never rejected |
| `traceparent`, `tracestate` (request) | every authenticated operation | `{ type: string }`. An invalid value starts a new trace, never rejected |
| `x-aghoo-end-user` (request, `EndUser`) | every authenticated operation | `{ type: string }`. Invalid → 401 `invalid_end_user_token`; a project without end-user config → 422 `feature_not_available` |
| `x-request-id` (response, `RequestId`) | every 2xx and every error response | `required: true`, `{ type: string }`; equals `error.request_id` |
| `x-ratelimit-{limit,remaining,reset}-{requests,tokens}` (response) | 2xx of createTurn, createDecision, facade; every 429 | integers ≥ 0; `reset` in whole seconds, rounded up; the most constraining bucket |
| `retry-after` (response) | 429, 503 | `{ type: integer, minimum: 1 }`; absent for `budget_exhausted` / `tenant_access_misconfigured` |
| `x-aghoo-idempotent-replay` (response) | 2xx of every POST | `{ type: string, enum: ['true'] }`; present only on a replay or attach |
| `www-authenticate` (response) | 401 | `{ type: string }` |
| `content-disposition`, `x-content-type-options` (response) | getFileDerivative 200 | `attachment; filename="<file_id>-<kind>[-<page>].<ext>"`; `nosniff` |

The rows marked (request) are `components.parameters`, referenced from each operation's `parameters`; the rows marked (response) are `components.headers`.

## 5. `TurnEvent` discriminator and SSE modelling

Before: an inline `oneOf` of 15 anonymous objects, no discriminator, and the `error` variant's code was `string` in TS.

After:
- 15 named, closed schemas: `TurnCreatedEvent`, `PreflightCompletedEvent`, `RouteSelectedEvent`, `DecisionCompletedEvent`, `DecisionUpdatedEvent`, `RouteConfirmedEvent`, `GenerationStartedEvent`, `GenerationDeltaEvent`, `GenerationCommittedEvent`, `GenerationCancelledEvent`, `GenerationReroutedEvent`, `GenerationCompletedEvent`, `TurnVerifiedEvent`, `TurnCompletedEvent`, `ErrorEvent`.
- Each variant's fields and enums are unchanged; only `additionalProperties: false` and `type: { type: string, const: ... }` are added.

```yaml
    TurnEvent:
      description: |
        One SSE `data:` payload. Each frame is `id: <n>` (monotonic per turn), `event: <type>` and one line of
        `data: <JSON>`. A `: ping` comment is sent whenever no frame was written for 15 s. Clients ignore unknown event types.
      oneOf:
        - { $ref: '#/components/schemas/TurnCreatedEvent' }
        # ... one $ref per event ...
        - { $ref: '#/components/schemas/ErrorEvent' }
      discriminator:
        propertyName: type
        mapping:
          turn.created: '#/components/schemas/TurnCreatedEvent'
          # ... one entry per event ...
          error: '#/components/schemas/ErrorEvent'
```

types.ts change:

```ts
// before
  | { type: "error"; code: string; message: string; retryable: boolean };
// after
  | { type: "error"; code: OpenEnum<ErrorCode>; message: string; retryable: boolean };
```

File events (watchFile 200 description):
- each frame is `id: <n>` (1, 2, … per stream), `event: file.updated`, then one line of `data: <FileObject JSON>`;
- the first frame is the current snapshot;
- the server closes the stream after the first snapshot with status `ready`, `partial`, `rejected`, `failed` or `deleted`;
- a `: ping` comment is sent whenever no frame was written for 15 s.

## 6. `DecisionCreateParams.profile` optional (gap 31)

```json
// decision.schema.json  $defs.DecisionCreateParams
// before
      "required": ["profile", "input", "questions"],
        "profile": { "type": "string", "pattern": "^[a-z0-9][a-z0-9._-]{0,63}(@[0-9]+)?$" },
// after
      "required": ["input", "questions"],
        "profile": { "type": "string", "pattern": "^[a-z0-9][a-z0-9._-]{0,63}(@[0-9]+)?$",
          "description": "Decision profile id, optionally version-pinned (router-default@1). Optional: the default is the tenant's defaults.decision_profile (lightweight-model-decide in the shipped configuration, D6). Must be the default or in the tenant's request_overrides allowlist (403 profile_not_allowed)." },
```

types.ts: `profile: string;` → `profile?: string;`, with the same doc comment. New example: `decision-request.valid-default-profile.json`.

## 7. Out-of-phase request values in Phase 1 (gap 36)

The list is exhaustive. Every row except the last is in a schema `description` or an operation description in Appendix A.

| Value | Phase-1 response | `param` |
|---|---|---|
| `strategy` = `speculative` / `hedge` | 422 `feature_not_available` | `strategy` |
| `generation.profile` = a `best_of_n` / `fuse` profile (these are also omitted from `GET /v1/profiles`) | 422 `feature_not_available` | `generation.profile` |
| A choice question with more options than the answering profile's smallest member `max_choices`: facade / `output_format: "jev"` only | 422 `unsupported_parameter` (permanent: no field for `unresolved_mass`). **Superseded 2026-10-10 (§14.0):** native requests and turn app questions are answered hierarchically, with no 422 | `questions[i].choices` |
| `retention` = `custom` (create / update session) | 422 `feature_not_available` | `retention` |
| `x-aghoo-end-user` on a project without end-user config | 422 `feature_not_available` | `x-aghoo-end-user` |
| Any `GET /v1/turns/{id}/events` | 501 `not_implemented` | null |
| DOCX / SVG / audio / video uploads | 415 `unsupported_media_type` (unchanged) | `file` |
| A bearer token that is not an API key (OIDC) | 401 `invalid_api_key` (securityScheme description) | null |

The OIDC row is in the `bearerAuth` description rather than in a schema.

## 8. Type fixes (gap 37) and the facade (IMPL-CON-12)

```yaml
# openapi.yaml ProfileInfo.version
# before
        version: { type: integer }
# after
        version: { type: string, pattern: '^[0-9]+$', description: Same form as provenance.profile_version }
```

```json
// decision.schema.json InputPart (input_image, data URL)
// before
              "pattern": "^data:image/(png|jpeg|webp|gif|heic|heif);base64,",
              "maxLength": 7000000
// after
              "pattern": "^data:image/(png|jpeg|webp|gif|heic|heif);base64,",
              "description": "Base64 data URL. The server decodes it and answers 413 image_too_large above 3,750,000 decoded bytes or 8,000 px per side; the JSON body cap (413 request_too_large) bounds the string. Remote http(s) URLs are never fetched."
```

```json
// openai-compat.schema.json OADecisionResponse.properties
// before
        "x_aghoo": { "type": "object" }
// after
        "x_aghoo": {
          "type": "object", "additionalProperties": false, "required": ["decision_id", "calibration"],
          "description": "Only with request header x-aghoo-include: ensemble. Keys are effective question names (the given name, or q{i} for unnamed questions).",
          "properties": {
            "decision_id": { "type": "string", "pattern": "^dec_" },
            "calibration": { "type": "object", "additionalProperties": { "enum": ["calibrated", "uncalibrated"] } },
            "mapped_unavailable": { "type": "object", "additionalProperties": { "enum": ["deadline_exceeded", "no_quorum", "all_members_failed", "no_capable_member"] } } } }
// OAInputPart input_image.image_url: { "type": "string" }
//   -> { "type": "string", "pattern": "^data:image/(png|jpeg|webp|gif|heic|heif);base64," }   (remote URL -> 400 schema_violation)
// $defs.Description -> $defs.OADescription (and both $refs): removes the redocly bundle name collision
```

The facade rules are now code the server imports: `check_examples.oa_request_invariants` covers unique explicit names, reserved `q<digits>`, true/"true" collisions, unique level labels and at most `OA_MAX_IMAGES = 20` images. Each rule returns `Violation(message, param)`.

## 9. Sessions, history, usage (gap 40) and the W4 views (gap 42)

All of these are new named schemas in Appendix A `components.schemas`, every one with `additionalProperties: false`:

| Schema | Fields |
|---|---|
| `SessionUpdate` (PATCH body; replaces `SessionCreate` there) | `metadata?`, `retention?`, `data_class?`; `minProperties: 1`; present fields replace |
| `SessionList` | `{data: Session[], has_more, next_cursor}` (newest first) |
| `SessionTurn` | `{turn_id, seq ≥ 1, role: user\|assistant, text, created_at, redactions[{start, end, reason}]}` (code-point offsets, `end` exclusive) |
| `SessionTurnList` | Oldest first |
| `UsageLine`, `UsageRow` | `UsageRow {key: string\|null, line, usage}` |
| `UsageReport` | `{group_by, from, to, data, has_more, next_cursor}`. getUsage gains `limit` / `after`; `group_by` defaults to `line`. Defaults and limits are in the operation description |
| `CalibratorStatus` | `rejected \| accepted \| active \| retired` |
| `CalibratorInfo` | `binding` → `decision.schema.json#/$defs/CalibrationBinding` (newly extracted, same content); `holdout` → `HoldoutMetrics \| null`; `acceptance`; `reliability{bins, ci}`; activation and retirement fields |
| `Interval` | `[low, high]` |
| `CalibratorList`, `LabelingRun`, `LabelingRunList`, `LabelingDroppedItem`, `LabelingDroppedList` | — |

The three new paths carry `tags: [admin]` and `x-noema-roles`:

| Path | Roles |
|---|---|
| `/v1/admin/calibrators` | tenant_admin, reviewer, auditor |
| `/v1/admin/labeling/runs` | tenant_admin, reviewer, auditor |
| `/v1/admin/labeling/dropped` | tenant_admin, reviewer |

## 10. SDK and codegen (gaps 35, 41)

### 10.1 Bundling

```bash
npx -y @redocly/cli@2.62.1 bundle contracts/openapi.yaml -o build/openapi.bundled.yaml   # 0 warnings after the OADescription rename
```

### 10.2 `sdk.ts` surface changes (Appendix B has the exact diff)

```ts
// before
  choice<const N extends string, const C extends Record<string, string | null>>(
    name: N, opts: { instructions: string; choices: C }): ChoiceQuestion<N, Extract<keyof C, string>>;
// after
  choice<const N extends string, const C extends readonly ChoiceOption[]>(
    name: N, opts: { instructions: string; choices: C }): ChoiceQuestion<N, C[number]["value"]>;
```

Other additions:
- `AghooError` with `status`, `type`, `code`, `param`, `requestId`, `retryable`, `retryAfterS` and `details`;
- `ProfileInfo.version: string`;
- `Session.owner?`;
- `SessionUpdate`, `SessionTurn`, `UsageLine`, `UsageGroupBy`, `UsageRow`, `CalibratorStatus`, `CalibratorInfo`, `LabelingRun`, `LabelingDroppedItem`;
- `FileObject.derivatives[].pages?`;
- client methods `sessions.list`, `sessions.update`, `sessions.turns → SessionTurn`, `files.derivative(id, kind, {page?}) → Blob`, `usage.list`, `admin.calibrators.list`, `admin.labeling.runs` and `admin.labeling.dropped`.

`sdk-usage.check.ts` adds `defaultProfileExample`, `choiceBuilderExample` (with a `@ts-expect-error` on the Record form), `resourcesExample` (with a `@ts-expect-error` on `version: number`), `errorExample` and an SSE `error` case.

### 10.3 Wire-compat check (goes into `sdk/typescript/test/wire-compat.check.ts`; verified, 0 errors)

Generate the wire types first: `openapi-typescript@7.13.0 build/openapi.bundled.yaml -o src/generated/openapi.d.ts --default-non-nullable false`.

```ts
import type { components } from "../src/generated/openapi";
import type * as T from "../src/types";            // vendored contracts/types.ts
import type * as S from "../src/sdk";              // vendored contracts/sdk.ts
import type * as O from "../src/openai-compat";    // vendored contracts/openai-compat.ts
type W = components["schemas"];
type Assert<X extends Y, Y> = X;
/** openapi-typescript drops allOf refinements of the answer union (e.g. boolean predicate candidates); answers are
 *  covered by the runtime client-model checks instead, so they are compared everywhere except inside DecisionResult. */
type NoAnswers<X> = X extends readonly (infer E)[] ? NoAnswers<E>[] : X extends { object: "decision"; answers: unknown } ? { [K in Exclude<keyof X, "answers">]: NoAnswers<X[K]> } : X extends object ? { [K in keyof X]: NoAnswers<X[K]> } : X;
type DeepMutable<X> = X extends readonly (infer E)[] ? DeepMutable<E>[] : X extends object ? { -readonly [K in keyof X]: DeepMutable<X[K]> } : X;
export type _1 = Assert<NoAnswers<W["TurnEvent"]>, NoAnswers<T.TurnEvent>>;
export type _2 = Assert<NoAnswers<W["TurnObject"]>, NoAnswers<S.TurnObject>>;
export type _3 = Assert<NoAnswers<W["DecisionResult"]>, NoAnswers<T.DecisionResult>>;
export type _4 = Assert<W["JevDecisionResponse"], T.JevDecisionResponse>;
export type _5 = Assert<W["Session"], S.Session>;
export type _6 = Assert<W["FileObject"], S.FileObject>;
export type _7 = Assert<W["ProfileInfo"], S.ProfileInfo>;
export type _8 = Assert<W["SessionTurn"], S.SessionTurn>;
export type _9 = Assert<W["UsageRow"], S.UsageRow>;
export type _10 = Assert<W["CalibratorInfo"], S.CalibratorInfo>;
export type _11 = Assert<W["LabelingRun"], S.LabelingRun>;
export type _12 = Assert<W["LabelingDroppedItem"], S.LabelingDroppedItem>;
export type _13 = Assert<W["Error"], T.ErrorEnvelope>;
export type _14 = Assert<W["OADecisionResponse"], O.OADecisionResponse>;
export type _15 = Assert<DeepMutable<S.TurnCreateParams>, W["TurnCreateParams"]>;
export type _16 = Assert<DeepMutable<T.DecisionCreateParams>, W["DecisionCreateParams"]>;
export type _17 = Assert<S.SessionUpdate, W["SessionUpdate"]>;
export type _18 = Assert<O.OADecisionCreateParams, W["OADecisionCreateParams"]>;
```

The `type` added to the answers' own `required` lists and `additionalProperties: true` on the JSON-state input exist so this check compiles. Validation outcomes are unchanged (48/48 examples).

### 10.4 Server models (Wave B; recorded here because the flags were verified against this bundle)

```bash
datamodel-codegen --input build/openapi.bundled.yaml --input-file-type openapi --openapi-scopes schemas \
  --output src/noema/contracts/_generated/models.py --output-model-type pydantic_v2.BaseModel \
  --base-class noema.contracts._base.StrictBase --target-python-version 3.12 --use-annotated --field-constraints \
  --enum-field-as-literal all --extra-fields forbid --strict-types str int float bool --use-standard-collections \
  --use-union-operator --use-type-alias --use-schema-description --disable-timestamp --use-double-quotes --formatters builtin
```

`StrictBase` is `ConfigDict(extra="forbid", strict=True, regex_engine="python-re", protected_namespaces=())`:
- `regex_engine="python-re"` is needed for the `Name` look-ahead pattern, which the Rust regex engine rejects;
- `--use-type-alias` avoids an unhashable `RootModel` used as the `JevDecisionResponse.answers` key, and needs `pydantic>=2.12`.

## 11. `check_examples.py`, the audit loader and the examples (gap 41)

- `check_examples.py`:
  - module-scope code is limited to constants and definitions;
  - the CLI (`self_tests()` + the example loop + `sys.exit`) moves into `def main() -> int` under `if __name__ == "__main__": sys.exit(main())`;
  - `python3 -I check_examples.py` behaves exactly as before; `import check_examples` and `runpy.run_path(...)` define the validators without running them;
  - `class Violation(str)` carries `.param`: printing and truthiness are unchanged, so the audit and the CLI keep working;
  - `request_invariants` and `oa_request_invariants` return `Violation`s with `param` paths (`questions`, `questions[i].choices`, `questions[i].levels`, `questions[i].name`, `input`);
  - `OA_MAX_IMAGES = 20` with a rule in `oa_request_invariants`;
  - `oa_invariants` checks `x_aghoo`: `calibration` keys = the effective names of the non-refusal answers, and `mapped_unavailable` keys ⊆ the refusals.
- `qa/run_design_audit.py`:
  - `load_checkers()` drops the `source.partition("\nself_tests()\n")` + `exec` workaround and uses `examples = runpy.run_path(str(CONTRACTS / "check_examples.py"))`;
  - still 41/41 handled; no audit cases added or changed.
- `make_examples.py` writes the five new examples. The 43 existing files come out byte-identical, which I checked by regenerating the unpatched tree.

| New example | Expected | Rejected or accepted by |
|---|---|---|
| `decision-request.valid-default-profile.json` | valid | `profile` omitted |
| `openai-request.invalid-21-images.json` | invalid | `oa_request_invariants`: "21 input_image parts; at most 20 per request (Bedrock limit)" |
| `openai-request.invalid-remote-image-url.json` | invalid | Schema (`image_url` pattern) |
| `openai-response.valid-ensemble.json` | valid | `x_aghoo` with `calibration: {q0: uncalibrated}` and `mapped_unavailable: {regression: deadline_exceeded}` |
| `openai-response.invalid-x-aghoo-mapped-not-refusal.json` | invalid | `oa_invariants`: `mapped_unavailable` names `q0`, which is a score answer |

Totals: 48 examples (13 valid, 35 invalid); the docs that cite the count are updated (01, 03 §0, 08 §13). *Superseded (pre-§14):* the combined rev-6 tree has 54 examples (§1, §14.6); design revision 7 has 56 (R1's lone-group pair, 08 §13).

## 12. `check_docs.sh` stays green

One step is added after the OpenAPI lint:

```bash
# Bundle openapi.yaml with its external JSON Schemas (the input of the SDK and server-model generators): no warnings.
if (cd contracts && b=$(npx -y @redocly/cli@latest bundle openapi.yaml -o "$out/openapi.bundled.yaml" 2>&1) && ! grep -qi "warning" <<<"$b"); then echo "bundle  PASS"; else echo "bundle  FAIL"; fail=1; fi
```

Rules for later patches, including those from other roles:
- run `python3 -I contracts/make_examples.py` whenever examples change, and never hand-edit generated example files;
- keep `openapi.yaml` free of Redocly warnings, including bundle renames: no `$defs` name may appear in both JSON Schemas with different content;
- any new error code needs a row in 03 §1, in `ERROR_TABLE` and in the `x-noema-error-codes` of the operations that use it;
- keep `check_examples.py` importable, with nothing executable at module scope beyond constants.

## 13. Prose patches (in Appendix B)

| File | Change |
|---|---|
| `03-contracts.md` | §0: 48 examples; lint and bundle. §1: pinning values; `x-request-id`; 401/403/413/422 rows; new 500/501 rows; "one status per code" + rate-limit headers. §2: resume note; new W4 row; `/v1/admin/*` note. §4.1: optional `profile`. §4.2: remote URL → 400 `schema_violation`. §9.1: array `q.choice`; `lastEventId` note |
| `07-api-user-guide.md` | Two `q.choice` snippets; the `speculative` row (Phase 3); resume (501 in Phase 1); errors table row for `feature_not_available` / `not_implemented`; pinning value; FAQ (speculative from Phase 3) |
| `05-implementation-plan.md` | L72: SDK wire types generated; clients hand-written against `contracts/sdk.ts` |
| `02-architecture.md` | L764: DOCX → 415 `unsupported_media_type` |
| `08-technical-reference.md` | L33: the default profile applies when `profile` is omitted; the facade always names its profile. §13: 48 examples; lint and bundle |
| `01-feasibility-study.md` | L52: 48 examples |
| `README.md` | Verify comment: lint + bundle |

---

## 14. Consolidation addendum (lead architect, after the critic passes; supersedes §1–§13 and Appendix A/B where they overlap)

**§14.0 Owner answers (2026-10-10).** The hierarchical method is built in Phase 1 (IMPL-027), so no payload text may return 422 `feature_not_available` for a hierarchical question. Native `/v1/decisions` and turn app questions are answered hierarchically; the facade and `output_format: "jev"` keep 422 `unsupported_parameter`. The deltas are in §7 (row), §14.1 item 2, §14.5 (`hierarchy_errors`), §14.6 (the hierarchical example), §14.7 (`normalized_entropy`), §14.8 (audit cases 6–8) and §14.9 (prose). IMPL-085 and IMPL-086 change no contract file.

This addendum is **not yet merged into the payloads below**. The contract-patch applier builds the **combined rev-6 payload** (IMPL-067):
1. apply Appendix A + B to a scratch copy of `docs/noema`;
2. apply every delta in §14.1–§14.9, in order;
3. regenerate the examples with `make_examples.py`;
4. run `check_docs.sh` (all steps), the TS wire-compat check (§10.3, extended by §14.7), the jsonschema-rs differential and the design audit (41 cases plus the §14.8 additions);
5. regenerate the §1 hash table once and publish `noema-rev6-combined.patch` plus the new `openapi.yaml`.

The §1 "after" hashes and the "48 examples" counts are stale until step 5. *Wave F note (2026-10-12):* `noema-rev6-combined.patch` was never published as a file (B0 item 32). The combined rev-6 tree is archived as `/codes/aghooai/.backups/docs-noema-rev6-2026-10-10.tgz`; its `openapi.yaml` is `2ea1c827…`. `diff -ruN` between the rev-5 and rev-6 archives gives the 5,710-line combined patch. Appendix A still holds the pre-§14 `openapi.yaml` (`ea30e652…`). Design revision 7 (`docs/noema` README) is the current base. Each delta names the IMPL decision it implements. **Steps 1–5 ran on 2026-10-10:** §1 now records the combined tree (54 examples, 19 valid and 35 invalid; 49 config negative tests; 49 audit cases), applied in place to `docs/noema`; the combined patch is reproducible as `diff -ruN` from the rev-5 backup to that tree.

### 14.1 `contracts/openapi.yaml` (Appendix A text)

1. **Facade description** (IMPL-031, critic 4/35). Replace:
   ```yaml
   # before
           `model` is required (OpenAI shape): `aghoo/<profile>[@version]`. An unknown profile is 400 `model_not_available`;
           a profile outside the tenant allowlist is 403 `profile_not_allowed`. A choice question that needs the hierarchical
           method is 422 `unsupported_parameter`. If every answer is unavailable: 503 `all_routes_unavailable`. Request rules
   # after
           `model` is required (OpenAI shape): `aghoo/<profile>[@version]`. A `model` that is not of that form, or a version pin
           that is not the profile's active version, is 400 `model_not_available` (`details.allowed`). An unknown or
           non-allowlisted profile is 403 `profile_not_allowed` (`details.allowed` lists the caller's profiles; the two cases are
           not distinguished). A choice question that needs the hierarchical method is 422 `unsupported_parameter` (permanent:
           this format can't carry unresolved mass). If every answer is unavailable: 503 `all_routes_unavailable` (the decision
           is stored; `details.decision_id`). Request rules
   ```
2. **createDecision description** (IMPL-031, IMPL-036, IMPL-057). After "…(`lightweight-model-decide` in the shipped configuration, D6)." insert:

   > A choice question with more options than the profile's flat limit is answered hierarchically (`choice_method: "hierarchical"`, `groups`, `unresolved_mass`; 08 §3.4); set a longer `deadline_ms` for it. With `output_format: "jev"` such a question is 422 `unsupported_parameter` (permanent: the Jev shape can't carry unresolved mass). If every answer is unavailable the response is 200 with `status: "partial"`, unless every member failed on tenant access (503 `tenant_access_misconfigured`, `details.decision_id`).

   (Revised 2026-10-10, §14.0: the earlier text returned 422 `feature_not_available` for native requests.)

   In the same description, "the tenant's `defaults.decision_profile` (`lightweight-model-decide` in the shipped configuration, D6)" → "the tenant's `defaults.decision_profile` (`lightweight-model-decide` unless the tenant configures another, D6)".
3. **`x-noema-error-codes`** (IMPL-032, IMPL-074):
   - createDecision: remove `no_capable_member`;
   - createDecisionOpenAICompat: remove `no_capable_member`, add `file_not_ready` and `file_rejected` (inline-image ingest; 409 is already declared).

   The per-operation status-set check still holds: 422 remains through the other codes.
4. **`RoutingDecision.served_spec_differs.description`** (IMPL-016) → "true when the served spec differs from the decided one: the answer committed on spec0 at the holdback deadline, or a pre-commit fallback to `routing.default_profile` served the turn; false when they are equal; null before a decision arrives."
5. **`CalibratorStatus` and `CalibratorInfo`** (IMPL-042, IMPL-072). Replace both with:
   ```yaml
       CalibratorStatus:
         type: string
         description: 'fitted: fitted, gate not yet evaluated. rejected: failed the acceptance gate. accepted: passed the gate, awaiting
           two-person activation (approve by one operator, activate by another). active: applied to answers whose binding matches.
           downgraded: drift detected (weekly ECE); never re-activated. retired: replaced, retired by hand, or its profile version left
           the active config.'
         enum: [fitted, rejected, accepted, active, downgraded, retired]
       CalibratorInfo:
         type: object
         additionalProperties: false
         required: [id, object, scope, question_name, status, method, parameters, binding, member_subset, holdout, acceptance,
                    reliability, created_at, approved_by, activated_by, activated_at, downgraded_at, downgrade_reason, retired_at, retired_reason]
         properties:
           id: { type: string, pattern: '^cal_' }
           object: { const: calibrator }
           scope: { type: string, enum: [platform, tenant], description: 'platform: fitted on platform seed data; applies only to platform
             question signatures, until a tenant artifact for the same binding is active' }
           question_name: { type: string }
           status: { $ref: '#/components/schemas/CalibratorStatus' }
           method: { type: string, enum: [temperature, platt], description: The pooled transform (Phase 1 methods) }
           parameters:
             type: object
             additionalProperties: false
             required: [pooled, member_T, epsilon]
             description: One artifact holds the per-member and pooled parts; they are only ever applied together (ADR-003 §1).
             properties:
               pooled: { type: object, additionalProperties: { type: number }, description: '{"temperature": T} or {"a": a, "b": b}' }
               member_T: { type: object, additionalProperties: { type: number, exclusiveMinimum: 0 }, description: 'member_id -> T_m' }
               epsilon: { type: number, exclusiveMinimum: 0 }
           binding: { $ref: './decision.schema.json#/$defs/CalibrationBinding' }
           member_subset: { type: array, minItems: 1, uniqueItems: true, items: { type: string } }
           holdout:
             oneOf:
               - { type: 'null' }
               - { $ref: './decision.schema.json#/$defs/HoldoutMetrics' }
           acceptance:
             type: object
             additionalProperties: false
             required: [passed, failed_rules]
             properties:
               passed: { type: boolean }
               failed_rules:
                 type: array
                 uniqueItems: true
                 items: { type: string, enum: [ece_max, nll_not_worse, n_min, flagged_classes, class_consensus_min, family_overlap_lift,
                                               label_source, member_subset_share] }
           reliability: { … unchanged from Appendix A … }
           created_at: { type: integer }
           approved_by: { type: [string, 'null'], description: Operator who approved activation (first of two) }
           activated_by: { type: array, maxItems: 2, uniqueItems: true, items: { type: string }, description: 'Both operator ids (approve, activate); empty until active' }
           activated_at: { type: [integer, 'null'] }
           downgraded_at: { type: [integer, 'null'] }
           downgrade_reason: { oneOf: [ { type: 'null' }, { type: string, enum: [drift_ece, drift_ci] } ] }
           retired_at: { type: [integer, 'null'] }
           retired_reason: { oneOf: [ { type: 'null' }, { type: string, enum: [replaced, manual, profile_version_retired] } ] }
   ```
   - `listCalibrators.status` filter: the same enum.
   - `sdk.ts` `CalibratorStatus`/`CalibratorInfo` change accordingly (§14.7). Wire-compat assertion `_10` stays.

### 14.2 `contracts/decision.schema.json`

1. **CP-DEC-1** (IMPL-047): `$defs.AnswerName` exactly as in `impl-decisions/decision.md` §CP-DEC-1, plus the `Name` description. Use `AnswerName` in `ScoredBase.name`, `RefusalAnswer.name`, `UnavailableAnswer.name` and `EscalationStage.questions.items`. Rebased onto rev 6: the answer `required` lists of IMPL-063 are unaffected.
2. **CP-DEC-2** (IMPL-048): `EscalationStage.properties.input_view_signature` and `prompt_templates`, exactly as in `impl-decisions/decision.md` §CP-DEC-2. Optional (not in `required`).
3. **`DecisionCreateParams.profile.description`**: "(lightweight-model-decide in the shipped configuration, D6)" → "(lightweight-model-decide unless the tenant configures another, D6)".

### 14.3 `contracts/openai-compat.schema.json`

`x_aghoo.description` → "Only with request header x-aghoo-include: ensemble. Keys are the client-visible question names (the given name, or q{i} for unnamed questions); the stored native decision uses internal names (q{i} for names that are not valid native names). `calibration` lists exactly the non-refusal answers; `mapped_unavailable` lists the answers mapped to refusal from unavailable." (IMPL-047)

### 14.4 `contracts/config.schema.json` and `contracts/check_config.py` (IMPL-038, IMPL-039)

- Schema: add `properties.platform`, `$defs.Platform`, `$defs.DataClassPolicy`, `Tenant.properties.limits`, `Labeling.properties.acceptance` and `Model.properties.context_window` exactly as in the **current** spec-05 §1 (§1.1–§1.3 and §1.5; Wave A2 revised it). In particular `platform.cost.prices_ref` is `{type: string, pattern: "^(db:[1-9][0-9]*|file:\\S+)$"}` with **no default** (absent = every price TBD, IMPL-019); the earlier IMPL-019 default path `prices.yaml` is withdrawn. Add `"maximum": 0.10` to `Labeling.family_overlap_lift_max`. Nothing becomes required.
- `check_config.py` semantic rules, each with a negative test in the module's table:
  - P1-C1 (`drift_ece_max ≥ ece_max`);
  - P1-C2 (`low_lt < high_gte`);
  - P1-C3 (`escalation.questions` ⊆ the source profile's questions);
  - P1-C4 (no `l2_cache: true` for confidential/restricted).

  The module's negative-test count goes from 45 to 49. The Phase-1-only rules (P1-T1, P1-R1, P1-D*, P1-G1, P1-L1, P1-P1) live in the server's `config/rules.py`, not in the design checker.
- The example yaml is unchanged and must still validate.

### 14.5 `contracts/check_examples.py`

On top of the Appendix B version:

```diff
-def band_of(value):
-    return "high" if value >= BANDS["high_gte"] else "low" if value < BANDS["low_lt"] else "medium"
+def band_of(value, bands=None):
+    b = bands or BANDS
+    return "high" if value >= b["high_gte"] else "low" if value < b["low_lt"] else "medium"
@@
-def holdout_errors(h, classes):
+def holdout_errors(h, classes, *, accept=None):
+    accept = accept or ACCEPT        # every ACCEPT[...] below reads accept[...]
@@
-def template_limit_errors(a, k, result, method):
+def template_limit_errors(a, k, result, method, *, template_max=None):
+    template_max = template_max or TEMPLATE_MAX   # the TEMPLATE_MAX.get(tpl) below reads template_max.get(tpl)
@@
-def result_invariants(result, req):
+def result_invariants(result, req, *, bands=None, accept=None, template_max=None):
+    """bands / accept / template_max default to the module constants (design examples); the server's test-mode validator passes
+    the active config snapshot's values (IMPL-039)."""
@@  (linear-pool raw_score check; critic 8: censored options carry no key)
-                    pooled = sum(m["weight"] * (m.get("calibrated_distribution") or m["distribution"])[key] for m in ok) / wsum
+                    pooled = sum(m["weight"] * (m.get("calibrated_distribution") or m["distribution"]).get(key, 0.0) for m in ok) / wsum
```

- Plus CP-DEC-2's `answering_stage` 4-tuple and its two call sites (`impl-decisions/decision.md` §CP-DEC-2), rebased.
- Calls inside `result_invariants` pass `bands`, `accept` and `template_max` through.
- `oa_invariants` is **unchanged** (IMPL-049 makes the server satisfy it for every K). *As applied:* `jev_invariants` got one fix: the score-key check compares sorted key sets, so the lexicographic sort no longer rejects every Jev score with 11 or more levels (B0 report item 24; §1 `check_examples.py` row).
- Add `JEV_PAIRS = {"decision-jev-response.valid-k16.json": "decision-request.valid-jev-k16.json"}`; the Jev loop uses `JEV_PAIRS.get(name, JEV_REQUEST)`.
- `OA_PAIRS["openai-response.valid-non-pattern-name.json"] = "openai-request.valid-non-pattern-name.json"`.
- **Hierarchical expansion check, exact** (§14.0, IMPL-027 (d)). `hierarchy_errors` compares the expanded set with the integer units of the emitted 4-dp group masses, the rule the server applies. The old `0.9 − TOL` comparison accepts an answer whose expanded groups cover only 0.8990–0.8999, one group short of the rule. Both shipped hierarchical examples keep their verdicts (valid: 0.9598; coverage-invalid: 0.4799):

  ```diff
  -    order = sorted(range(len(groups)), key=lambda i: (-groups[i]["probability"], i))
  -    need, covered = 0, 0.0
  +    units = [round(g["probability"] * 10_000) for g in groups]   # emitted 4-dp masses as integers (server rule, spec-06 §19.6)
  +    order = sorted(range(len(groups)), key=lambda i: (-units[i], i))
  +    need, covered = 0, 0
       for i in order[:3]:
           need += 1
  -        covered += groups[i]["probability"]
  -        if covered >= 0.9 - TOL:
  +        covered += units[i]
  +        if covered >= 9_000:
               break
  ```

  *Amendment (orchestrator ruling R1, 2026-10-10; canonical half by the api-platform applier):* the prefix must also hold at least 2 options in total, because `candidates` has `minItems: 2`; the loop breaks only when `covered >= 9_000` and the expanded groups' option count is ≥ 2 (still at most 3 groups). A lone dominant one-option group therefore expands the next group too (spec-06 §19.6, T-HIER-3), and 08 §3.4 says the same.

### 14.6 Examples (`make_examples.py`) and the 03 SSE example

New generated examples (48 → 54; 19 valid, 35 invalid):

| File | Expected | Purpose |
|---|---|---|
| `decision-result.valid-censored.json` | valid | T-LP-1 with `members` (as applied: `router-default@1` with `min_quorum` 1; m1 `ok` with `censored_options: ["F"]`, m2/m3 `error`/`throttled` with weight 0, not "a single logprob member"; B0 item 26; `unresolved_mass` 0.001). Exercises the `.get(key, 0.0)` fix; pairs with a new `decision-request.valid-censored.json` (6-option choice) |
| `decision-request.valid-censored.json` | valid | the request for the above |
| `openai-request.valid-non-pattern-name.json` / `openai-response.valid-non-pattern-name.json` | valid | explicit name `"Severity Level"`; `x_aghoo` keyed by it (IMPL-047) |
| `decision-request.valid-jev-k16.json` / `decision-jev-response.valid-k16.json` | valid | a 16-option choice and a 16-level score; the Jev map, score and confidence derived from the 2-dp map (IMPL-049) |

- `decision-result.valid-escalated.json` gains the CP-DEC-2 stage fields, with the same values as today's provenance. *As applied:* six existing examples were regenerated, not only this one (B0 item 29): `valid-escalated`, `invalid-image-no-adequate-view` (it inherits the stage fields), `valid-hierarchical`, `invalid-hierarchical-unresolved-renormalized`, `invalid-flat-over-member-limit` and `invalid-hierarchical-coverage` (all `decision-result.*`).
- **The hierarchical examples stay, and are used** (§14.0). `decision-request.valid-hierarchical.json` and `decision-result.valid-hierarchical.json` are now the expected input and output of a Phase-1 code path, not a design illustration:
  - spec-06 T-HIER-1 reproduces the result from the request;
  - spec-10 Q9 and the §14.8 cases 6–8 use them;
  - the three invalid hierarchical examples keep their verdicts.

  One generator fix aligns them with the server rule "confidence fields are computed before rounding" (spec-06 §11, IMPL-079). The change is in `make_examples.py` `hierarchical_answer`:

  ```diff
  -    conf = (dist[order[0]] - 1 / n) / (1 - 1 / n)
  -    buckets = list(dist.values()) + [unresolved]
  +    conf = (joint[order[0]] - 1 / n) / (1 - 1 / n)          # unrounded values, as the server computes them
  +    buckets = list(joint.values()) + [unresolved]
  @@
  -                          "margin": r4(dist[order[0]] - dist[order[1]]), "normalized_entropy": r4(H), "agreement": 1.0, "member_divergence": None},
  +                          "margin": r4(joint[order[0]] - joint[order[1]]), "normalized_entropy": r4(H), "agreement": 1.0, "member_divergence": None},
  ```

  In `decision-result.valid-hierarchical.json` only `confidence.normalized_entropy` changes, 0.749 → 0.7489. `value` (0.2434) and `margin` (0.024) are unchanged. Of the derived invalid examples, `…-unresolved-renormalized` and `…-flat-over-member-limit` change in the same field; `…-hierarchical-coverage` changes in `normalized_entropy` (0.6271 → 0.627) and `margin` (0.1919 → 0.192). All three keep their verdicts. Verified 2026-10-10 on a scratch copy of the rev-5 tree: with this fix and the §14.5 `hierarchy_errors` change, `make_examples.py` rewrites exactly these 4 files, and `check_examples.py` reports 0 unexpected outcomes. The example count stays 54.
- **03 §3.2 SSE example** (IMPL-012, critic 23). The `03-contracts.md` text block at L145-183 is replaced by:

```text
id: 1   event: turn.created
data: {"type":"turn.created","turn_id":"turn_01J9ZQ7","session_id":"ses_01J9ZQ4V7A","created_at":1791504400}

id: 2   event: preflight.completed
data: {"type":"preflight.completed","modalities":["text","document"],"est_input_tokens":6120,"degradations":[]}

id: 3   event: route.selected
data: {"type":"route.selected","attempt_id":"att_1","speculative":true,"degradations":[{"file_id":"file_01J9ZQ5B2C","attempt_id":"att_1","model":"us.openai.gpt-6.1-sol","from":"application/pdf","to":"markdown+page_images(2)","reason":"model_capability"}],"routing":{ …unchanged… }}

id: 4   event: generation.started
data: {"type":"generation.started","attempt_id":"att_1","route_id":"sol-fast","model":"us.openai.gpt-6.1-sol","speculative":true,"degradations":[{"file_id":"file_01J9ZQ5B2C","attempt_id":"att_1","model":"us.openai.gpt-6.1-sol","from":"application/pdf","to":"markdown+page_images(2)","reason":"model_capability"}]}

id: 5   event: decision.completed
data: {"type":"decision.completed","arrived":"during_holdback","decision":{"id":"dec_01J9ZQ8","object":"decision","status":"quorum","answers":[ ... ]}}

id: 6   event: generation.cancelled
data: {"type":"generation.cancelled","attempt_id":"att_1","reason":"spec_changed"}

id: 7   event: generation.started
data: {"type":"generation.started","attempt_id":"att_2","route_id":"opus-deep","model":"us.anthropic.claude-opus-5-5","speculative":false,"degradations":[]}

id: 8   event: decision.updated
data: {"type":"decision.updated","changed":["sensitive_data"],"decision":{"id":"dec_01J9ZQ8","status":"completed", ... }}

id: 9   event: route.confirmed
data: {"type":"route.confirmed","attempt_id":"att_2","routing":{ …unchanged… }}

id: 10  event: generation.committed
data: {"type":"generation.committed","attempt_id":"att_2","reason":"spec_agrees"}

id: 11  event: generation.delta
data: {"type":"generation.delta","attempt_id":"att_2","seq":0,"text":"Mostly, yes. "}

...

id: 51  event: generation.completed
data: {"type":"generation.completed","attempt_id":"att_2","finish_reason":"stop","usage":{ … }}

id: 52  event: turn.verified
data: { …unchanged… }

id: 53  event: turn.completed
data: { …unchanged… }
```

  The prose after the block gains: "`decision.updated` arrives once the decision is final (here at about 1 s, before Opus's first answer text at 2.3 s); events are emitted in time order (IMPL-012)."

### 14.7 `contracts/types.ts`, `contracts/sdk.ts` (comments and the W4 types)

| Location | Change | IMPL |
|---|---|---|
| `DecisionResult.status` comment | the `completed` / `quorum` / `partial` text of IMPL-002 | 001, 002 |
| `Conditioning.signature` comment | "sha256 over the template id and the rendered block; part of the attempt spec compared at commit" | 014 |
| `RoutingDecision.served_spec_differs` comment | as §14.1 item 4 | 016 |
| `HeuristicConfidence` field comments | `normalized_entropy`: "H/ln(n) over the candidates, plus one unresolved bucket when unresolved_mass > 0 (n as for normalized_max); H(p)/ln(K) when there is none" (revised 2026-10-10 for the hierarchical method, IMPL-079); `agreement`: "fraction of answering members whose argmax equals candidates[0]"; `member_divergence`: "mean base-2 Jensen–Shannon divergence member → linear pool (raw_score); null if 1 member" | 079 |
| `ScoredAnswerBase.name` comment | CP-DEC-1 comment | 047 |
| `EscalationStage` | CP-DEC-2 optional fields | 048 |
| `sdk.ts` `CalibratorStatus`, `CalibratorInfo` | match §14.1 item 5 (drop `level`/`member_id`; `parameters{pooled, member_T, epsilon}`; new status values; `approved_by`, `downgraded_at`, `downgrade_reason`) | 042 |

### 14.8 Design audit additions (`qa/run_design_audit.py`; 41 → 49 cases)

1. `answer_name_q0_response_only`: a `DecisionResult` answer named `q0` validates; a `DecisionCreateParams` question named `q0` is rejected (CP-DEC-1).
2. `escalated_stage_bound_calibration`: an escalated `lightweight-model-decide → router-default` result calibrated with the stage's view signature is accepted, and rejected with the stage-0 signature (CP-DEC-2).
3. `censored_member_pool_no_crash`: `result_invariants` returns violations, never raises, on a result whose member has `censored_options` (critic 8).
4. `jev_k16_rounded_map`: a 16-option Jev answer derived from the 2-dp map passes; the same answer with `confidence = round(native, 2)` from a native n = K + 1 value fails when they differ by > 0.02 (IMPL-049).
5. `facade_non_pattern_name`: `x_aghoo` keyed by `"Severity Level"` passes; keyed by `q0` fails (IMPL-047).
6. `hierarchical_native_answer_valid` (§14.0, IMPL-027): `check(decision-result.valid-hierarchical.json)` returns no violation against its request. It is recorded as `handled` when there are none (mechanism `accepted`). The native hierarchical answer is a served Phase-1 output: no contract text may turn it into a 422.
7. `hierarchical_expansion_exact_threshold` (§14.5): build a result with `runpy.run_path(CONTRACTS / "make_examples.py")["hierarchical_answer"]` (the generator's `main()` is guarded, so loading it writes nothing). Use stage 1 (0.4498, 0.0503, 0.4497, 0.0502) and expanded `["billing", "shipping"]`, which covers 0.8995 < 0.9. `result_invariants` must reject it ("top 3"). The rev-5 checker accepts it (0.8995 ≥ 0.9 − TOL); this was verified on a scratch copy on 2026-10-10.
8. `hierarchical_round1_only_rejected` (no silent truncation, spec-06 §19.5): the valid hierarchical result with every group `expanded: false`, `candidates: []` and `unresolved_mass: 1` is rejected. The schema already rejects it, and the case pins that, so a deadline-truncated answer can only be `unavailable`.

The suite goes from 41 to **49** cases (§14.8 items 1–8).

### 14.9 Prose patches (applied in the same payload; exact wording in the cited IMPL entry)

| File:line | Change | IMPL |
|---|---|---|
| 02:L233 | persistence sentence | 022 |
| 02:L322, 08:L195 | cache point in Phase 2 | 017 |
| 02:L343-345 | `metadata_only` members are skipped | 058 |
| 02:L412 | `ordinal_dispersion` formula text | 028 |
| 02:L423 | in-turn escalation sentence | 025 |
| 02:L490, 02:L585 | spec0 without conditioning; `direct` renders no block | 013, 014 |
| 02 §6.1, §6.3 (after L540) | attempt = one model call; `quorum` in-turn snapshot | 011, 002 |
| 02:L629, 02:L723, 02:L115 | L3 no-ops; summary Phase 2; language ID Phase 2 | 018, 045, 046 |
| 02:L664-715 ER | `tenant_id` on ATTEMPT, DECISION, DERIVATIVE | 009 |
| 03:L39-40 | idempotency storage rule | 077 |
| 03:L41 | deadline derivation table | 023 |
| 03:L82 | Phase 1 admin = `noema-admin` CLI | 044 |
| 03:L117 | turn `decisions.profile` default = `routing.router_profile` | 069 |
| 03:L124 | `verify` semantics | 024 |
| 03:L145-183 | the SSE example of §14.6 | 012 |
| 05:L75 (Phase 1 "Decision engine" row) | after "linear pool;" insert "the hierarchical choice method for choice questions above the flat limit (08 §3.4; native and turn app questions; facade and Jev: 422 `unsupported_parameter`);". The former row "03 §4.1, 07:L154, 08 §3.4: hierarchical in Phase 1 = native 422 `feature_not_available`" is **withdrawn** (§14.0): those texts already describe the built method and the facade/Jev 422 | 027, 036 |
| 03:L312, 03 §4.2/§4.3, 08:L237-238 | Jev/facade values from their own maps; censored-tail mapping | 049 |
| 04:L101-102, L132 | decision member calls: no retries | 050 |
| 04:L104 | decision members degrade on AccessDenied | 057 |
| 04:L110 | status wording | 001 |
| 04:L147 | L2 key part contents and put gates | 004 |
| 04:L191, 01:L300, README:L38 | per-tenant STS session wording | 007 |
| 04:L199 | HMAC-SHA256 with pepper | 008 |
| 04:L236, 04:L265 | pikepdf rewrite; deterministic PII recognizers in Phase 1 | 029 |
| 04:L282, 04:L283 | span names; usage line names | 006, 020 |
| 07:L317 | `decision.completed` status wording | 002 |
| 08:L33 | default profile wording | 031 |
| 08:L96 | trigger mapping; target re-asks; `questions?` filters platform questions | 005, 025, 068 |
| 08:L113, L118 | `uncertain_profile`; `holdback_ms` reserved | 026, 003 |
| 08:L179 | choice options permuted | 051 |
| 08 §3.2 | template text is code (spec canonical); conditioning signature sentence | 053, 014 |
| 08:L473-474 | table list; text `tenant_id` | 009, 010 |
| 08 §10 | price catalog key and fields | 019 |
| 08 §12 | ADR status: ADR-001…004 written (Wave A) | 081 |
| 02:L66 | "signed, versioned config" → "two-person approved, versioned config (re-validated at load; no signature in Phase 1)" | 044 |
| 02:L578 | the example line `complexity: "moderate"` → `complexity: "moderate" (≈70%) — uncalibrated, confidence medium` (every rendered line has a bin, a status and a band, 08 §3.2) | 053, 082 |
| 03:L206 | "For `store: false` turns the events live only in memory for the window" → "…live only in the event log (in-process plus its Redis mirror) for the window and are never written to Postgres" | 021 |

---

## Appendix A — `contracts/openapi.yaml` (complete replacement; sha256 `ea30e652e17eed6fdb22a9f6ed7fbebafd8560e7066a57ea69e3d9c64cf15a56`)

<!-- BEGIN contracts/openapi.yaml -->
````yaml
openapi: 3.1.0
info:
  title: AghooAI Noema API
  version: 0.3.0
  summary: Noema, a concurrent decision + generation proxy over Amazon Bedrock (design proposal).
  description: |
    Proposal-stage contract (no implementation yet). Decision objects are defined in `decision.schema.json`;
    the OpenAI Decisions-compatible facade in `openai-compat.schema.json`. All model calls run on Amazon Bedrock
    inside the tenant's own AWS account (decisions D1, D2). See docs/noema/07-api-user-guide.md.

    API versions (`Aghoo-Version`): current and default `2026-10-01`; supported: `2026-10-01`.
    Every error uses the `Error` envelope. Its HTTP status, `error.type` and `error.retryable` are fixed per
    `error.code` (03 §1); `x-noema-error-codes` lists the codes an operation may return.
    Phase 1 serves every operation here except `resumeTurnEvents`, which answers 501 `not_implemented` until Phase 2.
  license:
    name: Proprietary (design draft)
    identifier: LicenseRef-Proprietary
servers:
  - url: https://api.aghoo.example
    description: Placeholder host
security:
  - bearerAuth: []
tags:
  - name: turns
    description: Smart turns (decision + generation, SSE)
  - name: decisions
    description: Typed ensemble decisions (JEV-style; Decisions-API-shaped)
  - name: compat
    description: OpenAI-compatible facades
  - name: profiles
    description: Discover decision and generation profiles
  - name: sessions
    description: Stateful conversations
  - name: files
    description: Uploads, validation, derivatives
  - name: usage
    description: Usage and cost
  - name: admin
    description: Read-only calibration and labeling views (W4); the rest of the admin API is Phase 2
  - name: health
    description: Liveness and readiness

paths:
  /v1/turns:
    post:
      tags: [turns]
      operationId: createTurn
      summary: Create a turn (decision + generation run concurrently)
      parameters:
        - $ref: '#/components/parameters/IdempotencyKey'
        - $ref: '#/components/parameters/AghooVersion'
        - $ref: '#/components/parameters/DeadlineMs'
        - $ref: '#/components/parameters/RequestIdHeader'
        - $ref: '#/components/parameters/Traceparent'
        - $ref: '#/components/parameters/Tracestate'
        - $ref: '#/components/parameters/EndUser'
      requestBody:
        required: true
        content:
          application/json:
            schema: { $ref: '#/components/schemas/TurnCreateParams' }
      x-noema-error-codes: [schema_violation, unsupported_question_type, reserved_question_name, invalid_api_key, invalid_end_user_token,
                            permission_denied, profile_not_allowed, session_forbidden, not_found, session_not_found, idempotency_key_reused,
                            session_conflict, session_busy, file_not_ready, file_rejected, resume_window_expired, request_too_large,
                            image_too_large, unsupported_media_type, no_route_satisfies_constraints, no_capable_member,
                            unsupported_parameter, feature_not_available, rpm_exceeded, tpm_exceeded, queue_timeout, budget_exhausted,
                            internal_error, all_routes_unavailable, tenant_access_misconfigured, deadline_exceeded]
      responses:
        '200':
          description: |
            Turn result (stream=false) or an SSE stream of TurnEvent (stream=true). Errors after the stream started arrive
            as an `error` event, followed by `turn.completed`. 410 `resume_window_expired` answers an Idempotency-Key replay
            of a streamed turn whose event log has expired (`details.turn_id`; read the turn with getTurn).
          headers:
            x-request-id: { $ref: '#/components/headers/RequestId' }
            x-ratelimit-limit-requests: { $ref: '#/components/headers/RateLimitLimitRequests' }
            x-ratelimit-remaining-requests: { $ref: '#/components/headers/RateLimitRemainingRequests' }
            x-ratelimit-reset-requests: { $ref: '#/components/headers/RateLimitResetRequests' }
            x-ratelimit-limit-tokens: { $ref: '#/components/headers/RateLimitLimitTokens' }
            x-ratelimit-remaining-tokens: { $ref: '#/components/headers/RateLimitRemainingTokens' }
            x-ratelimit-reset-tokens: { $ref: '#/components/headers/RateLimitResetTokens' }
            x-aghoo-idempotent-replay: { $ref: '#/components/headers/IdempotentReplay' }
          content:
            application/json:
              schema: { $ref: '#/components/schemas/TurnObject' }
            text/event-stream:
              schema: { $ref: '#/components/schemas/TurnEvent' }
        '400': { $ref: '#/components/responses/BadRequest' }
        '401': { $ref: '#/components/responses/Unauthorized' }
        '403': { $ref: '#/components/responses/Forbidden' }
        '404': { $ref: '#/components/responses/NotFound' }
        '409': { $ref: '#/components/responses/Conflict' }
        '410': { $ref: '#/components/responses/Gone' }
        '413': { $ref: '#/components/responses/PayloadTooLarge' }
        '415': { $ref: '#/components/responses/UnsupportedMediaType' }
        '422': { $ref: '#/components/responses/Unprocessable' }
        '429': { $ref: '#/components/responses/RateLimited' }
        '500': { $ref: '#/components/responses/InternalError' }
        '503': { $ref: '#/components/responses/Unavailable' }
        '504': { $ref: '#/components/responses/GatewayTimeout' }
  /v1/turns/{turn_id}:
    get:
      tags: [turns]
      operationId: getTurn
      summary: Retrieve a stored turn
      parameters:
        - $ref: '#/components/parameters/TurnId'
        - $ref: '#/components/parameters/AghooVersion'
        - $ref: '#/components/parameters/RequestIdHeader'
        - $ref: '#/components/parameters/Traceparent'
        - $ref: '#/components/parameters/Tracestate'
        - $ref: '#/components/parameters/EndUser'
      x-noema-error-codes: [invalid_api_key, invalid_end_user_token, not_found, feature_not_available, rpm_exceeded, internal_error]
      responses:
        '200':
          description: Stored turn
          headers:
            x-request-id: { $ref: '#/components/headers/RequestId' }
          content:
            application/json:
              schema: { $ref: '#/components/schemas/TurnObject' }
        '401': { $ref: '#/components/responses/Unauthorized' }
        '404': { $ref: '#/components/responses/NotFound' }
        '422': { $ref: '#/components/responses/Unprocessable' }
        '429': { $ref: '#/components/responses/RateLimited' }
        '500': { $ref: '#/components/responses/InternalError' }
  /v1/turns/{turn_id}/cancel:
    post:
      tags: [turns]
      operationId: cancelTurn
      summary: Cancel a running turn (final; an explicit abort, unlike a dropped connection)
      description: |
        Idempotent. Stops the running attempts (committed partial text is kept) and ends the stream with `turn.completed`.
        A dropped connection is not a cancel: the turn keeps running for the resume window so the client can reattach.
      parameters:
        - $ref: '#/components/parameters/IdempotencyKey'
        - $ref: '#/components/parameters/AghooVersion'
        - $ref: '#/components/parameters/TurnId'
        - $ref: '#/components/parameters/RequestIdHeader'
        - $ref: '#/components/parameters/Traceparent'
        - $ref: '#/components/parameters/Tracestate'
        - $ref: '#/components/parameters/EndUser'
      x-noema-error-codes: [invalid_api_key, invalid_end_user_token, not_found, idempotency_key_reused, feature_not_available, rpm_exceeded,
                            internal_error]
      responses:
        '202':
          description: Cancellation requested, in progress, or the turn had already finished
          headers:
            x-request-id: { $ref: '#/components/headers/RequestId' }
            x-aghoo-idempotent-replay: { $ref: '#/components/headers/IdempotentReplay' }
          content:
            application/json:
              schema:
                type: object
                additionalProperties: false
                required: [turn_id, status]
                properties:
                  turn_id: { type: string }
                  status: { type: string, enum: [cancelling, cancelled, completed] }
        '401': { $ref: '#/components/responses/Unauthorized' }
        '404': { $ref: '#/components/responses/NotFound' }
        '409': { $ref: '#/components/responses/Conflict' }
        '422': { $ref: '#/components/responses/Unprocessable' }
        '429': { $ref: '#/components/responses/RateLimited' }
        '500': { $ref: '#/components/responses/InternalError' }
  /v1/turns/{turn_id}/events:
    get:
      tags: [turns]
      operationId: resumeTurnEvents
      summary: Resume a turn's event stream (within the resume window; Phase 2)
      description: Phase 1 answers every request with 501 `not_implemented`; the 200, 404 and 410 behavior below starts in Phase 2.
      parameters:
        - $ref: '#/components/parameters/TurnId'
        - $ref: '#/components/parameters/AghooVersion'
        - $ref: '#/components/parameters/RequestIdHeader'
        - $ref: '#/components/parameters/Traceparent'
        - $ref: '#/components/parameters/Tracestate'
        - $ref: '#/components/parameters/EndUser'
        - name: Last-Event-ID
          in: header
          required: false
          schema: { type: string }
      x-noema-error-codes: [invalid_api_key, invalid_end_user_token, not_found, resume_window_expired, feature_not_available, rpm_exceeded,
                            internal_error, not_implemented]
      responses:
        '200':
          description: SSE stream of TurnEvent after Last-Event-ID (Phase 2)
          headers:
            x-request-id: { $ref: '#/components/headers/RequestId' }
          content:
            text/event-stream:
              schema: { $ref: '#/components/schemas/TurnEvent' }
        '401': { $ref: '#/components/responses/Unauthorized' }
        '404': { $ref: '#/components/responses/NotFound' }
        '410': { $ref: '#/components/responses/Gone' }
        '422': { $ref: '#/components/responses/Unprocessable' }
        '429': { $ref: '#/components/responses/RateLimited' }
        '500': { $ref: '#/components/responses/InternalError' }
        '501': { $ref: '#/components/responses/NotImplemented' }

  /v1/decisions:
    post:
      tags: [decisions]
      operationId: createDecision
      summary: Typed decision (choice / score / predicate|noul) from a Bedrock decision profile
      description: |
        Default output is the native DecisionResult (candidates, calibration, confidence, provenance).
        With `output_format: "jev"` the response is a Jev-shaped JevDecisionResponse (answers keyed by name,
        noul = P(true), probability maps, score legend, 2-decimal values). `lightweight-model-decide` = gpt-6-luna alone;
        `router-default` = ensemble (gpt-6-luna + Claude Haiku 5.5 + DeepSeek V3.2). Without `profile`, the tenant's
        `defaults.decision_profile` answers (`lightweight-model-decide` in the shipped configuration, D6).
        Once member calls have started, the response is 200 (answers that miss the deadline are `unavailable`);
        504 `deadline_exceeded` means `Aghoo-Deadline-Ms` expired before any member call started.
      parameters:
        - $ref: '#/components/parameters/IdempotencyKey'
        - $ref: '#/components/parameters/AghooVersion'
        - $ref: '#/components/parameters/DeadlineMs'
        - $ref: '#/components/parameters/RequestIdHeader'
        - $ref: '#/components/parameters/Traceparent'
        - $ref: '#/components/parameters/Tracestate'
        - $ref: '#/components/parameters/EndUser'
      requestBody:
        required: true
        content:
          application/json:
            schema: { $ref: './decision.schema.json#/$defs/DecisionCreateParams' }
      x-noema-error-codes: [schema_violation, unsupported_question_type, invalid_api_key, invalid_end_user_token, permission_denied,
                            profile_not_allowed, not_found, session_not_found, idempotency_key_reused, file_not_ready, file_rejected,
                            request_too_large, image_too_large, unsupported_media_type, no_capable_member, unsupported_parameter,
                            feature_not_available, rpm_exceeded, tpm_exceeded, queue_timeout, budget_exhausted, internal_error,
                            all_routes_unavailable, tenant_access_misconfigured, deadline_exceeded]
      responses:
        '200':
          description: Decision result (native or Jev-shaped, per output_format)
          headers:
            x-request-id: { $ref: '#/components/headers/RequestId' }
            x-ratelimit-limit-requests: { $ref: '#/components/headers/RateLimitLimitRequests' }
            x-ratelimit-remaining-requests: { $ref: '#/components/headers/RateLimitRemainingRequests' }
            x-ratelimit-reset-requests: { $ref: '#/components/headers/RateLimitResetRequests' }
            x-ratelimit-limit-tokens: { $ref: '#/components/headers/RateLimitLimitTokens' }
            x-ratelimit-remaining-tokens: { $ref: '#/components/headers/RateLimitRemainingTokens' }
            x-ratelimit-reset-tokens: { $ref: '#/components/headers/RateLimitResetTokens' }
            x-aghoo-idempotent-replay: { $ref: '#/components/headers/IdempotentReplay' }
          content:
            application/json:
              schema:
                oneOf:
                  - { $ref: './decision.schema.json#/$defs/DecisionResult' }
                  - { $ref: './decision.schema.json#/$defs/JevDecisionResponse' }
        '400': { $ref: '#/components/responses/BadRequest' }
        '401': { $ref: '#/components/responses/Unauthorized' }
        '403': { $ref: '#/components/responses/Forbidden' }
        '404': { $ref: '#/components/responses/NotFound' }
        '409': { $ref: '#/components/responses/Conflict' }
        '413': { $ref: '#/components/responses/PayloadTooLarge' }
        '415': { $ref: '#/components/responses/UnsupportedMediaType' }
        '422': { $ref: '#/components/responses/Unprocessable' }
        '429': { $ref: '#/components/responses/RateLimited' }
        '500': { $ref: '#/components/responses/InternalError' }
        '503': { $ref: '#/components/responses/Unavailable' }
        '504': { $ref: '#/components/responses/GatewayTimeout' }
  /v1/decisions/{decision_id}:
    get:
      tags: [decisions]
      operationId: getDecision
      summary: Retrieve a stored decision (including late member merges)
      parameters:
        - name: decision_id
          in: path
          required: true
          schema: { type: string, pattern: '^dec_' }
        - $ref: '#/components/parameters/AghooVersion'
        - $ref: '#/components/parameters/RequestIdHeader'
        - $ref: '#/components/parameters/Traceparent'
        - $ref: '#/components/parameters/Tracestate'
        - $ref: '#/components/parameters/EndUser'
      x-noema-error-codes: [invalid_api_key, invalid_end_user_token, not_found, feature_not_available, rpm_exceeded, internal_error]
      responses:
        '200':
          description: Decision result
          headers:
            x-request-id: { $ref: '#/components/headers/RequestId' }
          content:
            application/json:
              schema: { $ref: './decision.schema.json#/$defs/DecisionResult' }
        '401': { $ref: '#/components/responses/Unauthorized' }
        '404': { $ref: '#/components/responses/NotFound' }
        '422': { $ref: '#/components/responses/Unprocessable' }
        '429': { $ref: '#/components/responses/RateLimited' }
        '500': { $ref: '#/components/responses/InternalError' }
  /openai/v1/decisions:
    post:
      tags: [compat]
      operationId: createDecisionOpenAICompat
      summary: OpenAI Decisions-compatible facade (shapes only; answered by a Bedrock decision profile)
      description: |
        `model` is required (OpenAI shape): `aghoo/<profile>[@version]`. An unknown profile is 400 `model_not_available`;
        a profile outside the tenant allowlist is 403 `profile_not_allowed`. A choice question that needs the hierarchical
        method is 422 `unsupported_parameter`. If every answer is unavailable: 503 `all_routes_unavailable`. Request rules
        that JSON Schema can't express (unique explicit names, reserved `q<digits>`, true/"true" collisions, at most 20
        images) are 400 `schema_violation` with `param`.
      parameters:
        - $ref: '#/components/parameters/IdempotencyKey'
        - $ref: '#/components/parameters/AghooVersion'
        - $ref: '#/components/parameters/DeadlineMs'
        - $ref: '#/components/parameters/RequestIdHeader'
        - $ref: '#/components/parameters/Traceparent'
        - $ref: '#/components/parameters/Tracestate'
        - $ref: '#/components/parameters/EndUser'
        - name: x-aghoo-include
          in: header
          required: false
          schema: { type: string, enum: [ensemble] }
      requestBody:
        required: true
        content:
          application/json:
            schema: { $ref: './openai-compat.schema.json#/$defs/OADecisionCreateParams' }
      x-noema-error-codes: [schema_violation, model_not_available, invalid_api_key, invalid_end_user_token, permission_denied,
                            profile_not_allowed, idempotency_key_reused, request_too_large, image_too_large, unsupported_media_type,
                            no_capable_member, unsupported_parameter, feature_not_available, rpm_exceeded, tpm_exceeded, queue_timeout,
                            budget_exhausted, internal_error, all_routes_unavailable, tenant_access_misconfigured, deadline_exceeded]
      responses:
        '200':
          description: OpenAI-shaped decision response
          headers:
            x-request-id: { $ref: '#/components/headers/RequestId' }
            x-ratelimit-limit-requests: { $ref: '#/components/headers/RateLimitLimitRequests' }
            x-ratelimit-remaining-requests: { $ref: '#/components/headers/RateLimitRemainingRequests' }
            x-ratelimit-reset-requests: { $ref: '#/components/headers/RateLimitResetRequests' }
            x-ratelimit-limit-tokens: { $ref: '#/components/headers/RateLimitLimitTokens' }
            x-ratelimit-remaining-tokens: { $ref: '#/components/headers/RateLimitRemainingTokens' }
            x-ratelimit-reset-tokens: { $ref: '#/components/headers/RateLimitResetTokens' }
            x-aghoo-idempotent-replay: { $ref: '#/components/headers/IdempotentReplay' }
          content:
            application/json:
              schema: { $ref: './openai-compat.schema.json#/$defs/OADecisionResponse' }
        '400': { $ref: '#/components/responses/BadRequest' }
        '401': { $ref: '#/components/responses/Unauthorized' }
        '403': { $ref: '#/components/responses/Forbidden' }
        '409': { $ref: '#/components/responses/Conflict' }
        '413': { $ref: '#/components/responses/PayloadTooLarge' }
        '415': { $ref: '#/components/responses/UnsupportedMediaType' }
        '422': { $ref: '#/components/responses/Unprocessable' }
        '429': { $ref: '#/components/responses/RateLimited' }
        '500': { $ref: '#/components/responses/InternalError' }
        '503': { $ref: '#/components/responses/Unavailable' }
        '504': { $ref: '#/components/responses/GatewayTimeout' }

  /v1/profiles:
    get:
      tags: [profiles]
      operationId: listProfiles
      summary: List the decision and generation profiles the caller may select
      description: Phase 1 omits generation profiles whose mode it doesn't serve (`best_of_n`, `fuse`).
      parameters:
        - name: kind
          in: query
          required: false
          schema: { type: string, enum: [decision, generation] }
        - $ref: '#/components/parameters/AghooVersion'
        - $ref: '#/components/parameters/RequestIdHeader'
        - $ref: '#/components/parameters/Traceparent'
        - $ref: '#/components/parameters/Tracestate'
        - $ref: '#/components/parameters/EndUser'
      x-noema-error-codes: [schema_violation, invalid_api_key, invalid_end_user_token, feature_not_available, rpm_exceeded, internal_error]
      responses:
        '200':
          description: Profiles after tenant allowlists
          headers:
            x-request-id: { $ref: '#/components/headers/RequestId' }
          content:
            application/json:
              schema:
                type: object
                additionalProperties: false
                required: [data]
                properties:
                  data:
                    type: array
                    items: { $ref: '#/components/schemas/ProfileInfo' }
        '400': { $ref: '#/components/responses/BadRequest' }
        '401': { $ref: '#/components/responses/Unauthorized' }
        '422': { $ref: '#/components/responses/Unprocessable' }
        '429': { $ref: '#/components/responses/RateLimited' }
        '500': { $ref: '#/components/responses/InternalError' }

  /v1/sessions:
    post:
      tags: [sessions]
      operationId: createSession
      summary: Create a session
      parameters:
        - $ref: '#/components/parameters/IdempotencyKey'
        - $ref: '#/components/parameters/AghooVersion'
        - $ref: '#/components/parameters/RequestIdHeader'
        - $ref: '#/components/parameters/Traceparent'
        - $ref: '#/components/parameters/Tracestate'
        - $ref: '#/components/parameters/EndUser'
      requestBody:
        required: false
        content:
          application/json:
            schema: { $ref: '#/components/schemas/SessionCreate' }
      x-noema-error-codes: [schema_violation, invalid_api_key, invalid_end_user_token, permission_denied, idempotency_key_reused,
                            request_too_large, unsupported_media_type, feature_not_available, rpm_exceeded, internal_error]
      responses:
        '201':
          description: Session
          headers:
            x-request-id: { $ref: '#/components/headers/RequestId' }
            x-aghoo-idempotent-replay: { $ref: '#/components/headers/IdempotentReplay' }
          content:
            application/json:
              schema: { $ref: '#/components/schemas/Session' }
        '400': { $ref: '#/components/responses/BadRequest' }
        '401': { $ref: '#/components/responses/Unauthorized' }
        '403': { $ref: '#/components/responses/Forbidden' }
        '409': { $ref: '#/components/responses/Conflict' }
        '413': { $ref: '#/components/responses/PayloadTooLarge' }
        '415': { $ref: '#/components/responses/UnsupportedMediaType' }
        '422': { $ref: '#/components/responses/Unprocessable' }
        '429': { $ref: '#/components/responses/RateLimited' }
        '500': { $ref: '#/components/responses/InternalError' }
    get:
      tags: [sessions]
      operationId: listSessions
      summary: List the caller's sessions (newest first)
      parameters:
        - $ref: '#/components/parameters/Limit'
        - $ref: '#/components/parameters/After'
        - $ref: '#/components/parameters/AghooVersion'
        - $ref: '#/components/parameters/RequestIdHeader'
        - $ref: '#/components/parameters/Traceparent'
        - $ref: '#/components/parameters/Tracestate'
        - $ref: '#/components/parameters/EndUser'
      x-noema-error-codes: [schema_violation, invalid_api_key, invalid_end_user_token, feature_not_available, rpm_exceeded, internal_error]
      responses:
        '200':
          description: Page of sessions
          headers:
            x-request-id: { $ref: '#/components/headers/RequestId' }
          content:
            application/json:
              schema: { $ref: '#/components/schemas/SessionList' }
        '400': { $ref: '#/components/responses/BadRequest' }
        '401': { $ref: '#/components/responses/Unauthorized' }
        '422': { $ref: '#/components/responses/Unprocessable' }
        '429': { $ref: '#/components/responses/RateLimited' }
        '500': { $ref: '#/components/responses/InternalError' }
  /v1/sessions/{session_id}:
    get:
      tags: [sessions]
      operationId: getSession
      summary: Retrieve a session
      parameters:
        - $ref: '#/components/parameters/SessionId'
        - $ref: '#/components/parameters/AghooVersion'
        - $ref: '#/components/parameters/RequestIdHeader'
        - $ref: '#/components/parameters/Traceparent'
        - $ref: '#/components/parameters/Tracestate'
        - $ref: '#/components/parameters/EndUser'
      x-noema-error-codes: [invalid_api_key, invalid_end_user_token, session_not_found, feature_not_available, rpm_exceeded, internal_error]
      responses:
        '200':
          description: Session
          headers:
            x-request-id: { $ref: '#/components/headers/RequestId' }
          content:
            application/json:
              schema: { $ref: '#/components/schemas/Session' }
        '401': { $ref: '#/components/responses/Unauthorized' }
        '404': { $ref: '#/components/responses/NotFound' }
        '422': { $ref: '#/components/responses/Unprocessable' }
        '429': { $ref: '#/components/responses/RateLimited' }
        '500': { $ref: '#/components/responses/InternalError' }
    patch:
      tags: [sessions]
      operationId: updateSession
      summary: Update metadata, retention or data class
      description: Present fields replace the stored value (`metadata` replaces the whole map). A new `data_class` applies to turns created afterwards; stored content is not re-processed.
      parameters:
        - $ref: '#/components/parameters/SessionId'
        - $ref: '#/components/parameters/AghooVersion'
        - $ref: '#/components/parameters/RequestIdHeader'
        - $ref: '#/components/parameters/Traceparent'
        - $ref: '#/components/parameters/Tracestate'
        - $ref: '#/components/parameters/EndUser'
      requestBody:
        required: true
        content:
          application/json:
            schema: { $ref: '#/components/schemas/SessionUpdate' }
      x-noema-error-codes: [schema_violation, invalid_api_key, invalid_end_user_token, permission_denied, session_forbidden,
                            session_not_found, request_too_large, unsupported_media_type, feature_not_available, rpm_exceeded,
                            internal_error]
      responses:
        '200':
          description: Session
          headers:
            x-request-id: { $ref: '#/components/headers/RequestId' }
          content:
            application/json:
              schema: { $ref: '#/components/schemas/Session' }
        '400': { $ref: '#/components/responses/BadRequest' }
        '401': { $ref: '#/components/responses/Unauthorized' }
        '403': { $ref: '#/components/responses/Forbidden' }
        '404': { $ref: '#/components/responses/NotFound' }
        '413': { $ref: '#/components/responses/PayloadTooLarge' }
        '415': { $ref: '#/components/responses/UnsupportedMediaType' }
        '422': { $ref: '#/components/responses/Unprocessable' }
        '429': { $ref: '#/components/responses/RateLimited' }
        '500': { $ref: '#/components/responses/InternalError' }
    delete:
      tags: [sessions]
      operationId: deleteSession
      summary: Delete a session (cascading, asynchronous)
      parameters:
        - $ref: '#/components/parameters/SessionId'
        - $ref: '#/components/parameters/AghooVersion'
        - $ref: '#/components/parameters/RequestIdHeader'
        - $ref: '#/components/parameters/Traceparent'
        - $ref: '#/components/parameters/Tracestate'
        - $ref: '#/components/parameters/EndUser'
      x-noema-error-codes: [invalid_api_key, invalid_end_user_token, permission_denied, session_forbidden, session_not_found,
                            feature_not_available, rpm_exceeded, internal_error]
      responses:
        '202':
          description: Deletion job started
          headers:
            x-request-id: { $ref: '#/components/headers/RequestId' }
          content:
            application/json:
              schema:
                type: object
                additionalProperties: false
                required: [id, deleted, deletion_job_id]
                properties:
                  id: { type: string }
                  deleted: { const: true }
                  deletion_job_id: { type: string }
        '401': { $ref: '#/components/responses/Unauthorized' }
        '403': { $ref: '#/components/responses/Forbidden' }
        '404': { $ref: '#/components/responses/NotFound' }
        '422': { $ref: '#/components/responses/Unprocessable' }
        '429': { $ref: '#/components/responses/RateLimited' }
        '500': { $ref: '#/components/responses/InternalError' }
  /v1/sessions/{session_id}/turns:
    get:
      tags: [sessions]
      operationId: listSessionTurns
      summary: Authorized history (paged, oldest first)
      parameters:
        - $ref: '#/components/parameters/SessionId'
        - $ref: '#/components/parameters/Limit'
        - $ref: '#/components/parameters/After'
        - $ref: '#/components/parameters/AghooVersion'
        - $ref: '#/components/parameters/RequestIdHeader'
        - $ref: '#/components/parameters/Traceparent'
        - $ref: '#/components/parameters/Tracestate'
        - $ref: '#/components/parameters/EndUser'
      x-noema-error-codes: [schema_violation, invalid_api_key, invalid_end_user_token, session_not_found, feature_not_available,
                            rpm_exceeded, internal_error]
      responses:
        '200':
          description: Page of history messages (each committed turn yields its user message, then its assistant message)
          headers:
            x-request-id: { $ref: '#/components/headers/RequestId' }
          content:
            application/json:
              schema: { $ref: '#/components/schemas/SessionTurnList' }
        '400': { $ref: '#/components/responses/BadRequest' }
        '401': { $ref: '#/components/responses/Unauthorized' }
        '404': { $ref: '#/components/responses/NotFound' }
        '422': { $ref: '#/components/responses/Unprocessable' }
        '429': { $ref: '#/components/responses/RateLimited' }
        '500': { $ref: '#/components/responses/InternalError' }

  /v1/files:
    post:
      tags: [files]
      operationId: uploadFile
      summary: Upload a file (validated and extracted asynchronously)
      parameters:
        - $ref: '#/components/parameters/IdempotencyKey'
        - $ref: '#/components/parameters/AghooVersion'
        - $ref: '#/components/parameters/RequestIdHeader'
        - $ref: '#/components/parameters/Traceparent'
        - $ref: '#/components/parameters/Tracestate'
        - $ref: '#/components/parameters/EndUser'
      requestBody:
        required: true
        content:
          multipart/form-data:
            schema:
              type: object
              required: [file, purpose]
              properties:
                file: { type: string, contentMediaType: application/octet-stream }
                purpose: { type: string, enum: [input] }
                session_id: { type: string }
      x-noema-error-codes: [schema_violation, invalid_api_key, invalid_end_user_token, permission_denied, session_not_found,
                            idempotency_key_reused, request_too_large, file_too_large, unsupported_media_type, feature_not_available,
                            rpm_exceeded, internal_error]
      responses:
        '202':
          description: Accepted; poll or watch for status
          headers:
            x-request-id: { $ref: '#/components/headers/RequestId' }
            x-aghoo-idempotent-replay: { $ref: '#/components/headers/IdempotentReplay' }
          content:
            application/json:
              schema: { $ref: '#/components/schemas/FileObject' }
        '400': { $ref: '#/components/responses/BadRequest' }
        '401': { $ref: '#/components/responses/Unauthorized' }
        '403': { $ref: '#/components/responses/Forbidden' }
        '404': { $ref: '#/components/responses/NotFound' }
        '409': { $ref: '#/components/responses/Conflict' }
        '413': { $ref: '#/components/responses/PayloadTooLarge' }
        '415': { $ref: '#/components/responses/UnsupportedMediaType' }
        '422': { $ref: '#/components/responses/Unprocessable' }
        '429': { $ref: '#/components/responses/RateLimited' }
        '500': { $ref: '#/components/responses/InternalError' }
  /v1/files/{file_id}:
    get:
      tags: [files]
      operationId: getFile
      summary: File status and derivatives
      parameters:
        - $ref: '#/components/parameters/FileId'
        - $ref: '#/components/parameters/AghooVersion'
        - $ref: '#/components/parameters/RequestIdHeader'
        - $ref: '#/components/parameters/Traceparent'
        - $ref: '#/components/parameters/Tracestate'
        - $ref: '#/components/parameters/EndUser'
      x-noema-error-codes: [invalid_api_key, invalid_end_user_token, not_found, feature_not_available, rpm_exceeded, internal_error]
      responses:
        '200':
          description: File
          headers:
            x-request-id: { $ref: '#/components/headers/RequestId' }
          content:
            application/json:
              schema: { $ref: '#/components/schemas/FileObject' }
        '401': { $ref: '#/components/responses/Unauthorized' }
        '404': { $ref: '#/components/responses/NotFound' }
        '422': { $ref: '#/components/responses/Unprocessable' }
        '429': { $ref: '#/components/responses/RateLimited' }
        '500': { $ref: '#/components/responses/InternalError' }
    delete:
      tags: [files]
      operationId: deleteFile
      summary: Delete a file and its derivatives
      parameters:
        - $ref: '#/components/parameters/FileId'
        - $ref: '#/components/parameters/AghooVersion'
        - $ref: '#/components/parameters/RequestIdHeader'
        - $ref: '#/components/parameters/Traceparent'
        - $ref: '#/components/parameters/Tracestate'
        - $ref: '#/components/parameters/EndUser'
      x-noema-error-codes: [invalid_api_key, invalid_end_user_token, permission_denied, not_found, feature_not_available, rpm_exceeded,
                            internal_error]
      responses:
        '200':
          description: Deleted
          headers:
            x-request-id: { $ref: '#/components/headers/RequestId' }
          content:
            application/json:
              schema:
                type: object
                additionalProperties: false
                required: [id, deleted]
                properties:
                  id: { type: string }
                  deleted: { const: true }
        '401': { $ref: '#/components/responses/Unauthorized' }
        '403': { $ref: '#/components/responses/Forbidden' }
        '404': { $ref: '#/components/responses/NotFound' }
        '422': { $ref: '#/components/responses/Unprocessable' }
        '429': { $ref: '#/components/responses/RateLimited' }
        '500': { $ref: '#/components/responses/InternalError' }
  /v1/files/{file_id}/events:
    get:
      tags: [files]
      operationId: watchFile
      summary: SSE stream of file status changes until a terminal status
      parameters:
        - $ref: '#/components/parameters/FileId'
        - $ref: '#/components/parameters/AghooVersion'
        - $ref: '#/components/parameters/RequestIdHeader'
        - $ref: '#/components/parameters/Traceparent'
        - $ref: '#/components/parameters/Tracestate'
        - $ref: '#/components/parameters/EndUser'
      x-noema-error-codes: [invalid_api_key, invalid_end_user_token, not_found, feature_not_available, rpm_exceeded, internal_error]
      responses:
        '200':
          description: |
            SSE stream of FileObject snapshots. Each frame is `id: <n>` (1, 2, … per stream), `event: file.updated` and one
            line of `data: <FileObject JSON>`. The first frame is the current snapshot; the server closes the stream after the
            first snapshot whose status is `ready`, `partial`, `rejected`, `failed` or `deleted`. A `: ping` comment is sent
            whenever no frame was written for 15 s.
          headers:
            x-request-id: { $ref: '#/components/headers/RequestId' }
          content:
            text/event-stream:
              schema: { $ref: '#/components/schemas/FileObject' }
        '401': { $ref: '#/components/responses/Unauthorized' }
        '404': { $ref: '#/components/responses/NotFound' }
        '422': { $ref: '#/components/responses/Unprocessable' }
        '429': { $ref: '#/components/responses/RateLimited' }
        '500': { $ref: '#/components/responses/InternalError' }
  /v1/files/{file_id}/derivatives/{kind}:
    get:
      tags: [files]
      operationId: getFileDerivative
      summary: Download a derivative (e.g. markdown, OCR text, a page image)
      parameters:
        - $ref: '#/components/parameters/FileId'
        - name: kind
          in: path
          required: true
          schema: { $ref: '#/components/schemas/DerivativeKind' }
        - name: page
          in: query
          required: false
          description: 1-based page number. Required for `page_images` (at most the derivative's `pages`); 400 `schema_violation` for any other kind.
          schema: { type: integer, minimum: 1, maximum: 600 }
        - $ref: '#/components/parameters/AghooVersion'
        - $ref: '#/components/parameters/RequestIdHeader'
        - $ref: '#/components/parameters/Traceparent'
        - $ref: '#/components/parameters/Tracestate'
        - $ref: '#/components/parameters/EndUser'
      x-noema-error-codes: [schema_violation, invalid_api_key, invalid_end_user_token, not_found, file_not_ready, file_rejected,
                            feature_not_available, rpm_exceeded, internal_error]
      responses:
        '200':
          description: |
            Derivative content: `markdown` → text/markdown; `text`, `ocr_text` → text/plain; `raster`, `thumbnail`,
            `page_images` → image/png or image/jpeg (as re-encoded); `pdf_sanitized` → application/pdf. A kind the file
            doesn't have is 404 `not_found`; a pending derivative is 409 `file_not_ready`.
          headers:
            x-request-id: { $ref: '#/components/headers/RequestId' }
            content-disposition: { $ref: '#/components/headers/ContentDisposition' }
            x-content-type-options: { $ref: '#/components/headers/ContentTypeOptions' }
          content:
            text/markdown:
              schema: { type: string }
            text/plain:
              schema: { type: string }
            image/png:
              schema: { type: string, contentMediaType: image/png }
            image/jpeg:
              schema: { type: string, contentMediaType: image/jpeg }
            application/pdf:
              schema: { type: string, contentMediaType: application/pdf }
        '400': { $ref: '#/components/responses/BadRequest' }
        '401': { $ref: '#/components/responses/Unauthorized' }
        '404': { $ref: '#/components/responses/NotFound' }
        '409': { $ref: '#/components/responses/Conflict' }
        '422': { $ref: '#/components/responses/Unprocessable' }
        '429': { $ref: '#/components/responses/RateLimited' }
        '500': { $ref: '#/components/responses/InternalError' }

  /v1/usage:
    get:
      tags: [usage]
      operationId: getUsage
      summary: Usage and cost by project, session, model, day or line
      description: |
        One row per (key, line) over the inclusive UTC date range, ordered by key, then line. Rows cover what the caller
        may see (its project; the whole tenant for `tenant_admin`). `to` defaults to today (UTC) and `from` to `to` minus
        29 days; a range longer than 366 days is 400 `schema_violation`.
      parameters:
        - name: group_by
          in: query
          required: false
          schema: { type: string, enum: [project, session, model, day, line], default: line }
        - name: from
          in: query
          required: false
          schema: { type: string, format: date }
        - name: to
          in: query
          required: false
          schema: { type: string, format: date }
        - $ref: '#/components/parameters/Limit'
        - $ref: '#/components/parameters/After'
        - $ref: '#/components/parameters/AghooVersion'
        - $ref: '#/components/parameters/RequestIdHeader'
        - $ref: '#/components/parameters/Traceparent'
        - $ref: '#/components/parameters/Tracestate'
        - $ref: '#/components/parameters/EndUser'
      x-noema-error-codes: [schema_violation, invalid_api_key, invalid_end_user_token, permission_denied, feature_not_available,
                            rpm_exceeded, internal_error]
      responses:
        '200':
          description: Usage rows
          headers:
            x-request-id: { $ref: '#/components/headers/RequestId' }
          content:
            application/json:
              schema: { $ref: '#/components/schemas/UsageReport' }
        '400': { $ref: '#/components/responses/BadRequest' }
        '401': { $ref: '#/components/responses/Unauthorized' }
        '403': { $ref: '#/components/responses/Forbidden' }
        '422': { $ref: '#/components/responses/Unprocessable' }
        '429': { $ref: '#/components/responses/RateLimited' }
        '500': { $ref: '#/components/responses/InternalError' }

  /v1/admin/calibrators:
    get:
      tags: [admin]
      operationId: listCalibrators
      summary: Calibrator artifacts with holdout metrics and reliability bins (read-only, W4)
      description: The tenant's calibrators plus the platform-default ones that can serve it, newest first. Roles tenant_admin, reviewer and auditor; any other role is 403 `permission_denied`.
      x-noema-roles: [tenant_admin, reviewer, auditor]
      parameters:
        - name: profile_id
          in: query
          required: false
          schema: { type: string }
        - name: question_name
          in: query
          required: false
          schema: { type: string }
        - name: status
          in: query
          required: false
          schema: { $ref: '#/components/schemas/CalibratorStatus' }
        - $ref: '#/components/parameters/Limit'
        - $ref: '#/components/parameters/After'
        - $ref: '#/components/parameters/AghooVersion'
        - $ref: '#/components/parameters/RequestIdHeader'
        - $ref: '#/components/parameters/Traceparent'
        - $ref: '#/components/parameters/Tracestate'
        - $ref: '#/components/parameters/EndUser'
      x-noema-error-codes: [schema_violation, invalid_api_key, invalid_end_user_token, permission_denied, feature_not_available, rpm_exceeded,
                            internal_error]
      responses:
        '200':
          description: Page of calibrators
          headers:
            x-request-id: { $ref: '#/components/headers/RequestId' }
          content:
            application/json:
              schema: { $ref: '#/components/schemas/CalibratorList' }
        '400': { $ref: '#/components/responses/BadRequest' }
        '401': { $ref: '#/components/responses/Unauthorized' }
        '403': { $ref: '#/components/responses/Forbidden' }
        '422': { $ref: '#/components/responses/Unprocessable' }
        '429': { $ref: '#/components/responses/RateLimited' }
        '500': { $ref: '#/components/responses/InternalError' }
  /v1/admin/labeling/runs:
    get:
      tags: [admin]
      operationId: listLabelingRuns
      summary: LLM-consensus labeling runs with consensus rates per class (read-only, W4)
      description: Newest first. Roles tenant_admin, reviewer and auditor; any other role is 403 `permission_denied`.
      x-noema-roles: [tenant_admin, reviewer, auditor]
      parameters:
        - name: profile_id
          in: query
          required: false
          schema: { type: string }
        - name: question_name
          in: query
          required: false
          schema: { type: string }
        - name: status
          in: query
          required: false
          schema: { type: string, enum: [running, completed, failed, cancelled] }
        - $ref: '#/components/parameters/Limit'
        - $ref: '#/components/parameters/After'
        - $ref: '#/components/parameters/AghooVersion'
        - $ref: '#/components/parameters/RequestIdHeader'
        - $ref: '#/components/parameters/Traceparent'
        - $ref: '#/components/parameters/Tracestate'
        - $ref: '#/components/parameters/EndUser'
      x-noema-error-codes: [schema_violation, invalid_api_key, invalid_end_user_token, permission_denied, feature_not_available, rpm_exceeded,
                            internal_error]
      responses:
        '200':
          description: Page of labeling runs
          headers:
            x-request-id: { $ref: '#/components/headers/RequestId' }
          content:
            application/json:
              schema: { $ref: '#/components/schemas/LabelingRunList' }
        '400': { $ref: '#/components/responses/BadRequest' }
        '401': { $ref: '#/components/responses/Unauthorized' }
        '403': { $ref: '#/components/responses/Forbidden' }
        '422': { $ref: '#/components/responses/Unprocessable' }
        '429': { $ref: '#/components/responses/RateLimited' }
        '500': { $ref: '#/components/responses/InternalError' }
  /v1/admin/labeling/dropped:
    get:
      tags: [admin]
      operationId: listLabelingDropped
      summary: Sampled items the labelers did not agree on, with both labelers' answers (read-only, W4)
      description: Newest first. Roles tenant_admin and reviewer; any other role is 403 `permission_denied`. `content.excerpt` is null unless the item's data class and log policy allow stored content.
      x-noema-roles: [tenant_admin, reviewer]
      parameters:
        - name: run_id
          in: query
          required: false
          schema: { type: string, pattern: '^lrun_' }
        - name: question_name
          in: query
          required: false
          schema: { type: string }
        - name: reason
          in: query
          required: false
          schema: { type: string, enum: [disagreed, refused, parse_error, labeler_error] }
        - $ref: '#/components/parameters/Limit'
        - $ref: '#/components/parameters/After'
        - $ref: '#/components/parameters/AghooVersion'
        - $ref: '#/components/parameters/RequestIdHeader'
        - $ref: '#/components/parameters/Traceparent'
        - $ref: '#/components/parameters/Tracestate'
        - $ref: '#/components/parameters/EndUser'
      x-noema-error-codes: [schema_violation, invalid_api_key, invalid_end_user_token, permission_denied, feature_not_available, rpm_exceeded,
                            internal_error]
      responses:
        '200':
          description: Page of dropped items
          headers:
            x-request-id: { $ref: '#/components/headers/RequestId' }
          content:
            application/json:
              schema: { $ref: '#/components/schemas/LabelingDroppedList' }
        '400': { $ref: '#/components/responses/BadRequest' }
        '401': { $ref: '#/components/responses/Unauthorized' }
        '403': { $ref: '#/components/responses/Forbidden' }
        '422': { $ref: '#/components/responses/Unprocessable' }
        '429': { $ref: '#/components/responses/RateLimited' }
        '500': { $ref: '#/components/responses/InternalError' }

  /healthz:
    get:
      tags: [health]
      operationId: healthz
      summary: Liveness
      security: []
      responses:
        '200': { description: Alive }
        '429': { description: Probe rate limited }
  /readyz:
    get:
      tags: [health]
      operationId: readyz
      summary: Readiness (config loaded, dependencies reachable)
      security: []
      responses:
        '200': { description: Ready }
        '429': { description: Probe rate limited }
        '503': { description: Not ready }

components:
  securitySchemes:
    bearerAuth:
      type: http
      scheme: bearer
      description: API key (ak_<env>_<id>.<secret>) or OIDC access token. Phase 1 accepts API keys only; any other token is 401 `invalid_api_key`.

  parameters:
    IdempotencyKey:
      name: Idempotency-Key
      in: header
      required: false
      description: Unique per (project, method, path) for 24 h and bound to a fingerprint of the canonical request body (03 §1).
      schema: { type: string, minLength: 1, maxLength: 128, pattern: '^[\x21-\x7E]+$' }
    AghooVersion:
      name: Aghoo-Version
      in: header
      required: false
      description: Pins API behavior. The enum lists the supported versions; the default is the current one. Any other value is 400 `schema_violation` (`param` = `Aghoo-Version`).
      schema: { type: string, enum: ['2026-10-01'], default: '2026-10-01' }
    DeadlineMs:
      name: Aghoo-Deadline-Ms
      in: header
      required: false
      description: End-to-end budget in milliseconds from request receipt (default 120000). Decision deadline and holdback are derived from it.
      schema: { type: integer, minimum: 100, maximum: 600000, default: 120000 }
    RequestIdHeader:
      name: x-request-id
      in: header
      required: false
      description: Client request id. A value of 1-128 characters from [A-Za-z0-9._:-] is echoed in the `x-request-id` response header and in `error.request_id`; any other value is replaced by a server id (never rejected).
      schema: { type: string }
    Traceparent:
      name: traceparent
      in: header
      required: false
      description: W3C Trace Context parent; the server span joins this trace. An invalid value starts a new trace (never rejected).
      schema: { type: string }
    Tracestate:
      name: tracestate
      in: header
      required: false
      description: W3C Trace Context vendor state, propagated with `traceparent`.
      schema: { type: string }
    EndUser:
      name: x-aghoo-end-user
      in: header
      required: false
      description: |
        Signed, audience-bound end-user JWT from the project's identity provider (04 §4.2); history and sessions are then
        scoped to that end user. An invalid, expired or wrong-audience token is 401 `invalid_end_user_token`. On a project
        without end-user verification configured, the header is 422 `feature_not_available`.
      schema: { type: string }
    TurnId:
      name: turn_id
      in: path
      required: true
      schema: { type: string }
    SessionId:
      name: session_id
      in: path
      required: true
      schema: { type: string, pattern: '^ses_' }
    FileId:
      name: file_id
      in: path
      required: true
      schema: { type: string, pattern: '^file_' }
    Limit:
      name: limit
      in: query
      required: false
      schema: { type: integer, minimum: 1, maximum: 200, default: 50 }
    After:
      name: after
      in: query
      required: false
      description: Opaque cursor from `next_cursor`. An unknown or tampered cursor is 400 `schema_violation`.
      schema: { type: string }

  headers:
    RequestId:
      description: Echo of a valid client `x-request-id`, else a server id (`req_<ULID>`). Equals `error.request_id`.
      required: true
      schema: { type: string }
    RateLimitLimitRequests:
      description: Requests per minute allowed by the most constraining bucket (key, end user, project or the tenant's model quota).
      schema: { type: integer, minimum: 0 }
    RateLimitRemainingRequests:
      description: Requests left in that bucket.
      schema: { type: integer, minimum: 0 }
    RateLimitResetRequests:
      description: Seconds (rounded up) until that bucket is full again.
      schema: { type: integer, minimum: 0 }
    RateLimitLimitTokens:
      description: Tokens per minute (input estimate + reserved max_output_tokens) allowed by the most constraining token bucket.
      schema: { type: integer, minimum: 0 }
    RateLimitRemainingTokens:
      description: Tokens left in that bucket after this request's reservation.
      schema: { type: integer, minimum: 0 }
    RateLimitResetTokens:
      description: Seconds (rounded up) until that bucket is full again.
      schema: { type: integer, minimum: 0 }
    RetryAfter:
      description: Seconds to wait before retrying (integer, at least 1). Absent when a retry cannot help (`budget_exhausted`, `tenant_access_misconfigured`).
      schema: { type: integer, minimum: 1 }
    IdempotentReplay:
      description: '`true` when the response comes from an earlier request with the same Idempotency-Key (its stored response, or attached to it while in flight): no new model calls and no new billing. Absent otherwise.'
      schema: { type: string, enum: ['true'] }
    WwwAuthenticate:
      description: Bearer challenge.
      schema: { type: string }
    ContentDisposition:
      description: '`attachment; filename="<file_id>-<kind>[-<page>].<ext>"`. The uploaded filename is never used.'
      schema: { type: string }
    ContentTypeOptions:
      description: Always `nosniff`.
      schema: { type: string, enum: [nosniff] }

  responses:
    BadRequest:
      description: '400 `invalid_request_error`: schema_violation, unsupported_question_type, reserved_question_name, model_not_available.'
      headers:
        x-request-id: { $ref: '#/components/headers/RequestId' }
      content:
        application/json:
          schema: { $ref: '#/components/schemas/Error' }
    Unauthorized:
      description: '401 `authentication_error`: invalid_api_key, invalid_end_user_token. The presented credential is never echoed.'
      headers:
        x-request-id: { $ref: '#/components/headers/RequestId' }
        www-authenticate: { $ref: '#/components/headers/WwwAuthenticate' }
      content:
        application/json:
          schema: { $ref: '#/components/schemas/Error' }
    Forbidden:
      description: '403 `permission_error`: permission_denied, profile_not_allowed, session_forbidden.'
      headers:
        x-request-id: { $ref: '#/components/headers/RequestId' }
      content:
        application/json:
          schema: { $ref: '#/components/schemas/Error' }
    NotFound:
      description: '404 `not_found_error`: not_found, session_not_found (also for ids of other tenants, projects or end users).'
      headers:
        x-request-id: { $ref: '#/components/headers/RequestId' }
      content:
        application/json:
          schema: { $ref: '#/components/schemas/Error' }
    Conflict:
      description: '409 `conflict_error`: idempotency_key_reused, session_conflict, session_busy, file_not_ready, file_rejected.'
      headers:
        x-request-id: { $ref: '#/components/headers/RequestId' }
      content:
        application/json:
          schema: { $ref: '#/components/schemas/Error' }
    Gone:
      description: '410 `invalid_request_error`: resume_window_expired.'
      headers:
        x-request-id: { $ref: '#/components/headers/RequestId' }
      content:
        application/json:
          schema: { $ref: '#/components/schemas/Error' }
    PayloadTooLarge:
      description: '413 `invalid_request_error`: request_too_large, file_too_large, image_too_large.'
      headers:
        x-request-id: { $ref: '#/components/headers/RequestId' }
      content:
        application/json:
          schema: { $ref: '#/components/schemas/Error' }
    UnsupportedMediaType:
      description: '415 `invalid_request_error`: unsupported_media_type (the request Content-Type, or an upload type outside the phase allowlist).'
      headers:
        x-request-id: { $ref: '#/components/headers/RequestId' }
      content:
        application/json:
          schema: { $ref: '#/components/schemas/Error' }
    Unprocessable:
      description: '422 `capability_error`: no_route_satisfies_constraints (`details.unmet`), no_capable_member, unsupported_parameter, feature_not_available (`param` names the out-of-phase field or header).'
      headers:
        x-request-id: { $ref: '#/components/headers/RequestId' }
      content:
        application/json:
          schema: { $ref: '#/components/schemas/Error' }
    RateLimited:
      description: '429 `rate_limit_error`: rpm_exceeded, tpm_exceeded, queue_timeout, budget_exhausted.'
      headers:
        x-request-id: { $ref: '#/components/headers/RequestId' }
        retry-after: { $ref: '#/components/headers/RetryAfter' }
        x-ratelimit-limit-requests: { $ref: '#/components/headers/RateLimitLimitRequests' }
        x-ratelimit-remaining-requests: { $ref: '#/components/headers/RateLimitRemainingRequests' }
        x-ratelimit-reset-requests: { $ref: '#/components/headers/RateLimitResetRequests' }
        x-ratelimit-limit-tokens: { $ref: '#/components/headers/RateLimitLimitTokens' }
        x-ratelimit-remaining-tokens: { $ref: '#/components/headers/RateLimitRemainingTokens' }
        x-ratelimit-reset-tokens: { $ref: '#/components/headers/RateLimitResetTokens' }
      content:
        application/json:
          schema: { $ref: '#/components/schemas/Error' }
    InternalError:
      description: '500 `server_error`: internal_error (an unexpected failure; retryable).'
      headers:
        x-request-id: { $ref: '#/components/headers/RequestId' }
      content:
        application/json:
          schema: { $ref: '#/components/schemas/Error' }
    NotImplemented:
      description: '501 `invalid_request_error`: not_implemented (the operation is not served in this phase).'
      headers:
        x-request-id: { $ref: '#/components/headers/RequestId' }
      content:
        application/json:
          schema: { $ref: '#/components/schemas/Error' }
    Unavailable:
      description: '503 `upstream_error`: all_routes_unavailable (retryable), tenant_access_misconfigured (not retryable).'
      headers:
        x-request-id: { $ref: '#/components/headers/RequestId' }
        retry-after: { $ref: '#/components/headers/RetryAfter' }
      content:
        application/json:
          schema: { $ref: '#/components/schemas/Error' }
    GatewayTimeout:
      description: '504 `upstream_error`: deadline_exceeded.'
      headers:
        x-request-id: { $ref: '#/components/headers/RequestId' }
      content:
        application/json:
          schema: { $ref: '#/components/schemas/Error' }

  schemas:
    Error:
      type: object
      additionalProperties: false
      required: [error]
      properties:
        error:
          type: object
          additionalProperties: false
          required: [type, code, message, param, request_id, retryable]
          properties:
            type:
              type: string
              enum: [invalid_request_error, authentication_error, permission_error, not_found_error, conflict_error,
                     capability_error, rate_limit_error, upstream_error, server_error]
            code: { $ref: '#/components/schemas/ErrorCode' }
            message: { type: string }
            param: { type: [string, 'null'], description: 'Offending field as a path (e.g. `questions[1].choices`) or a header name; null when no single field is at fault.' }
            request_id: { type: string }
            retryable: { type: boolean }
            details: { type: object }
    ErrorCode:
      type: string
      description: 'Each code has one HTTP status, `error.type` and `retryable` value (03 §1). `upstream_stalled` and `client_too_slow` occur only as SSE `error` events.'
      enum: [schema_violation, unsupported_question_type, reserved_question_name, unsupported_parameter, model_not_available, profile_not_allowed,
             invalid_api_key, session_forbidden, session_not_found, idempotency_key_reused, file_not_ready, file_rejected,
             file_too_large, image_too_large, unsupported_media_type, no_route_satisfies_constraints, no_capable_member,
             rpm_exceeded, tpm_exceeded, budget_exhausted, queue_timeout, all_routes_unavailable, deadline_exceeded,
             tenant_access_misconfigured, upstream_stalled, client_too_slow, resume_window_expired, session_conflict, session_busy,
             not_found, permission_denied, invalid_end_user_token, request_too_large, feature_not_available, internal_error,
             not_implemented]

    Effort: { type: string, enum: [none, low, medium, high, xhigh, max] }
    AnswerMode: { type: string, enum: [direct, cover_top2_and_clarify, clarify_first] }
    Strategy: { type: string, enum: [gate, holdback, speculative, hedge] }

    TurnCreateParams:
      type: object
      additionalProperties: false
      required: [input]
      properties:
        session_id: { type: string, pattern: '^ses_' }
        store: { type: boolean, default: true }
        expected_seq:
          type: integer
          minimum: 0
          description: Optimistic session ordering. The `seq` of the latest committed turn the client has seen; a different server value is 409 `session_conflict`. Without it, concurrent turns on one session are serialized (409 `session_busy` after the queue timeout).
        stream: { type: boolean, default: true }
        wait_for_files: { type: boolean, default: false }
        allow_partial_files: { type: boolean, default: false }
        retry_of: { type: string }
        instructions: { type: string, maxLength: 100000 }
        input:
          type: array
          minItems: 1
          items:
            type: object
            additionalProperties: false
            required: [role, content]
            properties:
              role: { const: user }
              content:
                type: array
                minItems: 1
                items: { $ref: './decision.schema.json#/$defs/InputPart' }
        decisions:
          type: object
          additionalProperties: false
          properties:
            profile: { type: string }
            questions: { type: array, maxItems: 32, items: { $ref: './decision.schema.json#/$defs/Question' } }
            blocking: { type: array, items: { type: string } }
            deadline_ms: { type: integer, minimum: 100, maximum: 30000 }
            min_quorum: { type: integer, minimum: 1 }
            include: { type: array, items: { type: string, enum: [members, member_distributions, stages] } }
        generation:
          type: object
          additionalProperties: false
          properties:
            profile: { type: string, description: '"auto" or a generation profile id from the allowlist. Phase 1: a `best_of_n` or `fuse` profile is 422 `feature_not_available`.' }
            effort: { $ref: '#/components/schemas/Effort' }
            max_output_tokens: { type: integer, minimum: 1 }
            temperature: { type: number, minimum: 0, maximum: 2 }
        strategy:
          description: 'Phase 1 serves `auto`, `gate` and `holdback`; `speculative` and `hedge` are 422 `feature_not_available`.'
          oneOf:
            - { const: auto }
            - { $ref: '#/components/schemas/Strategy' }
        conditioning:
          oneOf:
            - { type: string, enum: [auto, 'off'] }
            - { $ref: '#/components/schemas/AnswerMode' }
        verify: { type: boolean }
        history:
          type: object
          additionalProperties: false
          required: [mode]
          properties:
            mode: { type: string, enum: [auto, none, last_n] }
            max_tokens: { type: integer, minimum: 1 }
            last_n: { type: integer, minimum: 1 }
        metadata: { type: object, additionalProperties: { type: string } }

    Conditioning:
      type: object
      additionalProperties: false
      required: [answer_mode, signature, summary]
      properties:
        answer_mode: { $ref: '#/components/schemas/AnswerMode' }
        signature: { type: string }
        summary: { type: string }

    RoutingDecision:
      type: object
      additionalProperties: false
      required: [route_id, generation_profile, mode, selection, strategy, decision_id, policy_version, matched_bindings, basis, params, conditioning, served_spec_differs, reason, fallbacks]
      properties:
        route_id: { type: string }
        generation_profile: { type: string }
        mode: { type: string, enum: [single, fallback, race, best_of_n, fuse] }
        selection: { type: string, enum: [rule, expected_utility, pinned, sticky, default, fallback] }
        strategy: { $ref: '#/components/schemas/Strategy' }
        decision_id: { type: [string, 'null'] }
        policy_version: { type: string }
        matched_bindings: { type: array, items: { type: string } }
        basis:
          type: array
          items:
            type: object
            additionalProperties: false
            required: [name, value, probability, margin, band, status]
            properties:
              name: { type: string }
              value: { type: [string, boolean, number] }
              probability: { type: number }
              margin: { type: number }
              band: { type: string, enum: [high, medium, low] }
              status: { type: string, enum: [calibrated, provider_reported, uncalibrated] }
        params:
          type: object
          additionalProperties: false
          properties:
            effort: { $ref: '#/components/schemas/Effort' }
            max_output_tokens: { type: integer }
        conditioning:
          oneOf:
            - { type: 'null' }
            - { $ref: '#/components/schemas/Conditioning' }
        served_spec_differs:
          type: [boolean, 'null']
          description: true when the answer committed on spec0 at the holdback deadline and the decided spec differs; false when equal; null before a decision arrives.
        reason: { type: string }
        fallbacks: { type: array, items: { type: string } }

    Degradation:
      type: object
      additionalProperties: false
      required: [from, to, reason]
      properties:
        file_id: { type: string }
        part_index: { type: integer, minimum: 0 }
        attempt_id: { type: string }
        model: { type: string }
        from: { type: string }
        to: { type: string }
        reason:
          type: string
          enum: [model_capability, size_limit, media_budget, policy, param_unsupported, active_content_removed, not_ready, extraction_failed, rejected]

    TurnTimings:
      type: object
      additionalProperties: false
      required: [ttft_ms, decision_ms, decision_lead_ms, holdback_ms, e2e_ms, spec_restarts, wasted_output_tokens]
      properties:
        ttft_ms: { type: [integer, 'null'] }
        decision_ms: { type: [integer, 'null'] }
        decision_lead_ms: { type: [integer, 'null'] }
        holdback_ms: { type: [integer, 'null'] }
        e2e_ms: { type: integer }
        spec_restarts: { type: integer }
        wasted_output_tokens: { type: integer }

    TurnObject:
      type: object
      additionalProperties: false
      required: [turn_id, session_id, seq, text, decision, routing, attempts, verification, degradations, usage, timings]
      properties:
        turn_id: { type: string }
        session_id: { type: [string, 'null'] }
        seq: { type: [integer, 'null'], description: Position in the session (null when store is false) }
        text: { type: string }
        decision:
          oneOf:
            - { type: 'null' }
            - { $ref: './decision.schema.json#/$defs/DecisionResult' }
        routing: { $ref: '#/components/schemas/RoutingDecision' }
        attempts:
          type: array
          items:
            type: object
            additionalProperties: false
            required: [attempt_id, route_id, model, outcome, usage]
            properties:
              attempt_id: { type: string }
              route_id: { type: string }
              model: { type: string }
              outcome: { type: string, enum: [committed, cancelled, failed, not_selected] }
              usage: { $ref: './decision.schema.json#/$defs/Usage' }
        verification:
          oneOf:
            - { type: 'null' }
            - { $ref: './decision.schema.json#/$defs/DecisionResult' }
        degradations: { type: array, items: { $ref: '#/components/schemas/Degradation' } }
        usage: { $ref: './decision.schema.json#/$defs/Usage' }
        timings: { $ref: '#/components/schemas/TurnTimings' }

    TurnEvent:
      description: |
        One SSE `data:` payload. Each frame is `id: <n>` (monotonic per turn), `event: <type>` and one line of
        `data: <JSON>`. A `: ping` comment is sent whenever no frame was written for 15 s. Clients ignore unknown event types.
      oneOf:
        - { $ref: '#/components/schemas/TurnCreatedEvent' }
        - { $ref: '#/components/schemas/PreflightCompletedEvent' }
        - { $ref: '#/components/schemas/RouteSelectedEvent' }
        - { $ref: '#/components/schemas/DecisionCompletedEvent' }
        - { $ref: '#/components/schemas/DecisionUpdatedEvent' }
        - { $ref: '#/components/schemas/RouteConfirmedEvent' }
        - { $ref: '#/components/schemas/GenerationStartedEvent' }
        - { $ref: '#/components/schemas/GenerationDeltaEvent' }
        - { $ref: '#/components/schemas/GenerationCommittedEvent' }
        - { $ref: '#/components/schemas/GenerationCancelledEvent' }
        - { $ref: '#/components/schemas/GenerationReroutedEvent' }
        - { $ref: '#/components/schemas/GenerationCompletedEvent' }
        - { $ref: '#/components/schemas/TurnVerifiedEvent' }
        - { $ref: '#/components/schemas/TurnCompletedEvent' }
        - { $ref: '#/components/schemas/ErrorEvent' }
      discriminator:
        propertyName: type
        mapping:
          turn.created: '#/components/schemas/TurnCreatedEvent'
          preflight.completed: '#/components/schemas/PreflightCompletedEvent'
          route.selected: '#/components/schemas/RouteSelectedEvent'
          decision.completed: '#/components/schemas/DecisionCompletedEvent'
          decision.updated: '#/components/schemas/DecisionUpdatedEvent'
          route.confirmed: '#/components/schemas/RouteConfirmedEvent'
          generation.started: '#/components/schemas/GenerationStartedEvent'
          generation.delta: '#/components/schemas/GenerationDeltaEvent'
          generation.committed: '#/components/schemas/GenerationCommittedEvent'
          generation.cancelled: '#/components/schemas/GenerationCancelledEvent'
          generation.rerouted: '#/components/schemas/GenerationReroutedEvent'
          generation.completed: '#/components/schemas/GenerationCompletedEvent'
          turn.verified: '#/components/schemas/TurnVerifiedEvent'
          turn.completed: '#/components/schemas/TurnCompletedEvent'
          error: '#/components/schemas/ErrorEvent'
    TurnCreatedEvent:
      type: object
      additionalProperties: false
      required: [type, turn_id, session_id, created_at]
      properties:
        type: { type: string, const: turn.created }
        turn_id: { type: string }
        session_id: { type: [string, 'null'] }
        created_at: { type: integer }
    PreflightCompletedEvent:
      type: object
      additionalProperties: false
      required: [type, modalities, est_input_tokens, degradations]
      properties:
        type: { type: string, const: preflight.completed }
        modalities: { type: array, items: { type: string, enum: [text, image, audio, video, document] } }
        est_input_tokens: { type: integer }
        degradations: { type: array, items: { $ref: '#/components/schemas/Degradation' } }
    RouteSelectedEvent:
      type: object
      additionalProperties: false
      required: [type, attempt_id, routing, speculative, degradations]
      properties:
        type: { type: string, const: route.selected }
        attempt_id: { type: string }
        routing: { $ref: '#/components/schemas/RoutingDecision' }
        speculative: { type: boolean }
        degradations: { type: array, items: { $ref: '#/components/schemas/Degradation' } }
    DecisionCompletedEvent:
      type: object
      additionalProperties: false
      required: [type, decision, arrived]
      properties:
        type: { type: string, const: decision.completed }
        decision: { $ref: './decision.schema.json#/$defs/DecisionResult' }
        arrived: { type: string, enum: [before_generation, during_holdback, after_commit, after_completion] }
    DecisionUpdatedEvent:
      type: object
      additionalProperties: false
      required: [type, decision, changed]
      properties:
        type: { type: string, const: decision.updated }
        decision: { $ref: './decision.schema.json#/$defs/DecisionResult' }
        changed: { type: array, items: { type: string } }
    RouteConfirmedEvent:
      type: object
      additionalProperties: false
      required: [type, attempt_id, routing]
      properties:
        type: { type: string, const: route.confirmed }
        attempt_id: { type: string }
        routing: { $ref: '#/components/schemas/RoutingDecision' }
    GenerationStartedEvent:
      type: object
      additionalProperties: false
      required: [type, attempt_id, route_id, model, speculative, degradations]
      properties:
        type: { type: string, const: generation.started }
        attempt_id: { type: string }
        route_id: { type: string }
        model: { type: string }
        speculative: { type: boolean }
        degradations: { type: array, items: { $ref: '#/components/schemas/Degradation' } }
    GenerationDeltaEvent:
      type: object
      additionalProperties: false
      required: [type, attempt_id, seq, text]
      properties:
        type: { type: string, const: generation.delta }
        attempt_id: { type: string }
        seq: { type: integer }
        text: { type: string }
    GenerationCommittedEvent:
      type: object
      additionalProperties: false
      required: [type, attempt_id, reason]
      properties:
        type: { type: string, const: generation.committed }
        attempt_id: { type: string }
        reason: { type: string, enum: [spec_agrees, holdback_deadline, gate, speculative, selected] }
    GenerationCancelledEvent:
      type: object
      additionalProperties: false
      required: [type, attempt_id, reason]
      properties:
        type: { type: string, const: generation.cancelled }
        attempt_id: { type: string }
        reason: { type: string, enum: [spec_changed, lost_race, not_selected, client_abort, failed] }
    GenerationReroutedEvent:
      type: object
      additionalProperties: false
      required: [type, from_attempt_id, to_attempt_id, reason, discard_from_seq]
      properties:
        type: { type: string, const: generation.rerouted }
        from_attempt_id: { type: string }
        to_attempt_id: { type: string }
        reason: { type: string }
        discard_from_seq: { type: [integer, 'null'] }
    GenerationCompletedEvent:
      type: object
      additionalProperties: false
      required: [type, attempt_id, finish_reason, usage]
      properties:
        type: { type: string, const: generation.completed }
        attempt_id: { type: string }
        finish_reason: { type: string, enum: [stop, length, content_filter, cancelled] }
        usage: { $ref: './decision.schema.json#/$defs/Usage' }
    TurnVerifiedEvent:
      type: object
      additionalProperties: false
      required: [type, verification]
      properties:
        type: { type: string, const: turn.verified }
        verification: { $ref: './decision.schema.json#/$defs/DecisionResult' }
    TurnCompletedEvent:
      type: object
      additionalProperties: false
      description: Always the last event of a stream. Late decision members, verification and usage have settled before it; nothing follows (a later view is GET /v1/turns/{id}).
      required: [type, turn_id, usage, timings]
      properties:
        type: { type: string, const: turn.completed }
        turn_id: { type: string }
        usage: { $ref: './decision.schema.json#/$defs/Usage' }
        timings: { $ref: '#/components/schemas/TurnTimings' }
    ErrorEvent:
      type: object
      additionalProperties: false
      required: [type, code, message, retryable]
      properties:
        type: { type: string, const: error }
        code: { $ref: '#/components/schemas/ErrorCode' }
        message: { type: string }
        retryable: { type: boolean }

    SessionCreate:
      type: object
      additionalProperties: false
      properties:
        metadata: { type: object, additionalProperties: { type: string } }
        retention: { type: string, enum: [default, ephemeral, custom], description: 'Phase 1: `custom` is 422 `feature_not_available`.' }
        data_class: { type: string, enum: [public, internal, confidential, restricted] }
    SessionUpdate:
      type: object
      additionalProperties: false
      minProperties: 1
      description: Present fields replace the stored value; `metadata` replaces the whole map (`{}` clears it).
      properties:
        metadata: { type: object, additionalProperties: { type: string } }
        retention: { type: string, enum: [default, ephemeral, custom], description: 'Phase 1: `custom` is 422 `feature_not_available`.' }
        data_class: { type: string, enum: [public, internal, confidential, restricted] }
    Session:
      type: object
      additionalProperties: false
      required: [id, created_at, last_seq, data_class, retention, metadata]
      properties:
        id: { type: string }
        created_at: { type: integer }
        last_seq: { type: integer, minimum: 0, description: "`seq` of the latest committed turn (0 = none); the value to send as expected_seq" }
        owner: { type: string }
        data_class: { type: string, enum: [public, internal, confidential, restricted] }
        retention:
          type: object
          additionalProperties: false
          required: [policy, expires_at]
          properties:
            policy: { type: string, enum: [default, ephemeral, custom] }
            expires_at: { type: [integer, 'null'] }
        metadata: { type: object, additionalProperties: { type: string } }
    SessionList:
      type: object
      additionalProperties: false
      required: [data, has_more, next_cursor]
      properties:
        data: { type: array, items: { $ref: '#/components/schemas/Session' } }
        has_more: { type: boolean }
        next_cursor: { type: [string, 'null'] }
    SessionTurn:
      type: object
      additionalProperties: false
      required: [turn_id, seq, role, text, created_at, redactions]
      properties:
        turn_id: { type: string }
        seq: { type: integer, minimum: 1 }
        role: { type: string, enum: [user, assistant] }
        text: { type: string, description: 'Committed text; redacted spans read "[redacted]".' }
        created_at: { type: integer }
        redactions:
          type: array
          items:
            type: object
            additionalProperties: false
            required: [start, end, reason]
            properties:
              start: { type: integer, minimum: 0, description: Offset in Unicode code points of `text` }
              end: { type: integer, minimum: 0, description: Exclusive end offset in code points }
              reason: { type: string, description: 'Redaction filter label (e.g. email, card_number)' }
    SessionTurnList:
      type: object
      additionalProperties: false
      required: [data, has_more, next_cursor]
      properties:
        data: { type: array, items: { $ref: '#/components/schemas/SessionTurn' } }
        has_more: { type: boolean }
        next_cursor: { type: [string, 'null'] }

    DerivativeKind:
      type: string
      enum: [text, markdown, page_images, pdf_sanitized, raster, thumbnail, ocr_text, transcript, keyframes, timeline, audio_normalized]
    FileObject:
      type: object
      additionalProperties: false
      required: [id, filename, media_type, bytes, sha256, status, derivatives]
      properties:
        id: { type: string }
        filename: { type: string, description: Display only; never sent to models }
        media_type: { type: string }
        bytes: { type: integer }
        sha256: { type: string }
        status: { type: string, enum: [quarantined, processing, ready, partial, rejected, failed, deleted] }
        derivatives:
          type: array
          items:
            type: object
            additionalProperties: false
            required: [kind, id, status]
            properties:
              kind: { $ref: '#/components/schemas/DerivativeKind' }
              id: { type: string }
              status: { type: string, enum: [pending, ready, failed] }
              pages: { type: integer, minimum: 1, description: 'page_images only: number of page images (download each with ?page=)' }
        rejection:
          type: object
          additionalProperties: false
          required: [code, message]
          properties:
            code:
              type: string
              enum: [type_mismatch, not_allowlisted, file_too_large, too_many_pages, duration_exceeded, pixel_limit, encrypted_pdf,
                     decompression_bomb, corrupt, unsupported_codec, empty_content, malware_detected]
            message: { type: string }
        failure:
          type: object
          additionalProperties: false
          required: [code, message, retryable]
          properties:
            code: { type: string, enum: [extraction_failed, ocr_failed, transcription_failed, timeout] }
            message: { type: string }
            retryable: { const: true }

    ProfileInfo:
      type: object
      additionalProperties: false
      required: [id, version, kind, description, models, modalities]
      properties:
        id: { type: string }
        version: { type: string, pattern: '^[0-9]+$', description: Same form as provenance.profile_version }
        kind: { type: string, enum: [decision, generation] }
        description: { type: string }
        models: { type: array, items: { type: string } }
        mode: { type: string, enum: [single, fallback, race, best_of_n, fuse] }
        modalities: { type: array, items: { type: string, enum: [text, image, document] } }
        streaming: { type: string, enum: [live, after_selection] }

    UsageLine: { type: string, enum: [decision, generation, labeling, verification, waste, cache_savings] }
    UsageRow:
      type: object
      additionalProperties: false
      required: [key, line, usage]
      properties:
        key:
          type: [string, 'null']
          description: 'Value of the group_by dimension: project id, session id, Bedrock model id, UTC day (YYYY-MM-DD) or the line name. null when the dimension does not apply (e.g. session, for a decision made without a session).'
        line: { $ref: '#/components/schemas/UsageLine' }
        usage: { $ref: './decision.schema.json#/$defs/Usage' }
    UsageReport:
      type: object
      additionalProperties: false
      required: [group_by, from, to, data, has_more, next_cursor]
      properties:
        group_by: { type: string, enum: [project, session, model, day, line] }
        from: { type: string, format: date }
        to: { type: string, format: date }
        data: { type: array, items: { $ref: '#/components/schemas/UsageRow' } }
        has_more: { type: boolean }
        next_cursor: { type: [string, 'null'] }

    CalibratorStatus:
      type: string
      description: 'rejected: failed acceptance. accepted: passed acceptance, awaiting two-person activation. active: applied to answers whose binding matches. retired: replaced, retired by hand or downgraded by drift.'
      enum: [rejected, accepted, active, retired]
    CalibratorInfo:
      type: object
      additionalProperties: false
      required: [id, object, scope, level, question_name, status, method, binding, member_subset, holdout, acceptance, reliability,
                 created_at, activated_at, retired_at, retired_reason]
      properties:
        id: { type: string, pattern: '^cal_' }
        object: { const: calibrator }
        scope: { type: string, enum: [platform, tenant], description: 'platform: the platform-default calibrator fitted on platform seed data, used until the tenant has its own' }
        level: { type: string, enum: [pooled, member] }
        member_id: { type: string, description: Present exactly when level is member }
        question_name: { type: string }
        status: { $ref: '#/components/schemas/CalibratorStatus' }
        method: { type: string, enum: [temperature, platt, isotonic, dirichlet] }
        parameters: { type: object, additionalProperties: { type: number }, description: 'Fitted parameters, e.g. {"temperature": 1.31}' }
        binding: { $ref: './decision.schema.json#/$defs/CalibrationBinding' }
        member_subset: { type: array, minItems: 1, uniqueItems: true, items: { type: string } }
        holdout:
          oneOf:
            - { type: 'null' }
            - { $ref: './decision.schema.json#/$defs/HoldoutMetrics' }
        acceptance:
          type: object
          additionalProperties: false
          required: [passed, failed_rules]
          properties:
            passed: { type: boolean }
            failed_rules:
              type: array
              items: { type: string, enum: [ece_max, n_min, nll_not_worse, class_consensus_min, family_overlap_lift] }
        reliability:
          type: object
          additionalProperties: false
          required: [bins, ci]
          properties:
            bins:
              type: array
              items:
                type: object
                additionalProperties: false
                required: [lower, upper, n, mean_predicted, observed]
                properties:
                  lower: { type: number, minimum: 0, maximum: 1 }
                  upper: { type: number, minimum: 0, maximum: 1 }
                  n: { type: integer, minimum: 0 }
                  mean_predicted: { type: [number, 'null'], minimum: 0, maximum: 1 }
                  observed: { type: [number, 'null'], minimum: 0, maximum: 1 }
            ci:
              type: object
              additionalProperties: false
              description: Bootstrap 95% intervals [low, high] of the holdout metrics.
              properties:
                ece: { $ref: '#/components/schemas/Interval' }
                brier: { $ref: '#/components/schemas/Interval' }
                nll: { $ref: '#/components/schemas/Interval' }
        created_at: { type: integer }
        activated_at: { type: [integer, 'null'] }
        activated_by: { type: array, items: { type: string }, description: Approver principal ids (two-person activation) }
        retired_at: { type: [integer, 'null'] }
        retired_reason:
          oneOf:
            - { type: 'null' }
            - { type: string, enum: [replaced, drift, manual, profile_version_retired] }
    Interval:
      type: array
      minItems: 2
      maxItems: 2
      items: { type: number }
    CalibratorList:
      type: object
      additionalProperties: false
      required: [data, has_more, next_cursor]
      properties:
        data: { type: array, items: { $ref: '#/components/schemas/CalibratorInfo' } }
        has_more: { type: boolean }
        next_cursor: { type: [string, 'null'] }
    LabelingRun:
      type: object
      additionalProperties: false
      required: [id, object, status, profile_id, profile_version, question_name, question_signature, labelers, rule, counts, consensus_rate,
                 consensus_rate_by_class, n_by_class, usage, started_at, completed_at]
      properties:
        id: { type: string, pattern: '^lrun_' }
        object: { const: labeling_run }
        status: { type: string, enum: [running, completed, failed, cancelled] }
        profile_id: { type: string }
        profile_version: { type: string, pattern: '^[0-9]+$' }
        question_name: { type: string }
        question_signature: { type: string, pattern: '^sha256:[0-9a-f]{64}$' }
        labelers: { type: array, minItems: 2, uniqueItems: true, items: { type: string }, description: Bedrock model ids (D4) }
        rule: { const: unanimous }
        counts:
          type: object
          additionalProperties: false
          description: Once the run has completed, kept plus the dropped counts equals sampled.
          required: [sampled, sampled_uniform, sampled_uncertainty, kept, dropped_disagreed, dropped_refused, dropped_parse_error, dropped_labeler_error]
          properties:
            sampled: { type: integer, minimum: 0 }
            sampled_uniform: { type: integer, minimum: 0 }
            sampled_uncertainty: { type: integer, minimum: 0 }
            kept: { type: integer, minimum: 0 }
            dropped_disagreed: { type: integer, minimum: 0 }
            dropped_refused: { type: integer, minimum: 0 }
            dropped_parse_error: { type: integer, minimum: 0 }
            dropped_labeler_error: { type: integer, minimum: 0 }
        consensus_rate: { type: [number, 'null'], minimum: 0, maximum: 1, description: kept / sampled; null before any item has finished }
        consensus_rate_by_class: { type: object, additionalProperties: { type: number, minimum: 0, maximum: 1 } }
        n_by_class: { type: object, additionalProperties: { type: integer, minimum: 0 }, description: Kept labels per class }
        usage: { $ref: './decision.schema.json#/$defs/Usage' }
        started_at: { type: integer }
        completed_at: { type: [integer, 'null'] }
    LabelingRunList:
      type: object
      additionalProperties: false
      required: [data, has_more, next_cursor]
      properties:
        data: { type: array, items: { $ref: '#/components/schemas/LabelingRun' } }
        has_more: { type: boolean }
        next_cursor: { type: [string, 'null'] }
    LabelingDroppedItem:
      type: object
      additionalProperties: false
      required: [id, object, run_id, decision_id, question_name, question_signature, input_view_signature, reason, sample_reason,
                 labeler_answers, ensemble, content, sampled_at]
      properties:
        id: { type: string, pattern: '^ldrop_' }
        object: { const: labeling_dropped_item }
        run_id: { type: string, pattern: '^lrun_' }
        decision_id: { type: string, pattern: '^dec_' }
        question_name: { type: string }
        question_signature: { type: string, pattern: '^sha256:[0-9a-f]{64}$' }
        input_view_signature: { type: string, pattern: '^sha256:[0-9a-f]{64}$' }
        reason: { type: string, enum: [disagreed, refused, parse_error, labeler_error] }
        sample_reason: { type: string, enum: [uniform, uncertainty] }
        labeler_answers:
          type: array
          minItems: 2
          items:
            type: object
            additionalProperties: false
            required: [model, status, value]
            properties:
              model: { type: string }
              status: { type: string, enum: [ok, refused, parse_error, error] }
              value: { type: [string, boolean, 'null'], description: "The labeler's answer (choice value, score label or predicate truth); null unless status is ok" }
        ensemble:
          type: object
          additionalProperties: false
          required: [value, probability, band, calibration_status]
          properties:
            value: { type: [string, boolean, 'null'] }
            probability: { type: [number, 'null'], minimum: 0, maximum: 1 }
            band:
              oneOf:
                - { type: 'null' }
                - { type: string, enum: [high, medium, low] }
            calibration_status: { type: string, enum: [calibrated, provider_reported, uncalibrated] }
        content:
          type: object
          additionalProperties: false
          required: [excerpt, redacted]
          properties:
            excerpt: { type: [string, 'null'], maxLength: 2000, description: 'Input excerpt after the redaction filter; null when the data class or log policy forbids stored content' }
            redacted: { type: boolean }
        sampled_at: { type: integer }
    LabelingDroppedList:
      type: object
      additionalProperties: false
      required: [data, has_more, next_cursor]
      properties:
        data: { type: array, items: { $ref: '#/components/schemas/LabelingDroppedItem' } }
        has_more: { type: boolean }
        next_cursor: { type: [string, 'null'] }
````
<!-- END contracts/openapi.yaml -->

## Appendix B — unified diff for every other changed file (`cd docs/noema && patch -p1`)

Created with `diff -u` against the rev-5 tree. Apply it before regenerating the examples (§0, steps 2–4). Its counts ("48 examples", "45 negative tests", "41 cases") are pre-§14 and superseded by §1 (54 examples, 49 config negative tests, 49 audit cases); the payload itself is kept byte-identical.

<!-- BEGIN noema-rev6.patch -->
````diff
--- a/contracts/decision.schema.json
+++ b/contracts/decision.schema.json
@@ -204,7 +204,7 @@
             "image_url": {
               "type": "string",
               "pattern": "^data:image/(png|jpeg|webp|gif|heic|heif);base64,",
-              "maxLength": 7000000
+              "description": "Base64 data URL. The server decodes it and answers 413 image_too_large above 3,750,000 decoded bytes or 8,000 px per side; the JSON body cap (413 request_too_large) bounds the string. Remote http(s) URLs are never fetched."
             },
             "detail": {
               "enum": [
@@ -270,14 +270,14 @@
       "type": "object",
       "additionalProperties": false,
       "required": [
-        "profile",
         "input",
         "questions"
       ],
       "properties": {
         "profile": {
           "type": "string",
-          "pattern": "^[a-z0-9][a-z0-9._-]{0,63}(@[0-9]+)?$"
+          "pattern": "^[a-z0-9][a-z0-9._-]{0,63}(@[0-9]+)?$",
+          "description": "Decision profile id, optionally version-pinned (router-default@1). Optional: the default is the tenant's defaults.decision_profile (lightweight-model-decide in the shipped configuration, D6). Must be the default or in the tenant's request_overrides allowlist (403 profile_not_allowed)."
         },
         "input": {
           "oneOf": [
@@ -312,6 +312,7 @@
             {
               "type": "object",
               "minProperties": 1,
+              "additionalProperties": true,
               "description": "JEV-style JSON state"
             }
           ]
@@ -523,6 +524,42 @@
       },
       "description": "Evidence behind status calibrated. Calibration is relative to LLM-consensus labels (D4) on the items where the labelers agreed; `scope` says which population that is."
     },
+    "CalibrationBinding": {
+      "type": "object",
+      "additionalProperties": false,
+      "required": [
+        "profile_id",
+        "profile_version",
+        "question_signature",
+        "input_view_signature",
+        "choice_method"
+      ],
+      "description": "What the calibrator was fitted for. An answer is `calibrated` only if this matches the stage that produced it, the question, the input views, the answering member subset and the choice method.",
+      "properties": {
+        "profile_id": {
+          "type": "string"
+        },
+        "profile_version": {
+          "type": "string",
+          "pattern": "^[0-9]+$"
+        },
+        "question_signature": {
+          "type": "string",
+          "pattern": "^sha256:[0-9a-f]{64}$",
+          "description": "sha256 of this question's canonical definition."
+        },
+        "input_view_signature": {
+          "type": "string",
+          "pattern": "^sha256:[0-9a-f]{64}$"
+        },
+        "choice_method": {
+          "enum": [
+            "flat",
+            "hierarchical"
+          ]
+        }
+      }
+    },
     "Calibration": {
       "type": "object",
       "additionalProperties": false,
@@ -567,40 +604,7 @@
           "$ref": "#/$defs/HoldoutMetrics"
         },
         "binding": {
-          "type": "object",
-          "additionalProperties": false,
-          "required": [
-            "profile_id",
-            "profile_version",
-            "question_signature",
-            "input_view_signature",
-            "choice_method"
-          ],
-          "description": "What the calibrator was fitted for. An answer is `calibrated` only if this matches the stage that produced it, the question, the input views, the answering member subset and the choice method.",
-          "properties": {
-            "profile_id": {
-              "type": "string"
-            },
-            "profile_version": {
-              "type": "string",
-              "pattern": "^[0-9]+$"
-            },
-            "question_signature": {
-              "type": "string",
-              "pattern": "^sha256:[0-9a-f]{64}$",
-              "description": "sha256 of this question's canonical definition."
-            },
-            "input_view_signature": {
-              "type": "string",
-              "pattern": "^sha256:[0-9a-f]{64}$"
-            },
-            "choice_method": {
-              "enum": [
-                "flat",
-                "hierarchical"
-              ]
-            }
-          }
+          "$ref": "#/$defs/CalibrationBinding"
         }
       },
       "if": {
@@ -1034,6 +1038,7 @@
         }
       ],
       "required": [
+        "type",
         "probability"
       ],
       "properties": {
@@ -1071,6 +1076,7 @@
         }
       ],
       "required": [
+        "type",
         "choice"
       ],
       "properties": {
@@ -1153,6 +1159,7 @@
         }
       ],
       "required": [
+        "type",
         "score",
         "level"
       ],
--- a/contracts/openai-compat.schema.json
+++ b/contracts/openai-compat.schema.json
@@ -2,15 +2,15 @@
   "$schema": "https://json-schema.org/draft/2020-12/schema",
   "$id": "https://aghoo.example/schemas/openai-compat-decisions/v0.2",
   "title": "OpenAI Decisions-compatible facade (POST /openai/v1/decisions)",
-  "description": "Mirrors the OpenAI Decisions API reference [OA-R] as of 2026-10-09: optional names, optional/nullable descriptions, string|boolean choice values, 2-255 choices, string-or-array message content, image detail, safety_identifier. Answers come from a Bedrock decision profile (D1, default lightweight-model-decide); Noema never calls OpenAI. Compatibility is of shapes only. At most 32 questions and 16 score levels; explicit names must be unique and not q<digits>; true and \"true\" can't both be choices of one question (checked by check_examples.py).",
+  "description": "Mirrors the OpenAI Decisions API reference [OA-R] as of 2026-10-09: optional names, optional/nullable descriptions, string|boolean choice values, 2-255 choices, string-or-array message content, image detail, safety_identifier. Answers come from a Bedrock decision profile (D1, default lightweight-model-decide); Noema never calls OpenAI. Compatibility is of shapes only. At most 32 questions and 16 score levels; explicit names must be unique and not q<digits>; true and \"true\" can't both be choices of one question; at most 20 input_image parts per request (checked by check_examples.py). Images are data URLs only: remote URLs are never fetched (400 schema_violation).",
   "$defs": {
-    "Description": { "type": ["string", "null"] },
+    "OADescription": { "type": ["string", "null"] },
     "OAInputPart": {
       "oneOf": [
         { "type": "object", "additionalProperties": false, "required": ["type", "text"],
           "properties": { "type": { "const": "input_text" }, "text": { "type": "string", "maxLength": 10485760 } } },
         { "type": "object", "additionalProperties": false, "required": ["type", "image_url"],
-          "properties": { "type": { "const": "input_image" }, "image_url": { "type": "string" },
+          "properties": { "type": { "const": "input_image" }, "image_url": { "type": "string", "pattern": "^data:image/(png|jpeg|webp|gif|heic|heif);base64," },
                           "detail": { "enum": ["low", "high", "auto", "original", null] } } }
       ]
     },
@@ -29,12 +29,12 @@
           "properties": { "type": { "const": "choice" }, "instructions": { "type": "string" }, "name": { "type": ["string", "null"] },
             "choices": { "type": "array", "minItems": 2, "maxItems": 255, "items": {
               "type": "object", "additionalProperties": false, "required": ["value"],
-              "properties": { "value": { "type": ["string", "boolean"] }, "description": { "$ref": "#/$defs/Description" } } } } } },
+              "properties": { "value": { "type": ["string", "boolean"] }, "description": { "$ref": "#/$defs/OADescription" } } } } } },
         { "type": "object", "additionalProperties": false, "required": ["type", "instructions", "levels"],
           "properties": { "type": { "const": "score" }, "instructions": { "type": "string" }, "name": { "type": ["string", "null"] },
             "levels": { "type": "array", "minItems": 2, "maxItems": 16, "items": {
               "type": "object", "additionalProperties": false, "required": ["label"],
-              "properties": { "label": { "type": "string" }, "description": { "$ref": "#/$defs/Description" } } } } } }
+              "properties": { "label": { "type": "string" }, "description": { "$ref": "#/$defs/OADescription" } } } } } }
       ]
     },
     "OADecisionCreateParams": {
@@ -79,7 +79,13 @@
             "output_tokens_details": { "type": "object", "additionalProperties": false, "required": ["reasoning_tokens"],
               "properties": { "reasoning_tokens": { "type": "integer", "minimum": 0 } } },
             "total_tokens": { "type": "integer", "minimum": 0 } } },
-        "x_aghoo": { "type": "object" }
+        "x_aghoo": {
+          "type": "object", "additionalProperties": false, "required": ["decision_id", "calibration"],
+          "description": "Only with request header x-aghoo-include: ensemble. Keys are effective question names (the given name, or q{i} for unnamed questions).",
+          "properties": {
+            "decision_id": { "type": "string", "pattern": "^dec_" },
+            "calibration": { "type": "object", "additionalProperties": { "enum": ["calibrated", "uncalibrated"] } },
+            "mapped_unavailable": { "type": "object", "additionalProperties": { "enum": ["deadline_exceeded", "no_quorum", "all_members_failed", "no_capable_member"] } } } }
       }
     }
   }
--- a/contracts/types.ts
+++ b/contracts/types.ts
@@ -91,8 +91,11 @@
 export type DecisionInput = string | UserMessage[] | { [k: string]: JsonValue };
 
 export interface DecisionCreateParams<Qs extends readonly Question[] = readonly Question[]> {
-  /** Decision profile id, optionally pinned to a version: "router-default" or "router-default@1". */
-  profile: string;
+  /**
+   * Decision profile id, optionally pinned to a version: "router-default" or "router-default@1". Default: the tenant's
+   * `defaults.decision_profile` (`lightweight-model-decide` in the shipped configuration, D6).
+   */
+  profile?: string;
   input: DecisionInput;
   /** 1-32 questions. Names are unique, match ^[a-z][a-z0-9_]{0,63}$, and q<digits> is reserved for the facade's generated names. */
   questions: Qs;
@@ -525,6 +528,37 @@
     | "rejected";
 }
 
+// ---------------------------------------------------------------------------------------------
+// Errors. Each code has one HTTP status, `error.type` and `retryable` value (03 §1).
+// ---------------------------------------------------------------------------------------------
+
+export type ErrorCode =
+  | "schema_violation" | "unsupported_question_type" | "reserved_question_name" | "unsupported_parameter" | "model_not_available"
+  | "profile_not_allowed" | "invalid_api_key" | "session_forbidden" | "session_not_found" | "idempotency_key_reused"
+  | "file_not_ready" | "file_rejected" | "file_too_large" | "image_too_large" | "unsupported_media_type"
+  | "no_route_satisfies_constraints" | "no_capable_member" | "rpm_exceeded" | "tpm_exceeded" | "budget_exhausted" | "queue_timeout"
+  | "all_routes_unavailable" | "deadline_exceeded" | "tenant_access_misconfigured" | "upstream_stalled" | "client_too_slow"
+  | "resume_window_expired" | "session_conflict" | "session_busy" | "not_found" | "permission_denied" | "invalid_end_user_token"
+  | "request_too_large" | "feature_not_available" | "internal_error" | "not_implemented";
+
+export type ErrorType =
+  | "invalid_request_error" | "authentication_error" | "permission_error" | "not_found_error" | "conflict_error"
+  | "capability_error" | "rate_limit_error" | "upstream_error" | "server_error";
+
+/** HTTP error body (OpenAI-shaped). Codes are an open enum: a later server may add codes without a version bump. */
+export interface ErrorEnvelope {
+  error: {
+    type: OpenEnum<ErrorType>;
+    code: OpenEnum<ErrorCode>;
+    message: string;
+    /** Offending field path (e.g. "questions[1].choices") or header name; null when no single field is at fault. */
+    param: string | null;
+    request_id: string;
+    retryable: boolean;
+    details?: { [k: string]: JsonValue };
+  };
+}
+
 export type TurnEvent =
   | { type: "turn.created"; turn_id: string; session_id: string | null; created_at: number }
   | { type: "preflight.completed"; modalities: Modality[]; est_input_tokens: number; degradations: Degradation[] }
@@ -541,7 +575,7 @@
   | { type: "turn.verified"; verification: DecisionResult } // post-generation answer checks by the decision ensemble
   /** Always the last event: late decision members, verification and usage have settled before it. */
   | { type: "turn.completed"; turn_id: string; usage: Usage; timings: TurnTimings }
-  | { type: "error"; code: string; message: string; retryable: boolean };
+  | { type: "error"; code: OpenEnum<ErrorCode>; message: string; retryable: boolean };
 
 export interface TurnTimings {
   ttft_ms: number | null; // first committed delta delivered to the client
--- a/contracts/sdk.ts
+++ b/contracts/sdk.ts
@@ -1,17 +1,26 @@
 /**
- * TypeScript SDK surface (declarations only; proposal v0.2).
+ * TypeScript SDK surface (declarations only; proposal v0.3).
  * Builders infer literal names/values so answers are typed per question.
+ * The client is hand-written against this surface; its wire types are generated from the bundled OpenAPI document
+ * (openapi-typescript) and must stay assignable to the types here.
  */
 import type {
   AnswerMode,
+  CalibrationBinding,
+  ChoiceOption,
   ChoiceQuestion,
   Degradation,
   DecisionCreateParams,
   DecisionResult,
   Effort,
+  ErrorCode,
+  ErrorType,
+  HoldoutMetrics,
   JevDecisionResponse,
+  JsonValue,
   GenerationMode,
   InputPart,
+  OpenEnum,
   PredicateQuestion,
   Question,
   RoutingDecision,
@@ -33,16 +42,33 @@
     name: N,
     opts: { instructions: string; criteria?: { true?: string; false?: string } },
   ): PredicateQuestion<N, "noul">;
-  choice<const N extends string, const C extends Record<string, string | null>>(
+  /**
+   * Options in declared order: the order members see, ties break by, and the facade echoes. An array (not a Record), so
+   * integer-like values keep their order and each option can carry a `group` for the hierarchical method.
+   */
+  choice<const N extends string, const C extends readonly ChoiceOption[]>(
     name: N,
     opts: { instructions: string; choices: C },
-  ): ChoiceQuestion<N, Extract<keyof C, string>>;
+  ): ChoiceQuestion<N, C[number]["value"]>;
   score<const N extends string, const L extends readonly { label: string; description?: string | null }[]>(
     name: N,
     opts: { instructions: string; levels: L },
   ): ScoreQuestion<N, L[number]["label"]>;
 };
 
+/** Thrown for every non-2xx HTTP response (after retries). SSE `error` events are yielded by TurnStream, not thrown. */
+export declare class AghooError extends Error {
+  readonly status: number;
+  readonly type: OpenEnum<ErrorType>;
+  readonly code: OpenEnum<ErrorCode>;
+  readonly param: string | null;
+  readonly requestId: string | null;
+  readonly retryable: boolean;
+  /** From `retry-after`, in seconds. */
+  readonly retryAfterS: number | null;
+  readonly details: { [k: string]: JsonValue } | undefined;
+}
+
 export interface RequestOptions {
   signal?: AbortSignal;
   timeoutMs?: number;
@@ -118,11 +144,30 @@
   id: string;
   created_at: number;
   last_seq: number; // seq of the latest committed turn (0 = none)
+  owner?: string;
   data_class: DataClass;
   retention: { policy: "default" | "ephemeral" | "custom"; expires_at: number | null };
   metadata: Record<string, string>;
 }
 
+/** PATCH semantics: present fields replace the stored value; `metadata` replaces the whole map. Phase 1: retention "custom" is 422. */
+export interface SessionUpdate {
+  metadata?: Record<string, string>;
+  retention?: Session["retention"]["policy"];
+  data_class?: DataClass;
+}
+
+/** One history message; a committed turn yields its user message, then its assistant message. */
+export interface SessionTurn {
+  turn_id: string;
+  seq: number;
+  role: "user" | "assistant";
+  text: string; // redacted spans read "[redacted]"
+  created_at: number;
+  /** Offsets in Unicode code points of `text` (use Array.from(text) to slice); `end` is exclusive. */
+  redactions: { start: number; end: number; reason: string }[];
+}
+
 export type DerivativeKind =
   | "text" | "markdown" | "page_images" | "pdf_sanitized" | "raster" | "thumbnail" | "ocr_text"
   | "transcript" | "keyframes" | "timeline" | "audio_normalized";
@@ -134,7 +179,7 @@
   bytes: number;
   sha256: string;
   status: "quarantined" | "processing" | "ready" | "partial" | "rejected" | "failed" | "deleted";
-  derivatives: { kind: DerivativeKind; id: string; status: "pending" | "ready" | "failed" }[];
+  derivatives: { kind: DerivativeKind; id: string; status: "pending" | "ready" | "failed"; pages?: number }[];
   rejection?: { code: FileRejectionCode; message: string };
   failure?: { code: "extraction_failed" | "ocr_failed" | "transcription_failed" | "timeout"; message: string; retryable: true };
 }
@@ -145,7 +190,7 @@
 
 export interface ProfileInfo {
   id: string; // e.g. "router-default"
-  version: number;
+  version: string; // "^[0-9]+$", same form as provenance.profile_version
   kind: "decision" | "generation";
   description: string;
   /** decision: member model ids; generation: member model ids in order. */
@@ -155,6 +200,85 @@
   streaming?: "live" | "after_selection";
 }
 
+export type UsageLine = "decision" | "generation" | "labeling" | "verification" | "waste" | "cache_savings";
+export type UsageGroupBy = "project" | "session" | "model" | "day" | "line";
+
+/** One (key, line) row; key = project id, session id, Bedrock model id, UTC day or line name (null when it doesn't apply). */
+export interface UsageRow {
+  key: string | null;
+  line: UsageLine;
+  usage: Usage;
+}
+
+// --- Read-only W4 views (GET /v1/admin/...; roles tenant_admin, reviewer, auditor; dropped items: tenant_admin, reviewer) ---
+
+export type CalibratorStatus = "rejected" | "accepted" | "active" | "retired";
+
+export interface CalibratorInfo {
+  id: string;
+  object: "calibrator";
+  scope: "platform" | "tenant";
+  level: "pooled" | "member";
+  member_id?: string;
+  question_name: string;
+  status: CalibratorStatus;
+  method: "temperature" | "platt" | "isotonic" | "dirichlet";
+  parameters?: Record<string, number>;
+  binding: CalibrationBinding;
+  member_subset: string[];
+  holdout: HoldoutMetrics | null;
+  acceptance: { passed: boolean; failed_rules: ("ece_max" | "n_min" | "nll_not_worse" | "class_consensus_min" | "family_overlap_lift")[] };
+  reliability: {
+    bins: { lower: number; upper: number; n: number; mean_predicted: number | null; observed: number | null }[];
+    ci: { ece?: number[]; brier?: number[]; nll?: number[] }; // each [low, high]
+  };
+  created_at: number;
+  activated_at: number | null;
+  activated_by?: string[];
+  retired_at: number | null;
+  retired_reason: "replaced" | "drift" | "manual" | "profile_version_retired" | null;
+}
+
+export interface LabelingRun {
+  id: string;
+  object: "labeling_run";
+  status: "running" | "completed" | "failed" | "cancelled";
+  profile_id: string;
+  profile_version: string;
+  question_name: string;
+  question_signature: string;
+  labelers: string[];
+  rule: "unanimous";
+  counts: {
+    sampled: number; sampled_uniform: number; sampled_uncertainty: number; kept: number;
+    dropped_disagreed: number; dropped_refused: number; dropped_parse_error: number; dropped_labeler_error: number;
+  };
+  consensus_rate: number | null;
+  consensus_rate_by_class: Record<string, number>;
+  n_by_class: Record<string, number>;
+  usage: Usage;
+  started_at: number;
+  completed_at: number | null;
+}
+
+export interface LabelingDroppedItem {
+  id: string;
+  object: "labeling_dropped_item";
+  run_id: string;
+  decision_id: string;
+  question_name: string;
+  question_signature: string;
+  input_view_signature: string;
+  reason: "disagreed" | "refused" | "parse_error" | "labeler_error";
+  sample_reason: "uniform" | "uncertainty";
+  labeler_answers: { model: string; status: "ok" | "refused" | "parse_error" | "error"; value: string | boolean | null }[];
+  ensemble: { value: string | boolean | null; probability: number | null; band: "high" | "medium" | "low" | null; calibration_status: "calibrated" | "provider_reported" | "uncalibrated" };
+  content: { excerpt: string | null; redacted: boolean };
+  sampled_at: number;
+}
+
+type Page = { limit?: number };
+
 export declare class AghooClient {
   constructor(opts: { apiKey?: string; baseURL?: string; fetch?: typeof fetch });
 
@@ -190,7 +314,11 @@
       data_class?: DataClass;
     }): Promise<Session>;
     retrieve(id: string): Promise<Session>;
-    turns(id: string, params?: { limit?: number; after?: string }): AsyncIterable<{ turn_id: string; role: "user" | "assistant"; text: string }>;
+    /** Newest first; pages are fetched lazily. */
+    list(params?: Page): AsyncIterable<Session>;
+    update(id: string, params: SessionUpdate): Promise<Session>;
+    /** Oldest first. */
+    turns(id: string, params?: { limit?: number; after?: string }): AsyncIterable<SessionTurn>;
     delete(id: string): Promise<{ id: string; deleted: true; deletion_job_id: string }>;
   };
 
@@ -200,5 +328,23 @@
     /** Server-sent status updates until ready / partial / rejected / failed. */
     watch(id: string, opts?: RequestOptions): AsyncIterable<FileObject>;
     delete(id: string): Promise<{ id: string; deleted: true }>;
+    /** Derivative bytes; `page` (1-based) is required for page_images. Blob.type carries the content type. */
+    derivative(id: string, kind: DerivativeKind, params?: { page?: number }): Promise<Blob>;
+  };
+
+  usage: {
+    /** Rows ordered by key, then line; defaults: group_by "line", the last 30 UTC days. */
+    list(params?: Page & { group_by?: UsageGroupBy; from?: string; to?: string }): AsyncIterable<UsageRow>;
+  };
+
+  /** Read-only W4 views; other roles get AghooError code permission_denied. */
+  admin: {
+    calibrators: {
+      list(params?: Page & { profile_id?: string; question_name?: string; status?: CalibratorStatus }): AsyncIterable<CalibratorInfo>;
+    };
+    labeling: {
+      runs(params?: Page & { profile_id?: string; question_name?: string; status?: LabelingRun["status"] }): AsyncIterable<LabelingRun>;
+      dropped(params?: Page & { run_id?: string; question_name?: string; reason?: LabelingDroppedItem["reason"] }): AsyncIterable<LabelingDroppedItem>;
+    };
   };
 }
--- a/contracts/sdk-usage.check.ts
+++ b/contracts/sdk-usage.check.ts
@@ -1,7 +1,7 @@
 /**
  * Compile-time contract tests: `tsc --noEmit` must pass, and every @ts-expect-error must be needed.
  */
-import { AghooClient, q } from "./sdk";
+import { AghooClient, AghooError, q } from "./sdk";
 import type { OADecisionCreateParams } from "./openai-compat";
 
 declare const client: AghooClient;
@@ -9,7 +9,11 @@
 const questions = [
   q.choice("department", {
     instructions: "Which team should handle this message?",
-    choices: { billing: "Payments, refunds, invoices", technical: "Bugs and errors", other: null },
+    choices: [
+      { value: "billing", description: "Payments, refunds, invoices" },
+      { value: "technical", description: "Bugs and errors" },
+      { value: "other", description: null },
+    ],
   }),
   q.predicate("needs_human", {
     instructions: "The customer explicitly asks for a human.",
@@ -65,7 +69,7 @@
     output_format: "jev",
     input: "My package says delivered but I was also charged twice.",
     questions: [
-      q.choice("department", { instructions: "Which team?", choices: { billing: null, shipping: null, other: null } }),
+      q.choice("department", { instructions: "Which team?", choices: [{ value: "billing" }, { value: "shipping" }, { value: "other" }] }),
       q.score("urgency", { instructions: "How urgent?", levels: [{ label: "low" }, { label: "medium" }, { label: "high" }] }),
       q.noul("needs_human", { instructions: "The customer explicitly asks for a human." }),
     ] as const,
@@ -96,6 +100,52 @@
   if (a.type === "noul") void a.probability; // native answers echo the requested type name
 }
 
+export async function defaultProfileExample(): Promise<void> {
+  // No profile: the tenant's defaults.decision_profile answers (lightweight-model-decide, D6).
+  const d = await client.decisions.create({ input: "Where is my parcel?", questions });
+  void d.provenance.profile_id;
+}
+
+export function choiceBuilderExample(): void {
+  // Integer-like values keep their declared order, and options can carry hierarchical-method groups.
+  const sizes = q.choice("size", {
+    instructions: "Which size?",
+    choices: [{ value: "10", group: "small" }, { value: "2", group: "small" }, { value: "xl", group: "large" }, { value: "xxl", group: "large" }],
+  });
+  const v: "10" | "2" | "xl" | "xxl" = sizes.choices[0]!.value;
+  void v;
+  // @ts-expect-error the Record form was removed: object keys can't keep integer-like order or carry groups
+  q.choice("bad", { instructions: "Pick one", choices: { a: null, b: null } });
+}
+
+export async function resourcesExample(): Promise<void> {
+  const profiles = await client.profiles.list();
+  const version: string = profiles[0]!.version; // "1", same form as provenance.profile_version
+  // @ts-expect-error profile versions are strings
+  const n: number = profiles[0]!.version;
+  void version;
+  void n;
+  for await (const s of client.sessions.list({ limit: 20 })) void s.last_seq;
+  await client.sessions.update("ses_123", { metadata: { app: "support" }, data_class: "confidential" });
+  for await (const t of client.sessions.turns("ses_123")) void t.redactions;
+  const page = await client.files.derivative("file_1", "page_images", { page: 1 });
+  void page.type;
+  for await (const row of client.usage.list({ group_by: "model" })) void row.usage.cost_usd;
+  for await (const c of client.admin.calibrators.list({ status: "active" })) void c.holdout?.ece;
+  for await (const r of client.admin.labeling.runs()) void r.consensus_rate_by_class;
+  for await (const item of client.admin.labeling.dropped({ reason: "disagreed" })) void item.labeler_answers;
+}
+
+export async function errorExample(): Promise<string> {
+  try {
+    await client.turns.create({ input: [{ role: "user", content: [{ type: "input_text", text: "Hi" }] }], strategy: "hedge" });
+    return "ok";
+  } catch (e) {
+    if (e instanceof AghooError && e.code === "feature_not_available") return `not in this phase: ${e.param ?? ""}`;
+    throw e;
+  }
+}
+
 export async function jsonStateExample(): Promise<void> {
   // JEV-style structured state.
   await client.decisions.create({
@@ -129,6 +179,9 @@
       case "turn.verified":
         void ev.verification.answers;
         break;
+      case "error":
+        if (ev.code === "upstream_stalled") void ev.retryable; // keep the partial text; retry with retry_of
+        break;
     }
   }
 }
--- a/contracts/check_examples.py
+++ b/contracts/check_examples.py
@@ -3,6 +3,9 @@
 Usage: python3 -I check_examples.py   (exit 0 iff every example matches its expected outcome:
 *.valid*.json must pass, *.invalid-*.json must fail). Results are paired with their request (PAIRS) so
 request/result invariants (names, declared values, tie order) are checked too.
+
+Importable: the CLI runs only under `__main__`, so the server and the audit can reuse the invariant functions.
+`request_invariants` and `oa_request_invariants` return `Violation`s (strings that carry the API error `param`).
 """
 import hashlib
 import json
@@ -35,6 +38,17 @@
 OA_PAIRS = {}  # facade responses pair with openai-request.valid.json unless listed here
 AUTO_NAME = re.compile(r"^q[0-9]+$")  # generated names for unnamed facade questions are reserved
 JTOL = 0.011  # Jev values are rounded to 2 decimals
+OA_MAX_IMAGES = 20  # facade: input_image parts per request (the Bedrock Converse limit; 03 §4.2)
+
+
+class Violation(str):
+    """An invariant error message. It is a str (printing and truthiness are unchanged) that also carries `param`,
+    the path of the offending field for the API error envelope (None when the rule spans several fields)."""
+
+    def __new__(cls, message, param=None):
+        self = super().__new__(cls, message)
+        self.param = param
+        return self
 
 
 def jev_sum_tol(k):
@@ -129,17 +143,18 @@
     errs = []
     names = [q["name"] for q in req["questions"]]
     if len(names) != len(set(names)):
-        errs.append("question names must be unique")
-    for q in req["questions"]:
+        errs.append(Violation("question names must be unique", "questions"))
+    for i, q in enumerate(req["questions"]):
         vals = declared(q)
+        field = f"questions[{i}].{'levels' if q['type'] == 'score' else 'choices'}"
         if len(vals) != len(set(map(json.dumps, vals))):
-            errs.append(f"{q['name']}: choice values / level labels must be unique")
+            errs.append(Violation(f"{q['name']}: choice values / level labels must be unique", field))
         if q["type"] == "choice":
             groups = [c.get("group") for c in q["choices"]]
             if any(groups) and not all(groups):
-                errs.append(f"{q['name']}: either every choice has a group or none does")
+                errs.append(Violation(f"{q['name']}: either every choice has a group or none does", field))
             if all(groups) and len(set(groups)) < 2:
-                errs.append(f"{q['name']}: groups need at least two distinct labels")
+                errs.append(Violation(f"{q['name']}: groups need at least two distinct labels", field))
     return errs
 
 
@@ -385,19 +400,26 @@
 
 
 def oa_request_invariants(req):
-    """Facade request rules JSON Schema can't express: name uniqueness, reserved q{i} names, collision-free choice identity."""
+    """Facade request rules JSON Schema can't express: name uniqueness, reserved q{i} names, collision-free choice identity,
+    at most OA_MAX_IMAGES images."""
     errs = []
     named = [q["name"] for q in req["questions"] if q.get("name") is not None]
     if len(named) != len(set(named)):
-        errs.append("explicit question names must be unique")
-    errs += [f"name {n!r} is reserved for generated q{{i}} names" for n in named if AUTO_NAME.match(n)]
+        errs.append(Violation("explicit question names must be unique", "questions"))
+    errs += [Violation(f"name {q['name']!r} is reserved for generated q{{i}} names", f"questions[{i}].name")
+             for i, q in enumerate(req["questions"]) if q.get("name") is not None and AUTO_NAME.match(q["name"])]
     for i, q in enumerate(req["questions"]):
         if q["type"] == "choice":
             keys = [member_key(c["value"]) for c in q["choices"]]  # true and "true" serialize identically for members
             if len(keys) != len(set(keys)):
-                errs.append(f"questions[{i}]: choice values collide after serialization (true/\"true\", false/\"false\" or duplicates)")
+                errs.append(Violation(f"questions[{i}]: choice values collide after serialization (true/\"true\", false/\"false\" or duplicates)",
+                                      f"questions[{i}].choices"))
         if q["type"] == "score" and len({lv["label"] for lv in q["levels"]}) != len(q["levels"]):
-            errs.append(f"questions[{i}]: level labels must be unique")
+            errs.append(Violation(f"questions[{i}]: level labels must be unique", f"questions[{i}].levels"))
+    messages = req["input"] if isinstance(req["input"], list) else []
+    images = sum(p.get("type") == "input_image" for m in messages if isinstance(m["content"], list) for p in m["content"])
+    if images > OA_MAX_IMAGES:
+        errs.append(Violation(f"{images} input_image parts; at most {OA_MAX_IMAGES} per request (Bedrock limit)", "input"))
     return errs
 
 
@@ -441,6 +463,14 @@
                 errs.append(f"{tag}: score {a['score']} != expected index")
             if abs(ordinal_dispersion(probs) - a["confidence"]) > 0.02:
                 errs.append(f"{tag}: confidence != ordinal_dispersion")
+    x = res.get("x_aghoo")  # present only with x-aghoo-include: ensemble; keyed by effective names (given name or q{i})
+    if x is not None:
+        effective = [q.get("name") or f"q{i}" for i, q in enumerate(req["questions"])]
+        refused = {n for n, a in zip(effective, res["answers"]) if a["type"] == "refusal"}
+        if set(x["calibration"]) != set(effective) - refused:
+            errs.append(f"x_aghoo.calibration keys {sorted(x['calibration'])} != non-refusal answers {sorted(set(effective) - refused)}")
+        if not set(x.get("mapped_unavailable", {})) <= refused:
+            errs.append(f"x_aghoo.mapped_unavailable names answers that are not refusals: {sorted(set(x['mapped_unavailable']) - refused)}")
     u = res["usage"]
     if u["total_tokens"] != u["input_tokens"] + u["output_tokens"]:
         errs.append(f"usage.total_tokens {u['total_tokens']} != input + output")
@@ -476,14 +506,19 @@
     return result_invariants(doc, req)
 
 
-self_tests()
-failures = 0
-for path in sorted(EX.glob("*.json")):
-    errs = check(path)
-    expect_valid = ".valid" in path.name
-    ok = (not errs) == expect_valid
-    failures += not ok
-    detail = "valid" if not errs else f"rejected: {errs[0]}"
-    print(f"{'PASS' if ok else 'FAIL'}  {path.name:62s} {detail[:120]}")
-print(f"\nconfidence self-tests passed; {failures} unexpected outcome(s)")
-sys.exit(1 if failures else 0)
+def main():
+    self_tests()
+    failures = 0
+    for path in sorted(EX.glob("*.json")):
+        errs = check(path)
+        expect_valid = ".valid" in path.name
+        ok = (not errs) == expect_valid
+        failures += not ok
+        detail = "valid" if not errs else f"rejected: {errs[0]}"
+        print(f"{'PASS' if ok else 'FAIL'}  {path.name:62s} {detail[:120]}")
+    print(f"\nconfidence self-tests passed; {failures} unexpected outcome(s)")
+    return 1 if failures else 0
+
+
+if __name__ == "__main__":
+    sys.exit(main())
--- a/contracts/make_examples.py
+++ b/contracts/make_examples.py
@@ -233,6 +233,7 @@
     w("decision-result.valid.json", res)
     w("decision-request.valid-json-state.json", {"profile": "router-default", "input": {"order_id": "A-17", "events": ["delivered", "charged", "charged"]},
                                                  "questions": questions[:2]})
+    w("decision-request.valid-default-profile.json", {k: v for k, v in req.items() if k != "profile"})  # tenant defaults.decision_profile answers
 
     # image turn + escalation (illustrative): deepseek is text-only -> sees OCR text; department escalated to router-plus
     img_req = {"profile": "router-default@1", "questions": questions[:1], "include": ["members", "stages"],
@@ -386,6 +387,15 @@
     w("openai-request.invalid-bool-string-collision.json", {**oa_req, "questions": [oa_req["questions"][0], {**oa_req["questions"][1], "choices": [{"value": True}, {"value": "true"}, {"value": False}]}]})
     w("openai-request.invalid-256-choices.json", {**oa_req, "questions": [{"type": "choice", "instructions": "pick",
                                                                            "choices": [{"value": f"v{i}"} for i in range(256)]}]})
+    pixel = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
+    w("openai-request.invalid-21-images.json", {**oa_req, "input": [{"role": "user", "content": [{"type": "input_text", "text": "Which screenshot shows the bug?"}]
+                                                                    + [{"type": "input_image", "image_url": pixel}] * 21}]})
+    w("openai-request.invalid-remote-image-url.json", {**oa_req, "input": [{"role": "user", "content": [
+        {"type": "input_text", "text": "What does this screenshot show?"}, {"type": "input_image", "image_url": "https://example.com/shot.png"}]}]})
+    oa_ens = {**oa_res, "answers": [oa_res["answers"][0], {"type": "refusal", "name": "regression"}],
+              "x_aghoo": {"decision_id": "dec_01J9ZS9X2A", "calibration": {"q0": "uncalibrated"}, "mapped_unavailable": {"regression": "deadline_exceeded"}}}
+    w("openai-response.valid-ensemble.json", oa_ens)  # x-aghoo-include: ensemble; regression missed the deadline and is mapped to refusal
+    w("openai-response.invalid-x-aghoo-mapped-not-refusal.json", {**oa_ens, "x_aghoo": {**oa_ens["x_aghoo"], "mapped_unavailable": {"q0": "no_quorum"}}})
     print("examples written:", len(list(EX.glob("*.json"))))
 
 
--- a/qa/run_design_audit.py
+++ b/qa/run_design_audit.py
@@ -33,14 +33,8 @@
 def load_checkers():
     config = runpy.run_path(str(CONTRACTS / "check_config.py"))
     python = runpy.run_path(str(CONTRACTS / "python_sdk_types.py"))
-    # The example checker runs its CLI at module scope. Load only its definitions, before that CLI,
-    # so the suite does not read or generate every example file.
-    source = (CONTRACTS / "check_examples.py").read_text()
-    definitions, marker, _ = source.partition("\nself_tests()\n")
-    if not marker:
-        raise RuntimeError("Example checker layout changed; review the loader.")
-    examples = {"__file__": str(CONTRACTS / "check_examples.py")}
-    exec(compile(definitions, examples["__file__"], "exec"), examples)
+    # The example checker's CLI is guarded by __main__, so loading it defines the validators without running them.
+    examples = runpy.run_path(str(CONTRACTS / "check_examples.py"))
     return config, python, examples
 
 
--- a/check_docs.sh
+++ b/check_docs.sh
@@ -34,6 +34,8 @@
 if (cd contracts && python3 -I check_config.py >/dev/null); then echo "config  PASS"; else echo "config  FAIL"; (cd contracts && python3 -I check_config.py | grep FAIL); fail=1; fi
 if python3 -I qa/run_design_audit.py >/dev/null; then echo "audit   PASS"; else echo "audit   FAIL"; python3 -I qa/run_design_audit.py | grep REGRESSED; fail=1; fi
 if (cd contracts && npx -y @redocly/cli@latest lint openapi.yaml --format stylish 2>&1 | grep -q "Your API description is valid" && ! npx -y @redocly/cli@latest lint openapi.yaml --format stylish 2>&1 | grep -q "warning"); then echo "openapi PASS"; else echo "openapi FAIL"; fail=1; fi
+# Bundle openapi.yaml with its external JSON Schemas (the input of the SDK and server-model generators): no warnings.
+if (cd contracts && b=$(npx -y @redocly/cli@latest bundle openapi.yaml -o "$out/openapi.bundled.yaml" 2>&1) && ! grep -qi "warning" <<<"$b"); then echo "bundle  PASS"; else echo "bundle  FAIL"; fail=1; fi
 
 # Prose sanity: heading markers glued to text (spliced edits), unbalanced code fences
 if python3 -I - <<'EOF'
--- a/01-feasibility-study.md
+++ b/01-feasibility-study.md
@@ -49,7 +49,7 @@
 | Decision path: gpt-6-luna and an ensemble of smaller LLMs | **Feasible** [C on E] | `lightweight-model-decide` (one configurable lightweight model, gpt-6-luna by default) or `router-default` (gpt-6-luna + Haiku 5.5, verbalized; DeepSeek V3.2, logprobs) |
 | OpenAI `/v1/decisions`-like API | **Feasible** [C] | Native API + OpenAI-shaped facade with its own contract; never calls OpenAI |
 | JEV-like typed results (`choice` / `score` / `noul`) | **Feasible** [C on E] | Jev-shaped output format verified with gpt-6-luna (P13); "type-safe" means schema conformance only [JEV-V] |
-| Candidates, ensemble scores, confidence, provenance | **Feasible** [C] | Machine-checked: 43 examples, Pydantic validators, OpenAPI |
+| Candidates, ensemble scores, confidence, provenance | **Feasible** [C] | Machine-checked: 48 examples, Pydantic validators, OpenAPI |
 | Calibrated probabilities | **Feasible with LLM-consensus labels** | Calibrated vs consensus on the agreeing subset; family overlap (§4.1) |
 | Decision outputs leveraged for generation | **Feasible** [C on E] | Effort and conditioning verified (P10, P11); end-of-turn conditioning placement to be A/B-tested |
 | Configurable, ensembled decision and generation models | **Feasible** [C] | Config schema + cross-reference checker; `best_of_n` can't stream before selection |
--- a/02-architecture.md
+++ b/02-architecture.md
@@ -761,7 +761,7 @@
 | HEIC / HEIF | MVP | upload or inline | → PNG `raster` (pillow-heif in the sandbox) | iPhone default format |
 | PDF | MVP | upload | `pdf_sanitized`, `markdown`, `page_images`, `ocr_text` | Active content (JS/OpenAction/Launch/EmbeddedFile/XFA/RichMedia) is **stripped** by the pikepdf rewrite and reported as degradation `active_content_removed`. Encrypted → reject `encrypted_pdf` |
 | Markdown | MVP | upload or text | `markdown` (sanitized) | — |
-| DOCX | Phase 2 (MVP: 415 `not_allowlisted`) | upload | `markdown` (Docling) | OOXML zip-bomb and XXE checks |
+| DOCX | Phase 2 (MVP: 415 `unsupported_media_type`) | upload | `markdown` (Docling) | OOXML zip-bomb and XXE checks |
 | SVG | Phase 2 | upload | `raster` (resvg), `text` | Never forwarded or rendered inline |
 | MP3 / WAV / M4A / FLAC | Phase 2 | upload | `audio_normalized` (sandbox), `transcript` (job runner + Transcribe) | No selected model accepts audio natively (P10) |
 | MP4 / MOV / WebM | Phase 2 | upload | `keyframes` (≤ N), `ocr_text`, `audio_normalized`, `transcript`, `timeline` | No selected model accepts video natively (P9) |
--- a/03-contracts.md
+++ b/03-contracts.md
@@ -6,13 +6,13 @@
 
 | Artifact | Purpose | Check (`./check_docs.sh`) |
 |---|---|---|
-| [`contracts/openapi.yaml`](contracts/openapi.yaml) | OpenAPI 3.1 for every public endpoint; references the two JSON Schemas below | Redocly lint, 0 warnings |
+| [`contracts/openapi.yaml`](contracts/openapi.yaml) | OpenAPI 3.1 for every public endpoint; references the two JSON Schemas below | Redocly lint and bundle, 0 warnings |
 | [`contracts/types.ts`](contracts/types.ts) | Wire types: questions, input (incl. JSON state), answers, `DecisionResult`, routing, conditioning, `TurnEvent` | `tsc --strict` |
 | [`contracts/openai-compat.ts`](contracts/openai-compat.ts) | OpenAI Decisions-compatible facade types | `tsc --strict` |
 | [`contracts/sdk.ts`](contracts/sdk.ts) + [`sdk-usage.check.ts`](contracts/sdk-usage.check.ts) | TypeScript SDK surface; compile-time tests incl. `@ts-expect-error` negatives | `tsc --strict` |
 | [`contracts/decision.schema.json`](contracts/decision.schema.json) | JSON Schema 2020-12 for native decision requests and results | `check_examples.py` |
 | [`contracts/openai-compat.schema.json`](contracts/openai-compat.schema.json) | JSON Schema for the facade request and response | `check_examples.py` |
-| [`contracts/check_examples.py`](contracts/check_examples.py) | Schema + semantic invariants (native, Jev-shaped and facade), results paired with requests; formula self-tests | 43 examples (11 valid, 32 invalid) |
+| [`contracts/check_examples.py`](contracts/check_examples.py) | Schema + semantic invariants (native, Jev-shaped and facade), results paired with requests; formula self-tests | 48 examples (13 valid, 35 invalid) |
 | [`contracts/python_sdk_types.py`](contracts/python_sdk_types.py) | Pydantic models with invariant validators; response models ignore unknown fields | validates every result example |
 | [`contracts/config.schema.json`](contracts/config.schema.json) + [`check_config.py`](contracts/check_config.py) | Platform configuration schema and cross-reference rules | example config + 45 negative tests + 1 positive test |
 | [`contracts/make_examples.py`](contracts/make_examples.py) | Regenerates the examples from probe data (reproducible) | — |
@@ -26,13 +26,13 @@
     - an unrecognized answer `type` becomes an `UnknownAnswer` (`type: "unknown"`, `raw_type`, `raw`) instead of a validation error;
     - conservative handling: an unknown calibration status is `uncalibrated`; an `UnknownAnswer` is treated like `unavailable`; an unknown member status is not a vote.
   - **Breaking changes** go to `/v2`.
-  - **Pinning**: `Aghoo-Version: 2026-10-01` pins behavior.
+  - **Pinning**: `Aghoo-Version: 2026-10-01` pins behavior. It is the current, default and only supported value; any other value is 400 `schema_violation`.
   - **Profiles** can be pinned by version (`router-default@1`). Responses echo the resolved version.
 - **Authentication**: `Authorization: Bearer <token>`.
   - API key format: `ak_<env>_<id>.<secret>`. It's hashed at rest and scoped to a project and permissions.
   - OIDC access tokens are also accepted: from the UI via the BFF, or from SDKs acting for end users.
   - End-user delegation: the `x-aghoo-end-user` JWT, see [04 §4.2](04-caching-routing-security.md#42-authentication-and-authorization).
-- **Request identity**: `x-request-id` is echoed back. W3C `traceparent` is propagated. `Idempotency-Key` and `Aghoo-Version` are accepted on every POST.
+- **Request identity**: `x-request-id` is echoed back on every response (a valid client value, otherwise a server-generated id). W3C `traceparent` is propagated. `Idempotency-Key` and `Aghoo-Version` are accepted on every POST.
   - **Idempotency scope**: a key is unique per `(project, method, path)` and remembers a fingerprint of the canonical request body, for 24 h.
   - Same key, same fingerprint while the first request is in flight: the duplicate attaches to it (JSON waits for the result; SSE replays the event log from the first event).
   - Same key after completion: the stored response is replayed (`x-aghoo-idempotent-replay: true`) with no new model calls and no new billing.
@@ -47,19 +47,22 @@
 | HTTP | `type` | `code` | Retryable | Where |
 |---|---|---|---|---|
 | 400 | `invalid_request_error` | `schema_violation`, `unsupported_question_type`, `reserved_question_name` (an app question reuses a router question name), `model_not_available` (facade) | no | HTTP |
-| 401 | `authentication_error` | `invalid_api_key` | no | HTTP |
-| 403 | `permission_error` | `profile_not_allowed`, `session_forbidden` | no | HTTP |
+| 401 | `authentication_error` | `invalid_api_key`, `invalid_end_user_token` (`x-aghoo-end-user`) | no | HTTP |
+| 403 | `permission_error` | `permission_denied` (role or key scope), `profile_not_allowed`, `session_forbidden` | no | HTTP |
 | 404 | `not_found_error` | `session_not_found` (also for other tenants' ids), `not_found` | no | HTTP |
 | 409 | `conflict_error` | `idempotency_key_reused`, `session_conflict` (`expected_seq` mismatch), `session_busy` (another turn holds the session), `file_not_ready`, `file_rejected` | `session_busy`: yes; `file_not_ready`: yes, after watching the file | HTTP |
 | 410 | `invalid_request_error` | `resume_window_expired` | no | HTTP |
-| 413 / 415 | `invalid_request_error` | `file_too_large`, `image_too_large`, `unsupported_media_type` | no | HTTP |
-| 422 | `capability_error` | `no_route_satisfies_constraints` (`details.unmet`), `no_capable_member`, `unsupported_parameter` | no | HTTP |
+| 413 / 415 | `invalid_request_error` | 413: `request_too_large` (JSON body over 50 MiB), `file_too_large`, `image_too_large`; 415: `unsupported_media_type` | no | HTTP |
+| 422 | `capability_error` | `no_route_satisfies_constraints` (`details.unmet`), `no_capable_member`, `unsupported_parameter`, `feature_not_available` (a value or header the current phase doesn't serve; `param` names it) | no | HTTP |
 | 429 | `rate_limit_error` | `rpm_exceeded`, `tpm_exceeded`, `queue_timeout`, `budget_exhausted` | yes after `retry-after` (not `budget_exhausted`) | HTTP |
 | 503 | `upstream_error` | `all_routes_unavailable`, `tenant_access_misconfigured` | `all_routes_unavailable`: yes; `tenant_access_misconfigured`: no | HTTP or SSE |
+| 500 | `server_error` | `internal_error` | yes | HTTP or SSE |
+| 501 | `invalid_request_error` | `not_implemented` (an operation the current phase doesn't serve) | no | HTTP |
 | 504 | `upstream_error` | `deadline_exceeded` | yes | HTTP or SSE |
 | — | `upstream_error` | `upstream_stalled`, `client_too_slow` | `upstream_stalled`: yes (`retry_of`) | SSE `error` event |
 
-- **Rate-limit headers**: `x-ratelimit-{limit,remaining,reset}-{requests,tokens}`, plus `retry-after` on 429. Decision and labeling calls count toward the tenant's quotas.
+- Each code has exactly one HTTP status, `type` and retryable value; `x-noema-error-codes` in [`openapi.yaml`](contracts/openapi.yaml) lists the codes each operation may return.
+- **Rate-limit headers**: `x-ratelimit-{limit,remaining,reset}-{requests,tokens}` (`reset` in whole seconds) on model-calling responses, plus `retry-after` on 429. Decision and labeling calls count toward the tenant's quotas.
 - **Pagination**: cursor-based (`?limit=&after=`), returning `{ data, has_more, next_cursor }`.
 
 ## 2. Endpoint catalog
@@ -71,7 +74,7 @@
 | `POST /v1/turns` | Smart turn: authorized history, concurrent decision + generation, conditioning, verification; SSE or JSON | 1 | yes | [C] |
 | `GET /v1/turns/{id}` | Stored turn | 1 | yes | [C] |
 | `POST /v1/turns/{id}/cancel` | Explicit, idempotent cancel of a running turn (§3.4) | 1 | yes | [C] |
-| `GET /v1/turns/{id}/events` | Resume a stream (`Last-Event-ID`, within the resume window, §3.4) | 2 | yes | [C] |
+| `GET /v1/turns/{id}/events` | Resume a stream (`Last-Event-ID`, within the resume window, §3.4); Phase 1 answers 501 `not_implemented` | 2 | yes | [C] |
 | `POST /v1/decisions` · `GET /v1/decisions/{id}` | Typed decision (`choice` / `score` / `predicate`\|`noul`) from a Bedrock profile: `lightweight-model-decide` (one configurable lightweight model, default gpt-6-luna) or `router-default` (ensemble). Native output (candidates, calibration, confidence, provenance) or Jev-shaped output (`output_format: "jev"`) | 1 | yes | [C] |
 | `POST /openai/v1/decisions` | OpenAI Decisions-compatible facade: shapes only, answered by a Bedrock decision profile (default `lightweight-model-decide`) | 1 | yes | [C] |
 | `GET /v1/profiles?kind=decision\|generation` | Discover the profiles the caller may select | 1 | yes | [C] |
@@ -79,7 +82,8 @@
 | `POST/GET /v1/sessions` · `GET/PATCH/DELETE /v1/sessions/{id}` · `GET /v1/sessions/{id}/turns` | Sessions, retention, authorized history | 1 (ACL sharing: 2) | yes | [C] |
 | `POST /v1/files` · `GET/DELETE /v1/files/{id}` · `GET /v1/files/{id}/events` · `GET /v1/files/{id}/derivatives/{kind}` | Upload, status and its stream, derivatives | 1 | yes | [C] |
 | `GET /v1/usage` | Usage and cost: decision, generation, labeling, verification, waste and cache-savings lines | 1 | yes | [C] |
-| `/v1/admin/*` | Tenants and AWS accounts, models, profiles, routing policy, labeling, calibrators, keys, budgets, retention, audit (§8). Phase 1 is IaC-driven; the API and console come in Phase 2 | 2 | no | [C] |
+| `GET /v1/admin/calibrators` · `GET /v1/admin/labeling/runs` · `GET /v1/admin/labeling/dropped` | Read-only W4 views: calibrators with holdout metrics and reliability bins, labeling runs with per-class consensus, dropped items with both labelers' answers | 1 | yes | [C] |
+| `/v1/admin/*` | Tenants and AWS accounts, models, profiles, routing policy, labeling, calibrators, keys, budgets, retention, audit (§8). Phase 1 is IaC-driven (plus the read-only W4 views above); the API and console come in Phase 2 | 2 | no | [C] |
 | `GET /healthz` · `GET /readyz` | Liveness, readiness | 1 | yes | [E] |
 | `GET /metrics` | Prometheus | 1 | no | [E] |
 
@@ -218,7 +222,7 @@
 |---|---|
 | `input` | a string, user messages (text, images, files, `input_json` parts), or a top-level JSON **object** as JEV-style state. Jev's `state` also accepts arrays; here an array or scalar goes inside an `input_json` part (`{"type":"input_json","json":[…]}`) |
 | `questions` | `predicate` or its Jev synonym `noul` (optional `criteria.true` / `criteria.false`), `choice` (2–255 values, descriptions optional and nullable, optional `group` per value), `score` (2–16 levels, ordered). A question with more options than a member's template `max_choices` (16 logprob codes, 26 verbalized letters) is answered hierarchically ([08 §3.4](08-technical-reference.md#34-choice-cardinality-flat-and-hierarchical-methods-x)) |
-| `profile` | `lightweight-model-decide` (one configurable lightweight model, default gpt-6-luna; the Jev-like decision, D6; **default**), `router-default` (ensemble), `router-plus`; optionally version-pinned |
+| `profile` | `lightweight-model-decide` (one configurable lightweight model, default gpt-6-luna; the Jev-like decision, D6; **default**), `router-default` (ensemble), `router-plus`; optionally version-pinned. Optional: without it, the tenant's `defaults.decision_profile` answers |
 | `output_format` | `native` (default) or `jev` (§4.3) |
 | optional | `deadline_ms`, `min_quorum`, `escalation`, `include`, `safety_identifier` |
 
@@ -297,7 +301,7 @@
 | Up to 32 questions; 2–255 choices; 2–16 levels with optional descriptions | Accepted. More than 32 questions or 16 levels is 400 `schema_violation` (the same bounds as the native API). A choice question that needs the hierarchical method ([08 §3.4](08-technical-reference.md#34-choice-cardinality-flat-and-hierarchical-methods-x)) is 422 `unsupported_parameter` |
 | message `content` string or array; `type: "message"` | Accepted |
 | `input_image` data URL | Accepted. **≤ 20 images per request** (the Bedrock Converse limit; OpenAI allows 128), otherwise 400 with `param` |
-| `input_image` http(s) URL | **400 `unsupported_parameter`**: no server-side fetching (SSRF). Send a data URL or upload a file |
+| `input_image` http(s) URL | **400 `schema_violation`** (`param` points at the image): no server-side fetching (SSRF). Send a data URL or upload a file |
 | `detail` | Accepted and ignored (Noema resizes to the route's limits) |
 | `safety_identifier` | Stored with the decision for abuse tracking; never sent to models |
 | `model` not an `aghoo/…` profile (e.g. `gpt-6-luna`) | 400 `model_not_available`, listing the allowed profiles. `aghoo/lightweight-model-decide` runs gpt-6-luna on Bedrock, the model OpenAI documents for its Decisions API [OA-G]. Compatibility is of **shapes** only: the prompts, the pooling and the confidence formula are ours, so outputs are not equivalent to OpenAI's |
@@ -452,7 +456,7 @@
 
 const questions = [
   q.choice("department", { instructions: "Which team should handle this message?",
-    choices: { billing: "Payments, refunds", technical: "Bugs", other: null } }),
+    choices: [{ value: "billing", description: "Payments, refunds" }, { value: "technical", description: "Bugs" }, { value: "other" }] }),
   q.predicate("needs_human", { instructions: "The customer explicitly asks for a human.",
     criteria: { true: "Asks for a person", false: "No such request" } }),
 ] as const;
@@ -475,7 +479,7 @@
 - Retries (429/5xx/connect) happen only before the first stream event.
 - `Idempotency-Key` is automatic for POSTs.
 - `AbortSignal` cancels both the stream and the server-side turn.
-- `lastEventId` resume.
+- `lastEventId` resume (Phase 2; Phase 1 answers 501 `not_implemented`).
 - `files.watch()` streams file status.
 - Unknown fields and event types are ignored.
 
--- a/05-implementation-plan.md
+++ b/05-implementation-plan.md
@@ -69,7 +69,7 @@
 
 | Area | Deliverable |
 |---|---|
-| API | Every endpoint in [`contracts/openapi.yaml`](contracts/openapi.yaml) with phase 1 in [03 §2](03-contracts.md#2-endpoint-catalog): turns (SSE + JSON, `cancel`), decisions + the OpenAI-compatible decisions facade, profiles, sessions, files (+ events, derivatives), usage, health. Stream resume (`GET /v1/turns/{id}/events`) and the generation facades are Phase 2. SDKs generated from the OpenAPI spec |
+| API | Every endpoint in [`contracts/openapi.yaml`](contracts/openapi.yaml) with phase 1 in [03 §2](03-contracts.md#2-endpoint-catalog): turns (SSE + JSON, `cancel`), decisions + the OpenAI-compatible decisions facade, profiles, sessions, files (+ events, derivatives), usage, health. Stream resume (`GET /v1/turns/{id}/events`) and the generation facades are Phase 2. SDK wire types generated from the OpenAPI spec; the clients are hand-written against `contracts/sdk.ts` |
 | Tenancy | Tenant → AWS account → role mapping; STS session cache; per-(tenant account, model) quota buckets; AssumeRole failures fail closed |
 | Adapters | `bedrock-converse` (Converse/ConverseStream, `outputConfig.effort` and `textFormat`, `reasoningContent` filtering) and `bedrock-mantle-chat` (logprobs). The `probes/bedrock_*.py` checks become adapter self-tests |
 | Decision engine | `lightweight-model-decide@1` (gpt-6-luna, Jev-like) and `router-default@1`; `predicate`/`noul`/`choice`/`score`; native and Jev-shaped output (`output_format`); commit-critical vs non-blocking questions; one call per (member, question) in parallel; input views (`native` / `ocr_text` / `derived_text`); backup requests; linear pool; heuristic confidence and bands; gate / bounded holdback (early commit is [X] and off until its property test is recorded); `router-plus` escalation; provenance; L2 decision cache |
--- a/07-api-user-guide.md
+++ b/07-api-user-guide.md
@@ -109,7 +109,7 @@
 const d = await client.decisions.create({
   profile: "router-default",
   input: "My package says delivered but I was also charged twice.",
-  questions: [q.choice("department", { instructions: "Which team?", choices: { billing: null, shipping: null, other: null } })] as const,
+  questions: [q.choice("department", { instructions: "Which team?", choices: [{ value: "billing" }, { value: "shipping" }, { value: "other" }] })] as const,
 });
 const dept = d.byName.department;
 if (dept.type === "choice") console.log(dept.choice, dept.confidence.band);
@@ -252,7 +252,7 @@
 
 ```ts
 const r = await client.decisions.create({ profile: "lightweight-model-decide", output_format: "jev", input, questions: [
-  q.choice("department", { instructions: "Which team?", choices: { billing: null, shipping: null, other: null } }),
+  q.choice("department", { instructions: "Which team?", choices: [{ value: "billing" }, { value: "shipping" }, { value: "other" }] }),
   q.noul("needs_human", { instructions: "Asks for a human." }) ] as const });
 if (r.answers.needs_human.type === "noul" && r.answers.needs_human.noul > 0.5) escalate();
 ```
@@ -298,7 +298,7 @@
 | `generation.effort` | set by the decision (complexity) | Force `low` for speed or `high` for depth. Ignored, with a degradation, on models that don't support the value |
 | `generation.temperature` | — | Most default models reject it. It's honored only where supported (GLM-5.3) |
 | `conditioning` | `auto` | `off` for raw model behavior; `direct` to forbid clarifying questions |
-| `strategy` | `auto` (holdback) | `gate` waits for the decision before generating (slower, no restarts); `speculative` streams immediately (fastest, may answer on the wrong spec) |
+| `strategy` | `auto` (holdback) | `gate` waits for the decision before generating (slower, no restarts); `speculative` streams immediately (fastest, may answer on the wrong spec; Phase 3, before that 422 `feature_not_available`) |
 | `decisions.blocking` | — | Your own questions that must be answered before the answer streams |
 | `verify` | per policy | `true` adds answer checks (`turn.verified`) |
 | `store` | `true` | `false` for ephemeral turns (no history, no caching, no `GET` afterwards) |
@@ -331,7 +331,7 @@
 2. On `generation.rerouted` (only with the `speculative` strategy), delete text from `discard_from_seq` onward and continue with the new attempt.
 3. **Ignore event types you don't know.** New ones are added without a version bump.
 4. Heartbeats are SSE comments (`: ping`) every 15 s.
-5. **Resume** after a disconnect with `GET /v1/turns/{id}/events` and header `Last-Event-ID: <last id>`, within the resume window (5 minutes from the last event; otherwise 410 `resume_window_expired`, and a stored turn is still readable with `GET /v1/turns/{id}`). Resume is a Phase 2 endpoint. A dropped connection does **not** cancel the turn.
+5. **Resume** after a disconnect with `GET /v1/turns/{id}/events` and header `Last-Event-ID: <last id>`, within the resume window (5 minutes from the last event; otherwise 410 `resume_window_expired`, and a stored turn is still readable with `GET /v1/turns/{id}`). Resume is a Phase 2 endpoint (Phase 1 answers 501 `not_implemented`). A dropped connection does **not** cancel the turn.
 6. **Cancel on purpose** with `POST /v1/turns/{id}/cancel` (the SDK's `abort()` does this). It is final and idempotent: committed partial text is kept and the stream ends with `turn.completed`.
 7. **`turn.completed` is final.** Late decision members and verification arrive before it; `TurnStream.decision()` settles with it. Nothing follows.
 
@@ -428,6 +428,7 @@
 | Upstream trouble | `all_routes_unavailable` (503), `deadline_exceeded` (504) | Retry with backoff (same `Idempotency-Key`) |
 | Stream broke after text started | SSE `error` with `upstream_stalled` | Show the partial text; retry the turn with `retry_of: <turn_id>` |
 | Tenant misconfigured | `tenant_access_misconfigured` (503) | Not retryable; contact the platform admin |
+| Not served in this phase | `feature_not_available` (422; `param` names the field or header), `not_implemented` (501, e.g. stream resume) | Don't retry; drop the field or use the documented alternative |
 
 Retry rules:
 - **Every POST takes an `Idempotency-Key`**; a key is scoped to your project, method and path, and to the request body: retries with the same key and body return the same result or stream (nothing is re-run or re-billed), while the same key with a different body is 409 `idempotency_key_reused`.
@@ -448,7 +449,7 @@
 ## 10. Versioning and compatibility
 
 - **Additive changes** (fields, enum values, answer types, event types) ship without a version bump. Ignore what you don't know; the SDKs do. Unknown enum values stay plain strings, and an answer `type` the SDK doesn't know becomes an `UnknownAnswer` (`raw_type`, `raw`). Be conservative with them: an unknown `calibration.status` is `uncalibrated`, and an `UnknownAnswer` is `unavailable`.
-- **Pinning**: pin API behavior with `Aghoo-Version: YYYY-MM-DD`, and pin profiles with `@version`.
+- **Pinning**: pin API behavior with `Aghoo-Version: YYYY-MM-DD` (`2026-10-01` is the current and only supported value), and pin profiles with `@version`.
 - **Breaking changes** go to `/v2`, with a deprecation notice period.
 
 ## 11. Limits (initial)
@@ -470,7 +471,7 @@
 - **Why is my probability "uncalibrated"?** The profile version, stage or member set is new, or there aren't enough consensus labels yet (≥ 300 per question signature). A calibrator applies only to the stage it was fitted for: every `lightweight-model-decide` answer, and every answer that escalated to another stage, stays `uncalibrated` until that stage has its own accepted calibrator (which must also pass the labeler-family check). Use the ranking and the `band`, not the absolute number.
 - **What does "calibrated" mean exactly?** Among answers marked p ≈ 0.8, about 80% matched the consensus of GPT-6.1-Sol and Claude Sonnet 5.5 on held-out data. That's agreement with strong models, not ground truth, and it's measured on the items where those two agreed (see `holdout.consensus_rate` and `holdout.scope`).
 - **Why did the answer ask me a clarifying question?** The decision was ambiguous (band `low`), so Noema told the model to cover both readings and ask. Set `conditioning: "direct"` to forbid it.
-- **Why was there a pause before the first token?** Noema held the first tokens until the decision confirmed the route, for at most `max_hold_ms` (400 ms by default). It is free on reasoning routes, where the model thinks for longer than the decision takes. On fast routes the hold can reach the cap; the answer then commits on the early guess (`holdback_deadline`) and `routing.served_spec_differs` reports whether the decision later disagreed. Use `strategy: "speculative"` for zero hold (at the risk of a wrong-spec answer).
+- **Why was there a pause before the first token?** Noema held the first tokens until the decision confirmed the route, for at most `max_hold_ms` (400 ms by default). It is free on reasoning routes, where the model thinks for longer than the decision takes. On fast routes the hold can reach the cap; the answer then commits on the early guess (`holdback_deadline`) and `routing.served_spec_differs` reports whether the decision later disagreed. From Phase 3, `strategy: "speculative"` gives zero hold (at the risk of a wrong-spec answer).
 - **Why is Opus slow?** Opus reasons before answering. At its default effort the first text took up to ~12 s in the probes (2.05 s at `low`, P11). The `opus-deep` profile runs at `high` by default, and the bindings lower it to `low` only for `trivial` complexity: a `moderate` request keeps `high`. Send `generation.effort` to choose.
 - **My media question came back `unavailable: no_capable_member`.** No answering member had an adequate view of the media: for example the file's derivatives weren't ready (the view fell back to `metadata_only`), or the profile's only members were text-only. `lightweight-model-decide` (GPT-6-Luna sees images natively) and `router-default` both qualify for images. For a PDF or audio, the adequate view is the extracted Markdown or transcript, so check that the file is `ready`.
 - **Can I use `temperature`?** Only on models that support it (GLM-5.3 among the defaults). Elsewhere it's dropped with a `param_unsupported` degradation, or rejected if you pinned the profile.
--- a/08-technical-reference.md
+++ b/08-technical-reference.md
@@ -30,7 +30,7 @@
 | `external_id_ref` | `aws-sm://…` | yes | Secrets Manager reference; never inline |
 | `home_region` | region | yes | Mantle endpoint region; `us.` profiles fan out within the US |
 | `allowed_models` | aliases | no | Default: all models |
-| `defaults.decision_profile` / `.generation_profile` | profile ids | no | Defaults for requests. `decision_profile` is the default for `/v1/decisions` and the facade (`lightweight-model-decide` = one configurable lightweight model, gpt-6-luna by default, D6); turns always use `routing.router_profile` for routing |
+| `defaults.decision_profile` / `.generation_profile` | profile ids | no | Defaults for requests. `decision_profile` is the default for `/v1/decisions` when the request omits `profile` (the facade always names its profile in `model`; `lightweight-model-decide` = one configurable lightweight model, gpt-6-luna by default, D6); turns always use `routing.router_profile` for routing |
 | `request_overrides.decision_profiles` / `.generation_profiles` | profile ids | no | What a request may select (otherwise 403 `profile_not_allowed`) |
 
 ### 2.2 `models.<alias>`
@@ -549,11 +549,11 @@
 - **`docs/noema/check_docs.sh`** checks:
   - Mermaid render;
   - `tsc --strict` on the contracts;
-  - schema + invariants on 43 examples (native, hierarchical, Jev-shaped, facade);
+  - schema + invariants on 48 examples (native, hierarchical, Jev-shaped, facade);
   - Pydantic validators;
   - config schema + 45 negative tests and 1 positive test;
   - the design-QA regression suite (`qa/run_design_audit.py`, 41 cases: each original defect must still be rejected or handled);
-  - OpenAPI lint (0 warnings);
+  - OpenAPI lint and bundle (0 warnings);
   - prose sanity;
   - links and anchors.
 - **`probes/*.py`**: live Bedrock capability probes. Re-run on any model change; results go to `probes/results/`. Credentials come from the environment and are never printed. Bedrock only.
--- a/README.md
+++ b/README.md
@@ -72,7 +72,7 @@
 ```bash
 ./check_docs.sh   # Mermaid render · tsc --strict (contracts + SDK tests) · decision/facade schema examples + invariants
                   # · Pydantic validators · platform config + negative tests · design-QA regression suite (qa/)
-                  # · OpenAPI lint · prose sanity · links/anchors
+                  # · OpenAPI lint + bundle · prose sanity · links/anchors
 ```
 
 Evidence on disk:
````
<!-- END noema-rev6.patch -->
