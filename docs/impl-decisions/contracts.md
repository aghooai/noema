# Contracts: implementation decisions (Wave A, contracts analyst)

> **Superseded for implementation: the canonical file is [`../IMPL-DECISIONS.md`](../IMPL-DECISIONS.md)** (lead-architect consolidation, 2026-10-09). This file is kept as-is for traceability. Ids map as `IMPL-CON-01…11 → IMPL-030…042` (gaps 30–42), `IMPL-CON-12 → IMPL-056`, `IMPL-CON-13…18 → IMPL-062…067`. Where this file and the canonical file differ, the canonical file wins. The material changes:
>
> | Here | Canonical | Change |
> |---|---|---|
> | IMPL-CON-02 | IMPL-031 | No 400 "no default" path: the loader fills `lightweight-model-decide` (D6 for every tenant). The facade description: unknown or non-allowlisted → 403 (critic 4/5/35) |
> | IMPL-CON-03 | IMPL-032 | The 504 rule: `MIN_DISPATCH_MS` = 50 at stage-0 dispatch; `no_capable_member` removed from createDecision and the facade (critic 15/36/61) |
> | IMPL-CON-04 | IMPL-033 | `x-aghoo-end-user` **deferred to Phase 2**: 422 `feature_not_available` on every project; no JWKS (critic 58; your open question 3) |
> | IMPL-CON-07 | IMPL-036 | Adopted as the single hierarchical-code rule |
> | IMPL-CON-08 | IMPL-037 | Inline size check by arithmetic on the base64 length; the 8,000 px check in the sandbox job (critic 27/45) |
> | IMPL-CON-10 | IMPL-041 + IMPL-039 | `result_invariants`/`holdout_errors`/`band_of` parameterized; `check_sse_frame(..., decision_request=)` (critic 51) |
> | IMPL-CON-11 | IMPL-042 + IMPL-072 | `CalibratorInfo` aligned with ADR-003: no `level`/`member_id`; status `fitted\|rejected\|accepted\|active\|downgraded\|retired`; failed_rules A1–A8; two-person `approved_by`/`activated_by` (critic 11/31) |
> | §B.3 dependencies | IMPL-080 | Plus `openai` (PyPI and npm), `schemathesis`, `pytest-socket`, etc. (critic 54) |
> | 08-contract-patches | spec-08 §14 | The consolidation addendum supersedes the base payload where they overlap; one combined rev-6 payload (IMPL-067) |


These decisions cover gaps 30–37, 40, 41 and 42 from `.orchestration/context/01-gaps.md`, plus 7 new items (IMPL-CON-13…19) found while writing the patches. Every contract change is spelled out as an exact, tested patch in [`../impl-spec/08-contract-patches.md`](../impl-spec/08-contract-patches.md).

Line references point at the unpatched rev-5 design, with these abbreviations:

| Abbreviation | File |
|---|---|
| `03` | `docs/noema/03-contracts.md` |
| `openapi` | `contracts/openapi.yaml` |
| `dschema` | `contracts/decision.schema.json` |
| `oaschema` | `contracts/openai-compat.schema.json` |
| `types` | `contracts/types.ts` |
| `sdk` | `contracts/sdk.ts` |
| `pyt` | `contracts/python_sdk_types.py` |
| `chk` | `contracts/check_examples.py` |
| `audit` | `qa/run_design_audit.py` |

**Owner decisions.** D1–D6 and R5 are unchanged. IMPL-CON-02 implements the D6 default the docs already promise. IMPL-CON-07 touches R5, and it is flagged material there with an open question for the owner.

**Evidence.** The patched tree was built in a scratch copy and checked there:
- `check_docs.sh` passes every step (11 Mermaid renders, tsc, 48 examples, Pydantic, config, the 41 audit cases, Redocly lint and bundle with 0 warnings, prose and links).
- The patch applies cleanly to a fresh copy of `docs/noema`, and the result is byte-identical to the verified tree.
- Code generation was tried with the pinned tools (§B, §C):
  - TS wire types are assignable to the contract types in 18 of 18 assertions, and a mutation test proved the check catches drift;
  - Python: `jsonschema-rs` agrees with the reference `jsonschema` on all 48 examples plus mutations.

## Index

| Id | Gap | Title | Material | Contract patch |
|---|---|---|---|---|
| IMPL-CON-01 | 30 | `not_found` + the complete ErrorCode table (1 code → 1 status) | yes | yes |
| IMPL-CON-02 | 31 | `DecisionCreateParams.profile` is optional; the tenant default applies | yes (implements D6) | yes |
| IMPL-CON-03 | 32 | Error responses per operation: 401 and 500 everywhere, plus each operation's list | yes | yes |
| IMPL-CON-04 | 33 | Headers: rate limits, replay, trace, request id, end user, deadline, version | yes | yes |
| IMPL-CON-05 | 34 | Strict `TurnEvent` discriminator, `ErrorCode` in TS, file SSE event + heartbeat | yes | yes |
| IMPL-CON-06 | 35 | SDK strategy (TS generated wire types + hand-written client; Python hand-written) | yes | yes |
| IMPL-CON-07 | 36 | Phase-1 answer to out-of-phase values: 422 `feature_not_available` / 501 `not_implemented` | yes (touches R5) | yes |
| IMPL-CON-08 | 37 | Type fixes: `ProfileInfo.version`, inline image size, typed `x_aghoo`, facade rules | yes | yes |
| IMPL-CON-09 | 40 | `SessionUpdate`, the `SessionTurn` history item, usage pagination and `key` | yes | yes |
| IMPL-CON-10 | 41 | Server models, request gate, response validator; `check_examples.py` importable | no | yes |
| IMPL-CON-11 | 42 | Read-only W4 endpoints `/v1/admin/{calibrators,labeling/runs,labeling/dropped}` | yes | yes |
| IMPL-CON-12 | new | Facade remote image URL: 400 `schema_violation`, not `unsupported_parameter` | yes | yes |
| IMPL-CON-13 | new | Phase-2 upload types: 415 `unsupported_media_type`, not `not_allowlisted` | no | doc |
| IMPL-CON-14 | new | Closed response schemas in openapi.yaml; named schemas for codegen | no | yes |
| IMPL-CON-15 | new | Transport rules: body cap, Content-Type, unknown route/method, unknown query params | yes | yes |
| IMPL-CON-16 | new | Derivative download: per-kind content types, `?page=`, `pages` | yes | yes |
| IMPL-CON-17 | new | Machine-readable `x-noema-error-codes` / `x-noema-roles` + conformance tests | no | yes |
| IMPL-CON-18 | new | `check_docs.sh` bundle step; doc prose and example counts kept in sync | no | yes |

---

## A. Resolutions

**IMPL-CON-01 (gap #30): `not_found` + one complete error table.**

Decision. Add `not_found` to `ErrorCode`. It covers every unknown or foreign id except session ids:
- turn, decision, file, derivative kind, calibrator;
- `retry_of`;
- file ids in input parts;
- unknown routes.

Session ids keep `session_not_found`. Six more codes and one error type are added, and each code maps to exactly one HTTP status, `type` and `retryable` value (table below; frozen as `core/errors.py` `ERROR_TABLE`):

| New code | Status | Raised for |
|---|---|---|
| `permission_denied` | 403 | Role or key scope (W4 views, a key without `files.write`, and so on) |
| `invalid_end_user_token` | 401 | A bad `x-aghoo-end-user` token |
| `request_too_large` | 413 | IMPL-CON-15 |
| `feature_not_available` | 422 | IMPL-CON-07 |
| `internal_error` | 500 | New type `server_error` |
| `not_implemented` | 501 | IMPL-CON-07 |

`budget_exhausted` stays in the enum but is never raised in Phase 1, because budgets are Phase 2.

Rationale. A 1:1 code→status map turns the server's error mapping into a single frozen table. It also makes a cheap conformance test possible: every error response's status, type and retryable flag must equal the table entry. That is why resume gets its own `not_implemented` (501) instead of sharing `feature_not_available` across 422 and 501.

Refs: 03:47-60, 03:52, openapi:584-590, 06:225 (401s never echo the key).

Patch needed: contract (openapi `ErrorCode`/`Error`, types.ts `ErrorCode`/`ErrorType`/`ErrorEnvelope`) + doc (03 §1 table).

| code | HTTP | error.type | retryable |
|---|---|---|---|
| schema_violation | 400 | invalid_request_error | no |
| unsupported_question_type | 400 | invalid_request_error | no |
| reserved_question_name | 400 | invalid_request_error | no |
| model_not_available | 400 | invalid_request_error | no |
| invalid_api_key | 401 | authentication_error | no |
| invalid_end_user_token | 401 | authentication_error | no |
| permission_denied | 403 | permission_error | no |
| profile_not_allowed | 403 | permission_error | no |
| session_forbidden | 403 | permission_error | no |
| not_found | 404 | not_found_error | no |
| session_not_found | 404 | not_found_error | no |
| idempotency_key_reused | 409 | conflict_error | no |
| session_conflict | 409 | conflict_error | no (refetch `last_seq`, then resend) |
| session_busy | 409 | conflict_error | yes |
| file_not_ready | 409 | conflict_error | yes (after watching the file) |
| file_rejected | 409 | conflict_error | no |
| resume_window_expired | 410 | invalid_request_error | no |
| request_too_large | 413 | invalid_request_error | no |
| file_too_large | 413 | invalid_request_error | no |
| image_too_large | 413 | invalid_request_error | no |
| unsupported_media_type | 415 | invalid_request_error | no |
| no_route_satisfies_constraints | 422 | capability_error | no |
| no_capable_member | 422 | capability_error | no |
| unsupported_parameter | 422 | capability_error | no |
| feature_not_available | 422 | capability_error | no |
| rpm_exceeded | 429 | rate_limit_error | yes (`retry-after`) |
| tpm_exceeded | 429 | rate_limit_error | yes (`retry-after`) |
| queue_timeout | 429 | rate_limit_error | yes (`retry-after`) |
| budget_exhausted | 429 | rate_limit_error | no (no `retry-after`) |
| internal_error | 500 | server_error | yes |
| not_implemented | 501 | invalid_request_error | no |
| all_routes_unavailable | 503 | upstream_error | yes (`retry-after` when a breaker reopen time is known) |
| tenant_access_misconfigured | 503 | upstream_error | no |
| deadline_exceeded | 504 | upstream_error | yes |
| upstream_stalled | SSE only | upstream_error | yes (`retry_of`) |
| client_too_slow | SSE only | upstream_error | no |

The status-bearing codes may also appear as an SSE `error` event once a stream has started; the event carries the same `code` and `retryable` values.

**IMPL-CON-02 (gap #31): `profile` is optional.**

Decision. Remove `profile` from `DecisionCreateParams.required`, and make it optional (`profile?`) in types.ts. Resolution order:
1. the request's `profile`;
2. `tenants.<t>.defaults.decision_profile`;
3. otherwise 400 `schema_violation`, `param: "profile"` ("no default decision profile configured").

The default profile is always selectable. An explicit profile must equal the default or be in `request_overrides.decision_profiles`; otherwise 403 `profile_not_allowed`. The resolved `id@version` is echoed in `provenance` and in the Jev `model` field.

The facade keeps `model` required (OpenAI shape), so the tenant default never applies to it. 08:33 is reworded to match.

The idempotency fingerprint hashes the body as sent, so an omitted profile and the same profile sent explicitly are different fingerprints.

Rationale. 03:221 and 08:33 already document this default, and it is D6 (`lightweight-model-decide` = gpt-6-luna). Only the schema contradicted them. New example `decision-request.valid-default-profile.json`.

Refs: dschema:272-280, types:93-95, 03:221, 08:33, yaml:12.

Patch needed: contract + doc. Material: yes. It implements D6 as documented; D6 itself is unchanged.

**IMPL-CON-03 (gap #32): error responses per operation.**

Decision.
- Every authenticated operation declares 401 and 500.
- Every rate-limited operation declares 429. Phase 1 applies the per-key RPM bucket to every authenticated operation; this depends on ops-admission.
- Every operation that accepts `x-aghoo-end-user` declares 422, for `feature_not_available` on that header.
- Each operation carries `x-noema-error-codes`, the exact list of codes it may return. The set of statuses implied by those codes equals the operation's declared 4xx/5xx statuses; I checked this for all 23 authenticated operations.
- Shared responses `BadRequest`, `Unauthorized` (with `www-authenticate`), `Forbidden`, `NotFound`, `Conflict`, `Gone`, `PayloadTooLarge`, `UnsupportedMediaType`, `Unprocessable`, `RateLimited`, `InternalError`, `NotImplemented`, `Unavailable` and `GatewayTimeout` replace the old generic `Error` response.

Specific cases:

| Operation | Status | Case |
|---|---|---|
| createTurn | 410 | An Idempotency-Key replay of a streamed turn whose event log has expired (`details.turn_id`; read it with getTurn). Depends on gap 21 |
| createTurn | 504 | `stream: false` past the deadline |
| createTurn | 413 / 415 | Body cap, inline image, Content-Type |
| createDecision | 404 | Session or file ids |
| createDecision | 409 | Idempotency, `file_not_ready`, `file_rejected` |
| createDecision | 504 | Only when `Aghoo-Deadline-Ms` expires before any member call started; after that the response is 200 with `unavailable` answers |
| facade | 403, 409, 413, 415, 422, 503, 504 | Hierarchical choice question → 422 `unsupported_parameter`; every answer unavailable → 503 `all_routes_unavailable` |
| cancelTurn | 409 | `idempotency_key_reused` |

Rationale. Undeclared statuses make the test-mode response validator and schemathesis either useless or noisy. A per-operation code list lets one test catch a wrong code on the wrong operation.

Refs: openapi:51-473, 03:47-60, 03:297, 03:313.

Patch needed: contract. The full matrix is in 08-contract-patches §3.

**IMPL-CON-04 (gap #33): headers.**

Decision. Declared once in `components` and referenced from every operation.

Request headers:

| Header | Applies to | Behavior |
|---|---|---|
| `x-request-id` | all | Optional. A value of 1–128 chars from `[A-Za-z0-9._:-]` is echoed. Any other value is replaced by a server `req_<ULID>`, never rejected |
| `traceparent`, `tracestate` | all | W3C context; the server span joins the trace. An invalid value starts a new trace, never rejected |
| `x-aghoo-end-user` | data-plane and W4 operations | A JWT verified against a per-project static JWKS, issuer and audience from config, with no network fetch. Invalid, expired or wrong-audience → 401 `invalid_end_user_token`. A project without end-user config → 422 `feature_not_available`. Security-architect dependency; see open questions |
| `Aghoo-Deadline-Ms` | `createTurn`, `createDecision`, the facade (newly added) | Default 120000 |
| `Aghoo-Version` | all | `enum: ['2026-10-01']`, default `2026-10-01` (current, default and only supported). Any other value → 400 `schema_violation`, `param: "Aghoo-Version"`. Accepting it on GETs is harmless and keeps the middleware uniform |
| `Idempotency-Key` | POST | `minLength 1`, `maxLength 128`, printable ASCII |

Response headers:

| Header | Sent on | Behavior |
|---|---|---|
| `x-request-id` | every response, including errors | `required: true`; equals `error.request_id` |
| `x-ratelimit-{limit,remaining,reset}-{requests,tokens}` | 2xx of the three model-calling operations, and on every 429 | Integers. Each value comes from the most constraining bucket: key, end user, project or the tenant's model quota. `reset` is whole seconds, rounded up |
| `retry-after` | 429, and 503 when known | Integer seconds ≥ 1. Absent for `budget_exhausted` and `tenant_access_misconfigured` |
| `x-aghoo-idempotent-replay: true` | 2xx of a POST answered from an earlier request with the same key: a stored response, or attached to one in flight | Absent otherwise. Error responses are not stored, so a retry re-executes them; this keeps 409 `session_busy` retryable. Dependency: ops-ledger IdempotencyStore |

Rationale. These headers are documented in 03 §1 but were undeclared. Keeping one value per header avoids per-operation drift.

Refs: 03:29-41, 03:62, 04:201, 07:440, 01:366.

Patch needed: contract + doc (03 §1, 07 §10).

**IMPL-CON-05 (gap #34): SSE contracts.**

Decision.
- Each `TurnEvent` variant becomes a named component schema with `additionalProperties: false` (`TurnCreatedEvent` … `ErrorEvent`). `TurnEvent` is a `oneOf` of `$ref`s plus `discriminator: {propertyName: type, mapping: {...}}`. The generators emit a Pydantic discriminated union and a TS union of named types.
- types.ts adds `ErrorCode`, `ErrorType` and `ErrorEnvelope`. The SSE `error` variant's `code` becomes `OpenEnum<ErrorCode>`: response enums stay open per 03 §1, so a later code doesn't break clients.
- Framing, stated in the `TurnEvent` description:
  - each frame is `id: <n>`, then `event: <type>`, then one line of `data: <JSON>`, then a blank line;
  - `id` is strictly increasing per turn;
  - there is no `retry:` field;
  - a `: ping` comment is sent whenever no frame was written for 15 s.
- File events (watchFile):
  - each frame is `id: <n>` (1, 2, … per stream), `event: file.updated`, and `data` = a `FileObject` snapshot;
  - the first frame is the current snapshot;
  - the server closes the stream after the first snapshot with status `ready`, `partial`, `rejected`, `failed` or `deleted`;
  - the same 15 s ping rule applies.

Rationale. Discriminated unions make the generated code typed per event, and the validator can check that `event == data.type`. An idle-based ping is the simplest rule that satisfies "every 15 s" without extra frames during busy streams.

Refs: openapi:767-872, types:528-544, 03:139-141, 03:403, 07:333.

Patch needed: contract.

**IMPL-CON-06 (gap #35): SDK strategy.**

Decision.

*TypeScript* (`sdk/typescript`, package `@aghoo/sdk`):
- Wire types `src/generated/openapi.d.ts` are generated by `openapi-typescript@7.13.0 build/openapi.bundled.yaml --default-non-nullable false`.
- The client is hand-written against `contracts/sdk.ts`.
- `contracts/types.ts` and `sdk.ts` are vendored unchanged as the public types.
- The compile-time test `test/wire-compat.check.ts` (§C.3, verified 18/18) asserts:
  - generated response types are assignable to the public types;
  - public request params are assignable to the generated request types.
  - It exempts only `DecisionResult.answers`, because openapi-typescript drops allOf refinements. Answers are covered by the runtime client-model check (IMPL-CON-10) and `sdk-usage.check.ts`.

The `sdk.ts` changes:
- `q.choice(name, {instructions, choices: readonly ChoiceOption[]})` replaces the `Record` form. An array keeps declared order even for integer-like values (JS reorders object keys like `"10"` and `"2"`), and lets each option carry a `group`. A `@ts-expect-error` asserts the Record form is gone.
- New methods:
  - `sessions.list` (newest first) and `sessions.update(id, SessionUpdate)`;
  - `sessions.turns` returning `SessionTurn`;
  - `files.derivative(id, kind, {page?}) → Blob`;
  - `usage.list(...) → AsyncIterable<UsageRow>`;
  - `admin.calibrators.list`, `admin.labeling.runs` and `admin.labeling.dropped` (read-only W4, used by the UI).
- `AghooError` is thrown on non-2xx responses after retries. SSE `error` events are yielded, not thrown.
- `ProfileInfo.version` becomes a string.
- Retries: only before the first stream event, on retryable codes and connect errors, honoring `retry-after`; full jitter, base 0.5 s, cap 8 s, `maxRetries` 2.
- An automatic `Idempotency-Key` (UUIDv4) is reused across the retries of one call.
- `lastEventId` calls resume, which answers 501 in Phase 1.

*Python* (`sdk/python`, package `aghoo`, sync `Aghoo` only; 03 §9.2 shows no async client, and one would be speculative):
- A hand-written httpx client.
- Response models: `python_sdk_types.py` vendored as `aghoo/_decision_types.py` (tolerant: `extra=ignore`, open enums, `UnknownAnswer`), plus hand-written tolerant models in the same style for:
  - `TurnEvent` (discriminated union with an `UnknownEvent(raw_type, raw)` fallback);
  - `TurnObject`, `Session`, `SessionTurn`, `FileObject`, `ProfileInfo`, `UsageRow`.
- Request params are `TypedDict`s (no client-side validation; the server is authoritative). A unit test validates the params the SDK builds against the bundled schema with `jsonschema`.
- `DecisionSet` / `choice(...)` per 03 §9.2. `choice` accepts a `Mapping[str, str | None]` (Python dicts keep order) or a `Sequence[Option(value, description=None, group=None)]` when groups are needed.

Bundling. `npx @redocly/cli@2.62.1 bundle contracts/openapi.yaml -o build/openapi.bundled.yaml` resolves the external `$ref`s into `components/schemas`, keeping the `$defs` names. The only name collision (`Description` in both JSON Schemas) is removed by renaming the facade's `$defs.Description` to `OADescription`; this is not a wire change. `check_docs.sh` now fails on any bundle warning (IMPL-CON-18).

Rationale. Codegen can't produce `sdk.ts`'s literal-type builders. Generating the wire types and checking them against the hand-written surface gives drift detection without giving up the typed-by-name API.

Refs: 05:72, sdk:36-39, 03:448-506, pyt:1-31.

Patch needed: contract (sdk.ts, sdk-usage.check.ts, openai-compat rename) + doc (03 §9.1, 07 examples, 05:72).

**IMPL-CON-07 (gap #36): out-of-phase values.**

Decision. A request value, header or operation that the design assigns to a later phase gets a typed error, never a silent fallback. `param` always names the field or header.

| Input | Phase-1 answer | Why |
|---|---|---|
| `strategy: "speculative"` / `"hedge"` (turns) | 422 `feature_not_available`, `param: strategy` | Phase 3 (05:89, 05:117) |
| `generation.profile` = a profile with mode `best_of_n` or `fuse` | 422 `feature_not_available`, `param: generation.profile`. Such profiles are omitted from `GET /v1/profiles`. Config activation rejects bindings or intent routes that target them (config owner) | Phase 2 / [X] (05:77, 05:86) |
| A choice question whose option count exceeds the smallest `max_choices` among the answering profile's members (native decisions, turn app questions), i.e. the hierarchical method | 422 `feature_not_available`, `param: questions[i].choices`. On the facade and with `output_format: "jev"` it stays 422 `unsupported_parameter`, permanently (03:297, 03:375). An escalation stage that would need the hierarchical method doesn't run for that question; the stage-1 answer stands (decision owner) | The hierarchical method is [X] and not implemented in Phase 1 (PLAN; gap 27) |
| `retention: "custom"` (create/update session) | 422 `feature_not_available`, `param: retention` | Retention policies are Phase 2 (03 §8, W8) |
| `x-aghoo-end-user` on a project without end-user config | 422 `feature_not_available`, `param: x-aghoo-end-user` | IMPL-CON-04 |
| `GET /v1/turns/{id}/events` | 501 `not_implemented` on every request | Resume is Phase 2 (03:74, 05:72). I assume gap 21's owner keeps this; the contract needs no other Phase-1 behavior |
| DOCX / SVG / audio / video upload | 415 `unsupported_media_type` (existing code) | 03:411-414 |
| A non-API-key bearer token (OIDC) | 401 `invalid_api_key` | OIDC/BFF is Phase 2 (05:91) |

Unreachable in Phase 1, while the enums stay in the contract:
- `budget_exhausted`;
- `generation.rerouted` (speculative only);
- `generation.committed.reason` `speculative` / `selected`;
- `generation.cancelled.reason` `not_selected`;
- `RoutingDecision.mode` `best_of_n` / `fuse`;
- `selection: expected_utility`.

Tests assert that they never occur.

Rationale. A typed 422 keeps the request contract stable across phases: the schema accepts the value, and the server says it isn't served yet.

Refs: 05:68-91, 03:122, 03:220, PLAN "Wave A" bullet list.

Patch needed: contract (descriptions) + doc (07 strategy row and FAQ, 03 §2). Material: yes. **It touches R5.** The 2–255 schema limit and the hierarchical design are unchanged, but in Phase 1 a request above the profile's flat limit (16 for `router-default`, 26 for `lightweight-model-decide`) gets 422 instead of a hierarchical answer. Owner question 1.

**IMPL-CON-08 (gap #37): type fixes.**

Decision.
- `ProfileInfo.version`: `{type: string, pattern: '^[0-9]+$'}` in openapi and sdk.ts. The config keeps an int, and the server emits `str(version)`.
- Inline `image_url`:
  - drop `maxLength: 7000000` from dschema;
  - the server base64-decodes and answers 413 `image_too_large` above 3,750,000 decoded bytes. I read "3.75 MB" as decimal MB, which is conservative against Bedrock;
  - the same 413 applies above 8,000 px on either side, checked by the media pipeline before generation (media owner decides how);
  - the JSON body cap (IMPL-CON-15) bounds the string;
  - removing `maxLength` gives every oversize image the same documented 413, instead of 400 above 7 M chars and 413 below.
- `x_aghoo` is typed in oaschema with `additionalProperties: false`:
  - required: `decision_id` (`^dec_`), `calibration` (map → `calibrated|uncalibrated`);
  - optional: `mapped_unavailable` (map → `deadline_exceeded|no_quorum|all_members_failed|no_capable_member`);
  - keys are effective question names (the given name, or `q{i}`).
- The facade rules JSON Schema can't express are enforced by importing `check_examples.oa_request_invariants`, which now also enforces at most 20 `input_image` parts. Each violation maps to 400 `schema_violation` with `param` from `Violation.param`.
- `oa_invariants` also checks `x_aghoo`: `calibration` keys are exactly the non-refusal answers, and `mapped_unavailable` keys name refusals.

New examples:
- `openai-request.invalid-21-images.json`;
- `openai-response.valid-ensemble.json`;
- `openai-response.invalid-x-aghoo-mapped-not-refusal.json`.

Rationale. One string form for profile versions everywhere; documented codes for size limits; facade rules enforced by the same code the docs are checked with.

Refs: openapi:942, dschema:204-208, oaschema:82, ts openai-compat:59-64, 03:295-313.

Patch needed: contract.

**IMPL-CON-09 (gap #40): thin schemas.**

Decision.
- `PATCH /v1/sessions/{id}` takes a new `SessionUpdate`:
  - `{metadata?, retention?, data_class?}`, with `additionalProperties: false` and `minProperties: 1`;
  - present fields replace the stored value; `metadata` replaces the whole map, and `{}` clears it;
  - a new `data_class` applies to turns created afterwards; stored content is never re-processed. The change is audited (identity owner).
- `GET /v1/sessions/{id}/turns` returns `SessionTurnList` with items `SessionTurn{turn_id, seq, role, text, created_at, redactions[{start, end, reason}]}`:
  - committed turns only, oldest first;
  - each turn yields its user message, then its assistant message;
  - offsets are Unicode code points of `text`, `end` exclusive;
  - redacted spans read `[redacted]`.
- `listSessions` returns `SessionList`, newest first.
- `GET /v1/usage`:
  - adds `limit` / `after`; `group_by` defaults to `line`;
  - returns `UsageReport{group_by, from, to, data[UsageRow{key, line, usage}], has_more, next_cursor}`;
  - `key` = project id | session id | Bedrock model id | UTC day `YYYY-MM-DD` | line name, or `null` when the dimension doesn't apply (e.g. a decision without a session);
  - one row per (key, line), ordered by key then line;
  - `to` defaults to today (UTC) and `from` to `to` − 29 days; more than 366 days → 400;
  - visibility is the caller's project; `tenant_admin` sees the whole tenant.
- The `cache_savings` row's `usage.cost_usd` is the avoided cost, not spend (ops-ledger).

Rationale. These are the smallest schemas that W1, W2 and W7 need, and the SDK can page them generically.

Refs: openapi:280-345, 436-472, 03:393, 07:446-449.

Patch needed: contract.

**IMPL-CON-10 (gap #41): server-side models and validation.**

Decision.
1. **Request gate = JSON Schema, with `jsonschema-rs`.**
   - Validators are compiled per operation at startup from `build/openapi.bundled.yaml`, with `validate_formats=True`.
   - It is the only accept/reject authority for bodies and parameters (§B.2).
   - Evidence: 0 disagreements with the reference `jsonschema` 4.23 over 48 examples plus mutations. A request costs 0.003 ms and a `DecisionResult` 0.035 ms, against 0.82 ms and 20.4 ms for `jsonschema`.
2. **Typed access = strict generated Pydantic v2 models**, built after the gate: `datamodel-code-generator==0.83.0` with the exact flags in §B.3 and `StrictBase` (`extra=forbid`, `strict=True`, `regex_engine="python-re"` for the `^(?!q[0-9]+$)…` look-ahead, `protected_namespaces=()`).
   - Requires `pydantic>=2.12,<3` (`--use-type-alias` emits PEP 695 aliases; tested on 2.14.0).
   - The generated models are known to be looser than the schema in three places: explicit `null` on optional fields, `uniqueItems`, and `if/then` (calibrated ⇒ holdout). This is harmless because the gate runs first. It is why the models are never the gate.
   - Generated code is committed under `src/noema/contracts/_generated/`. `make contracts-check` regenerates it and diffs.
3. **Semantic request rules**: `check_examples.request_invariants` and `oa_request_invariants`, imported from the vendored contracts. They return `Violation` (a `str` with `.param`) → 400 `schema_violation`.
4. **Test-mode response validator** (`NOEMA_CONTRACT_VALIDATE=1`, set by the root conftest, refused in prod). It checks:
   - every response body and SSE `data` against the bundled schema for (operation, status, media type);
   - error status/type/retryable against `ERROR_TABLE`, and code ∈ the operation's `x-noema-error-codes`;
   - `x-request-id` present and equal to `error.request_id`;
   - SSE: `event == data.type` (`file.updated` for files), strictly increasing ids, `turn.completed` last on a normal close;
   - decision bodies against `result_invariants` (paired with the request), `jev_invariants` or `oa_invariants`;
   - the tolerant client models (`python_sdk_types.DecisionResult` / `JevDecisionResponse`) parse every decision body. This is the "server strict vs client tolerant" compatibility check.
5. **`python_sdk_types.py` stays client-only.** The server never imports it, except the test validator above.
6. **`check_examples.py` is importable.**
   - The CLI moves into `main()` under `if __name__ == "__main__"`.
   - `Violation`, `OA_MAX_IMAGES = 20` and the x_aghoo invariant are added.
   - `qa/run_design_audit.py` loads it with `runpy.run_path` instead of splitting the source at `self_tests()`. The audit is still 41/41, and `check_docs.sh` is green.

Rationale. The JSON Schema stays the single source of truth with exact semantics; Pydantic gives mypy-strict typed objects to module owners; and both client-tolerance and server-strictness are tested on every response.

Refs: pyt:1-31, chk:479-489, audit:33-44, 03:276, 05:146.

Patch needed: contract (chk, audit). Dependency: gap 39's owner decides how `BANDS` / `ACCEPT` / `TEMPLATE_MAX` reach `result_invariants` from the active config.

**IMPL-CON-11 (gap #42): read-only W4 endpoints.**

Decision. Phase 1 adds three read-only operations (tag `admin`; paginated with `limit`/`after`; newest first). Roles are machine-readable in `x-noema-roles`; every other role gets 403 `permission_denied`.

| Operation | Roles | Returns | Filters |
|---|---|---|---|
| `GET /v1/admin/calibrators` (listCalibrators) | tenant_admin, reviewer, auditor | `CalibratorInfo` (fields below) | `profile_id`, `question_name`, `status` |
| `GET /v1/admin/labeling/runs` (listLabelingRuns) | tenant_admin, reviewer, auditor | `LabelingRun` (fields below) | `profile_id`, `question_name`, `status` |
| `GET /v1/admin/labeling/dropped` (listLabelingDropped) | tenant_admin, reviewer (it shows content) | `LabelingDroppedItem` (fields below) | `run_id`, `question_name`, `reason` |

Response fields:
- `CalibratorInfo`:
  - `scope` platform|tenant, `level` pooled|member, `status` rejected|accepted|active|retired;
  - `method`, `parameters`, `binding` = dschema `CalibrationBinding` (now its own `$def`), `member_subset`;
  - `holdout` = `HoldoutMetrics` | null, `acceptance{passed, failed_rules}`;
  - `reliability{bins[], ci{ece, brier, nll}}`;
  - activation and retirement fields.
- `LabelingRun`:
  - `labelers` (≥ 2), `rule: unanimous`;
  - `counts{sampled, sampled_uniform, sampled_uncertainty, kept, dropped_*}`;
  - `consensus_rate`, `consensus_rate_by_class`, `n_by_class`;
  - `usage`.
- `LabelingDroppedItem`:
  - `reason`, `sample_reason`;
  - `labeler_answers[≥ 2]{model, status, value}`;
  - `ensemble{value, probability, band, calibration_status}`;
  - `content{excerpt ≤ 2000 chars after redaction | null, redacted}`. The excerpt is null when the data class or log policy forbids stored content.

Platform-default calibrators are listed with `scope: platform`, metadata only. Activation stays with the `noema-admin` CLI; the write API is Phase 2. The calibrator lifecycle enum is my proposal and may be refined by the labeling owner before the freeze.

Rationale. W4 is a Phase-1 deliverable (05:82). These three views cover the 03 W4 row (consensus per class, dropped items with both answers, reliability, ECE/Brier/NLL with CIs, coverage via `holdout.scope`) without opening the Phase-2 admin API.

Refs: 05:33 vs 05:82, 03:441, 03:517, 06:51-60, 04:200.

Patch needed: contract + doc (03 §2 catalog). The TS SDK gains `admin.*` (IMPL-CON-06).

**IMPL-CON-12 (new): facade remote image URL.**

Decision. 400 `schema_violation`. oaschema `image_url` gets the native data-URL pattern, and `param` points at the image. 03:300 changes from `unsupported_parameter` to `schema_violation`.

Rationale. 03:300 says 400 `unsupported_parameter`, while 03 §1 maps `unsupported_parameter` to 422; the 06:221 security gate requires 400. Keeping 1 code → 1 status (IMPL-CON-01) leaves `schema_violation` as the only consistent choice. New example `openai-request.invalid-remote-image-url.json`.

Patch needed: contract + doc.

**IMPL-CON-13 (new): Phase-2 upload types.**

Decision. HTTP 415 with code `unsupported_media_type` (03:411-414, 07 §6). 02:764's DOCX "415 `not_allowlisted`" is reworded. `not_allowlisted` stays a `FileObject.rejection.code`, for asynchronous rejections after sniffing.

Patch needed: doc (02).

**IMPL-CON-14 (new): closed response schemas and named components.**

Decision.
- Every object schema in openapi.yaml gets `additionalProperties: false`: Error, events, `TurnObject` and its attempts, `RoutingDecision`, `Degradation`, `TurnTimings`, `Session`, `FileObject`, `ProfileInfo`, list wrappers, admin schemas. The JSON Schemas are already closed.
- `Error.error` requires `param` (nullable) and `request_id`.
- Lists get named schemas (`SessionList`, `SessionTurnList`, `UsageReport`, `CalibratorList`, `LabelingRunList`, `LabelingDroppedList`).
- In dschema, `PredicateAnswer`, `ChoiceAnswer` and `ScoreAnswer` now list `type` in their own `required`, and the JSON-state input branch says `additionalProperties: true`. Both already followed from the schema, so validation is unchanged (verified on all examples); the change is needed for correct codegen.

Rationale. The contract describes exactly what the current server sends; clients stay tolerant by implementation (03 §1). Without closed schemas, the test validator can't catch stray fields.

Patch needed: contract.

**IMPL-CON-15 (new): transport rules.**

Decision.
- JSON bodies over 50 MiB → 413 `request_too_large`. Proposed config `limits.max_json_body_bytes: 52428800`, equal to the largest upload cap; gap 38's owner adds it.
- Multipart uploads are capped per type while streaming (413 `file_too_large`).
- A JSON operation whose Content-Type isn't `application/json` (charset optional), or an upload that isn't `multipart/form-data` → 415 `unsupported_media_type`.
- An unknown route or method → 404 `not_found`; the Starlette 405 is mapped to 404, as in OpenAI's "Invalid URL" style.
- An unknown query parameter → 400 `schema_violation`, `param` = its name. Unknown headers are ignored.
- Query and header values are coerced per their schema type before validation: `integer` ← `^-?[0-9]+$`, `boolean` ← `true|false`; anything else → 400.

Rationale. A body cap is a hard security control the design lacked, and the rest keeps every error typed. Owner question 2 covers the cap value.

Patch needed: contract (new code; responses).

**IMPL-CON-16 (new): derivative download.**

Decision. `GET /v1/files/{id}/derivatives/{kind}` content types per kind:

| Kind | Content type |
|---|---|
| `markdown` | text/markdown |
| `text`, `ocr_text` | text/plain |
| `raster`, `thumbnail`, `page_images` | image/png or image/jpeg, as re-encoded |
| `pdf_sanitized` | application/pdf |

- `?page=` (1-based) is required for `page_images` and rejected for other kinds (400).
- `FileObject.derivatives[].pages` gives the page-image count.
- Every download has `Content-Disposition: attachment; filename="<file_id>-<kind>[-<page>].<ext>"` (never the uploaded filename) and `X-Content-Type-Options: nosniff`.
- A kind the file lacks → 404 `not_found`; a pending derivative → 409 `file_not_ready`; a rejected file → 409 `file_rejected`.

Rationale. W2 previews need page images, and `page_images` is a set. `application/octet-stream` for everything defeats the response validator.

Patch needed: contract. Dependency: media-pipeline's storage layout per page.

**IMPL-CON-17 (new): machine-readable extensions.**

Decision. `x-noema-error-codes` on every operation, and `x-noema-roles` on the W4 operations. The conformance suite (`tests/contract_internal/test_openapi_contract.py`) asserts:
- `set(ErrorCode)` equals the openapi enum, which equals the `ERROR_TABLE` keys;
- the statuses implied by each operation's codes equal its declared 4xx/5xx statuses;
- every route registered in the app is in openapi and vice versa;
- `x-noema-roles` matches the authz policy.

The security architect may extend `x-noema-roles` to every operation to drive the authz matrix generator.

Patch needed: contract.

**IMPL-CON-18 (new): docs stay green and consistent.**

Decision.
- `check_docs.sh` gains a bundle step: `redocly bundle` must print no warning.
- Prose is synced:

| File | Change |
|---|---|
| 03 | §0 counts, §1 table and headers, §2 catalog, §4.1 profile row, §4.2 remote URL, §9.1 example and resume note |
| 07 | `q.choice` examples, strategy row, resume note, errors table, pinning, FAQ |
| 05:72 | SDK wording |
| 02:764 | DOCX 415 code |
| 08:33, 08 §13 | Default profile wording, verification list |
| 01:52, README verify comment | Counts and verify comment |

- Examples go from 43 to 48 (13 valid, 35 invalid), all generated by `make_examples.py`.

Patch needed: contract + doc. The exact unified diff is in 08-contract-patches Appendix B.

---

## B. Frozen server code fixed by these decisions (Wave B writes it)

### B.1 `src/noema/core/errors.py`

```python
class ErrorType(StrEnum):
    INVALID_REQUEST = "invalid_request_error"; AUTHENTICATION = "authentication_error"; PERMISSION = "permission_error"
    NOT_FOUND = "not_found_error"; CONFLICT = "conflict_error"; CAPABILITY = "capability_error"
    RATE_LIMIT = "rate_limit_error"; UPSTREAM = "upstream_error"; SERVER = "server_error"

class ErrorCode(StrEnum):            # values == openapi components.schemas.ErrorCode.enum (test-enforced), 36 members
    SCHEMA_VIOLATION = "schema_violation"  # ... one member per row of the IMPL-CON-01 table

@dataclass(frozen=True, slots=True)
class ErrorSpec:
    status: int | None               # None: SSE `error` event only (upstream_stalled, client_too_slow)
    type: ErrorType
    retryable: bool

ERROR_TABLE: Final[Mapping[ErrorCode, ErrorSpec]]   # exactly the IMPL-CON-01 table

class NoemaError(Exception):
    def __init__(self, code: ErrorCode, message: str, *, param: str | None = None,
                 details: Mapping[str, JsonValue] | None = None, retry_after_s: int | None = None) -> None: ...
    @property
    def spec(self) -> ErrorSpec: ...
    def envelope(self, request_id: str) -> dict[str, JsonValue]: ...  # {"error": {type, code, message, param, request_id, retryable[, details]}}
    def sse_event(self) -> dict[str, JsonValue]: ...                    # {"type": "error", code, message, retryable}
```
The error middleware maps an uncaught exception to `internal_error`. The message is generic and never contains exception text, which could hold secrets.

### B.2 `src/noema/contracts/` (generated or vendored; frozen)

```
contracts/_vendor/            byte copy of docs/noema/contracts (CI: identical to the canonical tree)
contracts/build/openapi.bundled.yaml   redocly bundle output (committed; CI re-bundles and diffs)
contracts/_base.py            StrictBase
contracts/_generated/models.py  datamodel-codegen output (committed; CI regenerates and diffs)
contracts/gate.py             RequestGate (jsonschema-rs)
contracts/rules.py            wraps _vendor/check_examples.{request_invariants, oa_request_invariants} -> NoemaError
contracts/validate.py         ContractValidator (tests only)
```

```python
class StrictBase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, regex_engine="python-re", protected_namespaces=())

class RequestGate:
    @classmethod
    def from_bundle(cls, bundle: Path) -> "RequestGate": ...
    def check_params(self, operation_id: str, *, path: Mapping[str, str], query: Mapping[str, list[str]],
                     headers: Mapping[str, str]) -> dict[str, JsonValue]: ...   # coerced; unknown query -> 400
    def check_body(self, operation_id: str, content_type: str | None, body: bytes) -> JsonValue: ...
        # 413 request_too_large -> 415 unsupported_media_type -> orjson decode (400) -> jsonschema-rs (400, param = first error path)

def param_path(instance_path: Sequence[str | int]) -> str: ...   # ["questions", 1, "choices"] -> "questions[1].choices"

class ContractValidator:                       # enabled only when NOEMA_CONTRACT_VALIDATE=1 (refuses to start otherwise in prod)
    def check_response(self, operation_id: str, status: int, headers: Mapping[str, str], body: bytes,
                       request_json: JsonValue | None) -> None: ...      # raises ContractViolation
    def check_sse_frame(self, operation_id: str, frame_id: str, event: str, data: bytes) -> None: ...
```

### B.3 Pinned generators (verified 2026-10-09)

```bash
npx -y @redocly/cli@2.62.1 bundle docs/noema/contracts/openapi.yaml -o src/noema/contracts/build/openapi.bundled.yaml
datamodel-codegen --input src/noema/contracts/build/openapi.bundled.yaml --input-file-type openapi --openapi-scopes schemas \
  --output src/noema/contracts/_generated/models.py --output-model-type pydantic_v2.BaseModel \
  --base-class noema.contracts._base.StrictBase --target-python-version 3.12 --use-annotated --field-constraints \
  --enum-field-as-literal all --extra-fields forbid --strict-types str int float bool --use-standard-collections \
  --use-union-operator --use-type-alias --use-schema-description --disable-timestamp --use-double-quotes --formatters builtin
npx -y openapi-typescript@7.13.0 src/noema/contracts/build/openapi.bundled.yaml -o sdk/typescript/src/generated/openapi.d.ts \
  --default-non-nullable false
```

Dependencies for B1 to pre-add:
- Python: `jsonschema-rs`, `jsonschema>=4.23` (reference), `hypothesis-jsonschema`, `datamodel-code-generator==0.83.0` (dev), `pydantic>=2.12,<3`, `orjson`;
- npm devDependencies: `@redocly/cli@2.62.1`, `openapi-typescript@7.13.0`, `typescript@5`.

Note for the orchestrator: `check_docs.sh` uses `@redocly/cli@latest`, which is 2.62.1 today; the repo pins it.

## C. Tests these decisions require (owners: foundation B2 / qa)

1. `tests/contract_internal/test_openapi_contract.py`: the IMPL-CON-17 assertions.
2. `tests/contract_internal/test_validator_differential.py`: python-jsonschema vs jsonschema-rs on every example plus 200 `hypothesis-jsonschema` samples per request schema. Every schema-valid sample must also parse with the generated models (generated ⊇ schema).
3. `sdk/typescript/test/wire-compat.check.ts`: the 18 assertions from 08-contract-patches §10.3 (TurnEvent, TurnObject, DecisionResult minus answers, Jev, Session, FileObject, ProfileInfo, SessionTurn, UsageRow, the three W4 types, ErrorEnvelope, OADecisionResponse, and four request types).
4. `tests/unit/api/test_errors.py`: `ERROR_TABLE` matches the IMPL-CON-01 table, the envelope shape, and that 401 never echoes the key.
5. Schemathesis at every wave gate, with the response validator on.

## D. Dependencies on other roles

| Role | Dependency |
|---|---|
| behavior / architect (gap 21) | resumeTurnEvents → 501 in Phase 1; the createTurn 410 replay rule after event-log expiry |
| behavior (gaps 11, 12, 1, 2, 5) | Any change to `attempts[].outcome`, event order, `arrived: after_completion`, decision `status` or `EscalationStage.triggered_by` patches the same files (openapi.yaml TurnObject/TurnEvent, types.ts). Apply this contract patch first, then rebase |
| security-architect (gaps 8, 9, 44) | `x-aghoo-end-user` verification (static per-project JWKS, issuer, audience) and role names in `x-noema-roles`; `permission_denied` for role/scope denial |
| config owner (gap 38) | `tenants.<t>.defaults.decision_profile`, `limits.max_json_body_bytes`, per-project end-user JWKS. Config activation rejects bindings/routes to `best_of_n`/`fuse` in Phase 1 |
| gap 39 owner | How `check_examples` constants (`BANDS`, `ACCEPT`, `TEMPLATE_MAX`) come from config when the server or tests call `result_invariants` |
| ops-ledger | IdempotencyStore: replay header on stored or in-flight responses; errors not stored; the `cache_savings` row semantics |
| ops-admission | The per-key RPM bucket on every authenticated operation (else drop 429/`rpm_exceeded` from non-model operations); rate-limit header values from the most constraining bucket |
| media-pipeline | Decoded inline-image size check at ingress, the 8,000 px check, per-page `page_images` storage, and `pages` |
| labeling | The calibrator lifecycle enum, run/dropped-item fields and RLS for platform-scope calibrators |
| decision | Escalation skips questions that would need the hierarchical method |

## E. Open questions for the owner

1. **R5 in Phase 1 (IMPL-CON-07).** The hierarchical method is not implemented in Phase 1 (PLAN: "rejected by config only, never implemented"). A choice question above the profile's flat limit (16 options for `router-default`, 26 for `lightweight-model-decide`) therefore gets 422 `feature_not_available` until the hierarchical method ships. The 2–255 contract limit is unchanged. Do you accept this Phase-1 behavior? The alternative is to build the hierarchical method in Phase 1.
2. **JSON body cap (IMPL-CON-15).** I propose 50 MiB (`limits.max_json_body_bytes`); the design had no cap. With 3.75 MB inline images (about 5 MB base64 each), a request carries at most about 10 maximum-size inline images. Facade clients wanting 20 images must send smaller ones or upload files. Do you accept the 50 MiB value?
3. **End-user delegation in Phase 1 (IMPL-CON-04).** `x-aghoo-end-user` would be verified against a static per-project JWKS from config, with no IdP fetch. If you want it deferred to Phase 2 with OIDC, the contract needs no change: every project then answers 422 `feature_not_available` for the header.
