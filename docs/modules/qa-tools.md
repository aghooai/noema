# qa-tools: the wire-level Bedrock mock (`tools/mockbedrock/`)

Wave C1, role qa-tools (spec-10 §2, IMPL-080). It is an HTTP server that speaks the Bedrock wire formats that
botocore/aiobotocore and the httpx/openai Mantle clients parse unchanged:
- Converse JSON;
- ConverseStream over AWS eventstream (binary framing, prelude and message CRC32);
- Mantle OpenAI-compatible chat completions, as JSON or SSE.

Scenarios in the frozen DSL (`tests/fakes/scenario.py`, spec-10 §2.4) drive the answers, the timing and the faults. The mock is test tooling only. Nothing in `src/noema` imports it, and `composition.py` does not wire it: tests and the compose stack point the adapters at it through the endpoint overrides (ADR-001 §8).

The author of this mock is not the adapter author. The wire format was implemented from spec-10 §2.1a, spec-02 §4.2, the botocore service model and the probes. Every frame is validated by decoding it with botocore's `EventStreamBuffer` and the real aiobotocore parser.

## Public surface

### Python (in-process; tests)
```python
from tools.mockbedrock import MockBedrock, ThreadedMockBedrock, VirtualClock, SystemClock, Quota

async with MockBedrock(scenario=doc) as mock:          # binds 127.0.0.1:0 on the running loop
    overrides = {"bedrock-runtime": mock.endpoint_url,  # http://127.0.0.1:<port>
                 "mantle": mock.mantle_url}             # http://127.0.0.1:<port>/v1
    mock.load_scenario(doc2); mock.release("barrier"); mock.reset()
    mock.requests(after=0, limit=None); mock.unmatched(); mock.set_quotas([{"model": m, "rpm": 10}])
```
- `MockBedrock(*, clock: MockClock | None = None, scenario: Scenario | str | bytes | Path | Mapping | None = None,
  record_limit: int = 50_000, record_bytes: int = 256 MiB, max_body: int = 64 MiB, region: str = "us-east-1")`:
  - `await start(host="127.0.0.1", port=0, *, reuse_port=False)` / `await stop()`, or `async with`; `reuse_port`
    (SO_REUSEPORT, C2b/D perf fixer) lets several mock processes listen on one port, the kernel spreading the
    connections over them (the load harness runs 4 processes behind the Converse and Mantle endpoints, spec-10 §5);
  - properties `port`, `endpoint_url`, `mantle_url`;
  - `region` is the only SigV4 region Converse accepts (see Auth below).
- `load_scenario` raises `tests.fakes.scenario.ScenarioError`. `set_quotas` raises `ValueError`. `requests(after, limit)`
  raises `ValueError` for a negative value and costs O(page).
- `ThreadedMockBedrock(host=, port=, scenario=, record_limit=, record_bytes=, region=)` runs the same server on its own asyncio loop in a daemon thread (`with ... as mock:`), with the same control methods forwarded thread-safely. Use it for synchronous clients (botocore, `openai.OpenAI`) or to keep the mock off the test's loop.
- `VirtualClock()` is a test clock: sleeps jump time forward and nothing waits, so the planned latencies can be asserted exactly from the records. `advance_ms(ms)` moves it, for `window_ms` and quota windows. `SystemClock` is the default and uses `time.monotonic_ns`, which every process on the host shares (spec-10 §5).

### CLI (compose service `mockbedrock`; ruling 4 of the wave-B handoff)
`python -m tools.mockbedrock --host 0.0.0.0 --port 8080 [--scenario FILE] [--record-limit N] [--record-bytes N]
[--region us-east-1] [--log-level info]`
- It runs on uvloop and needs only the runtime dependencies: `httptools`, `orjson`, `uvloop`, and `jsonschema`/`pyyaml` via the scenario loader. It imports neither `noema` nor botocore.
- It runs from the repo's `tools/` and `tests/fakes/`, mounted read-only.
- The first stdout line is `mockbedrock listening on http://<host>:<port>`, which gives the port when `--port 0` is used.
- SIGTERM and SIGINT stop it with exit 0.
- Verified in `noema-api:dev`: uid 10001, read-only root, `cap_drop ALL`. The compose healthcheck (`GET /_mock/requests` → 200) passes.
- **Healthcheck cost.** Use `GET /_mock/requests?limit=0` (O(1): `{"requests": [], "dropped", "last_seq"}`). The unpaged
  dump copies and serializes the whole ring on the event loop: with a full 50k ring of decision-sized records that is
  about 0.3-0.5 s, which stalls every stream and invalidates a load run (spec-10 §5 lag p99 ≤ 2 ms). The compose
  healthcheck still uses the unpaged form; `.orchestration/requests/qa-tools.md` item 3 asks infra to switch it.
  Measured with a full ring: `?limit=0` and a 100-record tail page each keep the loop lag under about 1 ms.

### HTTP endpoints
| Endpoint | Behavior |
|---|---|
| `POST /model/{modelId}/converse` | Converse JSON (spec-10 §2.1a); `modelId` is URL-decoded (ARNs; a raw `:` in the path is a signature mismatch, see Auth); `usage.outputTokens` is capped at the request's `maxTokens` (Wave E) |
| `POST /model/{modelId}/converse-stream` | `application/vnd.amazon.eventstream`, chunked; `messageStart`, reasoning deltas (block 0; Claude adds a `signature` delta; the OpenAI family, Sol and Luna, sends one `reasoningContent.redactedContent` delta instead, as live Sol does, Wave E), `contentBlockStop{0}`, text deltas (next block), `contentBlockStop`, `messageStop{stopReason}`, `metadata{usage, metrics}` (`outputTokens` capped at `maxTokens`); exception frames end the stream |
| `POST /v1/chat/completions` (also `/openai/v1/...`) | Mantle chat: JSON (`stream: false`, `logprobs`/`top_logprobs` ≤ 20; top-level `service_tier: "default"` as live, Wave E) or SSE (`data: <chunk>`, usage chunk only with `stream_options.include_usage`, `data: [DONE]`) |
| `POST /openai/v1/responses` | Luna (`openai.gpt-6-luna`, `openai.gpt-5.6-luna`) only, non-streaming, resolved with scenario op `chat`; `top_logprobs` → 400 "logprobs are not supported with reasoning models."; effort `minimal` → 400 |
| `POST /v1/responses` | 400 "The model '<m>' does not support the '/v1/responses' API" |
| `POST [/openai]/v1/decisions` | 400 "The requested model is not supported in the Decisions API." (every model) |
| `GET /_mock/requests?after=<seq>&limit=<n>` | `{"requests": [...], "dropped": n, "last_seq": n}`: at most `limit` records with seq > `after`, oldest first (record fields below); O(page); `limit=0` is the health probe; a non-integer or negative `after`/`limit` → 400 `{"errors": ["after and limit must be integers >= 0"]}` |
| `POST /_mock/scenario` (YAML or JSON body) / `GET /_mock/scenario` | load (200 `{"scenario", "rules"}`, 400 `{"errors": [...]}`) / the current document |
| `POST /_mock/barrier/{name}/release` | releases a barrier; held and future requests on it proceed |
| `POST /_mock/reset` | forget the scenario, the request log and the quotas (between tests) |
| `POST /_mock/quotas` `{"quotas": [{"model", "rpm"?, "tpm"?, "akid"?}]}` | replace the per-(AKID, model) Bedrock quotas (C1); `akid` omitted = each AKID gets its own window; `akid: "*"` = one window shared by every AKID (Wave E2, QL-4: a Bedrock quota is per account, and 2 api processes hold 2 STS sessions of one account) |

Any other path answers 404 with `x-amzn-ErrorType: UnknownOperationException`.

**Request records** (one per model request, added on arrival, so held requests are visible):
- `seq`, `method`, `path`, `op`, `model`;
- `headers`, with `authorization`, `x-amz-security-token` and `x-api-key` removed;
- `auth` {`scheme`, `akid`, `region`, `service`, `session_token`} and `akid`;
- `body`, parsed, with every `bytes` string replaced by `{"$bytes_len", "sha256"}`;
- `request_id`;
- `t_req_recv_ns` (CLOCK_MONOTONIC when the last body byte arrived) and `at_ms`;
- `nonce`, from `«nonce:<id>»` in the prompt;
- `rule`, `k` (the draw counter), `fault`, `barrier`;
- `status`, `error`;
- `t_first_byte_ns`, `t_first_text_sent_ns`, `t_done_ns`;
- `outcome`, one of `ok`, `rejected`, `fault`, `throttled_quota`, `unmatched`, `hang`, `reset` or `disconnected`.

The log is a ring bounded by count (`record_limit`) and by approximate retained bytes (`record_bytes`, default
256 MiB): each record is charged `record.approx_bytes` (string lengths plus 64 B per JSON node, plus 2 KiB for the
record itself), which measured at about 7.6 KB estimated vs 7.2 KB real for decision-sized requests. The oldest records
are evicted first and counted in `dropped`; the newest record is always kept. Prompt text is kept whole (the SE6-SE8
capture tests scan every prompt); only `bytes` blobs are replaced by their length and digest.

## Request pipeline (order matters; mirrors `tests/fakes/adapters.py`)
1. **Auth.**
   - Converse requires SigV4 (`AWS4-HMAC-SHA256 Credential=<AKID>/<date>/<region>/bedrock/aws4_request`) plus `X-Amz-Security-Token`. Otherwise it answers 403 `UnrecognizedClientException`.
   - The Converse credential scope must name the service `bedrock` and the mock's `region` (default `us-east-1`, spec-10 §2.2). Otherwise it answers 403 `InvalidSignatureException` with "Credential should be scoped to correct service: 'bedrock'." or "Credential should be scoped to a valid region, not '<region>'." (mock strings in the AWS wording).
   - The mock cannot verify signatures, but it rejects the one mismatch it can see (Wave E): a raw `:` in the Converse path. botocore always signs the model id with `:` as `%3A`, so a raw `:` means the client re-quoted the URL after signing; the answer is 403 `InvalidSignatureException` ("The request signature we calculated does not match ...").
   - Mantle requires the scenario's `mantle_auth`:
     - `bearer_short_term` is a token from `aws-bedrock-token-generator`, decoded offline to read the AKID and whether a session token is present;
     - `sigv4` means SigV4 plus a session token, with any signing service name accepted.
   - Without a scenario either Mantle mode is accepted. Long-term or unknown bearer keys always get 401 `authentication_error`.
   - Signatures are never verified. The AKID is recorded for tenant-isolation assertions.
2. **JSON parse**, then the **§2.2 fidelity rules** (`validate.py`, `models.py`). These run before any rule, so a violation fails even if a rule matches, and it does not consume `nth` or draw counters.
3. **Quota** (`/_mock/quotas`): a sliding 60 s window per (AKID, model), or per model for an `akid: "*"` quota, of requests and of tokens (`ceil(len(prompt)/4) + max_tokens`). A refusal answers 429 `ThrottlingException` and is not counted. Windows run on the absolute mock clock, so they stay valid when a scenario is reloaded (quotas survive loads; `reset` clears them).
4. **Scenario rule** (`ScenarioRuntime.resolve`). With no scenario or no matching rule the mock answers 500 `InternalServerException` (`mockbedrock: no scenario rule matches <model> <op>`), and the request shows in `unmatched()`, which is a test failure. The runtime's `requests` history (one `RequestInfo` with the prompt and the canonical wire body per request) is cleared after every resolve; the mock's own log replaces it.
5. **Barrier.** The request is held until it is released; if the client goes away first, the request is cancelled.
6. **Pre-byte faults.** These wait the call latency counted from the barrier release; on streams they wait the rule's `latency_ms`, or 0.
7. **Answer.**
   - Non-streaming: `completion()`, sent at the latency counted from the barrier release.
   - Streaming: `stream_plan()`, with each event at `t_req_recv + at_ms`. Frames due at the same instant are coalesced into one chunk.

**Determinism.** Every draw comes from the scenario runtime's `random.Random(f"{seed}|{model}|{op}|{sha256(body)}|{k}")`, where the body is the parsed request in canonical sorted-key JSON. Request ids, `pad` letters and Claude signatures come from the same draw. Results therefore repeat per seed and do not depend on the arrival order of different bodies.

**Fakes vs mock.** Rule matching, `nth`, `k`, `window_ms` and barriers mean the same on `tests/fakes/adapters.py` and on the mock, but the draw key does not: the fakes hash a ModelRequest-shaped body (`canonical_body`), the mock the canonical wire body. Sampled latencies, `rate` fault selection, request ids and `pad` letters therefore repeat per implementation but differ between the two. A scenario that must select the same requests on both (for example an F3/F12 rate-scoped fault ported from fake-based tests to compose) must use fixed `latency_ms`, `rate: 1.0`, `nth` or barriers.

**Latency defaults.** These are the probe table of `tests/fakes/scenario.py` (Wave E2: the call rows and the
low-effort stream rows follow the live smoke of 2026-10-11, `tests/cassettes/live-*`, which was faster than the
2026-10-09 probes; spec-10 §2.2's table predates it):
- DeepSeek: 300–560 ms (live 299–553, n 8), plus a 2% tail at 4–12 s (P11);
- Luna: 350–610 ms (live 353–601, n 13);
- Haiku: 640–950 ms (live 647–942, n 6), so a few percent, not ≥ 5%, pass the router's 1,000 ms deadline;
- stream first text: Sol low 630–1,250 (live, n 10), Sol above low 2,000–8,000 (live high 5,511, n 1); Opus low
  1,000–2,200 (live 1,078, P11 2,039), above low 2,000–11,600; GLM none 600–1,000, low 350–1,000 (live 358), above
  low 2,000–4,400.

Streams run at 60 tokens/s in 4-token chunks. Under probe defaults Opus, GLM and Sol stream reasoning deltas every
20 ms before the text at every effort except `none` and `low` (live: none of the three reasoned at `low`; Sol at
`high` sent one `redactedContent` delta). The planned reasoning never bills more than the request's `maxTokens`.

## Fidelity rules (spec-10 §2.2) and error texts
- **Probe strings (verbatim):**
  - temperature: Haiku, Opus and Sonnet get "The model returned the following errors: `temperature` is deprecated for this model."; Luna, Sol and Astra get "This model doesn't support the temperature field. Remove temperature and try again."; GLM and DeepSeek accept it.
  - effort `none` on Opus, Haiku and Sonnet: "unknown variant `none`, expected one of …".
  - effort `none` on Sol and `minimal` on Luna: the `unsupported_value` JSON. Sol's tail was truncated in the probe summary, so it is completed as "'low', 'medium', 'high', 'xhigh', and 'max'."
  - GLM accepts low…max including xhigh and rejects `none` with the live text (Wave E2; the 2026-10-09 probes had accepted it): "Unsupported value: 'none' is not supported with this model. Supported values are: …" (`param` `reasoning_effort`).
  - `additionalModelRequestFields.reasoning_effort` on OpenAI models: "Unknown parameter: 'reasoning_effort'.".
  - audio: "ContentBlock object at messages.i.content.j must set one of the following keys: …", with the per-family key list from the probes.
  - video on every model, images on DeepSeek and GLM, and documents on Luna, Sol, DeepSeek and GLM.
  - Mantle: Claude gets "does not support the '/v1/chat/completions' API"; Luna gets "model `…` isn't supported on this route"; an unknown model gets 404 "The model '…' does not exist".
- **Bedrock limits** (`validate.py`; the messages are mock strings):
  - ≤ 20 images, each ≤ 3,750,000 bytes and ≤ 8,000 px per side (read from the PNG, JPEG, GIF or WebP header; undecodable headers skip only the size rule);
  - ≤ 5 documents, each ≤ 4,500,000 bytes, with a text block in the same message;
  - the `DocumentBlock.name` rule;
  - images and documents only in `user` messages;
  - the conversation starts with `user` and alternates;
  - payload ≤ 20,000,000, computed as Σ `4·ceil(n/3)` over the blobs plus the UTF-8 text bytes, system text included (spec-07 §4.5).
- **Usage shapes.**
  - Converse JSON includes `cacheRead/WriteInputTokens` (0 by default), except DeepSeek, as in the probe. ConverseStream `metadata` includes them only when they are non-zero.
  - Mantle `prompt_tokens` is input + cached + cache-write. `prompt_tokens_details.cached_tokens` and `completion_tokens_details.reasoning_tokens` are sent only when non-zero.

| Fault (`fault.kind`) | Converse / ConverseStream | Mantle |
|---|---|---|
| `throttling` | 429 `ThrottlingException` "Too many requests, please wait before trying again." (+ `retry-after` from `retry_after_s`) | 429 `rate_limit_error` |
| `access_denied` | 403 `AccessDeniedException` "You don't have access to the model with the specified model ID." | 403 `permission_error` |
| `validation{message}` | 400 `ValidationException` | 400 `invalid_request_error` |
| `http_5xx{500\|503}` | 500 `InternalServerException` / 503 `ServiceUnavailableException` | 500/503 `server_error` |
| `model_timeout` | 408 `ModelTimeoutException` | 408 `timeout_error` |
| `connect_reset` | TCP RST (SO_LINGER 0 + abort) before any response byte | same |
| `hang_before_first_byte` | nothing sent until the client disconnects | same |
| `stall_after{n}` | n text chunks, then silence | same |
| `exception_event{type, after}` | stream: `after` chunks, then an exception frame (`modelStreamErrorException` adds `originalStatusCode`/`originalMessage`); non-stream: the HTTP error of that exception, see the table below | stream: a `data: {"error": …}` line, no `[DONE]`; non-stream: the same status, `error.type` by status |
| `malformed{bad_crc\|…}` | stream: message-CRC bit flip (`bad_crc`) or prelude-CRC flip (`bad_sse_line`, `no_done`); JSON: invalid body | `bad_sse_line`: a line without `data:`; `no_done`: ends without `[DONE]`; others: a truncated chunk |
| `truncated_json` | stream: a frame with valid CRCs and a truncated JSON payload; JSON: half the body | a truncated `data:` chunk / half the body |
| `reasoning_only`, `empty_text`, `refusal` | `max_tokens` after reasoning only (Converse JSON: Claude an empty `reasoningText` with a signature as live Haiku, the OpenAI family `redactedContent`, GLM reasoning text) / `end_turn` with no text / `content_filtered` | `length` / `stop` / `content_filter` + `message.refusal` |
| `invalid_verbalized{…}` | the renderer's variant text | same |

**`exception_event{type}` on a non-streaming call** (either case of the first letter). The statuses are the
bedrock-runtime service model's (botocore `service-2.json`; `test_exception_statuses_match_the_botocore_model` pins
them), so this is also how a scenario produces the spec-10 §2.1a errors that have no fault kind:

| `type` | HTTP | Mantle `error.type` |
|---|---|---|
| `ValidationException`, `ServiceQuotaExceededException` | 400 | `invalid_request_error` |
| `AccessDeniedException` | 403 | `permission_error` |
| `ResourceNotFoundException` | 404 | `not_found_error` |
| `ModelTimeoutException` | 408 | `timeout_error` |
| `ModelStreamErrorException`, `ModelErrorException` | 424 | `server_error` |
| `ThrottlingException`, `ModelNotReadyException` | 429 | `rate_limit_error` |
| `InternalServerException` / `ServiceUnavailableException` | 500 / 503 | `server_error` |
| any other name | 500 (`x-amzn-ErrorType` = the name) | `server_error` |

`ServiceQuotaExceededException` is 400 in the service model (not 429); spec-02 §4.3 still classifies it by name as
`throttled`. The default messages of the names without a probe string are mock strings.

**Hidden reasoning (Converse JSON, Wave E2).** `stream.reasoning.hidden_tokens: n` plans reasoning that Bedrock
hides on a non-streaming call: one reasoning block before the text (Claude: an empty `reasoningText` with a
signature; the OpenAI family: `redactedContent`; GLM: reasoning text). The n tokens count toward `maxTokens`: at or
above it the answer is the reasoning block alone with `stopReason: max_tokens` and `outputTokens` = `maxTokens` (the
live Haiku 5.5 verifier of L7, key for key: `test_hidden_reasoning.py`), else the text follows and is cut where the
budget ends. Nothing is planned by default (the live Haiku router calls did not reason).

**`pad`.** `stream.pad: n` adds a `"p"` member of n letters to every JSON payload of a stream: every eventstream event
and exception frame, and every SSE chunk including the usage chunk and the error line.

## Wiring (C2 integrator, adapters, QA)
- **Composition.** Nothing to wire. Set `NOEMA_AWS_ENDPOINT_OVERRIDES='{"bedrock-runtime": "<endpoint_url>", "mantle": "<mantle_url>"}'` (dev/test only, ADR-001 §8). The compose `app` profile already points at `http://mockbedrock:8080` and `.../v1`.
- **Per-test isolation** (spec-10 §1.1): use one in-process `MockBedrock` per test, or call `POST /_mock/reset` between tests on a shared container. Assert `mock.unmatched() == []` at the end of the test.
- **Adapter conformance against the mock** (C2 or adapters): register the real adapter kits with the overrides above; the mock needs STS-shaped credentials (a session token), for example moto's.
- **Load (L1).** Use `stream.stamp: true` together with `«nonce:<uuid>»` in the user text. Take `t_req_recv_ns` and `t_first_text_sent_ns` from `GET /_mock/requests?after=<last_seq>&limit=<n>` (page through with `after`; each page is O(n)). Never poll the unpaged dump during a run, and size `--record-limit`/`--record-bytes` so the records of interest are not evicted before they are read (`dropped` counts evictions).
- **Config keys read:** none from the Noema config. The inputs are the scenario document and the CLI flags.
- **Metrics/spans:** none. The logs go to `mockbedrock` (startup) and `mockbedrock.http` (handler failures).

## Tests (`tests/unit/mockbedrock/`, 235 tests, about 5 s with `-n 4`)
Run with `UV_NO_SYNC=1 uv run --frozen --no-sync pytest tests/unit/mockbedrock -n 4` (`make test MOD=mockbedrock`
finds the tests; `make lint`/`typecheck` have no `src/noema/mockbedrock`, so run ruff and `mypy --strict` on
`tools/mockbedrock tests/unit/mockbedrock`).
- `test_eventstream.py`: the spec-10 §2.1a self-test. Every frame decodes with `EventStreamBuffer`, and a flipped bit in either CRC raises `ChecksumMismatch`. The real aiobotocore `converse_stream` parser reads reasoning and text streams, Claude signatures, `pad` (events and exception frames), exception events (`EventStreamError` codes), malformed and truncated frames, and the `reasoning_only`/refusal streams.
- `test_converse.py`:
  - verbalized letters, honoring the permutation; harmony markers;
  - all 10 T-VB-1 texts delivered verbatim;
  - usage shapes; refusal, empty and reasoning-only answers;
  - every pre-byte fault: status, code, message and `retry-after`; `exception_event` names without a fault kind (ModelNotReady 429, ResourceNotFound 404, AccessDenied 403, ModelTimeout 408, ServiceQuotaExceeded 400, ModelError 424);
  - a real RST, a hang (Converse and ConverseStream: no status line, `t_first_byte_ns` None), broken JSON;
  - auth (session token, credential scope service/region, configurable region, a raw `:` in the model label) and records; the `invalid_verbalized` variants.
- `test_mantle.py`:
  - T-LP-1 (the mock's top list parsed with the spec-06 §5.6 rule gives exactly A 0.632008 … `unresolved` 0.001019, F censored);
  - split tokens (the "b" fragment listed, and cut by `top_k`);
  - usage, refusal and SSE with and without `include_usage`; `pad` on every SSE payload;
  - every pre-byte fault on chat and chat-stream (status, `error.type`/`code`, message, `retry-after`, `x-request-id`), non-stream `exception_event` (424, ModelNotReady 429), the openai client's `RateLimitError`, RST, hang (no status line);
  - `reasoning_only` (JSON and SSE), `empty_text`, the `invalid_verbalized` variants;
  - malformed SSE and chat-stream exceptions; route quirks, Luna on Responses, request validation and both auth modes.
- `test_disconnect.py`: the `disconnected` outcome on all four ops while held on a barrier, during the latency wait and mid-stream (ConverseStream and chat-stream, on a gated virtual clock); a half-closed client counts as gone.
- `test_goldens.py`: every shared wire golden with an expected body (`tests/golden/{decision,generation}_requests`, 12 today) replayed through aiobotocore (Converse/ConverseStream) or httpx (Mantle) gets a 200 and an `ok` record.
- `test_control.py`: control API, paging (`limit=0`, 400 for negative or non-integer values), the bounded log, the O(1) health probe on a full 50k ring next to a stream, bounded memory (runtime history cleared, byte budget), barriers, reset, `unmatched` outcomes, rpm/tpm quotas per AKID with window expiry and across a scenario reload, and a shared `akid: "*"` window over two AKIDs.
- `test_hidden_reasoning.py` (Wave E2): Converse JSON hidden reasoning against the live L7 Haiku cassette (key paths, stop, `outputTokens`), the text after it within the budget, the cut text, Sol's `redactedContent`, none by default.
- `test_timing.py` (VirtualClock): the exact stream schedule; fixed latency; draws that repeat per body and ignore arrival order; the DeepSeek tail share and the Haiku band over 400/200 calls; the Opus reasoning cadence above `low`, no reasoning for Opus, GLM and Sol at `low`, Sol `high`'s one `redactedContent` delta; `window_ms`, seeded `rate`, stamps and nonces, `stall_after` on both stream kinds.
- `test_cli_threaded.py`: the CLI contract (port line, healthcheck in both forms, every flag, scenario file, uvloop RST, SIGTERM exit 0); `ThreadedMockBedrock` with sync botocore and openai clients; RST on the asyncio loop.
- `test_parsing.py`: image headers, SigV4 and bearer parsing, redaction; the log ring (O(page) reads counted by copies on a 50k ring, byte budget, slot compaction); quota windows; the exception table against the botocore model.

spec-10 §3.1 assigns no module test ids to qa-tools. The mock supplies the fixtures behind T-LP-1, T-VB-1, MED-08/09 (the enforcing limits), F1–F12 (faults), L1 (stamps), C1 (quotas) and SE6–SE8 (request capture).

## Known limits
- The "drive the real converse adapter end to end" half of the spec-10 §2.1a self-test is not here. This role may not read or import `src/noema/adapters`, so the real aiobotocore parser stands in, and `test_goldens.py` replays the adapters' specified wire requests. The adapter kits against the mock are C2/adapters work (`.orchestration/requests/qa-tools.md` item 2, still open).
- **Memory bound.** Retained memory is about `record_bytes` (the log) plus, until the next load or reset, about 240 B per distinct (model, op, body) in the frozen `ScenarioRuntime` draw counter (`_body_counts`, needed for `k`): about 50 MB after 200k distinct requests. Each connection buffers one request body (≤ `max_body`) only while it is being received.
- The unpaged `GET /_mock/requests` is O(ring) on the event loop (see "Healthcheck cost").
- A client that half-closes its socket (`shutdown(SHUT_WR)`) after sending a request is treated as gone (`disconnected`), as uvicorn and aiohttp do: after a FIN the server cannot tell a half-close from a close without writing, and honoring half-closes would keep the held, hanging and waiting requests of vanished clients alive forever. No Bedrock client (botocore, aiohttp, httpx, openai) half-closes.
- No `/_mock` endpoint and no scenario key exposes HTTP/2, `contentBlockStart` (tool use) or Converse tool blocks; tool blocks are accepted and ignored.
- Responses streaming is refused with 400, since nothing in Phase 1 calls it.
- Chat-stream `logprobs` are degenerate (each chunk is one certain token). The decision members use `stream: false`.
- The scenario's `nth` and the draw counters are per instance. The spec-10 §5 placement of 4 mock processes therefore needs 4 instances (ports) or a single instance; one uvloop process comfortably serves the L1 rates. There is no `SO_REUSEPORT` mode.
- Mock-authored strings: the Bedrock limit messages, the auth, credential-scope and quota messages, the Mantle error `type`/`code` values outside the probes, the default messages of the exception names without a probe string, and the 5xx/timeout texts, which follow the standard Bedrock wording.

## Not delivered (owned by qa-tools and qa, OWNERS.yaml line 84; to be scheduled explicitly)
This wave delivered only the wire mock. The tooling below is in the role's ownership but was not built; each item
waits on the dependency named.

| Deliverable | Spec | Waits on |
|---|---|---|
| (Delivered in C2b/D by the perf engineer, see "Load harness" below.) The pytest gate wrappers `tests/load/test_overhead.py`, `test_ramp.py`, `test_soak.py`, `test_fanout.py`, `test_slow_clients.py`, `test_fairness.py`, `test_holdback.py` and the `reports/load/*` writer | spec-10 §5, §3 L1-L5, C1-C5 | QA (Wave E): the harness runs them; the C1 quota scenario, C3 slow readers and C5 two-tenant flood need driver options not built yet |
| Fault suite `tests/fault/test_f01_*.py` … `test_f12_*.py` (≥ 200 turns per row, 2 tenants, 2 API processes) | spec-10 §4 F1–F12 | the C2 integration (real adapters against the mock, generation fallback, decision members, breakers, RetryBudget), the compose `app` + `fault` profiles; the mock side (fault kinds, `window_ms`, `rate`, quotas, toxiproxy wiring in infra) is ready |
| Live fidelity check `tests/integration/mock_fidelity/` + the cassette recorder (`tests/cassettes/`) | spec-10 §2.3 | `NOEMA_LIVE=1` runs with owner approval and call budget (none were made in this wave) |

## Load harness (`tests/load/`, C2b/D perf engineer)
The spec-10 §5 driver and metrics, run against one api process and in-process `MockBedrock`s: `driver.py` (anyio +
httpx SSE streams, nonce per turn, stamps), `mockserve.py` (the mock plus an in-process record drain and a lag
sampler: no `/_mock/requests` paging on the mock's loop during a run), `serve.py` (the image CMD plus lag, GC and
profiling hooks), `stats.py` (nonce join, overhead with bootstrap CIs, validity), `run.py` (placement, stepped
plans, resource samples), `stack.py` (the load config with lifted tenant rate limits), `test_harness.py` (10
self-tests of the pure parts, not in make check). Usage and file list:
`tests/load/README.md`; the first report is `reports/perf/c2b-perf-smoke.md`.

Perf-review fixes (C2b/D perf fixer): `run.py` places the roles as spec-10 §5 sizes them (Postgres and Valkey pinned
with `docker update --cpuset-cpus`, 4 mock processes sharing the two ports with `reuse_port`, the api on 2 cores,
`--api-image` for a run from the image), writes `provenance` (HEAD + a digest of the uncommitted code paths, the
active config version and digest, the scenario digest, the interpreter or image id, placement, load) into
`results.json`, and A/B-tests `--dispatch-yields`. `stats.py` joins stream records by receive time (several mock
processes), reports `decision_ms`/`decision_lead_ms`, and judges validity per spec-10 §5 (`validity.valid`:
>= 99.9% `holdback_ms = 0` with `spec_agrees`, and every driver and mock process's lag p99 <= 2 ms).

