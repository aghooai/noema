# noema.identity (owner: identity; Wave C1)

Specs: spec-09 §2-§4 (authn, authz matrix, secrets), ADR-001 (STS, ExternalId, Mantle auth), spec-03 §2-§5 (auth,
operator, admin functions, retention), spec-05 §3 (activation), spec-08 §9 (sessions), IMPL-007, -008, -040, -044,
-072 (CLI part), -083. Routes: `src/noema/api/routes/sessions.py`.

## Purpose
API-key authentication, the spec-09 §3 authorization matrix, tenant credentials (STS broker, dev-only ambient
broker), platform/tenant secrets from Secrets Manager, the sessions service and routes with the `session.delete`
job, and the `noema-admin` operator CLI.

## Public surface (by module)
| Module | Surface |
|---|---|
| `apikeys` | `parse_bearer`, `parse_operator_key`, `mint_api_key`, `mint_operator_key`, `hash_key`, `verify_key` (constant time), `Pepper`, `PepperRing(current, previous=None, partial=False)`, `load_peppers(secrets, ref=PEPPER_REF, *, fallback=None)`, `pepper_value`, `PEPPER_REF = "noema/api-key-pepper"` |
| `authn` | `ApiKeyAuthenticator` (implements `Authenticator`; `start()` lifespan hook), `authfail_bucket(ip)`, `PEPPER_TTL_S` 300, `PEPPER_RETRY_S` 10, `PEPPER_NEGATIVE_S` 5; `keycache.KeyCache` (LRU 10,000, TTL 60 s, negative 10 s) |
| `authz` | `MatrixAuthorizer` (implements `Authorizer`: `check`, `list_scope`), `MATRIX`, `OPERATION_ACTIONS` (operationId -> Action), `roles_for(action)`, `visible(...)` |
| `revocation` | `run_revocation_listener(redis, keys, authn, security, clock)` (api background task), `handle_revocation(payload, authn, security)` |
| `secrets` | `SecretsManagerProvider` (implements `SecretsProvider`; `start()`, `aclose()`; raises `core.errors.SecretMissing` for a missing secret or version), `secret_id(ref)` |
| `sts` | `StsBroker` (implements `CredentialBroker`), `StsClient` / `AssumedRole` Protocols, `AssumeRoleResult(creds, assumed_role_arn)`, `classify_sts_error`, `external_id_pattern`, `role_session_name` |
| `sts_aws` | `AioStsClient` (the production `StsClient`; `start()`, `aclose()`), `broker_credentials(broker, tenant_id, aws_account_id, *, time_fetcher)` -> `BrokerCredentials` (aiobotocore refreshable credentials with the broker's 600/300 s windows) |
| `ambient` | `AmbientBroker` (dev/test only; implements `CredentialBroker`) |
| `mantle` | `short_term_bearer(creds, region, *, now, expires_at)` -> bearer `MantleAuth`; `SIGV4` |
| `security` | `SecurityEvents.emit(name, *, tenant_id, request_id=, job_id=, details=)` (log + `aghoo_security_violations_total` + outbox `audit_event`) |
| `sessions` | `SessionService(db=, jobs=, authz=, ids=, clock=)`: `create`, `list`, `get`, `update`, `delete`, `list_turns`; `session_wire(row)`, `expires_at(...)` |
| `history` | `turn_messages(turn_row)`, `user_text(input_parts)`, `part_text(part)`, `redact_spans(text, stored, *, markers=)`, `history_page(turns, session_id, *, after, limit)`, `encode_cursor` / `decode_cursor`, `STORED_MARKER` |
| `jobs` | `make_handlers(services) -> {"session.delete": handler}`, `delete_session_data(db, files, tenant_id, session_id)` |
| `cli` | `python -m noema.identity.cli` (`noema_admin.main`, `amain`, `build_parser`) |

## Wiring in composition.py (integrator, C2)
```python
from noema.identity.ambient import AmbientBroker
from noema.identity.authn import ApiKeyAuthenticator
from noema.identity.authz import MatrixAuthorizer
from noema.identity.revocation import run_revocation_listener
from noema.identity.secrets import SecretsManagerProvider
from noema.identity.security import SecurityEvents
from noema.identity.sts import StsBroker
from noema.identity.sts_aws import AioStsClient

ov = settings.endpoint_overrides                     # dev/test only (validated by process_settings)
secrets = SecretsManagerProvider(clock, region="us-east-1", endpoint_url=ov.get("secretsmanager"))
security = SecurityEvents(telemetry=telemetry, outbox=outbox, ids=ids, clock=clock)
sts = AioStsClient(region="us-east-1", endpoint_url=ov.get("sts"))   # platform (pod) credentials: default chain
broker = StsBroker(config=config, secrets=secrets, sts=sts, clock=clock, runtime=runtime,
                   deployment=env, pod_id=settings.pod_id, telemetry=telemetry, security=security)
# dev / live smoke instead: check_component("broker.ambient", env)
# broker = AmbientBroker(config=config, clock=clock, env=env, region=<a tenant home_region, e.g. "us-east-1">,
#                        endpoint_url=ov.get("sts"), security=security)
authn = ApiKeyAuthenticator(db=db, secrets=secrets, limiter=<ops-admission RateLimiter>, clock=clock,
                            runtime=runtime, env=env)
authz = MatrixAuthorizer()
```
- `start(services, "api")`: `await secrets.start()`, `await sts.start()` (create both AWS clients before traffic;
  their botocore data loads in a worker thread), `await broker.start()`, `await authn.start()` (loads the pepper ring;
  a failure is logged, not raised: requests retry), then `runtime.spawn("identity.keys_revoked", lambda:
  run_revocation_listener(redis, keys, authn, security, clock))`. `authn` needs the open runtime for its background
  pepper reload and touch/rehash tasks (without one it reloads inline). Jobs process: `await secrets.start()`,
  `await sts.start()`, `await broker.start()` (labeling calls use tenant STS) and `merge_handlers(...,
  noema.identity.jobs.make_handlers(services), ...)`.
- `stop`: `await broker.aclose()`, `await sts.aclose()`, `await secrets.aclose()`.
- Routes: `noema.api.routes.sessions.router` (already in `API_ROUTERS`); it uses `svc.db`, `svc.jobs`, `svc.authz`,
  `svc.ids`, `svc.clock`, `svc.idempotency` (createSession `Idempotency-Key`).
- Adapters: `broker_credentials(...)` is the ADR-001 §4 refreshable-credentials object for tenant aiobotocore
  clients. The model adapters no longer hold aiobotocore clients (E sprint A): they sign every call with its own
  `broker.get` + `assert_creds_for`, so the broker's 600/300 s windows apply (adapters.md).
- The seed `NOEMA_DEV_API_KEY` registration applies to the fake authenticator only; with the real one, mint a dev
  key with `noema-admin keys create --env test`.

## Conventions this module defines
- **Pepper secret** `noema/api-key-pepper`: JSON `{"version": <p>, "pepper": "<>= 32 chars>"}`. The current value is
  the current pepper; its `AWSPREVIOUS` version (moved by Secrets Manager on every new value) is the previous one.
  Only those two load (read concurrently); a row naming any other `p` fails (401). A missing `AWSPREVIOUS` version
  (ResourceNotFound, `SecretMissing`) means no previous pepper; a failed read of it (throttling, timeout, 5xx) keeps the previously
  loaded one and marks the ring `partial` (retried after 10 s). A previous-pepper success re-hashes the row
  (`auth.rehash_api_key`, after the response). Provisioning (IaC/moto) must create this secret before `bootstrap`.
  `tests/fakes/world.PLATFORM_SECRETS` holds a plain string pepper: use `tests/unit/identity/helpers.pepper_secrets`.
- `SecretsProvider.get(ref, version=)`: `version` = a UUID (VersionId) or a staging label (VersionStage); None reads
  `VersionStage=AWSCURRENT` explicitly (moto otherwise ignores staging-label moves). "No such secret or version"
  (ResourceNotFound, or an empty value) raises `core.errors.SecretMissing`; any other failure (throttling, timeout,
  denied, 5xx) a plain `NoemaError(internal_error)` (requests item 6); both are 500 `internal_error`.
- ExternalId: `aghoo-<tenant>-<32 base62>` in `platform/tenants/<tenant>/external-id` (`aws-sm://...`). Rotation is
  two steps: `rotate` stages `AWSPENDING` (brokers keep `AWSCURRENT`), `promote` moves `AWSCURRENT` to it after the
  trust policy lists both values (ADR-001 §3 "Rotation").
- Session retention: `default` = `created_at + platform.data_classes.<class>.retain_days`; `ephemeral` = 24 h;
  `custom` = 422 `feature_not_available` (`param: retention`). A PATCH of `data_class` or `retention` recomputes
  `expires_at` from `created_at`; a `data_class` change writes `audit_event` `session.data_class.update`.
- Session metadata (Wave E): at most 16 pairs, keys of at most 64 and values of at most 512 characters on
  createSession and updateSession, else 400 `schema_violation` (`param` `metadata`, or `metadata.<key>` for a long
  value). The contract leaves metadata unbounded (the 50 MiB body cap was the only bound), so one key could store
  megabytes per session and make one listSessions page gigabytes; the bounds follow OpenAI's metadata limits.
- History (`listSessionTurns`): every turn row of the session, seq ASC, each as its user then its assistant message.
  Stored content is the orchestrator's format (requests/orchestrator.md item 7): user text = the `text` parts and
  the `json` parts (`orjson.dumps(json).decode()`) of `turn.input_parts`, in part order, joined by `\n` (media parts
  have no text); assistant text = `output_text`. Every redaction reads `"[redacted]"` on the wire (offsets
  recomputed, code points, `end` exclusive, `reason` = the stored kind, e.g. `pii_email`). For a `redacted` turn the
  spans are the orchestrator's `[REDACTED:<kind>]` markers found in the rendered text (stored offsets of JSON parts
  are stale after the jsonb round trip, which reorders keys; requests item 2); other turns use
  `turn.redactions.{user,assistant}` (malformed, out-of-range and overlapping spans dropped). `metadata_only` turns
  read `[redacted]` with one span `metadata_only`. `limit` counts messages (like every list): a page can end after a
  turn's user message, and the opaque cursor (base64url of `[<turn repo cursor> | null, <split seq>]`) then resumes
  with that turn's assistant message (the turn is read whole if it was deleted meanwhile). A page costs one turn
  query, two when it ends mid-turn, three when it also starts mid-turn. Bad cursors are 400 `schema_violation`.
- deleteSession: enqueue `session.delete` (dedupe = session id, priority 100) first, then `mark_deleted`; the job
  deletes turns, decisions, purges files (`FileService.purge`), deletes late-finalized turns, then the session row.

## Authentication and authorization behavior
- `authenticate(headers, client_ip)`: per-IP bucket `authfail:<ip>` checked first (`RateLimiter.check`, 10/s, burst
  50; empty -> 429 `rpm_exceeded` with `retry_after_s`), header regex, cached row or `auth.lookup_api_key`, HMAC
  (current/previous pepper), key env vs row env and `ak_test_` in prod, `revoked_at`, `expires_at`, `ip_allowlist`
  (IPv4-mapped IPv6 peers are matched as IPv4). Every failure: 401 `invalid_api_key` "Invalid API key." plus one
  `auth.failure reason=... key_id=... client_ip=...` log line and one bucket debit. Postgres or pepper failures are
  500 `internal_error` (no bucket debit) and are never cached. `touch_api_key` at most once a minute per key, after
  the response. Wave E (06 §7, F11 `store: false` unaffected): while Postgres is unavailable, a key whose 60 s entry
  expired is checked against its last good row (`KeyCache.stale`, kept `STALE_S` = 600 s past the expiry, never
  refreshed; after a failed lookup such keys skip Postgres for `STALE_RETRY_S` = 5 s, one WARNING per window); a
  revocation still evicts it, and a key never seen is 500.
  With Postgres and Redis down at once, a key revoked just before stays usable for at most `STALE_S`.
- Hot-path loads: concurrent cache misses of one key share one `lookup_api_key` (single flight; a failure is 500 for
  every waiter, a cancelled leader hands over to a waiter). Pepper ring: loaded by `start()` or the first request (one
  shared load; a failure is negative-cached 5 s, so queued requests fail fast); past its 300 s TTL it is served stale
  while one background task (`runtime.spawn("identity.pepper_reload")`) reloads it; a failed or `partial` reload
  keeps the loaded versions and retries after 10 s. While the ring is `partial` with no previous pepper known, a key
  on an older version is 500 (it cannot be judged), not 401.
- Revocation: `keys_revoked {tenant_id, key_id}` evicts the LRU entry on every pod (< 1 s with Redis up; the 60 s TTL
  bounds it otherwise); a payload whose tenant differs from the cached row is dropped (`pubsub_tenant_mismatch`).
  A revocation that arrives while a lookup is in flight prevents that read from being cached (the shared read still
  answers the requests waiting on it). The listener reconnects 1 s after any Redis/OS error (tested). Wave E: while
  quiet it PINGs the subscribed connection every 5 s and resubscribes when nothing (no message, no PONG) arrived for
  15 s, so a connection that died without FIN/RST does not silently drop revocations for the life of the pod.
- `MatrixAuthorizer.check(who, action, resource=None)`: scope -> role (403 `permission_denied` where declared, else
  `not_found` / `session_not_found`) -> visibility V (404) -> write rule (403 `session_forbidden` on turn.create,
  `permission_denied` elsewhere; cancel -> 404). Create actions accept `kind="file"` / `kind="turn"` resources for
  file parts and `retry_of` (visibility only). `list_scope`: `none | own | project | tenant`.

## Credentials behavior (StsBroker)
- Cache per tenant, memory only; `DurationSeconds` 3600; soft refresh from `exp - 600 s` in the background
  (`runtime.spawn`; a failure backs off 10 s and never fails callers); hard refresh from `exp - 300 s`
  (single flight under a per-tenant lock); negative cache 10 s; `invalidate` only evicts sessions >= 60 s old.
- Every `get` compares the cached session's account, invoke role ARN and home region with the active config (a dict
  lookup); a mismatch (account move, region change, tenant removed) is a miss, so the new config takes effect on the
  next call instead of failing `assert_creds_for` until the session ages out.
- Causes: `role_not_allowlisted` (unknown tenant, or ARN not `arn:aws:iam::<aws_account_id>:role/AghooBedrockInvoke`),
  `external_id_unavailable`, `external_id_invalid`, `access_denied` (AccessDenied, InvalidClientTokenId, ExpiredToken,
  RegionDisabledException, MalformedPolicyDocument), `sts_unavailable` (anything else, or 3.5 s on the clock),
  `account_mismatch` (assumed-role ARN account), `ambient_account_mismatch` (AmbientBroker).
- Mantle: `platform.credentials.mantle_auth` of the snapshot at refresh time (`bearer_short_term` -> a presigned
  token valid until `min(creds expiry, now + 12 h)`; `sigv4` -> no token).

## Config keys read
`tenants.<t>.{aws_account_id, invoke_role_arn, external_id_ref, home_region}`, `platform.credentials.mantle_auth`,
`platform.data_classes.<class>.retain_days`; the CLI validates whole config files (spec-05 §3 steps 1-5, plus a
`file:` price catalog) and compares `decision_profiles.<id>.version` with calibrator artifacts on approve.

## Metrics, spans, logs
- Span `sts.assume_role` {`aghoo.tenant`, `aghoo.credentials.source`=sts}, `error.type` tenant_access_misconfigured.
- Counters `aghoo_tenant_access_failures_total{tenant,cause}`, `aghoo_security_violations_total{kind}` (kinds:
  tenant_access_misconfigured, account_mismatch, ambient_account_mismatch, pubsub_tenant_mismatch).
- Loggers: `noema.identity.authn` (auth.failure; warnings "api-key pepper reload failed ..." / "... could not read
  the previous version ..." / "api-key pepper preload failed ..."), `noema.identity.security` (security events;
  content-free), `noema.identity.sts`, `noema.identity.revocation`, `noema.identity.jobs`, `noema.identity.cli`
  (info "operator key <key_id> re-hashed with pepper v<p>").

## Error codes raised
`invalid_api_key`, `rpm_exceeded`, `internal_error`, `permission_denied`, `not_found`, `session_not_found`,
`session_forbidden`, `feature_not_available` (custom retention), `schema_violation` (bad cursor/limit: the history
cursor in `history`, the others from the repositories), `tenant_access_misconfigured` (with `details.cause`),
`idempotency_key_reused` (via the hook). CLI refusal reasons: `invalid_argument`, `forbidden`, `conflict`,
`not_found`, `invalid_state`, `two_person`, `operator_invalid` (exit 3), `unavailable`, `error`, `audit_failed`, and
NoemaError codes (`internal_error`).

## noema-admin
Environment: `NOEMA_ENV` (unset = prod rules), `NOEMA_OPERATOR_KEY` (all but `bootstrap` and `config validate`),
`NOEMA_CLI_DSN` (noema_cli), `NOEMA_DB_OWNER_DSN` (bootstrap), `NOEMA_AWS_ENDPOINT_OVERRIDES` (`secretsmanager`,
dev/test only), AWS default chain for Secrets Manager. Commands: `bootstrap --operator N`; `operators create --name N
| revoke OP_ID`; `config validate FILE | propose FILE | approve VERSION [--file FILE] | rollback VERSION | list`;
`tenants external-id create|rotate|promote --tenant T` (T must match `[A-Za-z0-9_-]{1,64}`); `projects create --tenant T --name N [--default-data-class C] |
list --tenant T`; `principals create --tenant T --project P --subject S --roles a,b`; `keys create --tenant T
--project P --principal PRN --scopes a,b [--env live|test] [--expires ISO] [--ip-allow CIDR,..] | list --tenant T
[--project P] | revoke KEY_ID`; `calibrators list [--scope platform|T] [--status S] | approve ID | activate ID |
retire ID [--reason manual|profile_version_retired]`; `prices import FILE`. Output: JSON on stdout; refusals
`{"error", "message"}` on stderr; exit 0 / 1 refused / 2 usage / 3 operator authentication. Database and Secrets
Manager failures are refusals too (wrong DSN password or unknown database / wrong role -> `invalid_argument`; other
Postgres, connection and botocore errors -> `unavailable`), never a traceback and never the DSN. Keys and
ExternalIds are printed once: an ExternalId whose write succeeded is printed on stdout even when its audit row then
fails (exit 1, `audit_failed` on stderr). After its own check the CLI proves the key to the database on the
command's connection (`PgAdminCalls.prove_operator`, revision 0003, Wave E), which then accepts that operator id
and no other (a plain id is `operator_invalid`, exit 3). An operator key verified with the previous pepper is re-hashed under the
current one before the command runs (`admin.rehash_operator`, audited `operator.rehash`; requests item 5; then
proved again under the current pepper); if the
previous pepper could not be read, operator authentication is `unavailable` (exit 1), not exit 3. `config approve`
with `--file` validates the file and refuses (`digest_mismatch`) unless its digest is the proposed version's; without
it, it reads the stored body (`admin.config_body`; `not_found` for an unknown version). It then activates and retires
every fitted/accepted/active calibrator whose (profile_id, profile_version) is not in the approved body. Both admin
functions are db revision 0002: against a 0001 database these two paths are refused `unavailable`.

## Test ids covered
SEC-AUTH-01…06; SEC-AZ-01 (unit level: every operation x role set x relation against an independent oracle plus 48
golden cells), SEC-AZ-02, SEC-AZ-03, SEC-AZ-04 (the six session operations on Postgres); SEC-STS-01…07, -09, -10,
-11, -12 (identity's logs/spans part), -13, -14, -16, -17; SEC-ISO-02 (keys_revoked); SEC-CFG-01 (approve by the
proposer refused, CLI part); spec-09 §1 security events (log, counter, outbox `audit_event`, applied on Postgres).
Review-fix regressions: `tests/unit/identity/{test_history,test_authn_hotpath,test_cli_refusals,
test_security_events}.py`, `test_sessions_routes.py` (message paging), `test_sts_broker.py` (config change),
`test_apikeys.py` (partial ring); `tests/integration/identity/{test_peppers_moto,test_security_pg}.py`,
`test_sessions_pg.py` (redacted turn through jsonb), `test_cli.py` (staged rotation, DSN refusals; C2a: operator
re-hash across two pepper rotations, `config approve` without `--file`). Conformance: `secrets_provider/secretsmanager`, `credential_broker/sts-fakes`,
`credential_broker/sts-moto`, `auth/apikey-fakedb`, `auth/apikey-postgres` (tests/integration/identity/
conformance_impls.py). Commands: `make test MOD=identity`, `make test-conformance PYTEST_ARGS='-k "sts or apikey or
secretsmanager"'`.

## Known limits
- moto evaluates no trust policy: denials come from `tests/fakes/sts_trust.trust_enforcer`; AWS-side enforcement
  and CloudTrail are a Phase 0 gate (not run). SEC-STS-08 and -15 need the adapters and the wire mock (adapters /
  qa-security). The full HTTP SEC-AZ-01 oracle run over every route is qa-security's (`tests/security`).
- `ephemeral` = 24 h is an identity decision (no spec text). The history text follows the orchestrator's stored
  format; its stored JSON-part offsets are stale after jsonb (requests item 2), so redacted spans come from markers:
  a user who types a literal `[REDACTED:<kind>]` into a `redacted`-policy turn sees it as a redaction (the
  orchestrator's own span rule has the same property).
- An operator re-hashes only when it runs a command: one that runs nothing between two pepper rotations is locked out
  (exit 3) and must be re-created by another operator (`operators revoke` + `operators create`).
- `tenants external-id` checks the tenant id format, not that the tenant exists: noema_cli cannot read the tenant
  list, and the ExternalId is minted before the tenant's config is approved (the trust policy comes first).
- Creating an AWS client still holds the GIL for about 10-15 ms while the worker thread parses botocore JSON
  (measured loop stall 13 ms instead of 50 ms); `start()` moves that before traffic.
- `ApiKeyAuthenticator` does one `RateLimiter.check` per request (the spec's "before key verification" rule).
