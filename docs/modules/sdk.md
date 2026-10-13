# SDKs: `@aghoo/sdk` (TypeScript) and `aghoo` (Python) (owner: sdk; Wave C1)

Spec: IMPL-035 (strategy), spec-08 §10 (codegen, `sdk.ts` surface, wire-compat check), §14.1, §14.7, 03 §9 (SDK
behaviors), spec-10 §3 gate Q2 (facade conformance with the openai client). Both SDKs are client libraries: they call
the Noema HTTP API only (never Bedrock or OpenAI directly) and import nothing from `noema`.

## Composition and wiring
Nothing to wire in `composition.py`: the SDKs are separate distributables (`sdk/typescript`, `sdk/python`). The
integrator and QA may use them as API clients in e2e tests: `new AghooClient({ baseURL, apiKey })` /
`Aghoo(base_url=..., api_key=...)` against `uvicorn noema.api.app:create_app --factory`.

## Layout
| Path | Contents |
|---|---|
| `sdk/typescript/src/{types,sdk,openai-compat}.ts` | vendored contract surface: `types.ts`, `sdk.ts` byte copies; `openai-compat.ts` with the OpenAI host in its doc comment replaced by the path (the repo's forbidden-endpoint scan allows that host only in the canonical file) |
| `sdk/typescript/src/generated/openapi.d.ts` | `openapi-typescript@7.13.0 --default-non-nullable false` over `src/noema/contracts/build/openapi.bundled.yaml` |
| `sdk/typescript/src/{client,builders,answers,accumulator,stream}.ts`, `core/`, `resources/` | the hand-written client |
| `sdk/typescript/scripts/sync-contracts.mjs` | `npm run sync` re-copies the vendored files and regenerates the wire types; `--check` (also run by `test/vendor.test.ts`) fails on drift |
| `sdk/typescript/test/*.check.ts` | type-level tests: `wire-compat.check.ts` (spec-08 §10.3 verbatim, 18 assertions), `sdk-usage.check.ts` (the vendored contract check with its two imports pointed at the implementation), `implements.check.ts` (the implementation is assignable to the declared `AghooClient`, `typeof AghooClient`, `AghooError`, `q`, `TurnStream`) |
| `sdk/python/src/aghoo/_decision_types.py` | byte copy of `contracts/python_sdk_types.py` (tolerant decision models; drift fails `test_contract_sync.py`) |
| `sdk/python/src/aghoo/{_client,_async_client,_transport,_stream,_events,_models,_params,_questions,_ops,_sse,_retry,_errors}.py` | the hand-written client |
| `sdk/fixtures/<operationId>.<case>.json` | shared response fixtures (status, headers, body or SSE frames), every one validated by the server's `ContractValidator` in `sdk/python/tests/test_contract_sync.py` |

## Commands
- TypeScript (packages resolve from the root `node_modules`; nothing to install): `cd sdk/typescript && npm run check`
  (= `sync:check`, `tsc -p tsconfig.json`, `vitest run`). `npm run build` emits `dist/` (ESM + `.d.ts`; Node >= 22).
- Python (root venv): `UV_NO_SYNC=1 uv run --frozen --no-sync pytest sdk/python -q`;
  `UV_NO_SYNC=1 uv run --frozen --no-sync ruff check sdk/python && ... ruff format --check sdk/python`;
  `cd sdk/python && UV_NO_SYNC=1 uv run --project ../.. --frozen --no-sync mypy` (strict; src and tests).
- After a contract change: `make sdk-sync` (re-copies `python_sdk_types.py` to `aghoo/_decision_types.py` and runs
  `npm run sync`; `make contracts-sync` / `contracts-generate` run it too).
- In `make check`: its last step is `sdk-check` (`make/sdk.mk`: `sdk-ts-check` + `sdk-py-check`).

## TypeScript public surface (`sdk/typescript/src/index.ts`)
- `new AghooClient(opts?: ClientOptions)`; `ClientOptions = { apiKey?, baseURL?, fetch?, maxRetries?, timeoutMs? }`
  (a superset of the contract's constructor). Defaults: `AGHOO_API_KEY` (no `Authorization` header when unset),
  `AGHOO_BASE_URL`, else `DEFAULT_BASE_URL` (the contract's `servers[0]`), `maxRetries` 2, `timeoutMs` 600000.
- Resources exactly as `contracts/sdk.ts`: `decisions.create` (overloads: `output_format: "jev"` ->
  `JevDecisionResponse<Qs>`, else `TypedDecisionResult<Qs>` with a non-enumerable `byName`), `decisions.retrieve`;
  `turns.stream/create/retrieve/cancel`; `profiles.list`; `sessions.create/retrieve/list/update/turns/delete`;
  `files.upload/retrieve/watch/delete/derivative`; `usage.list`; `admin.calibrators.list`, `admin.labeling.runs/dropped`.
  Every method also takes a trailing `RequestOptions` (`signal`, `timeoutMs`, `maxRetries`, `idempotencyKey`).
- `q.predicate/noul/choice/score` (array `choices` with `group`, literal inference).
- Errors: `AghooError` (contract fields: `status`, `type`, `code`, `param`, `requestId`, `retryable`, `retryAfterS`,
  `details`), plus `AghooConnectionError` (no usable response; stream ended early or with non-JSON data) and
  `AghooTimeoutError extends AghooConnectionError`. A non-envelope error body (e.g. a proxy page) gives
  `code: "unknown_error"`, `type` from the status, `retryable` for 408/429/5xx except 501.
- `TurnStreamImpl` (returned by `turns.stream`): async iterable once; adds `turnId` and `lastEventId` getters.
- An id that is empty, `.` or `..` rejects with TypeError before any request (WHATWG URL parsing would resolve it,
  `%2E%2E` included, so an untrusted id could retarget the call: `sessions.delete("..")` -> `DELETE /v1`).

## Python public surface (`aghoo`)
- `Aghoo(*, api_key=None, base_url=None, timeout=600.0, max_retries=2, http_client=None)` and `AsyncAghoo` (same
  arguments, `httpx.AsyncClient`); context managers; an injected http client is not closed.
- Same resources; params are keyword arguments typed by TypedDicts (`TurnParams`, `DecisionParams`,
  `SessionCreateParams`, `SessionUpdateParams`); per-call `options: RequestOptions` (`idempotency_key`, `timeout`,
  `max_retries`). `usage.list(from_=...)`; `files.derivative` returns `DerivativeContent(content, media_type)`;
  `files.upload(file: bytes | IO[bytes], filename=..., media_type=...)`: the sync client streams a seekable file in
  64 KiB chunks (sent from offset 0 on every attempt) and buffers a non-seekable one; `AsyncAghoo` reads a file
  object in a worker thread (`anyio.to_thread`), never on the event loop, and holds it in memory.
- An id that is empty, `.` or `..` raises ValueError before any request (`_ops.seg`).
- `decisions.create(Routing, input=...)` with `class Routing(DecisionSet)` fields annotated `Choice[Literal[...]]`,
  `Predicate`, `Score[Literal[...]]` and built with `choice/predicate/noul/score` (03 §9.2): returns a `Routing`
  instance with one typed answer per field (an undeclared value fails validation) and `.result`. A list of question
  dicts returns `DecisionResult`; `output_format="jev"` returns `JevDecisionResponse`. A class whose Literal values
  differ from its declared options raises TypeError at definition.
- Models: the vendored decision models plus tolerant `TurnEvent` (union with `UnknownEvent(raw_type, raw)`),
  `TurnObject`, `Session`, `SessionTurn`, `FileObject`, `ProfileInfo`, `UsageRow`, `CalibratorInfo`, `LabelingRun`,
  `LabelingDroppedItem`, `CancelResult`. `generation.delta` is built with `model_construct` (no per-delta validation).
- Errors: `AghooError` (`status, type, code, message, param, request_id, retryable, retry_after_s, details`),
  `AghooConnectionError`, `AghooTimeoutError`. A response that violates the decision invariants raises
  `pydantic.ValidationError` (the tolerant models enforce them by design).

## Behavior (both SDKs)
- Headers: `Authorization: Bearer <key>`, `Aghoo-Version: 2026-10-01`, `Accept`, `Content-Type` for JSON, and one
  `Idempotency-Key` (given or UUIDv4) on the POSTs that declare it (createTurn, cancelTurn, createDecision,
  createSession, uploadFile), reused across that call's retries. PATCH/DELETE/GET send none.
- Retries: connect/read errors, timeouts, and responses whose envelope says `retryable` (else 408/429/5xx except
  501), at most `maxRetries` (2); delay = `retry-after` seconds when sent (a value above 60 s is not waited for: the
  error is thrown), else full jitter `random * min(8 s, 0.5 s * 2^attempt)`. Streams retry only before their first
  event (the first event is read inside the retry loop). An error response with `x-aghoo-idempotent-replay: true`
  (a failure stored after a model call was dispatched, spec-11 §5) is thrown at once: the same Idempotency-Key can
  only replay it, so retrying it needs a new `idempotencyKey` / `idempotency_key`.
- Multipart upload: `purpose`, then `session_id`, then the `file` part (the server streams the body and rejects a
  `session_id` after the file with 400 `schema_violation`).
- Turn streams: SSE parsed incrementally (CR/LF/CRLF, split chunks, BOM, comments/pings, invalid UTF-8 -> U+FFFD,
  linear in the frame size); only the committed attempt's deltas are text, `generation.rerouted` drops text from
  `discard_from_seq`, `decision.updated` replaces the decision, `turn.completed` ends the stream; SSE `error` events
  are yielded.
  Answers of unknown types become `UnknownAnswer`. `finalText()/final_text()` and `decision()` settle with
  `turn.completed` and raise AghooConnectionError if the stream ended before it.
- Abort: `abort()` POSTs `/v1/turns/{id}/cancel` once (never after `turn.completed`); the stream then ends normally.
  Before `turn.created`, an attempt in flight is read until its turn id arrives and then cancelled, while a retry
  backoff ends at once and no new attempt starts (no turn is created for an aborted call; the stream then ends empty:
  no events, final text `""`, decision null). A dropped connection or leaving the loop is not a cancel.
  TS: an aborted `signal` cancels the turn and closes the stream (iteration rejects with the signal's reason; before
  `turn.created` the same rules apply). The stream listens to the signal only until it settles or `turn.completed`
  arrives (`{ signal }` listener option), so one app-wide signal can be reused: settled streams stay collectable and
  a later abort cancels nothing. Internals: `TurnStreamSource.open(conn, stop)`; `Transport.send/sse(..., stop)`.
  Python: `abort()` may run in another thread (sync) or task (async) while the stream is opening: it sets a stop
  event (`threading.Event` / `anyio.Event`) that cuts the backoff short (`_retry.sleep/async_sleep(delay, stop)`) and
  makes the open raise the internal `OpenStopped`; it returns None and the open sends the cancel with turn.created.
  `abort()` returns the cancel answer only when that call sent it.
- An interrupted open (cancellation, KeyboardInterrupt, any exception while reading the first event) closes the
  streamed response (Python async: shielded `aclose`).
- Resume (Phase 2): `turns.stream(params, { lastEventId, turnId })` / `last_event_id=..., turn_id=...` calls
  `GET /v1/turns/{id}/events` with `Last-Event-ID`; Phase 1 servers answer 501 `not_implemented` (thrown).

## Config keys, metrics, spans, error codes
- Reads only the environment: `AGHOO_API_KEY`, `AGHOO_BASE_URL`; live tests: `NOEMA_LIVE=1`, `NOEMA_LIVE_BASE_URL`,
  `NOEMA_LIVE_API_KEY`; the Q2 facade suite: the four `NOEMA_FACADE_*` variables above. No Noema config keys. Emits no metrics or spans (client side). Raises no error code of its
  own: every `ErrorCode` comes from the server envelope (open enum).

## Tests (TS: 92, of which 6 env-gated live tests skipped; Python: 192, of which 1 live test skipped)
- Gate Q2 (spec-10 §3), TS half: **pending C2 wiring** (a launcher; `.orchestration/requests/sdk.md` item 3).
  `sdk/typescript/test/facade.openai.live.test.ts` runs the unmodified `openai@7.32.0` client against a real facade
  (`NOEMA_FACADE_BASE_URL`, `NOEMA_FACADE_API_KEY`, `NOEMA_FACADE_THROTTLED_API_KEY`,
  `NOEMA_FACADE_UNAVAILABLE_INSTRUCTIONS`; attempts counted on the client's `fetch`): acceptance and parsing,
  `x-aghoo-include: ensemble`, `unavailable` -> `refusal` with and without the header, retries on 503 and 429, the
  error envelope. All 5 cases passed in this wave against the in-process decision harness (fake world, no Bedrock)
  with a scratch launcher (the code is in the request). `test/facade.openai.test.ts` stays as the offline msw suite:
  it checks only the client's parsing of the contract examples and fixtures, not the facade.
  The Python half (`tests/integration/decision/test_facade_openai_sdk.py`) belongs to the decision role.
- spec-08 §10.3 wire-compat (18 assertions) and the vendored `sdk-usage.check.ts`, compiled by `tsc --strict`
  (`test/typecheck.test.ts` runs it inside vitest).
- Golden SSE G01–G12b (all 17 fixtures) through `TurnStream` in TS and in Python (sync and async), with pings, an
  unknown event type and 7-byte chunking: event order, deltas, final text (oracle: the stored `output_text`, else all
  deltas), decision, turn id, last event id, request body and headers.
- Review fixes (each a regression test that fails with the fix reverted): TS `stream.test.ts` (a signal reused
  across streams is detached and cancels nothing; abort during the retry backoff by signal and by `abort()` sends no
  second POST; deterministic waits: fetch calls are recorded synchronously, no timed sleeps), `files-usage.test.ts`
  (raw multipart part order), `transport.test.ts` (replayed error not retried; dot and empty ids rejected). Python
  `test_abort_concurrency.py` (sync thread and async task abort while the POST is in flight and during the backoff),
  `test_transport_hardening.py` (replay, ids, response closed on an interrupted or cancelled open, invalid UTF-8),
  `test_upload_streaming.py` (chunked seekable upload resent whole on retry, non-seekable buffered, async read off
  the loop), `test_sse.py` (U+FFFD).
- Every request the Python SDK builds passes the server's `RequestGate` (params, headers, Content-Type, JSON Schema)
  and the semantic decision rules (`test_request_conformance.py`, all 22 SDK operations); the TS request params are
  covered at compile time by wire-compat assertions `_15`–`_18`.

## Known limits
- `lastEventId` alone cannot address a turn, so resume needs the extra `turnId` option (an SDK extension).
- TS `TurnStream` is iterable once; `decision()`/`finalText()`/`abort()` called before iteration consume it. TS opens
  the request when `turns.stream()` is called; Python opens on `__enter__` or the first event.
- TS skips unknown event types (the contract union is closed); Python yields `UnknownEvent` (IMPL-035).
- Python `timeout` is httpx's per-operation timeout (connect/read/write/pool), so on a stream it bounds every read:
  a silence between events longer than it (server pings every 15 s) ends the stream with AghooConnectionError. TS
  `timeoutMs` bounds each attempt up to the full response (streams: to the first event; no read timeout after it).
- `AsyncAghoo.files.upload` holds the whole file in memory (read off the loop); the sync client streams seekable
  files. A seekable file is always sent from offset 0.
- `aghoo` declares `anyio>=4,<5`, `httpx>=0.28,<0.29`, `pydantic>=2.12,<3` (anyio is imported directly).
- Python adds `AsyncAghoo` as the task requested (IMPL-035 names only the sync client); `usage.get` in the task
  brief is `usage.list` in the contract.
- `sdk/typescript/package-lock.json` was resolved offline from the npm cache; tests use the root toolchain.
- Live tests (`NOEMA_LIVE=1`) make no model calls and were not run in this wave.
