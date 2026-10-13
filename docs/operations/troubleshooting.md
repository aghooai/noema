# Troubleshooting

Symptoms with their usual cause and fix. Alert-driven procedures are in [runbooks.md](runbooks.md); the client-side
error handling is in the design's API guide (`07-api-user-guide.md` §8). Every error response carries
`error.request_id`; the api's access line carries the same `request_id` and the `key_id`, so
`$C logs --no-log-prefix api-1 | grep <request_id>` finds the request on a compose stack (`$C` as in
[README.md](README.md); run on a stack while writing this page). `make e2e-logs` shows only the last 200 lines of
each service, which under load covers only a few seconds.

## 1. Startup and stack problems

| Symptom | Cause | Fix |
|---|---|---|
| `/readyz` 503 right after start; authenticated calls answer 500 `internal_error` (retryable); log `no active platform config yet (noema-admin config approve)` | no config version is active | propose and approve one ([configuration.md](configuration.md) section 3); pods heal within 5 s |
| `/readyz` 503 on a running pod | the pod is draining (SIGTERM received) | expected during a rollout; it exits within the stop grace |
| the process exits at start. api: a traceback ending in `noema.composition.StartupError: <reason>` and `ERROR:    Application startup failed. Exiting.` (exit 3); jobs and media-worker: one line `<process>: startup refused: <reason>` (exit 2) | an invalid or forbidden process variable, for example `NOEMA_REDIS_URL is required outside dev/test` (both forms seen with `docker run -e NOEMA_ENV=prod` on the `:wfinfra` images), `NOEMA_DEV_API_KEY is not allowed with NOEMA_ENV=prod`, a non-empty `NOEMA_AWS_ENDPOINT_OVERRIDES` outside dev/test | fix the variable ([deployment.md](deployment.md) section 3) |
| an api container restarts in a loop with `Errno 98` (address in use) | an image built before the two-worker change binds the same metrics port in both workers | rebuild the images (`make e2e-images`) or run them with `NOEMA_STACK_API_WORKERS=1` |
| `make up PROFILES=app` starts containers that never get ready | the stack was not seeded | use `make e2e-up` (it bootstraps); stop the other with `make down` |
| the bootstrap refuses after a moto restart | moto keeps its state in memory (peppers, DSN secrets, ExternalIds) while Postgres kept the seeded rows; `bootstrap` is one-time | `make e2e-down && make e2e-up` |
| a second stack fails on ports | two stacks on one port base | give each its own `E2E_PROJECT` and `E2E_PORT_BASE` (e.g. `E2E_PORT_BASE=28200`) |
| two replicas share nothing (no `/metrics`, separate Redis namespaces) | `NOEMA_STACK_ENV=test` | use `dev` (the default) for multi-process stacks |
| a rotated `noema/db-dsn` is not picked up | a running pool keeps its DSN | restart the processes |
| `aghoo_circuit_state` stays 2 although the upstream is healthy | the breaker moves to half-open only on a call | send traffic (the e2e suite probes each replica); it closes after the half-open probes |
| `labeling.item` jobs fail and retry on a mock stack | the default mock scenario has no labeler rule | expected on the mock (2% sampling) |
| `noema-admin` exits 3 | the operator key failed to authenticate (wrong or revoked `NOEMA_OPERATOR_KEY`) | use a live operator's key |
| `noema-admin` exits 1 with `{"error": "two_person", ...}` | the approver is the proposer | approve with a second operator |
| a model capability WARNING at start (`model capability probe discrepancy: ...`) | a config capability claim the recorded probes refute | fix the model's `modalities` / `controls` in the config |

## 2. Error codes

Status and retryability are fixed per code (`ERROR_TABLE`, IMPL-030). Retry only `retryable: true`, with backoff and
`retry-after` when present.

| Code | Status | Usual cause | Fix |
|---|---|---|---|
| `schema_violation` | 400 | the body or query fails the contract: unknown field, a remote `image_url`, more than 64 media parts, U+0000 in a structured field, control characters in a query id | fix the request; `error.param` names the field |
| `unsupported_question_type`, `reserved_question_name` | 400 | a question type or name the profile cannot take | fix the questions |
| `model_not_available` | 400 | the facade's `model` is not of the form `aghoo/<profile>` (e.g. `router-default` without the prefix) | use `aghoo/<profile>`; an existing profile the tenant may not use is 403 `profile_not_allowed` |
| `invalid_api_key` | 401 | malformed, unknown, revoked or expired key, wrong environment (`ak_test_` in prod) or IP not allowed; the message never says which | check the key with `noema-admin keys list`; the api log line `auth.failure reason=<r> key_id=<id> client_ip=<ip>` has the reason |
| `invalid_end_user_token` | 401 | reserved (end-user delegation is Phase 2) | - |
| `permission_denied` | 403 | the role or the key's scopes do not allow the operation | mint a key with the scope, or use a principal with the role |
| `profile_not_allowed` | 403 | a pinned profile outside `tenants.<t>.request_overrides`, or an unknown profile | allow it in the config, or drop the pin |
| `session_forbidden` | 403 | another principal's session | use the owner's key |
| `not_found`, `session_not_found` | 404 | unknown, deleted or other-tenant resource; ids with control characters | check the id and the tenant |
| `idempotency_key_reused` | 409 | the same `Idempotency-Key` with another body, or from another principal | a new key per distinct request |
| `session_conflict` | 409 | `expected_seq` is stale | re-read the session and retry with its `last_seq` |
| `session_busy` | 409 (retryable) | another turn holds the session past `queue_timeout_ms` | retry after it finishes |
| `file_not_ready` (retryable), `file_rejected` | 409 | the upload is still processing, or the sandbox rejected it | wait (`wait_for_files: true`) or upload a valid file |
| `resume_window_expired` | 410 | an idempotent replay of a streamed turn after the 5-minute window, or a cross-pod attach with Redis down | start a new turn |
| `request_too_large`, `file_too_large`, `image_too_large` | 413 | over 50 MiB JSON, an upload over its per-type cap, an inline image over 3,750,000 bytes decoded | smaller inputs, or upload files instead of inline images |
| `unsupported_media_type` | 415 | content type not JSON, or a Phase 2 upload type (audio, video, SVG, DOCX) | - |
| `unsupported_parameter` | 422 | a parameter the route or model cannot honour, e.g. hierarchical on the facade or with `output_format: "jev"`, a pinned effort the model lacks | drop or change the parameter |
| `feature_not_available` | 422 | an out-of-phase value: `speculative` / `hedge`, `best_of_n` / `fuse`, `retention: custom`, `x-aghoo-end-user` | not in Phase 1 |
| `no_route_satisfies_constraints`, `no_capable_member` | 422 | no model of the profile can take the input (modality, context window) | another profile, or fewer media |
| `rpm_exceeded`, `tpm_exceeded` | 429 (retryable) | admission limits (per key, project, tenant, model quota mirror); also the per-IP auth-failure bucket; also (`rpm_exceeded`, `retry-after: 5`) more than 32 open `watchFile` event streams per project on one api process (`noema.api.overload.WatchSlots`) | honour `retry-after` and the `x-ratelimit-*` headers; close idle file event streams |
| `queue_timeout` | 429 (retryable) | the per-process cap (`retry-after: 1`), or the admission fair queue | retry; operators: [runbooks.md](runbooks.md#noema-turns-shed) |
| `budget_exhausted` | 429 | never raised in Phase 1 (budgets are Phase 2) | - |
| `internal_error` | 500 (retryable) | no active config yet, Postgres down for a `store: true` turn, an unexpected error (log `unhandled error (request_id=...)`) | retry; operators check Postgres and the config |
| `not_implemented` | 501 | `GET /v1/turns/{id}/events` (resume, Phase 2) | - |
| `all_routes_unavailable` | 503 (retryable) | every generation route failed (breakers open, upstream errors); also a turn cut by shutdown after 30 s | retry; operators: [runbooks.md](runbooks.md#noema-circuit-open) |
| `tenant_access_misconfigured` | 503 | the tenant's STS or model access fails; `details.cause` says which | [tenant-onboarding.md](tenant-onboarding.md#3-when-the-first-call-fails) |
| `deadline_exceeded` | 504 (retryable) | the deadline passed before any model call started | a larger `Aghoo-Deadline-Ms` |
| `upstream_stalled` | SSE only (retryable) | the committed stream stalled after the commit (30 s idle timer); the partial text is kept | retry the turn |
| `client_too_slow` | SSE only | the client read the stream too slowly; the turn is stored `failed` | read faster (C3: detection at 30 s, [reports/qa/wave-e-summary.md](../../reports/qa/wave-e-summary.md)) |
