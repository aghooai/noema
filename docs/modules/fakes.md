# tests/fakes, conformance suites, golden fixtures (owner: fakes-telemetry; frozen after Wave B)

Spec: spec-02 §14 (fakes and suites), spec-10 §2.4 (scenario DSL), spec-04 §4.2 (golden SSE). This page records the
conventions C-wave roles rely on, including the B4 fix round (2026-10-10).

## Using the fakes
- `tests.fakes.world.build_world(clock=FakeClock())` wires every fake on one clock, config and scenario; fields are
  named like `Services`. `world.runtime` is a real `TaskGroupRuntime` (open it with `await world.runtime.open()`
  and close it in the same task). `world.fake(name)` returns the fake of a `Services` field.
- Time: drive `FakeClock` with `advance()` / `autojump()`. Timeouts in real and fake code use
  `noema.core.clock.deadline_scope` (the fake-only helper is gone).
- `tests/fakes/protocol_checks.py` fails `make typecheck-tests` when a fake drifts from its Protocol.

## Semantics aligned with the real implementations (B4)
- `FakeIdempotencyRepo.take_over` is conditional (expired, not_stored or lapsed lease) and sets `resource_id`;
  `finish` only changes an in_flight record of its owner; bodies over 1 MiB are stored as `response_body_key`.
- `FakeAttemptRepo.list_for_turn` / `FakeDerivativeRepo.list_for_file` read through the turn/file row (project
  scope); `FakeDerivativeRepo.upsert` merges into an existing row and needs `id`, `status`, `extractor_version` for
  a new one.
- `FakeDecisionRun.finalize` puts a `decision_row` outbox record when the row insert fails (never raises); the
  `redacted` content policy stores the redacted input; `row_for(policy)`; `ctx.decision_id` is the run id.
- `FakeSystemCalls`: `set_calibrator_status` (system actors: active -> downgraded only), `bump_epoch`,
  `outbox_dead(attempts=)`. `FakeJobQueue.enqueue(max_attempts=)`. `FakeFileService.purge`, 409 `file_not_ready` /
  `file_rejected` from `open_derivative`, `text_layer_pages=(1,)` on page_images. `FakeS3Client` multipart uploads.
  `FakeAuthorizer`: `file.watch` / `file.derivative.read` exclude the auditor. `FakeRateLimiter` (GCRA on the clock).
- `FakeWorkGraphPlanner` applies the escalation question filter by name (`QuestionWork`).

## Conformance suites (`tests/contract_internal`)
- Register a real implementation from a `conformance_impls.py` in a path you own (see `registry.py`). New suites:
  `rate_limiter` (ops-admission), `process_runtime`. New optional kit hooks: `DecisionKit.db_available` /
  `outbox_records`, `MediaKit.pending_uploads` / `reject` (tests needing them skip when they are None).
- Real database kits that drive leases with a `FakeClock` age the stored lease/expiry columns
  (`tests/integration/db_repos/_conformance.py`, `AgingClock`).
- Identity: define `AssumeRoleResult` as a Protocol (read-only `creds: AwsCredentials`, `assumed_role_arn: str`) or
  with exactly those field names, so `tests.fakes.credentials.FakeStsClient` type-checks against `StsClient`.

## API tests
- `tests.unit.api.apifakes.make_app()` mounts test-only stub routers (`stub_routers()`): healthz 200, readyz 503,
  every other operation the typed 501. The middleware tests therefore never run the route modules the C-wave owners
  fill in; only `test_registered_routes_equal_the_openapi_operations` inspects `API_ROUTERS`.

## FakeClock: what "blocked" means (C2a)
`settle()` / `autojump()` move time only when every other task is blocked: each one waits on a pending future, the
loop's ready queue holds nothing but the settling tasks' own steps (no task wakeup, no done-callback such as an
anyio task group collecting a child, no anyio cancel-delivery retry), no real-time timer is due, and no
`anyio.to_thread` call is in flight on the default limiter (FastAPI's threadpool included; settle polls it every
1 ms of real time). Not visible: real socket I/O still on the wire and asyncio's own executor; suites with real I/O
mark it themselves (e.g. the ops-ledger harness's busy counter). It reads asyncio internals (`Task._fut_waiter`,
`loop._ready`, `loop._scheduled`), so it is asyncio-only.

## Additions and changed semantics (C2a)
| Fake | Change |
|---|---|
| `FakeSecretsProvider` | a missing secret or version raises `SecretMissing`; `fail(ref)` raises a plain `NoemaError(internal_error)` |
| `FakeCostMeter.price` | `*, snapshot=None` (prices with that snapshot's catalog) |
| `FakeRetryBudget` | `note_request` (no-op) |
| `FakeProfileRun` | `upstream_started` property (the transitional setter was removed in C2b) |
| `FakeDerivativeRepo` / `FakeLabelRepo` | `facts` column; `open_run(..., question_signature)`, `items_for_decision`, `kept_items` |
| `FakeSystemCalls` | `finish_job` returns the new state (None when the worker no longer holds the job); `retention_page` (keyset cursor, bytewise order) |
| `FakeDecisionRun` | under `hold_until_cancel`, the answers still open when S1 came by itself close (`timeout`) at `ctx.deadline`, so `updates()` always ends |
| `FakeInputViewResolver` | ruling R3: a budget-cut image keeps its non-empty `ocr_text`, else omitted; labels numbered per class (a well-formed unused given label is kept) |
| `FakeFileService` | every fake PDF has `PDF_PAGES` = 2 pages; `page_images.bytes` covers them all |
| `FakeAdmission` | `x-ratelimit-reset-*` in integer seconds (openapi) |
| `scenario.option_letters` | reads the `<app_question>{...}` block (a mention of the tag in the system text is skipped) |

## Additions (C2b)
- `FakeFileService`: a ready PDF's `pdf_sanitized.active_content` is `()`; `set_active_content(file_id, facts)` (after
  the file is ready) sets the removed facts (`DerivativeRef.active_content`).
- `FakeProfileRun` fills `AttemptRecord.first_text_at` (the attempt's own first text on the `started_at` clock).
- `FakeWorkGraphPlanner` imports `EFFORT_RESERVE` / `DECISION_PROMPT_OVERHEAD` from `noema.config.models`.
- `generation_engine` suite: the records carry `first_text_at` (None for an attempt without text).

## Changes (Wave E2, contracts-config-mock fixer; live-smoke fidelity)
- `scenario.RequestInfo.max_tokens` (default None = unknown): the fakes pass `ModelRequest.max_tokens`, the mock the
  wire `maxTokens`. A stream plan's reasoning tokens never exceed `max_tokens - text tokens`.
- `stream.reasoning.hidden_tokens` (schema and `StreamSpec.hidden_reasoning_tokens`): reasoning Bedrock hides on a
  non-streaming call (live Haiku 5.5). `completion()` counts it toward `max_tokens`: at or above it the answer has no
  text and stops `max_tokens` (the decision engine's `reasoning_only`), else the text follows (cut at the budget);
  usage adds it to `output_tokens` and reports `reasoning_tokens`; `Completion.hidden_reasoning` carries it.
- Probe defaults: the call rows (DeepSeek 300-560, Luna 350-610, Haiku 640-950 ms) and the low-effort stream rows
  follow the 2026-10-11 live cassettes; Opus, GLM and Sol plan reasoning deltas at every effort except `none` and
  `low` (before: Opus always, GLM except `none`/unset, Sol never). Only scenarios on `defaults.latency: probe` (the
  default) see this; every golden, e2e, fault and security scenario uses fixed latencies.
