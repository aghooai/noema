# E3 perf sprint: CPU per turn and nearly free shedding (perf-cpu-shed)

Wave E3, role perf-cpu-shed, 2026-10-12 (04:50-10:50 UTC). Scope (brief): `src/noema/adapters/**` (not
`resilience/**`), `src/noema/api/**` (the overload/shed path), `src/noema/db/engine.py` (pool reset on release),
their tests. No live Bedrock call, no AWS resource (moto and the wire mock only), no git write. Raw data:
`/tmp/noema-e3cpu/runs/<run>` on the build host; the analyzed `results.json` of every run cited here:
`reports/perf/e3-perf-cpu-shed/<run>.json` (each with `provenance` and `config`). Gate view:
[reports/qa/e3/perf-cpu-shed.md](../qa/e3/perf-cpu-shed.md).

## 1. Verdict

| Item | Result |
|---|---|
| (1) dict-based SigV4 signer | the header list (names, values, order) = `SigV4Auth.add_auth`'s on every Converse/ConverseStream golden x 6 regions x with/without a token and **2,000 fuzzed requests**; the whole request = aiobotocore's (60 cases, unchanged test). **73.1 -> 16.9 us per call**; in the 100-streams/pod profile **1.49 -> 0.58 ms per turn** |
| (2) eventstream decoder | messages and the ending exception = botocore `EventStreamBuffer`'s on the 21 equivalence-suite bodies, 16 wire-mock streams (incl. the L1 stream) and **1,800 fuzzed streams** (corrupt, truncated, inconsistent, every header type), each whole, in 1, 7, 64 bytes and split at random; the reader = aiobotocore's (84 cases, unchanged). Decoding **6.48 -> 1.63 us per message**, the reader 8.67 -> 3.51 us per event; in the profile the reader **3.27 -> 2.12 ms per turn** |
| (3) shedding, C5 | the per-process cap at the edge (before the body read, authentication and the middleware stack; tenant from the key cache): **16.5 vs 187 us per refused request** (unit-test app; ~10 us under load); the share is max-min fair; a tenant saturated by its own admission limits waits at most 12 turns per process, not counted against others; the 429 is the route's, byte for byte. **C5: base 1.309 / 1.253 / 1.165 (FAIL x3) -> 1.078 [1.069, 1.087] / 1.079 [1.069, 1.096] (PASS x2)**, 0 victim errors (base: 2 in one run); with the GC sprint's code too: 1.032 |
| (4) asyncpg pool reset | the app pool releases without the reset query (`reset_on_release=False`); a released connection carries no `app.tenant_id` / `app.project_id` after commit, rollback, cancel, savepoint and system calls (same backend, query logger, source scan); **240 -> 207 us of client CPU per unit of work** and one round trip less on the ingress path |
| CPU per turn (ABAB, 2-process pod) | **100 streams/pod 46.95 -> 44.70 ms (-4.8%), 200 streams/pod 45.75 -> 43.65 ms (-4.6%)**; the profile attributes -2.1 ms per turn to the changed paths |
| TTFT overhead | 100/pod p50 10.52 / 10.27 -> 10.02 / 9.94 ms (upper CIs 10.07 / 10.02: L1's 10 ms bound just missed on this role's code alone), p95 16.4 -> 14.5-14.6 ms; 200/pod p50 14.94 / 14.38 -> 14.31 / 13.63 ms, p95 47-52 -> 48 ms (GC-bound) |
| floor (TTFT overhead p50) | 2 streams 9.25 / 8.97 -> 8.88 / 8.79 ms; 4 streams 8.93 -> 8.74 ms |
| regression | `make check` all green on the working tree; `make e2e` 52/52 three times on this role's own pinned stack (images from the snapshots) |

## 2. Method

- **Stack**: compose project `noema-e3cpu-perf` (postgres 32632, moto 32650, valkey 32679; api 32690, mocks
  32681/32682), seeded by `tools/bootstrap_dev.py`, then `python -m tests.load.stack activate` twice: load config v2
  (`sha256:b475dc16...`, the E2 QA lead's) and **v3 = v2 + t_beta `concurrent_streams_per_project` 40**
  (`sha256:916f53ef...`, C5's flooder limit; every run here uses v3, which changes nothing for t_acme).
- **Placement** = the QA lead's E2 placement (as the parallel perf-gc-objects agent): one 2-vCPU pod of **2 api
  processes on CPUs 46-47** (cores 22-23, sibling CPUs idle), Postgres + Valkey on 44-45, 8 mock processes (Converse
  16-19, Mantle 40-43), drivers on 4-15 and 28-39 (`tools/run4.sh`).
- **Quiet host, serialized with the other perf agent**: a run starts only when no other `tests.load.run` process is
  alive and the 1-minute loadavg is < 6 (< 4 for the runs before 08:10; the other agent's runner used < 6; polled
  every 2 s; the process match is anchored at the interpreter, so shell command lines that merely mention the
  module do not count); every run's `runs/<run>.monitor` records foreign load processes every 10 s (0 in every
  sample of every run cited). The 1-minute loadavg during the windows was 2.1-7.5 (means per window in the tables;
  every one < 8). This role's test suites, image builds and e2e stack ran pinned to CPUs 0-3,24-27, outside the
  placement.
- **Code under test, isolated from the parallel sprint**: snapshots `snap-base` = `git archive wave-e2` (1a28933,
  the tree make check is green on), `snap-e3a` = `snap-base` + this role's files at 07:20 (Wave E's share rule),
  `snap-e3b` = `snap-base` + exactly this role's final 19 files (`SNAPSHOT.txt`; the tree's files differ from it by
  one docstring), run with `PYTHONPATH=<snap>/src` from the snapshot directory; `provenance.code.tree_digest` is a sha256 over the
  snapshot's code paths (`tests/load/run.py` patched to hash files instead of asking git).
- **Microbenchmarks**: one pinned idle core (`taskset -c 2`, outside the load placement), `time.process_time`,
  best of 5-7 repetitions, before (`snap-base` on `PYTHONPATH`) and after (`snap-e3a`/`e3b`, same code for these
  paths); scripts in
  `reports/perf/e3-perf-cpu-shed/bench/`.
- **Quantities** as spec-10 §5: TTFT overhead = client TTFT - mock TTFT (bootstrap 95% CIs), CPU per turn = api pod
  CPU (utime + stime, `/proc`) / turns per second, window validity (holdback 0 with `spec_agrees` >= 99.9%, every
  driver and mock process lag p99 <= 2 ms). C5: victim client TTFT p95 of window 1 / window 0 with the bootstrap CI
  of the ratio (the QA lead's `ratio.py`, 1,000 independent resamples).

## 3. The changes, each with its equivalence evidence and microbenchmark

### 3.1 A dict-based SigV4 signer for the lean Converse path (`adapters/_sigv4.py`, `_converse_wire.signed_headers`)

`sign_post` computes exactly what `botocore.auth.SigV4Auth.add_auth` adds to a `POST` with a bytes body and one
`Content-Type` header, without `AWSRequest`, `HTTPHeaders` (an `email.message`) and the per-call `urlsplit`:
`X-Amz-Date`, `X-Amz-Security-Token` (when the credentials carry one), the canonical request (the URL path through
botocore's own `normalize_url_path` then `quote(safe="/~")`, so the percent-encoded model id is encoded again; the
query sorted as botocore sorts it; the four signed headers lower-cased, sorted, whitespace collapsed; `host` from
the URL as `_host_from_url` derives it: lower case, IPv6 in brackets, the port unless the scheme's default; the
payload's SHA-256), the string to sign, the HMAC key chain and the `Authorization` header, in botocore's header order.
The per-URL parts are cached (`lru_cache`, 4,096 URLs: a few per region and model). The SDK's unsigned headers are
unchanged. The signing clock is `_sigv4.utcnow` (UTC, as botocore's `get_current_datetime`; the tests freeze both).

| Evidence | Cases |
|---|---|
| `tests/unit/adapters/test_sigv4.py`: the header list (names, values, order) = SigV4Auth's | every Converse and ConverseStream golden x 6 regions (incl. `cn-north-1`, `us-gov-west-1`) x with/without a session token; **2,000 fuzzed requests** (4 seeds x 500): 10 regions incl. unknown ones, ARN, `:`, `..`, `.`, `a/../b` and random model ids from 90 characters (unicode, `%`, `+`, `?`, `#`, spaces), endpoint overrides (ports 80/443/8443/9000, upper-case host, IPv6, a path prefix, query strings with repeated keys), session tokens (none, empty, 1-1,200 chars, embedded whitespace), empty / binary / JSON bodies, timestamps over 20 years, two service names |
| `test_lean_converse_wire.py` (unchanged, E sprint A) | the whole request (URL, body, every signed header, `SignedHeaders`) = aiobotocore's for every golden, both operations, 6 regions, ARN and special-character ids, no token, an endpoint override (60 cases) |
| `tests/integration/adapters/test_unary_transport.py` | the path on the wire is the signed path for `:` and ARN ids, and the server-side recomputed signature matches (clock frozen for both signers) |
| mutation check | dropping the token's whitespace collapse, the path normalization or the default-port rule each fails the fuzz test |

Microbenchmark (`bench/sigv4_bench.py`: `signed_headers` for the decision golden `haiku_verbalized_effort_low`,
1,033-byte body, best of 7 x 5,000): **73.1 -> 16.9 us per call** (-77%); of the 16.9 us, the signature is 12.4 (five
HMAC-SHA256s 6.2, `strftime` 1.8, the body hash 0.9) and the SDK's `uuid4` invocation id 3.4. A turn signs ~11
Converse requests (10 decision members on Converse + the stream): ~0.6 ms of CPU per turn off by the
microbenchmark; under load the profile at 100 streams/pod shows **1.49 -> 0.58 ms per turn** (section 5.3: botocore's
per-call `AWSRequest` and `email.message` objects cost more there than in a tight loop).

### 3.2 A faster ConverseStream eventstream decoder (`adapters/_eventstream.py`, `converse._ConverseRaw`)

`EventStreamDecoder` yields the (headers, payload) messages botocore's `EventStreamBuffer` yields for the same bytes,
however they are chunked. A well-formed message (prelude CRC, lengths within botocore's limits, `total_length >=
headers_length + 16`, message CRC, string headers) is decoded with `struct.unpack_from` and `zlib.crc32` over a
`memoryview` on one bytes buffer with a read position, instead of botocore's slice-and-copy per field and its
`MessagePrelude` / `EventStreamMessage` objects; a header block already seen in the stream is not parsed again (a
per-stream cache of at most 16 blocks of <= 256 bytes). Both CRC checks and the limits are kept, and checked at the
same points (the prelude as soon as its 12 bytes arrived, the message once complete). Any other header block
(another header type, a truncated or repeated header, an undecodable name) is parsed by botocore's own
`EventStreamHeaderParser` (its result or its exception); a corrupt CRC or an over-long or inconsistent prelude hands
the unread bytes to a botocore `EventStreamBuffer` for the rest of the stream. So the decoder raises exactly
botocore's exceptions (`ChecksumMismatch`, `InvalidHeadersLength`, `InvalidPayloadLength`, `DuplicateHeader`,
`struct.error`, `KeyError`, `UnicodeDecodeError`) at the same message, and `classify` sees the same failures.
`parse_event` takes (headers, payload) and builds the response dict as `EventStreamMessage.to_response_dict` did.

| Evidence | Cases |
|---|---|
| `tests/unit/adapters/test_eventstream.py`: messages + the ending exception (type, args, and whether the next call raises it again) = botocore's | the 21 stream bodies of the lean ConverseStream equivalence suite (`test_lean_converse_stream.CASES`: text, reasoning, refusals, unknown events, every exception type, an error message, corrupt CRC, truncation, ...) x 5 chunkings (whole, 1, 7, 64 bytes, random splits); **1,800 fuzzed streams** x 5 chunkings (1-8 frames with every header type and unknown ones, repeated and undecodable names, bit flips, truncations, garbage, inconsistent totals and header lengths with valid CRCs, preludes over both limits): 2,953 messages, outcomes ChecksumMismatch 320, UnicodeDecodeError 310, KeyError 288, DuplicateHeader 114, InvalidHeadersLength 107, InvalidPayloadLength 98, struct.error 69, none 494; 8 named edge frames |
| `tests/unit/adapters/test_eventstream_mock.py` | the wire mock's ConverseStream bodies (in-process, virtual clock) for 16 scenarios: the L1 load stream (`overhead.yaml`, 104 messages), Claude / GLM / OpenAI-family reasoning, padded and multi-byte chunks, the 5 declared exception types, corrupt message and prelude CRCs, truncated JSON, refusal, reasoning-only; each in the chunks the socket delivered, whole, 1, 7, 64 bytes and random splits; then the full reader (`_ConverseRaw` -> `feed`) yields the same events with either decoder |
| `test_lean_converse_stream.py` (unchanged, E sprint A) | the reader against the aiobotocore client: 21 bodies x 4 chunkings (84 cases) still equal |
| conformance and integration | the adapter conformance kit over `@mockbedrock` sockets, `test_against_mockbedrock.py` (streams, faults, timeouts, cancellation) |
| mutation check | a payload off by one byte, no duplicate-name check, no name bound, yielding one byte early, no header-length limit: each fails the tests; skipping the prelude CRC check fails one (the error then surfaces once the whole message arrived, not at its 12th byte); dropping the `total >= headers + 16` guard hangs the fuzz (a zero-length message never advances) |

Microbenchmarks (L1 stream body: messageStart, 100 text deltas, contentBlockStop, messageStop, metadata = 104 messages):

| Measure (best of 7) | botocore (`snap-base`) | `EventStreamDecoder` (`snap-e3a`) |
|---|---|---|
| decode only, one frame per chunk (`bench/es_decode_bench.py`), us per message | 6.48 | **1.63** (-75%) |
| the whole reader per event: `_ConverseRaw.next()` + `feed`, one frame per chunk (`bench/es_bench.py`) | 8.67 | **3.51** (-60%) |
| the same, 4 KiB chunks | 8.23 | **3.27** |

Per turn (104 events): 0.90 -> 0.37 ms of CPU for the reader by the microbenchmark; under load the profile at 100
streams/pod shows the decoding **1.73 -> 0.51 ms** and the whole reader **3.27 -> 2.12 ms per turn** (section 5.3).

### 3.3 Nearly free shedding and the per-process cap policy (`api/shed.py`, `api/overload.py`, `api/app.py`, `api/routes/capacity.py`, `api/routes/turns.py`)

**Where the flood's CPU went** (C5 base: CPU profile of both api processes in the flood window, `c5-base-prof`,
12,585 samples; the counts from `c5-base-0`): the flooder (t_beta, 200 streams against its limit of 40 concurrent) sent ~65 refused createTurns
per second per pod, of two kinds: ~41/s **admission `queue_timeout`s** (each ran the full ingress, the session unit
of work, then waited 2 s in the fair queue for its own full slot set; admission alone was 6.8% of the samples,
`TurnService.create` 10.2%) and ~23/s **per-process-cap sheds** (authentication with its per-IP Redis check 1.8%,
the access log 1.8% of which the log record's redaction is 60%, the gate, the envelope). The queued flood turns also
held 50 of each process's 120 slots and their objects: the full GCs in the flood window took 135-140 ms (p50, max
185-247 ms) every 7-8 s against 51-61 ms every 10-14 s without the flood (`api-*.gc`), and the victim's 2 x 429 in
`c5-base-0` were cap sheds (its uneven split put it above its share of 60 on one process while the flood's waiting
turns held the rest).

**The policy** (`TurnSlots`; documented in its docstring and here):

| Rule | Behaviour |
|---|---|
| Slots | one per running turn, per admitted createTurn not yet running, per createDecision / facade request in flight; cap C = `NOEMA_MAX_TURNS_PER_PROCESS` (120), burst B = max(1, C // 10) (12) |
| **Fair share (max-min, new)** | each tenant's share S is the max-min fair share of C among the tenants holding slots plus the caller (`fair_share`, water filling: tenants holding less than an equal split keep what they hold, the rest is split equally among the others). Below S a caller is admitted while the process holds < C + B; at or above S while < C. A lone tenant may use all of C; a newcomer always gets in (B of headroom); freed slots go to the tenants below their share; at most C + B held. Wave E used S = C // A (A = tenants holding slots plus the caller), which treated a tenant holding a few slots as if it held a full split, so a tenant holding more than C // A was refused although the others left it room |
| **Saturated tenants (new)** | a tenant one of whose turns timed out in the admission queue on this process is saturated for `SATURATED_S` (2 s) after the latest such timeout. While saturated it holds at most B waiting createTurn slots (admitted, not yet running); further createTurns of it are refused at once. Those waiting turns do not count against the other tenants (they wait for the tenant's own limits, not for this process: the process holds at most C + B plus B per saturated tenant). Its running turns, its decisions and every other tenant are unaffected. So a tenant flooding past its own limits cannot fill the cap with turns that wait for its own limit (most of which would time out after 2 s of work) |
| Refusal | 429 `queue_timeout` (`rate_limit_error`, retryable), `retry-after: 1`, `x-request-id`; `aghoo_turns_shed_total` |
| **Edge shed (new)** | before the middleware stack (`NoemaApp.__call__` -> `shed`), for unkeyed `POST /v1/turns`, `/v1/decisions`, `/openai/v1/decisions`, only while the process holds >= C slots or (createTurn) a tenant is saturated: the caller's tenant from the authenticator's in-process key cache (the `Authorization` key id; no Postgres, no Redis, no secret check) -> `TurnSlots.sheds_early`, the same decision `try_acquire` would make. A refusal is the route's 429 byte for byte, with one `edge shed` log line per second at most instead of an access line per request |
| Precedence | a key id the cache does not hold (unknown, malformed, not used here for 60 s) always gets the full ingress (its 401, or the route's shed); a cached key id is not verified at the edge, so over the cap a wrong secret for it is 429 rather than 401 and the gate's 400/413/415 are not reached; keyed requests (`Idempotency-Key`) are never shed at the edge (step 7 decides); the route's check stays authoritative; off with the test-mode response validator |

| Evidence | Cases |
|---|---|
| `tests/unit/api/test_edge_shed.py` (13) | an over-cap createTurn gets the route's 429 **byte for byte** (status, raw header list, body) without authentication; the three capped operations are shed at the edge; below the cap, an uncached key, a keyed request, no key, a tenant below its share all reach authentication; the edge decides as `try_acquire` would (3,000 random slot states, 4 caps, up to 4 tenants, unknown callers never shed); the saturation bound (burst pending turns, decisions and other tenants unaffected, expiry, no cap = no bound); a real admission `queue_timeout` saturates the tenant and its next createTurn is shed at the edge |
| `tests/unit/api/test_overload.py` (+2) | the max-min share (water-filling levels; the C5 situations: a tenant below its max-min share admitted within the burst, one at it refused at the cap) and a saturated tenant's waiting turns not counted against the others; unchanged: the route's shed, the keyed duplicate attaching at the cap, decisions and the facade under the cap, the newcomer admitted at once; all with the response validator on (contract-checked envelopes) |
| load drivers | every 429 of every C5 run validated by the driver's `ContractValidator` (0 violations) |

Microbenchmark (`bench/shed_bench.py`, the unit-test app with the fake authenticator, direct ASGI calls, best of
7 x 2,000): a refused createTurn costs **16.5 us at the edge vs 187.2 us through the route** (the fake app has no
Redis auth check and no JSON log formatter: the route figure is a lower bound; production's was ~0.7 ms, E sprint B).

### 3.4 asyncpg: no reset round trip on release for the app pool (`db/engine.py`)

asyncpg resets every released connection with one more query (`SELECT pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN
*; RESET ALL`). On the `noema_app` pool it has nothing to undo: every unit of work is one transaction whose RLS scope
is `set_config(..., true)` (transaction-local), the system calls are single SECURITY DEFINER calls (their own
transactions; no session setting), advisory locks are transaction-level (`pg_advisory_xact_lock`), LISTEN runs on
dedicated unpooled connections (`ops/notify_bridge/listener.py`, `connect()`), nothing declares a holdable cursor;
the only session setting in the code (`noema.operator_key`, the CLI's operator proof) is on the CLI's unpooled
connection. So `app_pool_settings` sets `PoolSettings.reset_on_release=False` and the pool's `reset` is a no-op;
asyncpg still rolls back a transaction left open (a cancelled block) and drops listener callbacks before reuse.
Every other pool (CLI, owner, the test fixtures' default `PoolSettings`) keeps the full reset.

| Evidence | Cases |
|---|---|
| `tests/integration/db/test_pool_reset.py` (8) | on the production settings with one connection (the same backend pid every time): `app.tenant_id` / `app.project_id` are '' or unset after a committed unit of work, a rolled-back one, a cancelled one (mid-statement), a nested savepoint that set the project, and a system call; a query logger proves the app pool sends no reset query and the default pool does; a source scan fails on any session-level state outside the two unpooled modules (`set_config(..., false)`, a SQL `SET` without `LOCAL`, `add_listener`, `pg_(try_)advisory_lock(`, `WITH HOLD`) |
| `tests/integration/db/test_uow.py` (unchanged) | transaction-local settings, commit / rollback, system calls without tenant scope |
| e2e | the compose stack runs `app_pool_settings` (52/52, section 6) |

Microbenchmark (`bench/pool_bench.py`, this stack's Postgres, 4 connections, 20 concurrent workers x 500 units of
work of BEGIN + set_config + SELECT + COMMIT, 4 alternations, best): **240.0 -> 206.7 us of client CPU per unit of
work** (-33 us, -14%) and one Postgres round trip less per unit of work. That round trip was on the serial path: the
ingress unit of work's release is awaited before admission and the generation request (section 5.2).

## 4. C5 fairness before and after

`run4.sh <snap> <run> --plan 100:300,100:300 --warmup-s 60 --settle-s 60 --side flood:t_beta:200:1` (the QA
lead's gated form): the victim t_acme runs 100 streams per pod in both 300 s windows; window 1 adds t_beta at 200
streams = 5x its `concurrent_streams_per_project` (40); a shed turn retries after `retry-after` on its session.
Pass: victim client TTFT p95 <= 1.10 x solo (bootstrap 95% CI of the ratio). `base` = wave-e2; `e3a` = all this
role's changes with Wave E's share rule (`cap // tenants`); `e3b` = the final code (max-min share, waiting turns of
a saturated tenant not counted against others); `comb` = the perf-gc-objects agent's latest snapshot
(`/tmp/noema-e3gc/snap-c5`: task-free `deadline_scope`, fewer tasks and objects per turn) plus this role's `e3b`
files, i.e. the two sprints together (a diagnostic of the combination; the QA lead gates the final tree).
Columns: victim TTFT overhead / ingress / decision p95 under the flood (ms); pod CPU (cores) solo ->
flood; full-GC p50 per process under the flood (ms); the flooder's 200 / 429 / transport errors; admission
`queue_timeout`s / `aghoo_turns_shed_total` (both processes, end of window 1); 1-minute loadavg mean per window.

| Run | victim p95 solo -> flood (ms) | ratio [95% CI] | victim errors | ovh p95 | ingress p95 | decision p95 | pod CPU | full GC p50 | flooder 200 / 429 / err | adm. timeouts / sheds | load | valid |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| c5-base-0 (05:49, raw data overwritten: `c5-base-0.summary.txt`) | 317.9 -> 416.1 | **1.309** [1.293, 1.323] | 2 | 115.5 | - | - | 0.69 -> 1.15 | 135 / 140 | 2,117 / 23,254 / 0 | 14,744 / 8,443 | 4.6 / 5.0 | yes |
| c5-base-1 | 317.1 -> 397.4 | **1.253** [1.239, 1.270] | 0 | 97.0 | 80.6 | 148 | 0.67 -> 1.12 | 124 / 134 | 2,116 / 24,453 / 0 | 14,201 / 10,181 | 4.0 / 4.9 | yes |
| c5-base-2 | 316.9 -> 369.3 | **1.165** [1.144, 1.186] | 0 | 68.7 | 54.9 | 141 | 0.68 -> 1.14 | 132 / 133 | 2,117 / 23,649 / 0 | 14,559 / 9,005 | 6.1 / 7.5 | yes |
| c5-e3a-1 | 316.8 -> 384.8 | **1.214** [1.207, 1.227] | 7 | 83.5 | 72.6 | 137 | 0.65 -> 1.01 | 96 / 109 | 2,106 / 43,086 / 3 | 6,030 / 37,023 | 4.5 / 4.3 | yes |
| c5-e3b-1 | 317.2 -> 342.0 | **1.078** [1.069, 1.087] | 0 | 40.1 | 30.1 | 96 | 0.66 -> 1.02 | 111 / 112 | 2,109 / 43,204 / 2 | 5,735 / 37,453 | 6.4 / 5.9 | yes |
| c5-e3b-2 | 314.9 -> 339.8 | **1.079** [1.069, 1.096] | 0 | 37.5 | 25.6 | 90 | 0.66 -> 1.02 | 101 / 116 | 2,104 / 42,848 / 2 | 5,980 / 36,842 | 5.6 / 4.9 | yes |
| c5-comb-1 | 314.1 -> 324.0 | **1.032** [1.026, 1.036] | 0 | 23.2 | 19.1 | 77 | 0.61 -> 0.94 | 59 / 55 | 2,109 / 43,013 / 4 | 5,947 / 37,034 | 4.0 / 5.9 | yes |

- **Before (wave-e2): C5 fails in all 3 runs, 1.165-1.309** (the QA lead's E2 spread was 1.06-1.29). **After (`e3b`):
  passes twice, 1.078 [1.069, 1.087] and 1.079 [1.069, 1.096]**, 0 victim errors; with the GC sprint's code as
  well (`comb`): 1.032 [1.026, 1.036]. Every window valid; loadavg means 4.0-7.5.
- Where it came from: the flood's admission `queue_timeout`s fell from ~14,200-14,700 to ~5,700-6,000 per run
  (saturated tenants wait at most 12 turns per process; the rest are refused at the edge in ~10 us: the
  `c5-e3a-prof` profile has 171 samples in `api/shed.py` for ~17,800 edge sheds), the flood's other refusals moved
  from the route to the edge (the flooder now retries faster: ~43,000 429s per run instead of ~24,000, at ~1.5% of
  the pod's samples in total); pod CPU under the flood 1.12-1.15 -> 1.02 cores; the victim's ingress p95 55-81 ->
  26-30 ms and decision p95 141-148 -> 90-96 ms. Full-GC pauses under the flood are still 101-116 ms (p50) on this
  role's code alone (124-140 ms before; 55-59 ms with the GC sprint's code, which is what takes `comb` to 1.03).
- `e3a` (this role's changes with Wave E's share rule) is the A/B of the share rule: 7 victim turns were refused
  (429) because the victim, split unevenly between the processes, held more than `cap // 2` on one process while the
  flood's waiting turns filled the rest; the max-min share and not counting a saturated tenant's waiting turns
  against others (`e3b`) removed that, and its window 1 also had the flood landing mostly on one process (31,884 of
  37,023 sheds on one of the two processes), which concentrates the flood's cost on half of the victim's turns.
- The flooder's 2-4 transport errors per run (`ReadError` before any response byte, 2-4 of ~45,000 requests; none
  for the victim, none before) are the HTTP keep-alive close race: the driver's httpx pool and uvicorn both expire
  idle connections after 5 s, and the edge's immediate 429s leave more pooled connections idle near 5 s. An early
  response before the body is read keeps keep-alive intact (`bench/early_response.py`: uvicorn + httptools, 3,000
  pooled POSTs with 0 transport errors; 200 raw connections whose body arrives after the 429, each answering a
  second request: 400/400). Finding for qa-tools: a driver `keepalive_expiry` below the server's 5 s avoids it.
- `c5-e3b-2`'s first ~2 minutes overlapped this role's `make check` (pinned to CPUs 0-3,24-27, outside the
  placement; its integration step uses the shared `noema-dev` Postgres, unpinned); its solo window is in line
  with the others (victim p95 314.9 ms, overhead p95 14.9 ms), i.e. not inflated (which would have lowered the ratio).

## 5. CPU per turn and the floor (L1 workload, one 2-process pod)

### 5.1 CPU per turn and TTFT overhead at 100 and 200 streams per pod (ABAB)

`--plan 100:180,200:180 --warmup-s 45 --settle-s 30`, `base` vs `e3b` interleaved (base-1, e3b-1, base-2, e3b-2).

| run | streams | n | turns/s | TTFT ovh p50 [CI] | p95 [CI] | p99 | e2e ovh p50 / p95 | ingress p50 | api lag p99 | pod CPU | ms/turn | load | valid | errors |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cpu-base-1 | 100 | 2,601 | 14.45 | 10.52 [10.43, 10.60] | 16.42 [15.88, 17.04] | 30.3 | 17.7 / 26.8 | 9.6 | 5.6 | 0.688 | 47.6 | 4.2 | yes | 0 |
| cpu-base-1 | 200 | 5,186 | 28.81 | 14.94 [14.79, 15.18] | 52.23 [49.48, 56.25] | 129.6 | 25.8 / 94.1 | 12.7 | 8.3 | 1.335 | 46.3 | 4.8 | yes | 0 |
| cpu-e3b-1 | 100 | 2,602 | 14.46 | 10.02 [9.97, 10.07] | 14.50 [14.01, 15.17] | 40.7 | 17.1 / 22.6 | 9.2 | 4.5 | 0.648 | 44.8 | 3.7 | yes | 0 |
| cpu-e3b-1 | 200 | 5,191 | 28.84 | 14.31 [14.15, 14.49] | 47.72 [45.20, 50.92] | 120.2 | 24.3 / 90.8 | 12.2 | 7.1 | 1.269 | 44.0 | 5.6 | yes | 0 |
| cpu-base-2 | 100 | 2,602 | 14.46 | 10.27 [10.22, 10.33] | 16.32 [15.74, 16.94] | 52.9 | 17.4 / 27.0 | 9.4 | 5.3 | 0.670 | 46.3 | 5.2 | yes | 0 |
| cpu-base-2 | 200 | 5,191 | 28.84 | 14.38 [14.25, 14.52] | 47.37 [43.50, 51.08] | 126.8 | 24.7 / 87.6 | 12.3 | 7.9 | 1.304 | 45.2 | 4.7 | yes | 0 |
| cpu-e3b-2 | 100 | 2,603 | 14.46 | 9.94 [9.88, 10.02] | 14.63 [14.12, 15.00] | 41.8 | 16.9 / 23.3 | 9.2 | 4.4 | 0.645 | 44.6 | 4.9 | yes | 0 |
| cpu-e3b-2 | 200 | 5,190 | 28.83 | 13.63 [13.49, 13.78] | 48.20 [45.33, 51.47] | 121.3 | 22.7 / 93.4 | 11.8 | 6.7 | 1.248 | 43.3 | 4.8 | yes | 0 |

- **CPU per turn: 100 streams/pod 47.6 / 46.3 -> 44.8 / 44.6 ms (means 46.95 -> 44.70, -2.25 ms, -4.8%); 200
  streams/pod 46.3 / 45.2 -> 44.0 / 43.3 ms (45.75 -> 43.65, -2.1 ms, -4.6%)**, both pairs ordered the same way
  (every e3b window below every base window); the CPU profiles attribute -2.1 ms per turn to the changed paths
  (section 5.3).
- TTFT overhead at 100 streams/pod: p50 10.52 / 10.27 -> 10.02 / 9.94 ms, p95 16.42 / 16.32 -> 14.50 / 14.63 ms;
  at 200: p50 14.94 / 14.38 -> 14.31 / 13.63 ms, p95 52.2 / 47.4 -> 47.7 / 48.2 ms (the p95 at 200 is set by the
  full-GC pauses, the parallel GC sprint's subject). api lag p99 5.6 / 5.3 -> 4.5 / 4.4 ms (100) and 8.3 / 7.9 ->
  7.1 / 6.7 ms (200). Every window valid; loadavg means 3.7-5.6. 0 errors, no 429 at either density.

### 5.2 The floor (2 streams per pod)

`--plan 2:150 --warmup-s 30` ABAB (43 turns per window), then one pair at 4 streams, `--plan 4:300 --warmup-s 30` (174 turns).

| run | streams | n | turns/s | TTFT ovh p50 [CI] | p95 [CI] | p99 | e2e ovh p50 / p95 | ingress p50 | api lag p99 | pod CPU | ms/turn | load | valid | errors |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| floor-base-1 | 2 | 43 | 0.29 | 9.25 [9.06, 9.54] | 11.53 [10.63, 12.43] | 12.3 | 16.1 / 18.2 | 8.7 | 0.8 | 0.033 | 113.8 | 2.7 | yes | 0 |
| floor-e3b-1 | 2 | 43 | 0.29 | 8.88 [8.60, 9.01] | 10.51 [9.37, 22.24] | 18.1 | 15.5 / 17.4 | 8.3 | 0.8 | 0.031 | 106.9 | 2.6 | yes | 0 |
| floor-base-2 | 2 | 43 | 0.29 | 8.97 [8.77, 9.08] | 10.21 [9.60, 10.95] | 10.8 | 15.5 / 17.1 | 8.3 | 0.8 | 0.031 | 106.9 | 2.1 | yes | 0 |
| floor-e3b-2 | 2 | 43 | 0.29 | 8.79 [8.69, 8.92] | 10.38 [9.44, 23.21] | 19.0 | 15.3 / 17.8 | 8.2 | 0.8 | 0.031 | 106.9 | 2.7 | yes | 0 |
| floor4-base-1 | 4 | 174 | 0.58 | 8.93 [8.83, 9.02] | 10.69 [10.00, 12.16] | 18.3 | 15.6 / 17.5 | 8.4 | 0.9 | 0.053 | 91.4 | 2.2 | yes | 0 |
| floor4-e3b-1 | 4 | 174 | 0.58 | 8.74 [8.62, 8.86] | 11.48 [10.32, 17.28] | 24.5 | 15.3 / 21.2 | 8.2 | 0.9 | 0.050 | 86.2 | 2.9 | yes | 0 |

- **The floor (TTFT overhead p50) is 0.2-0.4 ms lower in every pair**: 2 streams 9.25 / 8.97 -> 8.88 / 8.79 ms, 4
  streams 8.93 [8.83, 9.02] -> 8.74 [8.62, 8.86] ms; ingress p50 8.7 / 8.3 / 8.4 -> 8.3 / 8.2 / 8.2 ms. That is the
  reset round trip the ingress unit of work's release no longer awaits before admission and the generation request,
  plus ~0.06 ms of signing for the stream request. (CPU per turn at 2-4 streams is the processes' background work
  spread over few turns, not a per-turn cost.)

### 5.3 Where the CPU went (CPU-time profiles at 100 streams per pod, `prof-base-100` vs `prof-e3b-100`)

`--plan 100:120 --warmup-s 45 --profile cpu --profile-step 0` (SIGPROF CPU-time sampler in both api processes, every
1 ms of CPU; the profiler adds ~2-4% of CPU; ms per turn = share x the window's pod CPU x 1000 / turns per second):

| Path (inclusive) | base share | base ms/turn | e3b share | e3b ms/turn |
|---|---|---|---|---|
| SigV4 signing (`signed_headers`) | 3.16% | 1.49 | 1.28% | 0.58 |
|   of which botocore SigV4Auth / AWSRequest | 2.81% | 1.32 | 0.00% | 0.00 |
|   of which `_sigv4.sign_post` | 0.00% | 0.00 | 0.90% | 0.41 |
| ConverseStream reader (`_ConverseRaw.next`) | 6.96% | 3.27 | 4.68% | 2.12 |
|   of which eventstream decoding (botocore / `_eventstream`) | 3.67% | 1.73 | 1.13% | 0.51 |
|   of which `parse_event` | 0.93% | 0.44 | 0.85% | 0.39 |
| asyncpg release (`PoolConnectionHolder.release`) | 0.15% | 0.07 | 0.15% | 0.07 |
|   of which the reset query (`Connection.reset`) | 0.12% | 0.05 | 0.00% | 0.00 |
| decision member calls (`MemberCallRunner`) | 15.96% | 7.51 | 14.75% | 6.69 |
| ingress (`IngressMiddleware.__call__`) | 5.59% | 2.63 | 6.21% | 2.82 |
| total (profiled step) | 100% (7,819 samples) | 47.1 | 100% (7,408 samples) | 45.4 |

The windows themselves (profiler on):

| run | streams | n | turns/s | TTFT ovh p50 [CI] | p95 [CI] | p99 | e2e ovh p50 / p95 | ingress p50 | api lag p99 | pod CPU | ms/turn | load | valid | errors |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| prof-base-100 | 100 | 1,732 | 14.43 | 10.43 [10.34, 10.51] | 16.93 [16.33, 17.33] | 28.7 | 17.5 / 23.6 | 9.6 | 5.4 | 0.679 | 47.1 | 3.4 | yes | 0 |
| prof-e3b-100 | 100 | 1,733 | 14.44 | 10.29 [10.20, 10.39] | 15.36 [14.78, 16.14] | 20.5 | 17.1 / 23.5 | 9.4 | 4.5 | 0.655 | 45.4 | 3.5 | yes | 0 |

- The changed paths account for **-2.1 ms per turn** (signing -0.91; the stream reader -1.15: decoding -1.22,
  the rest within noise; the reset query -0.05; the profiled totals: 47.1 -> 45.4), the load A/B's -2.1 to -2.25 ms (5.1). It is more than
  the microbenchmarks' ~1.2 ms: under load the botocore paths also paid cache misses and the collection of their
  per-call objects (an `AWSRequest` with `email.message` headers per signed call: `email/` was 0.55% of the samples,
  `botocore/awsrequest.py` 0.27% and `botocore/utils.py` 0.56%; now 0, 0 and 0.08% (the signer's cached URL parts); a `MessagePrelude` and an
  `EventStreamMessage` and the field slices per event).
- The decision member calls fall 7.51 -> 6.69 ms per turn (the signer: 10 of the 11 signed calls are members).

## 6. Regression and checks

| Check | Result |
|---|---|
| `make check` on the working tree (all three E3 roles' edits; 09:13-09:19 UTC, pinned to CPUs 0-3,24-27) | **all green**: lint, typecheck (327 files), typecheck-tests (590), importlint (27 contracts kept), forbidden-scan, ownership, contracts-check, unit 3,444 passed, conformance 757 passed / 8 skipped, integration 573 passed / 21 skipped, sdk-check |
| `make check` again at the end (10:34-10:45 UTC, the final tree; every file of this role identical to `snap-final`) | first run: 1 failure outside this role's paths, `tests/integration/media_pipeline/test_pg_s3_redis.py::test_upload_process_watch_download_and_purge` (`partial` instead of `ready`; passed 3 of 3 alone); second run: **all green** (unit 3,444, conformance 757 / 8 skipped, integration 573 / 21 skipped, sdk-check) |
| unit + conformance on `snap-e3b` (wave-e2 + this role's files only) | 4,151 passed, 8 skipped |
| integration on `snap-e3b` (shared `noema-dev` services) | 568 passed, 22 skipped |
| `make e2e` on this role's own stack (`noema-e3cpu-e2e`, port base 32100, every container pinned to CPUs 0-3,24-27 by a compose override), images built from the snapshots (`noema-api:e3cpu*`, `noema-worker:e3cpu*`; media `noema-media:qae2`, unchanged code) | `snap-e3a`: **52 passed** (154 s); `snap-final` (= `snap-e3b` + one docstring): **52 passed** (152.9 s), **52 passed** (153.6 s) |
| new tests | `test_sigv4.py` 17, `test_eventstream.py` 36, `test_eventstream_mock.py` 32, `test_edge_shed.py` 13, `test_overload.py` +2, `tests/integration/db/test_pool_reset.py` 8 |

## 7. Findings for other owners (measured; not in this role's paths)

- **Spec text (spec owner / misc-fixes, docs/impl-spec)**: spec-11 §7 ("after authentication and the request gate")
  and spec-04 §1 step 5 ("Then the per-process cap") describe the cap's place before Wave E3. Proposed text: "The
  per-process cap is checked twice: at the edge, before the middleware stack, for a caller whose key id the
  in-process key cache holds (its tenant unverified there; a key id the cache does not hold always gets the full
  ingress, so its 401 keeps its precedence; over the cap a wrong secret for a cached key id is 429), and in the
  route after authentication and the request gate. The share is max-min fair; a tenant whose turn timed out in the
  admission queue is saturated for 2 s and holds at most cap ÷ 10 waiting turns per process, which do not count
  against other tenants." The `edge shed` summary log line (at most one per second) replaces the access line of
  edge-refused requests.
- **telemetry (`telemetry/logs.py` `RedactingFilter`)**: an access log line costs ~0.15-0.25 ms of CPU, 60% of it
  the redaction of the record (C5 base profile: `_log_access` 1.8% of the api's samples at ~85 requests per second
  per pod). Every request pays it; a cheaper redaction of the fixed access-line format would help every gate.
- **qa-tools (`tests/load/driver.py`)**: httpx's `keepalive_expiry` (5 s) equals uvicorn's `timeout_keep_alive`
  (5 s), so a pooled connection can be reused just as the server closes it (2-4 `ReadError`s per C5 run on the
  flooder, section 4); a driver expiry of e.g. 4 s avoids the race.
- **misc-fixes (R8) asked for an explicit cause argument in `adapters/_base.py`** (their report, R8): not done here
  (the frozen `RetryBudget.try_spend` signature; no behavior change needed).
- **Still in this role's paths, not done**: a signing-key cache (5 HMACs, ~5 us per call) and a per-second
  timestamp cache (~2 us) would take the signer from 12 to ~6 us; not done (a derived-key cache keyed by the secret
  needs its own security review for a gain of ~0.06 ms per turn).

## 8. Reproduce

```
# the stack (once): base services of project noema-e3cpu-perf on 326xx, bootstrap, load config v2, then v3 (C5)
env NOEMA_PG_PORT=32632 NOEMA_MOTO_PORT=32650 NOEMA_VALKEY_PORT=32679 NOEMA_API1_PORT=0 NOEMA_API2_PORT=0 \
  NOEMA_API1_METRICS_PORT=0 NOEMA_API2_METRICS_PORT=0 NOEMA_API1_METRICS2_PORT=0 NOEMA_API2_METRICS2_PORT=0 \
  NOEMA_MOCKBEDROCK_PORT=0 NOEMA_FAULT_TOXIPROXY_PORT=0 NOEMA_FAULT_PG_PORT=0 NOEMA_FAULT_VALKEY_PORT=0 \
  NOEMA_FAULT_MOTO_PORT=0 docker compose -p noema-e3cpu-perf -f deploy/compose/docker-compose.yml up -d --wait postgres valkey moto
UV_NO_SYNC=1 uv run --frozen --no-sync python -m tools.bootstrap_dev --compose-project noema-e3cpu-perf \
  --state ~/.noema/stacks/noema-e3cpu-perf.json
UV_NO_SYNC=1 uv run --frozen --no-sync python -m tests.load.stack activate --state ~/.noema/stacks/noema-e3cpu-perf.json
UV_NO_SYNC=1 uv run --frozen --no-sync python -m tests.load.stack activate --state ~/.noema/stacks/noema-e3cpu-perf.json \
  --limit t_beta:concurrent_streams_per_project=40
# snapshots and runs (tools/ in reports/perf/e3-perf-cpu-shed/): snap.sh <name>; queue.sh <file> of
# "<run> <snapshot> <run.py args>" lines, each through run4.sh (placement, serialization, loadavg < 6)
# C5:  --plan 100:300,100:300 --warmup-s 60 --settle-s 60 --side flood:t_beta:200:1   (ratio.py <run> 0:1)
# CPU: --plan 100:180,200:180 --warmup-s 45 --settle-s 30;  floor: --plan 2:150 --warmup-s 30
# profiles: --profile cpu --profile-step <k>   (inclusive.py <run> <frame patterns...>)
# microbenchmarks: taskset -c 2 python bench/<name>.py  with PYTHONPATH=<snapshot>/src:<snapshot>
# e2e on own pinned stack, images from a snapshot (docker buildx builder pinned to 0-3,24-27):
#   make e2e E2E_PROJECT=noema-e3cpu-e2e E2E_PORT_BASE=32100 E2E_BUILD=0 \
#     COMPOSE_FILE="deploy/compose/docker-compose.yml -f tools/cpuset.override.yml" (+ NOEMA_*_IMAGE)
```
