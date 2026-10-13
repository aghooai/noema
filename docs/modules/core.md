# noema.core: frozen types and Protocols (owner: platform; frozen after Wave B)

Spec: spec-02 (every Protocol), spec-01 §2 (core imports nothing else from noema). This page lists the conventions
the C-wave roles build on, including the interface additions of the B4 fix round (2026-10-10).

## Time and deadlines (`clock.py`)
- Read time only through `Clock` (`services.clock`); tests drive `tests.fakes.clock.FakeClock`.
- Timeouts: `async with deadline_scope(clock, at) as scope:` or `async with deadline.scope() as scope:`. The scope
  is driven by the injected clock (it fires under `FakeClock`), has `move_on_after` semantics (check
  `scope.cancelled_caught`, then raise your own error, e.g. `deadline_exceeded`) and passes body exceptions through
  unwrapped. `Deadline.scope()` is an async context manager (spec-02 §1 said sync; it could not be Clock-driven).

## Process services (`protocols/services.py`, `protocols/runtime.py`, `runtime.py`, `settings.py`)
- `ServiceSet`: every `composition.Services` field, read-only, plus `runtime` and `settings`. Domain code types
  its dependencies with it (never imports `noema.composition`); `noema.api.deps.ApiServices` is the same type.
- Job handlers: each owner exports `def make_handlers(services: ServiceSet) -> JobHandlers` (exact kind ->
  `async (Job) -> None`) from its jobs module. The entrypoint merges them (`merge_handlers`, a kind claimed twice
  is an error) and expands `--kinds` globs (`expand_kinds`).
- `services.runtime` (`ProcessRuntime`, implemented by `TaskGroupRuntime`): `spawn(name, fn)` for work that
  outlives a request (failures are logged, never raised), `on_drain(name, cb)` for shutdown (`cb(grace_s)`, 30 s in
  the api), `draining` (readyz 503), `local(key, factory)` for process state such as `"turns.registry"` (no module
  globals).
- `services.settings` (`ProcessSettings`): `pod_id` (leases and owners: `<pod>:<request_id>`, `<pod>:<pid>`),
  `s3_bucket` (default `noema-<env>`), `redis_url`, `db_dsn_ref`, `drain_delay_s`, `metrics_port`, endpoint overrides.
- `objects.py`: `sse_args(env, tenant_id)` for every tenant object put (SSE-KMS `alias/noema-<env>-<tenant_id>` in
  staging/prod, AES256 in dev/test), `default_bucket(env)`.

## Additions by Protocol (B4)
| Protocol / type | Addition |
|---|---|
| `SessionRepo` | `delete(session_id)` (hard; turns first) |
| `IdempotencyRepo` | `take_over(..., resource_id=None)` (conditional); `finish` fenced by owner + in_flight; big bodies by key |
| `SystemCalls` | `outbox_dead(..., attempts=0)`, `set_calibrator_status(actor, id, status, *, reason)`, `bump_epoch(actor, scope_key)` |
| `JobQueue.enqueue` | `max_attempts=5` (media.process uses 2) |
| `Action` | `file.watch` (watchFile), `file.derivative.read` (getFileDerivative): the auditor may only `file.read` |
| `Authenticator` | documents 429 `rpm_exceeded` from the per-IP bucket `authfail:<ip>` (`RateLimiter`, new in `protocols/ops.py`) |
| `HealthRegistry`, `RetryBudget` | keyed by the Bedrock model id (`model_id`), never the config alias |
| `DecisionWork` | `questions: tuple[QuestionWork(name, k, source), ...]` replaces `question_ks` (Q_k name filter) |
| `Admission.reserve` | `deadline=` (stream-slot lease = deadline + 30 s); labeling builds its `WorkGraph` directly |
| `ModelRequest` | `snapshot=` (the caller's pinned config; adapters resolve the alias from it) |
| `DecisionContext` / `DecisionRun` | `decision_id=` (createDecision's pre-generated id); `row_for(content_policy)` |
| `DerivativeRef` / `ContentPart` | `text_layer_pages` (page_images); `page_texts` (pre-read by preflight for the planner) |
| `FileService` | `purge(tenant_id, file_id)` (S3 + rows; used by session.delete and retention); `open_derivative` 409s |
| `Degradation` | `to_wire()` (the openapi object; unset optionals omitted) |
| `SecretStr` | `SecretStr(value, register=True)` registers it for redaction while the object lives; never register client-presented credentials |

## Additions by Protocol (C2a, frozen change requests)
| Protocol / type | Addition |
|---|---|
| `errors.SecretMissing` | a `NoemaError(internal_error)` subclass: the secret or the requested version does not exist; an unreadable backend stays a plain `NoemaError(internal_error)` (`SecretsProvider.get`) |
| `ids.IdPrefix` | `"ldrop"` (a dropped label item; kept items stay `lab_`) |
| `ProfileRun` | `upstream_started` (sync property: an attempt received MessageStart, reasoning, text or a stop) |
| `PlannedPart` | `file_id` (`""` = not set), `text_chars` (kept length of a "page 1 of n, partial" cut) |
| `MediaNegotiator.negotiate` | blocks for media parts only; degradations carry `model` and `attempt_id=None` (the generation engine stamps it) |
| `RetryBudget` | `note_request(tenant_account, model_id)` (the ratio budget's base; the adapters call it per request) |
| `CostMeter.price` | `*, snapshot=None` (the caller's pinned catalog; decision, generation and labeling pass `ctx.snapshot`); `region` = the tenant's `home_region` |
| `DerivativeRepo.upsert` | column `facts` (pdf_sanitized only: `{"active_content": [...]}`) |
| `LabelRepo` | `open_run(..., question_signature)`; `items_for_decision(decision_id)`; `kept_items(profile, version, signature, since, sample_reason)` |
| `SystemCalls` | `finish_job` returns `FinishedJobState \| None`; `retention_page(limit, *, after)` with `RetentionCandidate` (4-tuple) supersedes `retention_candidates` |
| `SpanName` | `labeling.item`, `labeling.run`, `labeling.drift` |
| `RedisKeys` | `labeling_daily(tenant_id, day)` (the exact daily sampling cap, INCR + EXPIRE 2 days) |
| `ProcessSettings` | `sandbox_backend` (NOEMA_SANDBOX_BACKEND), `spool_dir` (NOEMA_SPOOL_DIR), `sandbox_socket` (NOEMA_SANDBOX_SOCKET); `SandboxBackend` |

## Additions by Protocol (C2b, residual closer)
| Protocol / type | Addition |
|---|---|
| `DerivativeRef` | `active_content: tuple[str, ...] \| None` (pdf_sanitized only, from `derivative.facts`; `()` = nothing removed); a non-empty value is the turn's `active_content_removed` degradation (spec-09 §6.3 step 5) |
| `DecisionContext` | `escalate_slack_ms: int \| None` (turns): with `escalate_until` set, the engine uses `min(t0 + stage-0 deadline_ms + slack_ms, deadline)` from its own t0 |
| `AttemptRecord` | `first_text_at: datetime \| None` (the attempt's own first text, on the clock of `started_at`; `timings.holdback_ms`) |

Implementation change without an interface change (C2b perf smoke, reports/perf/c2b-perf-smoke.md):
`text.clean_text` is one regex pass over `[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]` (general category Cc is exactly
U+0000-U+001F and U+007F-U+009F, an immutable Unicode property) instead of a per-character `unicodedata.category`
lookup: 30x faster on prompt-sized text, where it was ~10% of the api's turn CPU (`esc` on the history and on every
decision member's input). `tests/unit/core/test_text_clean.py` compares it with the definition on every code point.

Shared constants in `noema.config.models` (not core; C2b): `EFFORT_RESERVE` (spec-06 §5.5) and
`DECISION_PROMPT_OVERHEAD` (spec-11 §1.1), imported by decision, labeling, ops-admission and the fake planner.

Behavior change without an interface change (Wave E2, finding 6; spec-06 §5.2, spec-09 §5): datamarked text uses
U+02C6 for whitespace, so `escape_reserved_tags` now also treats U+02C6 as a gap character and ends a reserved tag
name at anything but an ASCII word character (`(?![0-9A-Za-z_])` instead of `\b`, which did not end the name before
`ˆ`, category Lm): `<ˆ/untrusted_input>` and `</untrusted_inputˆ>` are escaped like their whitespace forms. `datamark`
removes a literal U+02C6 of the untrusted text before marking, so every `ˆ` in its output is one whitespace run. The
SEC-INJ-01 vectors are unchanged (`tests/unit/core/test_text_datamark_escape.py`).
