# E sprint A: decision fan-out transport and ingress units of work (Wave E, perf-decision-ingress, 2026-10-11)

Scope: the CPU per turn and the serial overhead floor of one api process (c2b perf smoke §6-7), in this role's
areas: the adapters' transport (decision members' Converse and Mantle calls, then ConverseStream), the ingress units
of work, and the decision member runner. Bedrock-only: no model call left the host for these measurements (the wire
mock `tools/mockbedrock`, moto, the stack's Postgres and Valkey); no AWS resource touched. Raw data:
`/tmp/perfa/runs/<run>` on the build host; the summaries (each `results.json` with `provenance`), the two profiles,
the two traces and the measurement scripts are copied to `reports/perf/e-sprint-a/`.
QA gate view: `reports/qa/perf-decision-ingress.md`.

## 1. Result

| | base (wave-c2b) | final (this sprint) |
|---|---|---|
| api CPU per turn, 50 / 100 streams (ABBA means, c1-c8) | 67.6 / 63.8 ms | 52.9 / 49.4 ms (a3; -22%); a4 49.9-51.1 / 48.1-48.7 on a quieter host |
| TTFT overhead p50 / p95 at 50 streams | 14.1 / 29.9 ms | 11.3 / 20.8 ms (a3, campaign); 10.1-10.4 / 17.2-19.5 ms (a4, d1/d3) |
| TTFT overhead p50 / p95 at 100 streams | 51.7 / 128.3 ms | 21.4 / 82.9 ms (a3); 18.3-18.9 / 67.8-68.7 ms (a4) |
| overhead floor at 2 streams (p50, back to back) | 11.5 ms | 9.55 ms |
| units of work / Postgres `execute` round trips before the generation request | 3 / 12 (18 per turn) | 1 / 4 (10 per turn) |
| the generation request leaves the api (2 streams) | 9.19 ms | 7.96 ms |
| S1 (`decision_ms` p50 at 50 streams) | 75 ms | 59-64 ms |
| capacity of one process (ramp) / lag p99 < 50 ms up to | ~15.5 turns/s / < 125 streams | ~21.0 turns/s / 125 streams |

Per change (CPU per turn at 50 / 100 streams): the lean unary path for member calls -10.7 / -10.7 ms; the ingress
fold -0.0 / -0.3 ms (it is a latency change: ingress p50 -1.2 / -8.9 ms, TTFT p50 -1.3 / -9.1 ms); the lean
ConverseStream plus the member fast path -4.0 / -3.5 ms. Every reported window is valid by spec-10 §5; 0 contract
violations; 0 errors below the knee.

Not met: L1 (p50 <= 10 / p95 <= 25 ms at 200 streams per pod): one process saturates at about 150 streams of this
workload (21 turns/s), and even at 50 streams the p50's upper CI is 10.2-10.5 ms; C2's lag target holds up to 125
streams, not 200. 200 streams of this workload need about 1.4 cores of this code: the c2b recommendation of two api
processes per 2-vCPU pod stands, with about 35% more headroom per process.

Equivalence (section 3.4): the lean requests are byte for byte aiobotocore's (40 cases incl. every golden, both
operations, 6 regions); responses, parameter validation, stream decoding and transport failures classify as before
(36 + 31 + 84 + 15 differential cases against a real aiobotocore client), but for 6 deliberate differences toward
the documented contract (section 3.5, 4 of them pre-existing defects, section 9). `make check`: every step green but
`ownership`, which fails only on other QA agents' unowned files; `make e2e` on this sprint's code (a snapshot of
wave-c2b plus these files, its own images and stack): 52/52, twice.

## 2. Method

- **Code under test, isolated from concurrent edits.** Other Wave E agents edit the working tree while these runs
  happen (sprint B in `composition.py`, `api/`, `streaming/`, `telemetry/`). Every measured variant is therefore a
  source snapshot: `base` = `git archive HEAD` (b85f318, tag wave-c2b) into `/tmp/perfa/base`; each variant = `base`
  plus exactly this sprint's files of that step (`/tmp/perfa/tools/snap.sh`), run with `PYTHONPATH=<snapshot>/src`
  ahead of the venv's editable install. `provenance.code.tree_digest` in each results file is the sha256 over the
  snapshot's `src`, `tests/load` and `tools/mockbedrock` (the `/tmp/perfa/tools/runwrap.py` replacement of
  `run.code_version`, which needs a git checkout). The createTurn route change is applied to the base route by
  `/tmp/perfa/patches/route_create.py` (the live route also carries sprint B's turn cap).
  - `a1` = base + the lean unary path (`adapters/_unary.py`, `_converse_wire.py`, `converse.py` and `mantle.py`
    `complete()`).
  - `a2` = a1 + the ingress fold (`turns/ingress.py`, `service.py`, `session_order.py`, the route).
  - `a3` = a2 + the lean ConverseStream (`converse.py`, `_converse_wire.py`, `_unary.py`, `adapters/__init__.py`)
    + the member call fast path (`decision/members.py`).
- **Harness**: `tests/load` unchanged (spec-10 §5 L1 scenario `overhead.yaml`: members 50 ms; Sol first text
  300 ms, 400 tokens at 60 tokens/s, stamped; every pre-commit key pinned; a nonce per turn, so all 15 router-default
  calls run). Stack: compose project `noema-perf-a` (postgres, valkey, moto; ports 297xx), bootstrapped and switched
  to the load config (`python -m tests.load.stack activate`; config v2 `sha256:848146b0...de4b6`).
- **Placement (disjoint from sprint B's concurrent runs on CPUs 20-31 and 40-47)**: api `taskset 14-15` (one hardware
  thread of two physical cores, as c2b's 46-47), mocks `8-9`, `32-33`, `10-11`, `34-35` (4 processes, 2 logical CPUs
  each; Converse and Mantle pairs share their ports), drivers `12-13,36-37`, Postgres and Valkey `docker update
  --cpuset-cpus 4-7`; CPUs 38-39 (the api's SMT siblings) left idle. Not c2b's placement, so the absolute numbers are
  comparable within this report, not with c2b's tables to the decimal (this report's base at 50 streams: 67.6 ms of
  CPU per turn, c2b's 64-68).
- **Plan**: 60 s warm-up, 50 streams 120 s, 30 s settle, 100 streams 120 s (`--plan 50:120,100:120`). The four
  variants ran in ABBA order (`base a1 a2 a3 a3 a2 a1 base`, runs `c1`-`c8`, `/tmp/perfa/tools/campaign.sh`), so a
  slow drift of the shared host weighs on every variant alike; each cell below is the mean of the two runs.
- **Host**: 48 vCPU EPYC 7R13 shared with other agents' stacks and tests; every run started with the 1-minute
  loadavg below 8 (the wrapper waits for it; per-window mean/max in the results). spec-10 §5 validity per window:
  >= 99.9% of turns with `holdback_ms = 0` and `spec_agrees`, every driver and mock process lag p99 <= 2 ms.
- **CPU per turn** = api process CPU (utime + stime from `/proc`, 5 s samples) / turns per second over the window.
  TTFT overhead per spec-10 §5 (client first delta - send - (mock first text - mock receive)), bootstrap 95% CIs.
- **Contract validation**: the drivers validate the first turn of every stream (and every non-200) with the
  `ContractValidator`: 0 violations in every run (`driver-*.jsonl` summaries).

## 3. Changes

### 3.1 Lean unary path for decision member calls (a1)

`ConverseAdapter.complete` and `MantleAdapter.complete` (decision, verification and labeling members; `stream:
false` generation) no longer go through aiobotocore (Converse) or httpx (Mantle):

- `adapters/_converse_wire.py` builds the Converse request byte for byte as aiobotocore does: botocore's endpoint
  rules and partitions (`EndpointProvider`, resolved once per region), `/model/{percent_encode(modelId)}/converse`,
  the rest-json body of `wire.converse_params` without `modelId` (`json.dumps` with botocore's separators and
  `ensure_ascii`, blobs base64, botocore's float rule), `Content-Type: application/json`, signed by
  `botocore.auth.SigV4Auth` (signing name `bedrock`, the credentials' region), then the SDK's unsigned
  `amz-sdk-invocation-id` / `amz-sdk-request: attempt=1`. Parameter validation is exactly what botocore's
  `ParamValidator` enforces on these shapes (leaf types, the `min` constraints; it checks no `max`, pattern or enum).
  Responses are parsed as botocore parses them (a body whose fields `_response_of` reads have their modeled JSON
  types is used as decoded, any other JSON object goes through `RestJSONParser`; an error status (>= 300) becomes the
  `ClientError` botocore raises, so `classify` is unchanged).
- `adapters/_unary.py` `AiohttpUnary`: one pooled aiohttp client per (tenant, account, region) (the pool key is
  unchanged) with aiobotocore's transport settings (connect 3 s, read 300 s, keep-alive 12 s, 512 connections,
  aiobotocore's verifying TLS context, built once in a worker thread; `Accept-Encoding: identity`) and aiobotocore's
  exception mapping of `AIOHTTPSession.send`, in its order, for failures before the response headers.
- Credentials: each call signs with the credentials of its own `broker.get` + `assert_creds_for` (IMPL-076 step 2,
  unchanged), so they are as fresh as the broker's (ADR-001 §4 soft/hard 600/300 s windows); the adapter-held
  `BrokerRefreshableCredentials` is gone (it existed only to refresh the aiobotocore clients).
- Mantle sends its unchanged request (bearer or SigV4 headers; `user-agent: noema`) over the same transport.

Micro-benchmark (`reports/perf/e-sprint-a/tools/bench_client.py` + `bench_server.py`, one core, 15 concurrent calls against a local keep-alive
server returning a canned Converse body, 3,000 calls): CPU per call aiobotocore `converse()` 722 µs, httpx POST +
SigV4 + JSON 1,419 µs, aiohttp POST + SigV4 + JSON 254 µs (of which SigV4Auth + body 80 µs).

### 3.2 One unit of work for ingress steps 6-10 (a2)

Before, a stored turn on a session opened three units of work before the generation request (step 6 session read,
step 10 lease, step 10 history read), each `BEGIN`, `set_config`, the query, `COMMIT` and asyncpg's reset on
release. Now:

- `session_order.acquire_then(..., then=)` runs the history read in the unit of work that takes the lease (one
  transaction; the history is read at the locked `last_seq`; a failed read rolls the lease back). Every stored turn.
- `TurnService.create` (the createTurn route's steps 6-11): a stored turn of a session without file parts,
  `retry_of` or an Idempotency-Key (those read through other units of work) runs steps 6-10 in ONE unit of work: the
  session read and its authorization, step 7 (a no-op without a key), the plan (sync), step 9, one lease attempt and
  the history read, then `COMMIT` before admission (step 11, which may queue). Busy or pending: the unit commits and
  the spec-04 §2 FIFO wait runs as before. The error order is unchanged: step 9's `broker.get` is made before the
  unit opens (so the open transaction never waits on STS) and its failure is raised at step 9's place
  (`test_ing_1_holds_in_the_folded_unit_of_work` repeats T-ING-1 without keys).
- Units of work before the generation request: 3 -> 1 (`test_a_turn_without_a_key_runs_steps_6_to_10_in_one_unit_
  of_work`); Postgres `execute` round trips per turn 18 -> 10 in the 2-stream trace (section 6).

The auth key lookup was already cached (identity's `KeyCache`, 60 s; the trace shows no Postgres call in
`authn.authenticate`); its per-request Redis check of the per-IP failure bucket remains (section 8).

### 3.3 Lean ConverseStream and the member call fast path (a3)

- ConverseStream (the generation stream) takes the same lean path: the same request at `/converse-stream`
  (byte-compared too), `AiohttpUnary.open()` returns the response after its headers, and `_ConverseRaw` decodes the
  body with botocore's `EventStreamBuffer` (its CRC checks), parsing each message as aiobotocore's event stream did
  (`parse_event`: typed payloads as decoded, any other one through botocore's `EventStreamJSONParser`; unknown event
  types skipped; exception and error messages raise botocore's `EventStreamError`). aiobotocore's async event stream
  cost about 57 µs per event (9.6% of the api's CPU at 50 streams, profile `a2-prof-50`). No aiobotocore client is
  left in the model adapters.
- `MemberCallRunner.run`: a member whose backup cannot fire runs its one call in the unit's task (no hedge task
  group, memory stream or extra task per call; members with a possible backup keep the hedge). Same outcomes,
  records, reservation debits and cancellation (the decision suites unchanged and green). The final code keeps the
  hop a hedged call's task takes as one checkpoint, so the members keep their dispatch order (a4, section 4.1).

### 3.4 Equivalence evidence (all in `make check`)

| What | Test | Cases |
|---|---|---|
| Converse and ConverseStream request bytes = aiobotocore's | `tests/unit/adapters/test_lean_converse_wire.py` | every Converse golden of `tests/golden/{decision,generation}_requests` + 5 edge requests (non-ASCII, escapes, NUL, emoji, image and document blobs, history, temperature, stop, effort) x both operations; 6 regions incl. `cn-north-1`, `us-gov-west-1`; ARN and special-character model ids; no session token; an endpoint override: URL, body, `Content-Type`, `X-Amz-Date`, `X-Amz-Security-Token`, `Authorization` equal (SigV4 clock frozen), `SignedHeaders` equal, no other signed header (40 cases) |
| spec-02 §4.2 goldens through the adapters | `test_wire_and_registry.py::test_adapters_put_the_goldens_on_the_wire`, `tests/contract_internal/test_adapter.py::test_golden_wire_requests` (both kits, incl. `@mockbedrock` over real sockets) | every golden |
| Response parse and §4.3 classification = aiobotocore's | `test_lean_converse_parse.py` | 36 raw responses: text, reasoning, refusals, cache usage, unknown members, usage of other types, every error form (`x-amzn-errortype`, `__type`, `code`, generic 5xx HTML/empty, retry-after delta and date, 300, 418) -> the same `ModelResponse` or `(kind, message, provider_code, cause, retry_after_s)`; 8 deliberate differences (3.5) |
| Parameter validation = `ParamValidator` | `test_lean_converse_parse.py` | 31 edge cases (empty and wrong-typed leaves, NaN, str blobs); the enforced `min` constraints equal the service model's (fails first on a botocore model change) |
| Stream decoding = aiobotocore's | `test_lean_converse_stream.py` | 21 eventstream bodies x 4 chunkings (whole, 1, 7, 64 bytes): the same normalized events and failure classification (exceptions of every type, an error message, corrupt CRC, truncation, unknown event types, padded payloads, usage of other types); 2 bodies (8 cases) deliberately different (3.5) |
| Transport failure mapping = aiobotocore's `AIOHTTPSession.send` | `test_unary_mapping.py` | 15 aiohttp failure types: the same botocore exception, kind, provider code, connect-retry eligibility and `possibly_delivered`; cancellation unmapped; a body-read failure closes the response |
| Real sockets | `tests/integration/adapters/test_unary_transport.py`, the `@mockbedrock` conformance kit, `test_against_mockbedrock.py` | refused, a disconnect before the response, a truncated body, keep-alive reuse, no redirects, identity encoding, no ambient proxy, a cancelled call's connection closed and not reused; the whole adapter conformance suite (streams, timeouts, cancellation, goldens) over sockets |
| Account assertion, credential refresh, timeouts, cancellation, deadline | `test_credentials.py`, `test_timeouts.py`, `test_stream_cancel.py`, `test_deadline_bounds.py`, `test_failures_wrapped.py`, the conformance kits | swapped credentials fail before any byte (both paths); ambient `AWS_*` and `AWS_BEARER_TOKEN_BEDROCK` never sign; every call signs with the broker's current credentials (same session before the mandatory window, the re-minted one after it); every timeout and cancellation case of C1-C2b on the lean paths |

Live corroboration (not this role's run, no call made by this role): the live-smoke agent's run of 07:34-07:37 UTC
(journal `.orchestration/reports/live-calls.jsonl`, `reports/live/`) ran after the lean `complete()` path was in the
working tree (`converse.py` 07:22, `_unary.py` 07:26, `mantle.py` 07:27 UTC): Mantle bearer and SigV4 `complete()`
against Bedrock ok (`mantle-auth.json`), L7's 20 member calls through it (`l7-turn.json`: `decision.completed`,
`turn.verified`). The lean ConverseStream (a3) has not met live Bedrock.

### 3.5 Deliberate behavior differences (all toward the documented contract)

1. A 2xx Converse body that is no Converse response (empty, not JSON, not an object, or one botocore's parser
   cannot take) is `AdapterError(retryable, malformed:response)`, as on Mantle. aiobotocore made an empty or HTML
   body an empty `end_turn` answer (a `stream: false` turn would commit empty text) and a wrong-shaped one a fatal
   `AttributeError`; a wrong-shaped answer escaped `complete()` as a raw `TypeError` (not an `AdapterError`).
2. A ConverseStream payload botocore's parser cannot take is `malformed:chunk` (F8), not a fatal `AttributeError`.
3. A failure while reading a `complete()` response body after its headers is botocore's `ResponseStreamingError`
   (retryable, never the in-adapter connect retry): IMPL-016 / spec-02 §4.1 allow that retry only before any
   response byte; aiobotocore retried it (`ConnectionClosedError` from a `ClientPayloadError`).
4. Redirects are never followed (botocore never follows them; aiobotocore's aiohttp followed up to 10 with the
   signed headers).
5. Mantle `complete()` transport failures carry botocore's exception names as provider codes
   (`EndpointConnectionError`, `ConnectionClosedError`, ...) instead of httpx's (`ConnectError`, ...); kinds,
   `timeout:connect` / `timeout:read`, retry eligibility and `possibly_delivered` are unchanged. Provider codes of
   transport failures appear only in logs and the `timeout:` prefix rule.
6. The `user-agent` is `noema` (unsigned; it was botocore's).

## 4. Per-change deltas (ABBA campaign c1-c8, 120 s windows, every window valid by spec-10 §5)

Mean of the two runs of each variant (both values shown where they matter); Δ = change of the CPU per turn against
the previous variant. TTFT and e2e overhead in ms, CPU in ms of api CPU per turn.

| variant | streams | CPU ms/turn (runs) | mean | Δ vs previous | TTFT ovh p50 (runs) | p95 (runs) | p99 mean | e2e ovh p50 mean | ingress p50 mean | decision_ms p50 mean | api lag p99 mean | valid |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| base | 50 | 68.3 / 66.9 | 67.6 |  | 14.2 / 13.9 | 29.3 / 30.5 | 52.3 | 22.2 | 12.4 | 75 | 12.8 | True |
| base | 100 | 63.7 / 63.9 | 63.8 |  | 46.2 / 57.1 | 122.2 / 134.3 | 172.6 | 101.1 | 39.2 | 110 | 28.8 | True |
| a1 | 50 | 57.5 / 56.3 | 56.9 | -10.7 | 13.4 / 13.2 | 26.1 / 22.1 | 47.4 | 20.5 | 12.0 | 62 | 7.5 | True |
| a1 | 100 | 53.5 / 52.8 | 53.2 | -10.7 | 37.7 / 42.4 | 134.2 / 120.0 | 173.7 | 72.3 | 29.9 | 85 | 16.9 | True |
| a2 | 50 | 56.5 / 57.3 | 56.9 | -0.0 | 11.9 / 12.1 | 19.1 / 23.1 | 42.4 | 19.4 | 10.8 | 62 | 7.5 | True |
| a2 | 100 | 52.8 / 53.0 | 52.9 | -0.3 | 31.8 / 30.1 | 104.2 / 95.6 | 137.8 | 59.4 | 21.0 | 82 | 16.4 | True |
| a3 | 50 | 53.1 / 52.7 | 52.9 | -4.0 | 11.3 / 11.3 | 19.6 / 22.0 | 44.0 | 18.8 | 10.3 | 64 | 7.1 | True |
| a3 | 100 | 49.5 / 49.3 | 49.4 | -3.5 | 23.1 / 19.7 | 83.0 / 82.8 | 120.0 | 41.7 | 15.5 | 74 | 13.2 | True |

Every run (`reports/perf/e-sprint-a/c<i>-<variant>.json`; `p [95% CI]`; ingress = client send -> generation request at
the mock; decision_ms = S1 - t_d0):

| run | streams | n | turns/s | TTFT ovh p50 [CI] | p95 [CI] | p99 | e2e ovh p50 | p95 | ingress p50 | decision_ms p50/p95 | api lag p99 | api CPU (cores) | CPU ms/turn | load mean/max | valid | errors |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| c1-base | 50 | 856 | 7.13 | 14.2 [13.8, 14.4] | 29.3 [26.6, 31.6] | 49.9 [38.8, 57.0] | 22.5 [22.1, 23.0] | 51.2 [40.6, 66.2] | 12.4 | 75.0 [75.0, 76.0]/103.2 [100.0, 105.5] | 12.8 | 0.487 | 68.3 | 8.7/11.2 | yes | 0 |
| c1-base | 100 | 1714 | 14.28 | 46.2 [44.0, 48.6] | 122.2 [111.4, 126.8] | 173.3 [153.4, 184.7] | 92.3 [90.0, 95.3] | 181.1 [174.8, 189.5] | 34.2 | 105.0 [104.0, 107.0]/209.0 [203.3, 214.0] | 27.1 | 0.910 | 63.7 | 6.2/7.1 | yes | 0 |
| c2-a1 | 50 | 856 | 7.13 | 13.4 [13.2, 13.6] | 26.1 [22.9, 40.0] | 53.1 [47.0, 59.7] | 20.7 [20.5, 20.9] | 36.8 [33.5, 46.4] | 12.1 | 62.0 [62.0, 62.0]/86.8 [75.2, 92.2] | 7.6 | 0.410 | 57.5 | 6.3/9.4 | yes | 0 |
| c2-a1 | 100 | 1722 | 14.35 | 37.7 [35.1, 40.4] | 134.2 [124.8, 142.1] | 187.0 [174.6, 219.2] | 67.0 [62.2, 72.5] | 219.5 [206.0, 229.3] | 27.7 | 83.0 [81.0, 84.0]/176.0 [170.0, 185.9] | 17.1 | 0.768 | 53.5 | 5.5/6.2 | yes | 0 |
| c3-a2 | 50 | 855 | 7.12 | 11.9 [11.7, 12.0] | 19.1 [18.3, 20.1] | 40.9 [30.1, 43.9] | 19.1 [19.0, 19.3] | 30.5 [27.1, 32.6] | 10.7 | 62.0 [62.0, 62.0]/75.3 [71.0, 84.0] | 7.4 | 0.402 | 56.5 | 5.3/6.6 | yes | 0 |
| c3-a2 | 100 | 1721 | 14.34 | 31.8 [29.4, 33.6] | 104.2 [96.0, 111.8] | 150.0 [142.6, 159.6] | 61.4 [55.9, 66.7] | 179.9 [174.8, 187.1] | 20.9 | 83.0 [81.0, 85.0]/175.0 [166.0, 183.0] | 16.2 | 0.757 | 52.8 | 5.0/5.7 | yes | 0 |
| c4-a3 | 50 | 855 | 7.12 | 11.3 [11.1, 11.5] | 19.6 [17.9, 26.1] | 43.8 [40.4, 49.2] | 18.8 [18.6, 19.0] | 32.0 [27.5, 36.2] | 10.3 | 64.0 [63.0, 64.0]/71.0 [69.3, 74.3] | 7.3 | 0.378 | 53.1 | 3.9/5.7 | yes | 0 |
| c4-a3 | 100 | 1730 | 14.42 | 23.1 [22.4, 24.1] | 83.0 [76.5, 90.8] | 122.1 [114.3, 138.3] | 46.0 [44.1, 48.4] | 129.6 [125.8, 138.1] | 16.6 | 76.0 [75.0, 77.0]/146.0 [140.1, 154.0] | 13.7 | 0.714 | 49.5 | 4.9/5.5 | yes | 0 |
| c5-a3 | 50 | 855 | 7.12 | 11.3 [11.1, 11.4] | 22.0 [19.9, 30.6] | 44.3 [41.8, 46.1] | 18.8 [18.7, 19.0] | 37.1 [31.5, 43.9] | 10.3 | 64.0 [63.0, 64.0]/71.0 [69.0, 74.0] | 7.0 | 0.375 | 52.7 | 3.9/4.9 | yes | 0 |
| c5-a3 | 100 | 1734 | 14.45 | 19.7 [19.1, 20.4] | 82.8 [73.7, 89.0] | 118.0 [109.8, 129.7] | 37.5 [36.3, 38.8] | 112.3 [105.7, 119.2] | 14.5 | 72.0 [72.0, 73.0]/149.0 [144.0, 156.0] | 12.8 | 0.712 | 49.3 | 5.3/6.0 | yes | 0 |
| c6-a2 | 50 | 855 | 7.12 | 12.1 [11.9, 12.2] | 23.1 [21.4, 25.1] | 43.8 [37.6, 48.2] | 19.6 [19.4, 19.8] | 35.5 [31.6, 40.7] | 10.9 | 63.0 [62.0, 63.0]/76.6 [71.0, 86.3] | 7.6 | 0.408 | 57.3 | 6.4/7.6 | yes | 0 |
| c6-a2 | 100 | 1717 | 14.31 | 30.1 [28.5, 32.6] | 95.6 [91.0, 99.5] | 125.7 [118.7, 136.4] | 57.5 [52.9, 63.6] | 165.3 [158.9, 170.7] | 21.0 | 82.0 [80.0, 84.0]/168.0 [159.0, 172.0] | 16.6 | 0.758 | 53.0 | 5.3/7.1 | yes | 0 |
| c7-a1 | 50 | 855 | 7.12 | 13.2 [13.0, 13.3] | 22.1 [21.0, 23.7] | 41.8 [34.7, 47.2] | 20.3 [20.2, 20.5] | 29.5 [27.8, 31.7] | 11.9 | 62.0 [62.0, 62.0]/72.0 [68.0, 76.3] | 7.5 | 0.401 | 56.3 | 5.9/7.1 | yes | 0 |
| c7-a1 | 100 | 1718 | 14.32 | 42.4 [39.8, 45.7] | 120.0 [113.6, 129.2] | 160.4 [151.8, 168.4] | 77.5 [72.8, 82.9] | 194.4 [187.2, 200.2] | 32.1 | 86.5 [85.0, 89.0]/164.0 [160.0, 170.3] | 16.8 | 0.756 | 52.8 | 5.5/8.1 | yes | 0 |
| c8-base | 50 | 855 | 7.12 | 13.9 [13.7, 14.2] | 30.5 [28.3, 35.0] | 54.6 [47.7, 59.2] | 21.9 [21.6, 22.2] | 55.9 [44.0, 63.0] | 12.3 | 75.0 [75.0, 75.0]/106.3 [95.3, 112.0] | 12.7 | 0.476 | 66.9 | 6.5/7.3 | yes | 0 |
| c8-base | 100 | 1712 | 14.27 | 57.1 [55.1, 59.4] | 134.3 [127.9, 139.6] | 171.8 [158.2, 187.7] | 109.8 [106.5, 113.4] | 192.3 [187.7, 195.5] | 44.2 | 115.0 [112.0, 117.0]/223.0 [218.0, 230.0] | 30.6 | 0.912 | 63.9 | 5.1/5.4 | yes | 0 |

### 4.1 The final code (a4 = a3 + the member dispatch order kept; d1-d3, ABA, a quieter host: loadavg 3)

a3's member fast path let calls without a possible backup start one task hop earlier than hedged ones, so the
DeepSeek members (Mantle, with backups) dispatched about 5 ms after the Converse ones (2-stream trace) and S1 came
2-3 ms later at 50 streams (`decision_ms` p50 64 vs 62). a4 adds the hop back as one checkpoint (no task): the
dispatch order is the base's again (members start in coordinator order; trace g2), `decision_ms` p50 59-60 vs a3's 63
in the same conditions; CPU and TTFT unchanged within noise.

| run | streams | n | turns/s | TTFT ovh p50 [CI] | p95 [CI] | p99 | e2e ovh p50 | p95 | ingress p50 | decision_ms p50/p95 | api lag p99 | api CPU (cores) | CPU ms/turn | load mean/max | valid | errors |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| d1-a4 | 50 | 855 | 7.12 | 10.4 [10.3, 10.5] | 19.5 [17.8, 25.2] | 39.9 [38.5, 40.9] | 17.3 [17.2, 17.4] | 31.2 [26.9, 39.4] | 9.6 | 60.0 [60.0, 60.0]/68.0 [65.3, 71.0] | 6.9 | 0.364 | 51.1 | 2.9/3.6 | yes | 0 |
| d1-a4 | 100 | 1738 | 14.48 | 18.9 [18.4, 19.5] | 68.7 [63.1, 75.8] | 105.9 [98.0, 114.7] | 37.0 [35.6, 38.1] | 112.4 [104.6, 118.1] | 13.7 | 71.0 [70.0, 72.0]/139.2 [134.0, 147.0] | 12.2 | 0.705 | 48.7 | 3.0/3.6 | yes | 0 |
| d2-a3 | 50 | 855 | 7.12 | 10.3 [10.2, 10.4] | 18.4 [17.0, 20.9] | 41.9 [38.5, 46.1] | 17.3 [17.1, 17.4] | 27.7 [24.3, 33.6] | 9.5 | 63.0 [63.0, 63.0]/69.0 [67.0, 74.0] | 6.8 | 0.362 | 50.8 | 3.2/3.8 | yes | 0 |
| d2-a3 | 100 | 1735 | 14.46 | 18.0 [17.4, 18.5] | 64.2 [57.4, 75.1] | 96.7 [92.5, 102.4] | 33.0 [31.9, 34.4] | 99.1 [95.9, 103.0] | 13.6 | 70.0 [70.0, 71.0]/142.0 [133.0, 148.0] | 11.5 | 0.685 | 47.4 | 3.2/3.4 | yes | 0 |
| d3-a4 | 50 | 855 | 7.12 | 10.1 [10.0, 10.2] | 17.2 [16.5, 18.5] | 40.1 [37.7, 46.2] | 16.8 [16.7, 16.9] | 21.4 [20.2, 22.7] | 9.2 | 59.0 [59.0, 59.0]/63.3 [62.3, 64.0] | 7.1 | 0.355 | 49.9 | 2.9/3.4 | yes | 0 |
| d3-a4 | 100 | 1738 | 14.48 | 18.3 [17.7, 18.9] | 67.8 [60.0, 75.4] | 102.1 [96.4, 111.2] | 33.7 [32.4, 35.0] | 101.8 [97.4, 107.8] | 13.5 | 69.0 [68.0, 70.0]/139.0 [134.0, 144.2] | 12.6 | 0.696 | 48.1 | 2.9/3.9 | yes | 0 |

## 5. Where the CPU went (CPU-time profiles at 50 streams, back to back: d5 base, d6 a4; SIGPROF every 1 ms of CPU)

Inclusive shares and the ms of CPU per turn they stand for (profiler on: about +2-4% CPU; host loadavg 3):

| Frame (inclusive) | base share | base ms/turn | final share | final ms/turn |
|---|---|---|---|---|
| decision member calls | 26.4% | 17.1 | 16.5% | 8.4 |
| Converse complete() | 15.6% | 10.1 | 11.7% | 6.0 |
| Mantle complete() | 9.6% | 6.2 | 3.7% | 1.9 |
| aiobotocore _make_api_call | 14.4% | 9.4 | 0.0% | 0.0 |
| httpx send | 5.7% | 3.7 | 0.0% | 0.0 |
| lean unary transport | 0.0% | 0.0 | 5.5% | 2.8 |
| SigV4 signing (lean) | 0.1% | 0.0 | 3.9% | 2.0 |
| generation stream events | 12.3% | 8.0 | 9.9% | 5.1 |
| aiobotocore event stream | 9.1% | 5.9 | 0.0% | 0.0 |
| ConverseStream reader (`_ConverseRaw.next`) | 9.2% | 5.9 | 6.5% | 3.3 |
| decision entry | 1.7% | 1.1 | 2.3% | 1.2 |
| createTurn ingress (route) | 2.8% | 1.9 | 3.7% | 1.9 |
| Postgres (asyncpg Connection) | 0.8% | 0.5 | 0.8% | 0.4 |
| Redis event mirror | 8.0% | 5.2 | 11.6% | 5.9 |
| SSE writer | 6.3% | 4.1 | 8.7% | 4.4 |
| OTel spans | 3.8% | 2.5 | 4.7% | 2.4 |
| asyncio task bookkeeping (WeakSet._remove) | 3.8% | 2.5 | 2.7% | 1.4 |
| total (profiled run) | 100% | 65.0 | 100% | 50.9 |

- The member calls went from 17.1 to 8.4 ms per turn: aiobotocore's `_make_api_call` (9.4 ms) and httpx's `send`
  (3.7 ms) are gone; the lean transport is 2.8 ms, SigV4Auth 2.0 ms (now the largest single piece, section 8.3).
- The generation stream's reader went from 5.9 to 3.3 ms per turn (~100 events): aiobotocore's async event stream
  is gone; what remains is botocore's `EventStreamBuffer` (1.6 ms) and aiohttp's `readany` (1.2 ms; section 8.4).
- Sprint B's areas (mirror, SSE writer, spans) did not change in these snapshots; their larger shares are the same
  milliseconds in a smaller total.

## 6. The serial floor (createTurn tracer at 2 streams, back to back: g1 base, g2 a4; median of 11 turns)

| Step (ms after the ingress entry) | base | a4 |
|---|---|---|
| Postgres `execute` round trips per turn (BEGIN, set_config, COMMIT, reset; whole turn) | 18 | 10 |
| units of work before the generation request | 3 | 1 |
| step 12 (`turn.start`) begins | 5.03 | 3.57 |
| decision entry (`decision.enter`) begins / its duration | 5.85 / 1.42 | 4.34 / 1.49 |
| the Sol stream request leaves (`stream.open`) | 9.19 | 7.96 |
| first Mantle / Converse member call starts | 11.20 / 11.78 | 8.97 / 9.30 |
| a Mantle member call's duration (the mock answers in 50 ms) | 63.6 | 51.1 |
| TTFT overhead p50 [CI] (`results.json`) | 11.50 [11.28, 11.68] | 9.55 [9.37, 9.83] |

The fold takes 1.5 ms off the ingress (8 round trips), the generation request leaves 1.2 ms earlier, and the
overhead floor is 2.0 ms lower. A Mantle member call took 63.6 ms through httpx for the mock's 50 ms (not analyzed
further) and 51.1 ms through the lean path, which is part of S1's earlier arrival (`decision_ms` p50 75 -> 59-64 ms
at 50 streams).


## 7. Ramp to 200 streams: the knee (base vs a3; `f-base-ramp`, `f-a3-ramp`; 120 s windows, labeled shortened)

Plan `125:120,150:120,175:120,200:120` after a 60 s warm-up, 30 s settles; one fresh api process each.

| run | streams | n | turns/s | TTFT ovh p50 [CI] | p95 [CI] | p99 | e2e ovh p50 | p95 | ingress p50 | decision_ms p50/p95 | api lag p99 | api CPU (cores) | CPU ms/turn | load mean/max | valid | errors |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| base-ramp | 125 | 1758 | 14.65 | 1282.3 [1276.0, 1292.5] | 1655.6 [1640.6, 1692.9] | 1845.8 [1822.3, 1854.9] | 1604.2 [1595.7, 1617.4] | 2245.7 [2204.2, 2279.2] | 742.2 | 1045.0 [1042.0, 1048.0]/1165.3 [1159.0, 1172.2] | 174.6 | 1.000 | 68.3 | 6.2/8.2 | no | 0 |
| base-ramp | 150 | 1805 | 15.04 | 2060.3 [2051.9, 2069.4] | 2456.0 [2432.0, 2485.0] | 2654.5 [2596.0, 2719.7] | 3049.6 [3036.9, 3066.6] | 3548.6 [3521.6, 3582.9] | 1416.6 | 1114.0 [1110.0, 1119.0]/1280.8 [1265.0, 1305.6] | 250.7 | 0.997 | 66.3 | 4.0/4.9 | no | 0 |
| base-ramp | 175 | 1857 | 15.47 | 2975.3 [2959.9, 2997.7] | 3758.8 [3686.5, 3799.5] | 4152.6 [4076.5, 4325.7] | 4211.5 [4186.9, 4239.0] | 5108.1 [5034.9, 5185.3] | 2275.5 | 1183.0 [1178.0, 1187.0]/1422.2 [1399.0, 1444.0] | 275.0 | 1.000 | 64.6 | 4.5/5.2 | no | {'500:internal_error': 1} |
| base-ramp | 200 | 1850 | 15.42 | 3989.6 [3945.7, 4040.3] | 5528.8 [5423.3, 5650.2] | 6333.0 [6049.2, 6470.0] | 5276.9 [5238.0, 5338.0] | 7070.7 [7006.5, 7162.9] | 3299.4 | 1185.0 [1179.0, 1192.0]/1479.5 [1447.0, 1511.0] | 297.0 | 1.000 | 64.9 | 4.3/4.9 | no | {'500:internal_error': 202, '409:session_busy': 4} |
| a3-ramp | 125 | 2142 | 17.85 | 36.2 [34.8, 37.5] | 126.5 [121.8, 132.4] | 191.1 [171.2, 199.6] | 77.3 [74.0, 80.4] | 213.6 [202.9, 219.5] | 24.3 | 86.0 [84.0, 87.0]/190.9 [185.9, 198.9] | 32.7 | 0.879 | 49.2 | 7.7/9.3 | yes | 0 |
| a3-ramp | 150 | 2524 | 21.03 | 94.3 [91.4, 97.2] | 254.0 [248.4, 260.3] | 305.0 [295.0, 313.2] | 200.9 [197.7, 205.0] | 486.4 [455.0, 554.3] | 70.9 | 136.0 [134.0, 137.5]/295.0 [283.0, 302.9] | 128.4 | 0.994 | 47.3 | 9.3/10.7 | no | 0 |
| a3-ramp | 175 | 2461 | 20.51 | 886.0 [878.1, 894.1] | 1311.4 [1296.4, 1324.5] | 1493.7 [1457.2, 1530.0] | 1602.0 [1583.4, 1612.3] | 2261.3 [2203.2, 2333.4] | 569.3 | 709.0 [701.0, 716.0]/976.0 [960.0, 993.0] | 240.6 | 0.999 | 48.7 | 7.7/8.4 | no | 0 |
| a3-ramp | 200 | 2407 | 20.06 | 1826.9 [1817.1, 1836.3] | 2117.6 [2090.2, 2151.7] | 2327.1 [2293.5, 2358.8] | 3023.6 [3009.7, 3038.2] | 3357.1 [3337.3, 3377.4] | 1118.2 | 1191.0 [1186.0, 1194.0]/1416.0 [1394.7, 1434.0] | 317.2 | 1.000 | 49.9 | 6.4/7.2 | no | 0 |

Base errors: 1 x 500 at 175 streams, 202 x 500 and 4 x 409 at 200 (pool and Redis timeouts past the knee, as in the
c2b ramp); a3: none at any level. Validity: base fails the holdback rule from 125 streams on (67-86%
`holdback_deadline`: saturated, S1 after the first text); a3 is valid at 125 streams (100% `holdback_ms = 0` with
`spec_agrees`, every driver and mock lag p99 <= 2 ms), at 150 it misses the 99.9% rule (98.4%: the knee) and from
175 on it is saturated.

- **Capacity of one process: about 15.5 -> 21.0 turns/s (+35%)**; the lag target (C2, p99 < 50 ms) now holds at 125
  streams (32.7 ms; base 174.6 ms there, base's knee is below 125 in this placement), not at 150 (128 ms).
- **CPU per turn at the knee: 64-68 ms (base) -> 47-50 ms (a3)**, so 200 streams of this workload need about
  200 / 7 s x 48 ms = 1.37 cores: still more than one Python process, as c2b section 7.1 concluded; the 08 §9
  sizing (2 processes per 2-vCPU pod) stays the recommendation, with more headroom per process.
- Past the knee a3 degrades without errors (TTFT queueing, `holdback_deadline` commits), base with 500s.

## 8. What is left (measured, not changed here: other owners' code, or not small)

1. **Orchestrator: the generation attempt starts after the decision entry.** In the 2-stream trace the decision
   run's entry takes 1.5 ms (its L2 `GET` waits behind other ready tasks), then the Sol stream request leaves about
   2 ms after it (section 6). Starting A1 before (or concurrently with) `decisions.run().__aenter__` would take
   about 1.5-2 ms off the overhead floor (`turns/runner.py`, orchestrator).
2. **db: asyncpg's reset on release.** Every unit of work still costs `BEGIN`, `set_config`, `COMMIT` and the pool's
   reset query (`SELECT pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; RESET ALL`) on release, 4 of the 10
   Postgres round trips of a turn (trace: 10 `execute` per turn, about 0.16 ms each at 2 streams, more under load).
   The app sets only transaction-local settings (`set_config(..., true)`), takes no session advisory locks and LISTENs
   on dedicated connections, so a pool `reset=` that skips the query for an idle connection would save one round trip
   per unit of work (`db/engine.py` `create_pool`, db owner's call; not measured here).
3. **SigV4 signing** (`botocore.auth.SigV4Auth` on an `AWSRequest`: email-message headers, `urlsplit`) is now the
   largest part of a member call's CPU: 3.9% of the turn at 50 streams (profile d6: 2.0 ms per turn for 10 Converse
   calls and the stream, about 180 µs each under the profiler, 65 µs in a micro-benchmark). A dict-based signer byte-compared against SigV4Auth (the test of 3.4 exists)
   would cut most of it; kept on SigV4Auth as the sprint asked.
4. **ConverseStream decoding**: botocore's `EventStreamBuffer` (Python header parsing + 2 CRC32s per message) is 3.2%
   of the turn (profile d6: 1.6 ms per turn, about 15 µs per event); a decoder on `memoryview` / `struct.unpack_from` with the same `ParserError`
   types could halve it.
5. **Authentication's per-request Redis check of the per-IP auth-failure bucket** (one `GET`/script call per request
   before any verification, spec-09 §2). Caching it locally would let a blocked IP through on this pod for the cache
   period: a security trade-off for the identity owner, not a perf fix.
6. **`DISPATCH_YIELDS` = 6** was tuned for member calls of ~1.1 ms of synchronous CPU (render, botocore, SigV4); a
   member call now costs about 0.3 ms, so the 90 extra task steps per turn buy less. An A/B (`--dispatch-yields 0|3|6`)
   on the final code is the decision owner's follow-up.
7. **sprint B's areas** (not touched): the Redis event mirror (10.8% inclusive at 50 streams), the SSE writer path
   (8.1%), OTel spans (4.8%), asyncio task bookkeeping (`WeakSet._remove`, GC of task objects).
8. **Tooling**: `tests/load/trace.py` wraps `TurnService.authorize` / `prepare`; the folded createTurn path calls
   `TurnService.create`, so those two labels are absent from its timelines (qa-tools; the Postgres and Redis round
   trips are still recorded).

## 9. Defects found (in this role's areas: fixed here, with their tests)

| Module | Defect (pre-existing, reproduced on the aiobotocore reference) | Fix and test |
|---|---|---|
| adapters | A 2xx Converse body that is not a Converse response (empty, HTML, not JSON) became an empty `end_turn` answer (a `stream: false` turn would commit empty text); a wrong-shaped JSON one a fatal `AttributeError`; a wrong-shaped answer escaped `complete()` as a raw `TypeError` (not an `AdapterError`). Repro: `test_lean_converse_parse.py` cases `empty_body`, `html_body`, `json_list`, `content_object` on the reference client | `malformed:response` (retryable, as Mantle): `test_lean_converse_parse.py::NOT_CONVERSE`, `test_complete.py::test_a_converse_answer_of_the_wrong_shape_is_retryable_malformed` |
| adapters | A ConverseStream payload of the wrong shape (`{"delta": 7}`, a JSON list) surfaced botocore's parser `AttributeError` as a fatal `ErrorEvent`, against F8 (`malformed:chunk`). Repro: `test_lean_converse_stream.py` cases `delta_not_an_object`, `payload_a_list` on the reference | `malformed:chunk`: `test_a_payload_the_parser_cannot_take_is_malformed_chunk_not_a_fatal` |
| adapters | aiobotocore retried a Converse call whose response body failed after its headers (`ClientPayloadError` -> `ConnectionClosedError`, a connect error) although IMPL-016 / spec-02 §4.1 allow the one retry only before any response byte | `ResponseStreamingError`, never retried: `test_unary_mapping.py::test_a_body_read_failure_is_a_streaming_error_never_retried`, `test_unary_transport.py::test_a_truncated_body_is_a_streaming_error_never_a_connect_error` |
| adapters | aiobotocore's aiohttp session followed up to 10 HTTP redirects with the signed tenant request (botocore never does) | `allow_redirects=False`: `test_unary_transport.py::test_keep_alive_reuse_no_redirects_identity_encoding` |

Outside this role's areas: none found by these runs (section 8 lists improvement opportunities, not defects).

## 10. Reproduce

Stack: `NOEMA_PG_PORT=29732 NOEMA_MOTO_PORT=29750 NOEMA_VALKEY_PORT=29779 docker compose -p noema-perf-a -f
deploy/compose/docker-compose.yml up -d --wait postgres valkey moto`, `python -m tools.bootstrap_dev
--compose-project noema-perf-a --state ~/.noema/stacks/noema-perf-a.json`, `python -m tests.load.stack activate
--state ~/.noema/stacks/noema-perf-a.json`. Snapshots: `git archive HEAD | tar -x -C /tmp/perfa/base`, then
`/tmp/perfa/tools/snap.sh <variant> <files>` (+ `/tmp/perfa/patches/route_create.py`). A run:
`/tmp/perfa/tools/run.sh /tmp/perfa/<variant> <name> --plan 50:120,100:120 --results /tmp/perfa/results/<name>.json`
(= `tests.load.run` with this report's placement, ports 2978x/2979x, the loadavg gate, the snapshot's `PYTHONPATH`).
Campaign: `/tmp/perfa/tools/campaign.sh`; tables: `/tmp/perfa/tools/abba.py`, `table.py`; traces:
`trace_summary.py`; profile children: `children.py`. Summaries copied to `reports/perf/e-sprint-a/`.
