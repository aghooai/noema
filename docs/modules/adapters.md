# adapters: Bedrock model adapters, capabilities, registry, resilience (`src/noema/adapters`)

Wave C1, role adapters; adapted in C2a (core `RetryBudget.note_request`, the 14 C1 review minors, the wire-mock
conformance kit, the L9 live labeler smoke) and in E sprint A (the lean unary path of `complete()`). Bedrock-only
(D1): every model call of Noema goes through `ConverseAdapter` (Converse and ConverseStream over the lean path) or
`MantleAdapter` (Mantle's OpenAI-compatible chat completions: `stream: false` over the lean unary
path, streams via httpx). Both implement the frozen `core.protocols.adapters.Adapter` exactly per spec-02 §4.1 (stream semantics),
§4.2 (wire mapping, verified against `tests/golden/{decision,generation}_requests`) and §4.3 (error table). The
package also provides `AdapterRegistryImpl`, the probe-verified capability matrix, the circuit breakers
(`HealthRegistry`), the retry budget (`RetryBudget`) and the per-attempt stream timeouts. It imports only `core`,
`config` and `telemetry` (import-linter clean).

## Public surface

| Name | Module | Implements |
|---|---|---|
| `ConverseAdapter` | `noema.adapters.converse` | `Adapter` for `models.<alias>.adapter == "bedrock-converse"` |
| `MantleAdapter` | `noema.adapters.mantle` | `Adapter` for `"bedrock-mantle-chat"` |
| `AdapterRegistryImpl(config, {kind: adapter})` | `noema.adapters.registry` | `AdapterRegistry` (`for_model(alias)` -> `models.<alias>.adapter` of the current snapshot; unknown alias -> `NoemaError(not_found)`) |
| `BreakerRegistry` | `noema.adapters.resilience.breakers` | `HealthRegistry` |
| `SlidingRetryBudget` | `noema.adapters.resilience.retry_budget` | `RetryBudget` (with `note_request`, on the core Protocol since C2a; the C1 alias `MeteredRetryBudget` was removed in C2b) |
| `SecurityEventSink` | `noema.adapters._base` (exported) | where an adapter-detected `account_mismatch` goes (identity's `SecurityEvents` fits structurally) |
| `StreamWatchdog` | `noema.adapters.resilience.timeouts` | internal stream timeouts (one watchdog task per stream) |
| `capabilities_of(ModelCfg)`, `PROBED`, `probe_discrepancies(snapshot)`, `probe_unverified(snapshot)` | `noema.adapters.capabilities` | `ModelCapabilities` from config; the probe matrix and its config cross-checks |
| `classify(exc)` | `noema.adapters.classify` | spec-02 §4.3 (`Adapter.classify_error`) |

Constructors (keyword-only):
```python
ConverseAdapter(*, config: ConfigRegistry, broker: CredentialBroker, clock: Clock, retry_budget: RetryBudget,
                telemetry: Telemetry | None = None, security: SecurityEventSink | None = None,
                endpoint_overrides: Mapping[str, str] | None = None, max_pool_connections: int = 512,
                unary_transport: Callable[[TenantCredentials], StreamTransport] | None = None)  # tests: the emulator
MantleAdapter(*, config, broker, clock, retry_budget, telemetry=None, security=None, endpoint_overrides=None,
              transport: httpx.AsyncBaseTransport | None = None, max_connections: int = 512,
              sigv4_service: str = "bedrock")
BreakerRegistry(clock, *, policy: BreakerPolicy | None = None, telemetry: Telemetry | None = None,
                keys: RedisKeys | None = None)          # keys None = no cross-pod sync
SlidingRetryBudget(clock, *, policy: RetryBudgetPolicy | None = None, keys: RedisKeys | None = None,
                   pod_id: str = "local")
```
Lifecycle: `await converse.prewarm()` / `await mantle.prewarm()` (optional, idempotent; the first client calls it
anyway) build the shared botocore data loader / TLS context in a worker thread; `await converse.aclose()` /
`await mantle.aclose()` close the pooled clients (a later call gets `AdapterError(retryable, "adapter_closed")`);
`await health.run(redis)`
and `await retry_budget.run(redis)` are the background sync tasks (they return at once when `keys` is None, run
until cancelled, and never raise for Redis failures). Extras: `adapter.open_streams` (live provider streams),
`health.apply_event(raw)` (one remote `breaker_events` payload), `health.origin` (this instance's event nonce),
`retry_budget.sync_once(redis)`.

## Wiring in `composition.py` (C2 integrator)

```python
from noema.adapters import (AdapterRegistryImpl, BreakerRegistry, ConverseAdapter, MantleAdapter,
                            SlidingRetryBudget, probe_discrepancies)

retry_budget = SlidingRetryBudget(clock, keys=keys, pod_id=settings.pod_id)   # ONE instance:
health = BreakerRegistry(clock, telemetry=telemetry, keys=keys)
security = SecurityEvents(telemetry=telemetry, outbox=outbox, ids=ids, clock=clock)  # noema.identity.security
common = dict(config=config, broker=broker, clock=clock, retry_budget=retry_budget, telemetry=telemetry,
              security=security,                                                 # audit row for account_mismatch
              endpoint_overrides=settings.endpoint_overrides)                   # keys "bedrock-runtime", "mantle"
converse, mantle = ConverseAdapter(**common), MantleAdapter(**common)
adapters = AdapterRegistryImpl(config, {"bedrock-converse": converse, "bedrock-mantle-chat": mantle})
# Services(adapters=adapters, health=health, retry_budget=retry_budget, ...)
# start(services, "api" | "jobs"):  runtime.spawn("breaker-sync", lambda: health.run(redis))
#                                   runtime.spawn("retry-budget-sync", lambda: retry_budget.run(redis))
#                                   await converse.prewarm(); await mantle.prewarm()   (optional, before traffic)
# stop(services) (after the drain): await converse.aclose(); await mantle.aclose()
# startup (optional): log every probe_discrepancies(config.current()) at WARNING
```
- The same `SlidingRetryBudget` must be `services.retry_budget` (generation's fallback guard) **and** the adapters'
  `retry_budget`: the adapters count every wire request into it (`note_request`), which the 10 % rule is computed
  from. `tests.fakes.health.FakeRetryBudget` (a no-op `note_request` since C2a) also works with the real adapters,
  but then the in-adapter retry follows the fake's token bucket, not the 10 % rule.
- Without `security`, an adapter-detected account mismatch is logged (spec-09 record) and counted, but no
  `audit_event` row is written (the adapters hold no outbox).
- The `Clock` of the adapters must be the clock of every `Deadline` callers pass (`services.clock`).
- `media-worker` needs no adapters; `jobs` needs them for labeling.

## Behavior

**Per call (both adapters).** (1) Pre-wire rules on `models.<alias>` of `req.snapshot or config.current()`
(`wire.check_request`), each `AdapterError(invalid_request)` with no provider code and nothing sent: alias unknown
or served by the other adapter, no messages, `max_tokens` outside `1..max_output_tokens_cap`, `temperature` with
`controls.temperature` false, an effort outside `controls.effort`, logprobs/top_logprobs on Converse, media on Mantle,
`top_logprobs` without logprobs or above `top_logprobs_max`, an image/document the model's `modalities` lack or in a
non-user message. (2) `broker.get(tenant)` + `assert_creds_for` (IMPL-076): a `TenantAccessMisconfigured` becomes
`AdapterError(tenant_access, cause)`; `account_mismatch` is also the spec-09 §1 security event (through
`security`, else the adapters' own structured record and counter). Any other broker exception is classified into an
`AdapterError` too (e.g. `ConnectionError` -> retryable, `SecretMissing` -> fatal). (3) The client of (tenant,
account, region) from the pool (a creation failure is a classified `AdapterError`; after `aclose()`
`AdapterError(retryable, "adapter_closed")`). (4) The request, with the one in-adapter retry: only
`retry="default"`, only for a connect/DNS/TLS error or a reset before any response byte, only if
`retry_budget.try_spend` grants it, after a 0-200 ms full jitter (IMPL-016). IMPL-016's "no model call happened"
holds for connect, DNS, TLS, proxy and write failures; a reset or disconnect after the request write
(`classify.possibly_delivered`, e.g. spec-10 `connect_reset`) is still retried (spec-02 §4.1) but may have been
billed, so it is logged at WARNING ("connect retry after a possible delivery"); no usage reports that first
dispatch. Decision, verification and labeling calls (`retry="none"`) are never retried. Throttling is never retried
in the adapter.
**Deadline.** The caller's deadline bounds the whole call, steps (2)-(4) included: `complete()` runs the credential
fetch, the client acquisition and the request under one deadline scope (a slow `broker.get` ends at the deadline
with `AdapterError(timeout:deadline)`), and `_send` refuses to put any request on the wire, first attempt or retry,
once the clock has reached the deadline (`AdapterError(timeout:deadline)`; aiohttp and httpx can write a request
before their first checkpoint, so the cancel scope alone would not stop it). Streams: see `__aenter__` below.

**Converse.** No aiobotocore client (E sprint A): Converse and ConverseStream send the request aiobotocore would build
over the lean path below, signed with the credentials of the call's own `broker.get` + `assert_creds_for` (step 2;
so the broker's ADR-001 §4 windows, advisory 600 s and mandatory 300 s, apply to every call: SEC-STS-17), never the
default credential chain, `AWS_PROFILE`, the shared config files, a configured endpoint URL or an ambient
`AWS_BEARER_TOKEN_BEDROCK` (SEC-STS-15); connect timeout 3 s, read timeout 300 s, 512 pooled connections; the only
retry is the adapter's connect retry. botocore's service model, endpoint rules and partitions are parsed once, in
a worker thread (`prewarm`), into the lean path's `ConverseModel`. Endpoint: the tenant's region (botocore's
endpoint rules), or the dev/test `bedrock-runtime` override. Parameters exactly `wire.converse_params` (`system`
only when non-empty, `inferenceConfig.{maxTokens,temperature?,stopSequences?}`, `outputConfig.effort` when set;
never `textFormat`, `cachePoint` or `additionalModelRequestFields`). A ConverseStream body is decoded with botocore's
`EventStreamBuffer` (CRC checks: `malformed:eventstream`) and each message parsed as aiobotocore's event stream
parsed it (`parse_event`: a payload whose read fields have their modeled types is used as decoded, any other one
goes through botocore's `EventStreamJSONParser`; events of an unknown type are skipped; exception and error
messages raise botocore's `EventStreamError`); a payload botocore's parser cannot take is `malformed:chunk` (F8;
aiobotocore surfaced the parser's AttributeError as fatal). An error status before the stream raises botocore's
`ClientError`, as for Converse.

**Lean unary path** (E sprint A; `complete()` of both adapters: decision, verification and labeling members,
`stream: false` generation; ConverseStream through `open()`). aiobotocore's per-call machinery and httpx cost 0.7
and 1.4 ms of CPU per member call (local micro-benchmark), the lean path 0.25 ms; aiobotocore's event stream about
50 µs per stream event (C2b perf smoke §6-7; reports/perf/e-sprint-a.md). `_unary.py`:
`AiohttpUnary`, one pooled aiohttp client per (tenant, account, region) with aiobotocore's transport settings
(connect 3 s, read 300 s, keep-alive 12 s, 512 connections, aiobotocore's verifying TLS context built once in a
worker thread, `Accept-Encoding: identity`, no redirects followed, no ambient proxy or netrc, the URL sent exactly as
signed: `yarl.URL(url, encoded=True)` as aiobotocore sends it; a str URL is re-quoted by aiohttp, which decodes the
model id's `%3A` back to `:` and so breaks SigV4 for every versioned id and ARN, Wave E), and aiobotocore's
exact exception mapping for failures before the response headers (`EndpointConnectionError`,
`ConnectionClosedError`, `ConnectTimeoutError`, `ReadTimeoutError`, `SSLError`, `HTTPClientError`), so the
classification, the connect-retry eligibility and `possibly_delivered` are unchanged; a failure while reading the
body is botocore's `ResponseStreamingError` (retryable, never the connect retry: IMPL-016 retries only before any
response byte; aiobotocore used to retry it). `_converse_wire.py`: the Converse request byte for byte as aiobotocore
builds it (botocore's endpoint rules and partitions, the percent-encoded model id, the rest-json body of
`converse_params` without `modelId`, `botocore.auth.SigV4Auth` with signing name `bedrock` and the credentials'
region, the SDK's unsigned `amz-sdk-invocation-id` / `amz-sdk-request`), signed with the credentials of the call's
own `broker.get` (so as fresh as the broker's);
exactly the parameter checks botocore's `ParamValidator` makes on these shapes (`ParamValidationError`); responses
parsed as botocore parses them (a body whose read fields have their modeled JSON types is used as decoded, any other
JSON object goes through botocore's `RestJSONParser`; an error status becomes botocore's `ClientError`; anything
else is `malformed:response`, above). Mantle sends its
unchanged request (bearer or SigV4 headers, `user-agent: noema`) over the same transport; with an injected httpx
`transport` (tests) `HttpxUnary` runs it over that transport instead. Transport-failure provider codes of Mantle
`complete()` are therefore botocore's exception names (they were httpx's); kinds, timeout codes and retry
eligibility are unchanged.

**Mantle.** `POST {https://bedrock-mantle.<region>.api.aws/v1 | mantle override}/chat/completions`, body exactly
`wire.mantle_body` (`stream: false` with `logprobs`/`top_logprobs` for members; `stream: true` +
`stream_options.include_usage` for streams). Auth from `creds.mantle` of the same `broker.get`: `Bearer <short-term
token>` or SigV4 (signing name `bedrock`, region = tenant region). Streams: httpx clients per pool key,
`trust_env=False`, all sharing one TLS context built in a worker thread (`prewarm`; loading the CA bundle took 5-20
ms per client), which the unary transports reuse.
`top_logprobs` is sent only when set (never `null`).

**Responses.** `text` = the answer text blocks (`reasoningContent` / `reasoning_content` excluded, counted).
`refused` only from provider signals: Converse `stopReason` `content_filtered` / `guardrail_intervened`; Mantle
`finish_reason == "content_filter"` or a non-empty `message.refusal` (IMPL-053). Stop reasons: Converse
`end_turn`, `max_tokens`, `stop_sequence`, `content_filtered`/`guardrail_intervened` -> `content_filter`,
`model_context_window_exceeded` -> `max_tokens`, others -> `other`; Mantle `stop` -> `end_turn`, `length` ->
`max_tokens`, `content_filter`, others -> `other`. Usage: Converse `input = inputTokens + cacheReadInputTokens +
cacheWriteInputTokens`, `cached_input = cacheRead`, `cache_write_input = cacheWrite`, `output = outputTokens`; Mantle
`prompt_tokens`, `completion_tokens`, `prompt_tokens_details.cached_tokens`. `reasoning_tokens` = the provider value
(Mantle `completion_tokens_details.reasoning_tokens`) or ceil(reasoning chars / 4) capped at the output (Bedrock has
no breakdown; `output_tokens` stays exact). When Bedrock hid the reasoning (a `redactedContent` block or delta, or a
reasoning block or signature with no reasoning text: live Haiku and Sol, Wave E) it is at least the output left after
the text's estimated tokens, `output - ceil(text chars / 4)`. Mantle `logprobs.content[]` -> `TokenLogprob(token, logprob, top)`.
`latency_ms` is measured on the clock from the send; `region` is the credentials' region; `model_revision` None.
A Mantle completion whose `content` or reasoning is not a string is `AdapterError(retryable, malformed:response)`;
so is a 2xx Converse body that is no Converse response (empty, not JSON, not an object, or fields of other types; E
sprint A: aiobotocore made an empty or non-JSON body an empty `end_turn` answer, a wrong-shaped one a fatal
AttributeError, and a wrong-shaped answer escaped `complete()` as a raw TypeError).

**Streams** (`noema.adapters._stream.StreamSession`, the context manager `stream()` returns).
- `__aenter__` is bounded by the connect window: `connect_s` from the call start, capped by the deadline. Steps
  (1)-(3) run inside it; if they do not finish in the window (a slow broker or client creation), `__aenter__` raises
  `AdapterError(timeout:connect)` (`timeout:deadline` when the deadline caps the window) and nothing is sent. Then
  the request runs in a background task and `__aenter__` waits for the response headers until the window ends. An
  error response or a non-retried connect error inside that window raises `AdapterError`; a request still pending
  then counts as connected, and its headers count against `first_event_s` (so a hang before the first byte ends as
  `ErrorEvent(timeout:first_event)`, F4).
- Events: `MessageStart` (Converse `messageStart`, Mantle first chunk), `ReasoningDelta(chars)` (never text),
  `TextDelta`, `Stop`, then `UsageEvent` (Converse ends the stream at `metadata`; Mantle reads until `[DONE]`). A
  usage chunk before the stop is held until after `Stop`.
- Timeouts (`StreamWatchdog`, one task per stream, all on the clock): `first_event_s` and `first_answer_s` from the
  call start (before the credential fetch); `idle_s` is provider silence: each wait for the next provider event (any
  frame, `contentBlockStop` included) starts a fresh idle window, so the time a consumer holds an event never counts
  and data already sent is read; each limit capped by the deadline; expiry -> `ErrorEvent(retryable,
  "timeout:first_event|first_answer|idle|deadline")`. After `Stop` only `idle_s` and the deadline apply (the
  trailing usage of a refusal or a reasoning-only stop is not cut by `first_answer_s`).
- Every failure after `__aenter__` is exactly one `ErrorEvent`, the last event: an error response after the connect
  window (full §4.3 kind), a ConverseStream exception frame (`retryable`, provider code e.g.
  `ModelStreamErrorException`, `ThrottlingException`), a Mantle `{"error": ...}` chunk (`retryable`), malformed
  input (`retryable`, `malformed:eventstream` bad CRC/framing, `malformed:json`, `malformed:sse_line`,
  `malformed:missing_done`, `malformed:truncated`, `malformed:chunk` for a provider event of the wrong shape: a
  choice or delta that is not an object, non-string content or reasoning; never a fatal `TypeError`/
  `AttributeError`, F8), a transport error (`retryable`). Mantle `[DONE]` without a
  `finish_reason` -> `Stop("other")`.
- After `Stop`, a failure or timeout ends the stream silently (the answer is complete; the caller estimates missing
  usage).
- Exit = cancel: the background tasks are cancelled and the provider response is closed without reading further
  (aiohttp `close()`, httpx `aclose()`); `open_streams` returns to 0 and the pooled client's lease is released. This
  is unconditional: also when the consumer leaves because an enclosing cancel scope was cancelled (generation's
  `att.scope.cancel()` for race losers, restarts, turn cancel, client disconnect), and also when closing the provider
  response fails; the cancellation (or the close error) is re-raised after the cleanup.
- Per event: one `CancelScope` plus a few comparisons and one normalized event object; no Pydantic, no per-event
  task or timer (about 13 µs per SSE chunk end to end through httpx in a local micro-benchmark).

**Failure side effects.** `ExpiredTokenException`, `UnrecognizedClientException` and Mantle 401: `broker.invalidate`
and the tenant's pooled clients are retired (the next call re-fetches credentials). Throttles count
`aghoo_bedrock_throttles_total`; provider-side tenant-access failures count `aghoo_tenant_access_failures_total`.
`ResourceNotFoundException` (404: a model or inference profile the account cannot use) is `tenant_access` /
`model_access_denied` (spec-02 §4.3 "model not enabled"); a Mantle 404 stays fatal.

**Breakers** (`BreakerPolicy`: window 30 s of 1 s buckets, `min_requests` 20, `failure_ratio` 0.5, `ttft_factor` 3,
`open_s` 15, `probe_every` 20 = 5 %, `baseline_alpha` 0.2, `baseline_windows` 3). Service breaker per (model id,
region): `error`/`timeout` fail, `ok` succeeds, `throttled` ignored; also opens when the window p95 of `ttft_ms` (>= 20
samples) exceeds 3x the baseline (EWMA of earlier windows' p95, armed after 3 windows). Quota breaker per (tenant
account, model id): `throttled` fails, `ok` succeeds. Open -> after `open_s` half-open (lazy) -> 1 in 20 `admit()` is
a probe (counted only when the call is admitted by both breakers); a successful probe outcome closes, a failed one
re-opens. In half-open only as many outcomes count as probes were admitted since the transition: the late outcome of
a call admitted before the open (a long stream) is ignored, so it neither closes the breaker during the burst nor
re-opens it without a probe (Wave E). `admit(model, region, account)` is False while either
breaker is open; `state(model, region)` is the service breaker. Callers (decision, generation) call `admit` before
and `record` after every call; the adapters do not. **What callers record**: the service breaker is shared by every
tenant, so only provider-health signals: `error` for a `retryable` or `fatal` failure, `timeout` for a provider
`timeout:*` (connect, first event, read, idle; never `timeout:deadline` or a call cut by the caller's deadline, which
is the caller's budget), `throttled` for `throttled`; a `tenant_access` or `invalid_request` failure is not recorded at all (else one
misconfigured tenant opens the breaker for all; generation and decision filter this way, request item 7 closed in C2b).
Cross-pod: local open/close transitions are published on `RedisKeys.breaker_events()` as `{model, region,
tenant_account?, state, until_ms?, origin}` (`until_ms` = wall-clock epoch ms of the half-open time; `origin` = the
instance nonce, so a pod ignores its own echoed events); remote `open` opens a closed or half-open breaker until
`until_ms` and extends an open one when later, keeping it local if local evidence opened it; remote `closed` closes
a half-open breaker or one opened remotely, never one opened by local evidence; malformed payloads are dropped.
A local opening logs WARNING `circuit open: <model id> <region>` (`quota circuit open: ...` for a quota breaker,
never the account): the JSON log formatter drops the `model`/`region` extras (C2b/D integration fix).

**Retry budget** (`RetryBudgetPolicy`: `ratio` 0.1, `window_s` 60 (1 s buckets), `min_retries` 10, `sync_s` 1,
`remote_stale_s` 5). `try_spend` grants while `retries + 1 <= max(min_retries, ratio * (requests - retries))` over
local + other pods' fresh totals, so upstream amplification stays <= 1.1 (F12) at load. The floor (`min_retries`
10, kept in C2a: the frozen `RetryBudget` contract is a token bucket and the conformance suite grants a fresh key's
first retry; without it a quiet tenant gets no fallback) bounds amplification at 1 + max(10 / N, 0.1) for N first
attempts per minute per (account, model): 1.1 from N = 100, at most 10 extra requests per minute below (request
item 8 asks for the IMPL ruling). Sync: every second each pod writes
`HSET retry_budget(account, model) <pod_id> "<requests>,<retries>,<wall_ms>"` + `EXPIRE 120` for every key used in
the last 2 windows and reads the other pods' fields (`HGETALL`); keys idle for 2 windows are dropped.

## Configuration and settings read

- `models.<alias>`: `adapter`, `id`, `controls.{temperature,effort,logprobs,top_logprobs_max,structured_output,
  reasoning}`, `modalities`, `max_output_tokens_cap`, `context_window` (capabilities and pre-wire rules).
- `TenantRef` (`tenants.<id>.aws_account_id`, `home_region`) from the caller; `creds.region` for endpoints.
- `platform.credentials.mantle_auth` is read by the broker (`creds.mantle.mode`), not by the adapters.
- Stream timeouts come from the caller (`ConfigSnapshot.route_timeouts(alias, effort)`, ROUTE_TIMEOUTS_V1).
- Process settings: `endpoint_overrides` (`bedrock-runtime`, `mantle`; validated and dev/test-only in composition),
  `pod_id` (retry-budget field), the Redis client and `RedisKeys`.

## Telemetry

Metrics (declared names only): `aghoo_bedrock_throttles_total{tenant,model}`,
`aghoo_tenant_access_failures_total{tenant,cause}` (adapter-detected causes: `model_access_denied`, `access_denied`,
`account_mismatch`; broker causes are the broker's), `aghoo_security_violations_total{kind="account_mismatch"}`,
`aghoo_bedrock_cached_input_ratio{model}` (cached / input since process start), `aghoo_circuit_state{model,region}`
(0 closed, 1 half-open, 2 open). Spans: none (callers own `generation.attempt` / `decision.member`). Logs:
`noema.adapters` (WARNING "model call failed" with adapter, model_id, tenant_id, kind, provider_code; INFO "connect
retry", WARNING "connect retry after a possible delivery" with `possibly_delivered`), `noema.adapters.security`
(without a `security` sink: the spec-09 §1 record `security name=account_mismatch tenant_id=<t> request_id=
kind=account_mismatch` with the structured keys `event=security`, `code`, `tenant`), `noema.adapters.breakers`,
`noema.adapters.retry_budget`; never prompt text, model output, provider error text or credentials.

## Errors raised

Only `AdapterError(ClassifiedError)` (from `complete` and `stream().__aenter__`) and `ErrorEvent`s; `kind` per
spec-02 §4.3; `message` is a fixed phrase. Provider codes: Bedrock error codes (PascalCase), HTTP statuses for
Mantle (`"429"`, `"401"`, ...), connect/transport exception names, `timeout:connect|read|first_event|first_answer|
idle|deadline`, `malformed:*`, `ParamValidationError`, `adapter_closed`, the exception name of an unexpected broker
or client-creation failure. `capabilities()` / `for_model()` raise the snapshot's
`NoemaError(not_found)` for an unknown alias.

## Tests

- `tests/unit/adapters/` (virtual time, no network): `emulator.py`/`emulator_wire.py` (the in-process wire
  emulator: the Converse transport answers with real Converse JSON / eventstream bytes / error responses; an httpx
  `MockTransport` for Mantle; driven by the scenario DSL), `support.py` (rig), and `test_converse_stream.py`,
  `test_mantle.py`, `test_complete.py`, `test_classify.py` (the exact probe ValidationException strings),
  `test_timeouts.py`, `test_breakers.py`, `test_retry_budget.py`, `test_credentials.py`, `test_wire_and_registry.py`
  (goldens, capabilities, probe matrix), `test_live_harness.py` (the live budget offline), `test_stream_cancel.py`
  (a consumer inside a cancel scope that another task cancels, mid-stream, while waiting for the headers or the
  credentials, repeated, on `FakeClock` and on `SystemClock`, both adapters: provider stream closed,
  `open_streams == 0`, pool in-flight 0; release still runs when the provider close fails),
  `test_deadline_bounds.py` (a slow broker under `complete()` and `stream()` for both adapters: elapsed = the
  deadline or `connect_s`, zero wire requests; an expired deadline sends nothing; `_send` never sends the connect
  retry after the deadline; `first_event_s` counts from the call start). C2a regression tests for the C1 review
  minors, on both adapters where it applies: `test_failures_wrapped.py` (unexpected broker errors, a call after
  `aclose()`, a client creation failure and a slow one under the deadline: always `AdapterError`, nothing on the
  wire), `test_timeouts.py` (a consumer slower than `idle_s` gets all buffered events; usage 8.5 s after a refusal
  stop with `first_answer_s` 8; idle still ends the post-stop wait), `test_mantle.py` / `test_converse_stream.py`
  (chunks and events of the wrong shape -> one retryable `malformed:chunk`; non-string completion content),
  `test_classify.py` (`ResourceNotFoundException`; `possibly_delivered`), `test_complete.py` (the possibly-delivered
  retry log), `test_credentials.py` (the spec-09 record; the sink with identity's `SecurityEvents` and its audit row;
  an ambient `AWS_BEARER_TOKEN_BEDROCK`), `test_breakers.py` (own echo ignored; a later remote open keeps local
  evidence local), `test_retry_budget.py` (the floor's amplification bound), `test_prewarm.py` (botocore data and
  sessions built in worker threads, one shared loader, one shared TLS context), `test_wire_and_registry.py` (no
  `top_logprobs: null`), `test_live_harness.py` (the labeling cap; L9 offline). E sprint A, the lean unary path
  against a real aiobotocore client as the reference (`lean_reference.py`: SigV4 clock frozen, `before-send`
  capture): `test_lean_converse_wire.py` (the signed request byte for byte: URL, body, `Content-Type`,
  `X-Amz-Date`, `X-Amz-Security-Token`, `Authorization`, for every Converse golden, edge requests (non-ASCII and
  escaped text, image and document blobs, temperature, stop sequences, effort), 6 regions incl. `cn-north-1` and
  `us-gov-west-1`, ARN and special-character model ids, no session token, an endpoint override),
  `test_lean_converse_parse.py` (36 raw responses, success bodies of every shape and every §4.3 error form: the same
  `ModelResponse` or `ClassifiedError`, but `malformed:response` for the 8 bodies that are no Converse response; 31 parameter edge cases: rejected exactly where `ParamValidator` rejects;
  the enforced `min` constraints equal the service model's), `test_unary_mapping.py` (15 aiohttp failure types:
  the same botocore exception, kind, provider code, connect-retry eligibility and `possibly_delivered` as
  aiobotocore's `AIOHTTPSession.send`; a body-read failure is a `ResponseStreamingError` that closes the response).
  `test_lean_converse_stream.py` (21 eventstream bodies, each whole and in 1-, 7- and 64-byte chunks: the same
  normalized events and the same classification of the failure that ends the stream as aiobotocore's event stream,
  exception and error messages of every type, corrupt CRCs, unknown event types, payloads of other types; the one
  deliberate difference, `malformed:chunk` for a payload the parser cannot take). The emulator's `converse_unary`
  serves the lean Converse path (golden capture from the wire body).
- `tests/integration/adapters/`: `conformance_impls.py` registers `adapter/converse+mantle` (emulator, golden wire
  capture, virtual time), `adapter/converse+mantle@mockbedrock` (the real adapters over sockets against
  `tools/mockbedrock`, wall time via `WallClock`, a `FakeClock` subclass that follows the system clock;
  `virtual_time=False` skips the 4 timing cases; the golden capture is the mock's request log, with Converse
  `modelId` from the path and blob digests mapped back to the bytes the client sent) and
  `health_registry/breakers+budget`; `test_resilience_redis.py` (two pods over the compose
  Valkey, Redis down); `test_against_mockbedrock.py` (the real adapters over sockets against qa-tools'
  `tools/mockbedrock`); `test_live_ambient.py` (the live broker against moto STS); `test_unary_transport.py`
  (`AiohttpUnary` over localhost sockets: refused, a disconnect before the response, a truncated body, keep-alive
  reuse, no redirects, `Accept-Encoding: identity`, no ambient proxy, a cancelled call's connection closed and not
  reused; Wave E: for a `-v1:0` id and an application-inference-profile ARN, unary and stream, the request line's
  path is the signed path and SigV4 recomputed over the received path equals the `Authorization` header).
- `tests/live/` (`NOEMA_LIVE=1`, markers `live` + `enable_socket`; run in Wave E, reports/qa/live-smoke.md, and
  Wave E3, reports/qa/e3/live-smoke.md): `conftest.py` (refuses to start when the global journal
  `.orchestration/reports/live-calls.jsonl` holds >= 87 (60 until the owner's +30 of 2026-10-12)
  calls or without `-p tests.live.bearer`; `NOEMA_LIVE_MOCK=1` rehearses every test against an in-process
  `tools/mockbedrock` with `mock_scenario.yaml`), `budget.py` (`CountingAdapterRegistry`, `Ledger`: 87 calls across
  every run, one journal line per call before any byte (the resolved model id), `need(n)` skips a test whose worst
  case does not fit, allowlist, max_tokens 256 / 512 generation / 1024 decision and verification members (CR #26
  budgets uncut) / 1040 labeling, cassettes under `tests/cassettes/<live|mock>-<run>/` with `input_chars`, both
  input estimates (K1) and the wire-shape taps of `wiretap.py`, which since Wave E3 also compare every lean
  request's signed headers with `SigV4Auth` and every ConverseStream message with `EventStreamBuffer`;
  `reports/live/calls.json`), `ambient.py` (ambient broker), `bearer.py`
  (keeps `AWS_BEARER_TOKEN_BEDROCK` past the root conftest's scrub), `livecfg.py` + `config/*.patch.yaml`,
  `apistack.py` (the in-process api on `composition.build("dev", wrap_adapters=...)`, `NOEMA_CREDENTIALS_BROKER=
  ambient`, over a `make e2e-up E2E_PROJECT=noema-live` stack), `test_adapters_live.py` (L1-L4, the capability
  matrix: pre-wire refusals and 3 provider probes, a `:` model id), `test_api_live.py` (L5-L7, the verified turn on
  the GLM race, L8 with its direct streams interleaved), `test_labelers_live.py`
  (L9), `test_zz_estimator.py` (K1), `fidelity.py` (`python -m tests.live.fidelity`: live vs mock cassettes,
  spec-10 §2.3).
- Run: `make test MOD=adapters` (309 tests incl. Redis/moto/mock integration; also under `XDIST=4`),
  `make test-conformance` (`-k "converse or breakers"` for these kits; `-k mockbedrock` for the wire-mock kit).

## Known limits

- `connect_s` cannot be told apart from the header wait at the HTTP-client level: a request with neither a response
  nor an error after `connect_s` is treated as connected; a TCP connect that hangs is caught by the clients' own 3 s
  connect timeout (Mantle uses the per-call `connect_s`), so it surfaces as a retryable error (and the in-adapter
  retry) rather than `timeout:connect`. `timeout:connect` is raised only when the credential fetch and client
  acquisition use up the whole connect window. The Mantle per-request httpx connect timeout is the full `connect_s`,
  not the part of the window left after the credential fetch (the window itself still bounds `__aenter__`).
- The first stream of a (tenant, account, region) creates its pooled client inside the connect window (about
  60 ms in worker threads for the first Converse client of the process unless `prewarm()` ran, then well under
  1 ms; under 1 ms for httpx once the TLS context exists); with `connect_s` = 3 s (ROUTE_TIMEOUTS_V1) this is harmless,
  but a caller passing a sub-100 ms `connect_s` would see `timeout:connect` on cold clients.
- A retry after a possible delivery (a reset after the request write) may double-bill one dispatch; it is logged,
  bounded by the retry budget, and not in any usage (request item 8 asks to record this in IMPL-016).
- The adapter-side `account_mismatch` audit row (through `security`) carries no `request_id`/`job_id`:
  `ModelRequest` has neither. The process JSON log formatter keeps only the `tenant`, `turn_id`, `event` and `code`
  extras (`noema.telemetry.logs.STRUCTURED_KEYS`), so the adapters' other log extras (model_id, kind,
  provider_code, ...) are visible only to non-JSON handlers and tests.
- A provider event that arrives exactly when a limit expires is discarded in favor of the timeout.
- `reasoning_tokens` on Converse is a character-based estimate (`output_tokens` is exact).
- The TTFT breaker rule mixes the TTFT samples of every caller of a model (decision members report latency as
  TTFT); its baseline is learned per process (3 windows) and is not shared across pods.
- Live smoke (2026-10-11, reports/qa/live-smoke.md): Mantle SigV4 with signing name `bedrock`, the short-term bearer
  presigned from IAM-user credentials and the long-term `AWS_BEARER_TOKEN_BEDROCK` all work (L3, L5-L7); Sonnet 5.5
  accepts PNG and PDF natively and effort `low` (gap 43). Short-term bearer tokens from assumed roles stay
  UNVERIFIED (no AssumeRole in the live smoke). Bedrock now rejects GLM-5.3 effort `none` (and accepts `xhigh`):
  `PROBED` records it (Wave E), so `probe_discrepancies` reports the shipped config's `none` (startup WARNING) until
  the config changes (`.orchestration/requests/io-fixer.md` item 1; the unit test pins it as `KNOWN_DRIFT`).
- The adapters cannot set `aghoo.credentials.source` on the caller's span (the Telemetry Protocol has no current-span
  accessor); callers that need it read `TenantCredentials.source` themselves.
- Change requests: `.orchestration/requests/adapters.md` (C1: composition `wrap_adapters` hook, `note_request` on
  the core `RetryBudget`, the textFormat note, an incident report, the outer-cancel suite case; C2a: item 7 decision's
  breaker outcomes and item 8 the IMPL/spec notes, both closed in C2b (IMPL-016 amendment; spec-02 §4.1, §4.3, §13;
  spec-09 §1; ADR-001 §4; spec-10 §6 L9), item 9 integrator wiring of `security` and `prewarm`).
