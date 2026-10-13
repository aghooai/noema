# 10 — QA and test strategy: wire mock, gate map, fault and load tooling, live smoke

Wave A, lead architect (the QA analyst's output was not written; this file is that deliverable; IMPL-080). Owners:
- qa-tools: `tools/mockbedrock/`, `tests/load/`, `tests/fault/` (a different agent from the adapter author);
- QA roles (Wave E): the runs and reports.

Gates come from 06 §11, as grouped in `.orchestration/context/03-env-probes-gates.md`.

## 1. Test layers

| Layer | Where | Runs in | Network |
|---|---|---|---|
| unit | `tests/unit/<module>/` | `make check` | none (`pytest-socket`) |
| conformance | `tests/contract_internal/` (spec-02 §14) | `make check` | localhost: per-session Postgres clone, Redis namespace |
| integration | `tests/integration/<module>/` | `make check` | localhost: uvicorn on port 0, wire mock, moto server, minio |
| e2e | `tests/e2e/` | `make e2e` | compose: 2 API processes, media-worker + media-sandbox, wire mock, moto, postgres, valkey, minio |
| security | `tests/security/` | `make qa-security` | compose |
| fault | `tests/fault/` | `make qa-fault` | compose + toxiproxy (its own project `-p noema-fault-<run>`) |
| load | `tests/load/` | `make qa-load` (never in `make check`) | compose |
| live | `tests/live/` | `NOEMA_LIVE=1 make smoke-live` | Bedrock (us-east-1), ≤ 60 calls (87 in total from Wave E3) |

Every response in every layer except load and live passes through the test-mode `ContractValidator` (IMPL-041). Schemathesis runs at every wave gate.

### 1.1 Per-agent isolation (up to 12 agents and their xdist workers run at once on one host)

`RUN = f"{NOEMA_AGENT_ID or 'local'}-{xdist worker id or 'w0'}-{uuid4().hex[:8]}"`, computed once per pytest session (the session fixture `noema_run_id` in the root `conftest.py`, foundation).

| Resource | Isolation | Cleanup |
|---|---|---|
| Postgres | `CREATE DATABASE noema_t_{RUN} TEMPLATE noema_template` (migrated once per `make check` by an flock-guarded fixture that never keeps a connection to the template); connect as `noema_app` (no BYPASSRLS) | `DROP DATABASE` at session end; `tools/reap_test_dbs.py` drops `noema_t_*` older than 6 h |
| Redis/Valkey | `RedisKeys(ns=f"ns:{RUN}:")`; pub/sub channels are keys of the same namespace | `SCAN MATCH ns:{RUN}:* + UNLINK` at session end |
| S3 (minio) | bucket `noema-t-{RUN}` | removed at session end |
| moto | one `ThreadedMotoServer` per session on port 0 (ADR-001 fixture) | stopped |
| Wire mock | one `tools/mockbedrock` instance per test (in-process on port 0), or one container per compose project; scenario and recorded requests are per instance (`POST /_mock/reset` between tests) | stopped |
| HTTP servers | uvicorn on port 0; URLs passed by fixture, never hard-coded | stopped |
| compose layers (e2e, security, fault, load) | `docker compose -p noema-{layer}-{RUN}` with no fixed `container_name` or host ports | `down -v` in a finalizer |
| Files | `tmp_path` only | pytest |
| Environment | `AWS_*` and `AWS_BEARER_TOKEN_BEDROCK` unset, `NOEMA_ENV=test`, `pytest-socket` allows only `127.0.0.1`/`::1` and the compose network | — |

A test may not rely on another test's state or order (`pytest-xdist --dist load` spreads tests arbitrarily).

## 2. Wire-level Bedrock mock (`tools/mockbedrock/`, qa-tools)

### 2.1 Endpoints (bound on port 0; the adapters point at it through dev/test endpoint overrides, ADR-001 §8)

| Endpoint | Protocol | Notes |
|---|---|---|
| `POST /model/{modelId}/converse` | JSON | decision members, verification, labeling, non-streaming generation |
| `POST /model/{modelId}/converse-stream` | AWS eventstream (§2.1a) | `messageStart`, `contentBlockDelta{text\|reasoningContent}`, `contentBlockStop`, `messageStop{stopReason}`, `metadata{usage}`; exception frames |
| `POST /v1/chat/completions` (Mantle; the adapter's base override ends in `/v1`) | JSON when `stream: false` (**decision members**); SSE when `stream: true` (§2.1a) | `logprobs` / `top_logprobs`; usage only with `stream_options.include_usage`; `[DONE]` |
| `GET /_mock/requests`, `POST /_mock/scenario`, `POST /_mock/barrier/{name}/release`, `POST /_mock/reset` | control | the recorded requests (method, path, headers minus secrets, parsed body, arrival time, AKID), the scenario load (§2.4), barriers |

### 2.1a Wire formats (what botocore/aiobotocore and the httpx Mantle client must parse unmodified)

**Converse JSON** (200): `{"output": {"message": {"role": "assistant", "content": [{"reasoningContent": {"reasoningText": {"text": "…"}}}?, {"text": "…"}]}}, "stopReason": "end_turn|max_tokens|stop_sequence|content_filtered|guardrail_intervened", "usage": {"inputTokens", "outputTokens", "totalTokens", "cacheReadInputTokens", "cacheWriteInputTokens"}, "metrics": {"latencyMs"}}`.

**Errors before the body** (Converse and ConverseStream): HTTP status + header `x-amzn-ErrorType: <Name>` + JSON `{"message": "<probe string>"}`; also `x-amzn-RequestId`. Names and statuses: `ValidationException` 400, `AccessDeniedException` 403, `ResourceNotFoundException` 404, `ModelTimeoutException` 408, `ThrottlingException` 429 (+ optional `retry-after`), `ModelNotReadyException` 429, `InternalServerException` 500, `ServiceUnavailableException` 503.

**ConverseStream eventstream** (200, `Content-Type: application/vnd.amazon.eventstream`, chunked). Each message, all integers big-endian:

```text
[total_length u32][headers_length u32][prelude_crc u32 = CRC32(first 8 bytes)]
[headers: repeat { name_len u8, name utf-8, type u8 = 7 (string), value_len u16, value utf-8 }]
[payload: JSON utf-8][message_crc u32 = CRC32(every preceding byte of the message, prelude and prelude_crc included)]
total_length = 12 + headers_length + len(payload) + 4; CRC32 = zlib.crc32 (IEEE 802.3)
```

| Frame | Headers | Payload |
|---|---|---|
| event | `:message-type: event`, `:event-type: <type>`, `:content-type: application/json` | `messageStart` `{"role": "assistant"}`; `contentBlockDelta` `{"contentBlockIndex": i, "delta": {"text": "…"}}` or `{"…", "delta": {"reasoningContent": {"text": "…"}}}` (then optionally `{"reasoningContent": {"signature": "…"}}` for Claude); `contentBlockStop` `{"contentBlockIndex": i}`; `messageStop` `{"stopReason": "…"}`; `metadata` `{"usage": {…as Converse}, "metrics": {"latencyMs": n}}` |
| exception | `:message-type: exception`, `:exception-type: throttlingException \| modelStreamErrorException \| internalServerException \| serviceUnavailableException \| validationException`, `:content-type: application/json` | `{"message": "…"}` (`modelStreamErrorException` adds `originalStatusCode`, `originalMessage`); the stream ends after it |

Order: `messageStart`, reasoning block (index 0) deltas + `contentBlockStop{0}` if any, text block (next index) deltas + `contentBlockStop`, `messageStop`, `metadata`. Quirk switches: `pad: true` adds a `"p": "<random a-z>"` member to every payload (parsers must ignore unknown members). Self-test: `tests/unit/mockbedrock/test_eventstream.py` decodes every frame with `botocore.eventstream.EventStreamBuffer` and drives the real converse adapter end to end; a flipped bit in either CRC must raise in botocore.

**Mantle chat JSON** (200): `{"id": "chatcmpl-…", "object": "chat.completion", "created", "model", "choices": [{"index": 0, "message": {"role": "assistant", "content": "B", "refusal": null}, "logprobs": {"content": [{"token": "B", "logprob": -0.11, "bytes": [66], "top_logprobs": [{"token", "logprob", "bytes"}, …≤ top_logprobs]}]} | null, "finish_reason": "stop|length|content_filter"}], "usage": {"prompt_tokens", "completion_tokens", "total_tokens", "prompt_tokens_details": {"cached_tokens"}?}}`. Errors: HTTP status + `{"error": {"message": "<probe string>", "type": "invalid_request_error", "code": null}}`.

**Mantle chat SSE** (`text/event-stream`): lines `data: <chunk JSON>\n\n`; chunk = `{"id", "object": "chat.completion.chunk", "created", "model", "choices": [{"index": 0, "delta": {"role": "assistant"}? {"content": "…"}? {"reasoning_content": "…"}?, "logprobs": {"content": [...]} | null, "finish_reason": null | "stop" | "length" | "content_filter"}], "usage": null}`. With `stream_options.include_usage: true` one extra chunk `{"choices": [], "usage": {…}}` precedes `data: [DONE]\n\n`; without it no usage is sent anywhere. Malformed-SSE faults: a line without `data:`, a truncated JSON chunk, a missing `[DONE]`.

### 2.2 Fidelity rules (from 03-env and `probes/results/*.json`)
- **Parameter validation per model**:
  - `temperature` rejected with the probe's exact `ValidationException` text (Haiku, Opus, Sonnet, Luna, Sol);
  - the effort accept/reject table (03-env);
  - `none` rejected for Sol, Opus and Haiku;
  - `minimal` rejected for Luna;
  - audio and video blocks rejected;
  - PDFs rejected for Luna, Sol, DeepSeek and GLM;
  - Claude and Luna on Mantle chat → 400 (Luna is only on `/openai/v1/responses`, which nothing in Phase 1 calls);
  - `logprobs` on Converse never reaches the mock (the adapter rejects it, spec-02 §4.2).
- **Bedrock limits**: ≤ 20 images (≤ 3.75 MB, ≤ 8,000²), ≤ 5 documents (≤ 4.5 MB, each with a text block in the same message), images and documents only in the `user` role, and a payload of at most 20,000,000 bytes measured as in spec-07 §4.5: Σ `4·ceil(n/3)` over every image and document byte string plus the UTF-8 bytes of every text (system and messages). `DocumentBlock.name` must match Converse's name rule (alphanumerics, single spaces, hyphens, parentheses and square brackets; ≤ 200 chars). A violation → `ValidationException`.
- **Auth**: Converse requires `Authorization: AWS4-HMAC-SHA256 Credential=<AKID>/<date>/us-east-1/bedrock/aws4_request, …` and `X-Amz-Security-Token` (else 403 `UnrecognizedClientException`); Mantle requires `Bearer` or SigV4 per the scenario's `mantle_auth` (ADR-001 §6). The mock cannot verify signatures; it records the AKID per request, and tenant-isolation tests assert that each request's AKID equals the one the broker issued for that tenant (SEC-STS-08 covers the adapter-side account assertion).
- **Determinism**: every random draw uses `random.Random(f"{seed}|{model}|{op}|{sha256(body)}|{k}")`, where `k` counts earlier requests with the same (model, op, body) in this scenario (backups repeat a body). Arrival order across different bodies therefore never changes a draw. Latency = `exp(N(μ, σ))` ms with `μ = (ln a + ln b)/2`, `σ = (ln b − ln a)/3.29` for the range [a, b] = [p5, p95]; defaults from the probes:

  | Model (op) | [p5, p95] | Tail |
  |---|---|---|
  | DeepSeek (chat) | 470–590 ms | 2% `uniform(4000, 12000)` ms |
  | Luna (converse) | 640–880 ms | — |
  | Haiku (converse) | 890–1800 ms (≥ 5% above 1 s) | — |
  | Sol / Opus / GLM (converse-stream) | first text: Sol low 710–750; Opus low 1900–2200, high 2000–11600; GLM none 600–1000, medium 2000–4400 ms | text 60 tokens/s in 4-token chunks; reasoning deltas every 20 ms before the first text (Opus, GLM ≠ none) |

  Scenarios override any of these (§2.4); timing-sensitive tests use barriers or fixed `latency_ms`, never the distributions.
- **Template-aware answers**:
  - for `verbalized-dist@1`/`verify-dist@1` prompts, the mock parses the `<app_question>` JSON and answers with the scenario's distribution keyed by option *value*, then maps it to the request's letters, so permutations are honored;
  - for `logprob-codes@2`, it emits `top_logprobs` built from the scenario: split tokens ("b"+"illing"), whitespace variants (" C"), EOS entries and censored tails (T-LP-1);
  - harmony `final<|message|>` markers are included for Luna when the scenario asks for them.
- **Refusals**: Converse `stopReason: content_filtered` / `guardrail_intervened`; Mantle `finish_reason: content_filter` and `message.refusal`.
- **Faults** (§4): throttling, AccessDenied, 5xx, hang before the first byte, mid-stream stall, a stream exception event, reasoning-only `max_tokens`, malformed SSE, invalid or truncated JSON, refusal.
- **Barriers**: a call can block on a named barrier until a test releases it, so timing tests are deterministic under 48-core contention.

### 2.3 Fidelity check against live
`NOEMA_LIVE=1` records redacted cassettes (no prompt text; shapes, event counts, timings, error strings) into `tests/cassettes/`. `tests/integration/mock_fidelity/` diffs the mock's output shape for the same request against each cassette: event types and order, usage field names, error strings.

### 2.4 Scenario DSL (`tests/fakes/scenario.py` + `scenario.schema.json`, B2, frozen with `tests/fakes`)

One YAML/JSON document drives both the fakes under `FakeClock` (virtual ms) and the wire mock (wall-clock ms); the golden fixtures of spec-04 §4.2 embed it. `tools/mockbedrock` imports the loader, never re-parses.

```yaml
scenario: G02_disagree_restart
seed: 7
mantle_auth: bearer_short_term          # what the mock accepts on Mantle
defaults: { latency: probe }            # probe | fixed_ms: 0
rules:                                  # evaluated in order per request; the first match answers; no match -> HTTP 500 + test failure
  - match: { model: deepseek.v3.2, op: chat, prompt_contains: "Which intent" }
    answer: { dist: { coding: 0.85, factual_qa: 0.10, chitchat: 0.05 } }  # by option value; mapped to letters (logprobs or JSON)
    latency_ms: 500
  - match: { model: us.anthropic.claude-haiku-5-5, prompt_contains: "sensitive" }
    barrier: haiku_sensitive            # held until POST /_mock/barrier/haiku_sensitive/release (or virtual time in fakes)
  - match: { model: us.openai.gpt-6.1-sol, op: converse-stream, nth: 1 }
    stream: { first_event_ms: 200, reasoning: { deltas: 0 }, first_text_ms: 720,
              text: "Sure, here is…", tokens_per_s: 60, stop: end_turn, usage: auto }
  - match: { model: us.anthropic.claude-opus-5-5, op: converse-stream }
    stream: { first_event_ms: 400, reasoning: { deltas: 100, every_ms: 20 }, first_text_ms: 2900, text: "…", stop: end_turn }
  - match: { model: us.zai.glm-5.3 }
    fault: { kind: stall_after, n: 3 }
```

| Key | Values |
|---|---|
| `match` | `model` (Bedrock id), `op` (`converse` \| `converse-stream` \| `chat` \| `chat-stream`), `prompt_contains` (substring of the concatenated system + message text), `nth` (the rule applies only to the n-th request that matches it, 1-based; use it only where arrival order is fixed, e.g. sequential attempts), `tenant_akid` |
| `answer` | decision members: `dist` by option value (predicates: `"true"`/`"false"`), or `raw_text` (verbatim body text); logprob extras `top_k`, `split_tokens`, `whitespace_variants`, `eos_mass`, `censor` (T-LP-1); `harmony_final: true` (Luna) |
| timing (ms from the request's arrival at the mock; in fakes, from the call) | `latency_ms` \| `latency: {p5, p95, tail_p, tail_ms: [lo, hi]}` \| `barrier: <name>`; streams: `first_event_ms`, `first_text_ms`, `tokens_per_s`, `chunk_tokens` (4), `reasoning: {deltas, every_ms}` |
| `stream` | `text` (or `tokens: n` of filler), `stop` (`end_turn` \| `max_tokens` \| `stop_sequence` \| `content_filtered` \| `guardrail_intervened`), `usage` (`auto` = chars ÷ 4, or explicit fields), `pad`, `stamp: true` (load mode, §5) |
| `fault.kind` | `throttling`, `access_denied`, `validation{message}`, `http_5xx{status: 500\|503}`, `model_timeout`, `connect_reset` (TCP RST before any byte), `hang_before_first_byte`, `stall_after{n}`, `exception_event{type, after: n}`, `reasoning_only` (reasoning then `max_tokens`, no text), `empty_text` (`end_turn`, no text), `malformed{bad_crc \| bad_sse_line \| no_done}`, `truncated_json`, `invalid_verbalized{missing_letter \| sum_out_of_range \| not_json}`, `refusal` |
| fault scope | `rate: 0.5` (seeded per draw key), `window_ms: [start, end]` from scenario load (5xx bursts, F3/F12) |

The mock validates every request against §2.2 before applying a rule, so a fidelity violation (e.g. `temperature` on Sol) fails like Bedrock even if a rule matches.

## 3. 06 §11 gate → test map

06 §11 Phase 1 = the Phase 1 targets of §2.4, §3.1, §4, §5, the §7 SLO tests, §8 estimator accuracy, §9, §10 (images, PDFs), contract tests and 100% schema-valid responses. Status: **now** = automatable in this build and gated (mock or local); **meas** = run and reported, no pass/fail until Phase 0 sets the threshold or the real data exists; **live-n** = the gated live smoke (§6), small n, labeled; **P0** = needs Phase 0 data or spend, reported *not run*; **prod** = production-only. Owners as in spec-01; QA (Wave E) runs `tests/load`, `tests/fault`, `tests/live`.

| Id | Gate (06 ref) | Test | Tool | Pass criterion | Status |
|---|---|---|---|---|---|
| Q1 | 100% schema-valid responses (§2.4, §11) | every response of the unit…security layers; `tests/contract_internal/test_schemathesis.py` | `ContractValidator` (`NOEMA_CONTRACT_VALIDATE=1`), `check_examples` invariants, schemathesis | 0 invalid responses in the whole run; schemathesis 0 failures on every operation | now |
| Q2 | OpenAI facade conformance 100% (§2.2) | `tests/integration/decision/test_facade_openai_sdk.py`; `sdk/typescript/test/facade.openai.test.ts` | `openai` (PyPI, npm), `base_url=http://127.0.0.1:<port>/openai/v1` | 100% of cases pass: acceptance, parsing, `unavailable` → `refusal` with and without `x-aghoo-include: ensemble`, SDK retries on 429/503 (counted on the mock) | now |
| Q3 | Jev conformance 100% (§2.2) | `tests/unit/decision/test_jev_property.py` + §8 Jev tests | hypothesis | every `jev` response validates `JevDecisionResponse` and `jev_invariants` for K ∈ {2, 3, 5, 16}; values = the IMPL-049 map of the same `decision_id` | now |
| Q9 | (supporting) Hierarchical method (08 §3.4; owner answer 1, IMPL-027): two rounds, expansion, unresolved mass never spread, deadline → `unavailable`, facade/Jev 422 | spec-06 T-HIER-1…10, spec-04 T-HIER-T1, spec-11 T-WG-5, the spec-08 §14.8 hierarchical audit cases | pytest, `FakeClock`, wire mock, `check_examples` | T-HIER-1 equals the regenerated `decision-result.valid-hierarchical.json`; every hierarchical result passes `result_invariants`; 0 `feature_not_available` for choice questions; the `deadline_exceeded` share at `deadline_ms` 1,000 vs 3,000 on the mock latency profile is reported | now (share: meas) |
| Q4 | Ensemble vs `lightweight-model-decide` vs cascade (§2.4 (1)–(3)) | — | Phase 0 harness on seed-v1 | paired CIs | P0 |
| Q5 | First `calibrated` signature (§2.3) | — | — | — | P0 (machinery = Q6) |
| Q6 | Calibration machinery: A1–A8, binding enforcement, drift downgrade (§2.3) | `tests/unit/labeling/test_acceptance.py`, `test_drift.py`; `tests/integration/decision/test_calibration_binding.py` | pytest, synthetic label sets | each rule rejects at its boundary (ECE 0.0501, NLL > uncalibrated, n_test 299, a class with n = 29 flagged, class consensus 0.59, lift 0.1001) and accepts just inside it; any binding-key mismatch → `uncalibrated`; weekly ECE 0.0801 → `downgraded` | now |
| Q7 | Selection-bias AUROC (§2.2) | — | — | — | P0 |
| Q8 | Holdback reporting: `holdback_deadline` share, `served_spec_differs` rate (§2.2) | `tests/load/test_holdback.py` report | load driver counts `generation.committed.reason` | reported | meas |
| R1 | Conditioning effect (§3.1) | — | D4 labelers | — | P0 |
| R2 | Conditioning placement (§3.1, ADR-004) | — | Phase 0 task 0.6 | — | P0 |
| R3 | Effort binding non-inferiority (§3.1) | — | — | — | P0 |
| R4 | Spec restarts ≤ 10%, waste ≤ 5% of generation cost (§3.1, §8) | `tests/load/test_replay.py` (a replay set of multi-turn sessions on the mock) | `aghoo_spec_restarts_total`, `waste` usage lines | computed and reported; the thresholds need the real agreement rate `a` | meas (gate P0) |
| R5 | Verification AUROC; precision ≥ 0.8 (§3.1) | machinery: spec-04 G08, `tests/unit/turns/test_verification.py` | — | machinery green | now (quality P0) |
| L1 | Proxy overhead p50 ≤ 10 ms, p95 ≤ 25 ms at 200 streams per pod (§4); owner decision 2026-10-12: gated at the sized density, the measured knee (design 06 §11 note, 08 §9) | `tests/load/test_overhead.py` (§5) | load driver, mock `stamp` | upper bootstrap 95% CI of p50 ≤ 10 ms and of p95 ≤ 25 ms, n ≥ 10,000 first deltas | now |
| L2 | Holdback agree: client TTFT p95 ≤ max(T_d, TTFA) + 50 ms (§4) | `tests/load/test_holdback.py::test_agree` | driver; mock with fixed T_d ∈ {300, 900} ms × TTFA ∈ {300, 720, 2400} ms | every cell, n ≥ 200, concurrency 50 | now |
| L3 | Holdback disagree: ≤ T_d + TTFT(r*) + 100 ms (§4) | `…::test_disagree` | same cells, forced restart | every cell | now |
| L4 | Decision quorum p95 ≤ 1,000 ms; quorum met ≥ 99% (§4) | `tests/load/test_decision_latency.py` | `aghoo_decision_quorum_reached_seconds` (IMPL-006); probe latencies incl. the Haiku tail | p95 ≤ 1,000 ms; ≥ 99% of turns with every commit-critical answer scored; n ≥ 2,000 turns | now (mock) |
| L5 | DeepSeek p99 with backups ≤ 1.5 s (§4) | `tests/load/test_backup_tail.py` | uncancelled `/v1/decisions` (`escalation: off`, `deadline_ms: 30000`, DeepSeek-only test profile) on the 2% outlier tail | p99 ≤ 1.5 s, n ≥ 5,000 | now (mock) |
| L6 | E2E ≤ 5% over direct Bedrock at p50, pinned single-model (§4) | `tests/live/test_e2e_pinned.py` | live smoke item L8 (§6) | p50 ratio reported with n = 5 pairs | live-n (gate P0) |
| L7 | First answer per route, Opus and GLM p50/p95 (§4) | — | — | — | P0 (the smoke records single samples) |
| C1 | 200 streams/pod with decision fan-out; admission counts every call, reserves the worst case, throttles before Bedrock (§5) | `tests/load/test_fanout.py` | driver; mock per-(tenant, model) quota that throttles above Q, with `tenants.<t>.limits.model_quotas` = Q | reservation ≥ issued, measured as `aghoo_reservation_overrun_total` = 0 over the run (T-ADM-3; T-WG-1…5 give the exact counts); 0 mock `ThrottlingException`s while admission answers 429 | now |
| C2 | Ramp 0 → 1,000 streams; event-loop lag p99 < 50 ms; memory per stream, CPU per 1k tokens/s, fds, pool saturation, Redis/Postgres latency (§5) | `tests/load/test_ramp.py` | driver, `aghoo_event_loop_lag_seconds`, `/proc/<pid>` sampling | lag p99 < 50 ms up to 200 streams; the rest measured; holds shortened to 2 min per step (labeled) | now ≤ 200, meas above |
| C3 | Slow clients → coalescing → `client_too_slow`, no effect on other streams (§5) | `tests/load/test_slow_clients.py` | 20 clients reading at 10% of the token rate among 180 normal ones | coalesced frames seen; `client_too_slow` at 30 ± 1 s; normal clients' TTFT p95 within 10% of a baseline run; queue high-water ≤ 256 events / 1 MiB | now |
| C4 | Soak 24 h at 60% (§5) | `tests/load/test_soak.py` | driver | 1 h at 60% of the C2 knee (labeled "shortened"): RSS growth ≤ 5% after a 10 min warm-up; `aghoo_stuck_attempts_total` = 0 | now (shortened) |
| C5 | Fairness: a flooding tenant raises another's p95 TTFT ≤ 10% (§5) | `tests/load/test_fairness.py` | 2 tenants, WFQ, the flooder at 5× its concurrency limit | victim p95 TTFT ≤ 1.10 × its solo run | now |
| F1–F12 | The 12 faults (§7) | `tests/fault/` (§4) | wire mock + toxiproxy | §4 rows | now |
| S1 | SLOs: pre-commit fallback ≥ 99%; zero duplicated client-visible text (§7) | asserted inside every `tests/fault/` run | SSE capture | fallback success ≥ 99% of turns whose first attempt failed; every client's rendered text equals the committed attempt's text once | now |
| S2 | Turn API availability 99.9% monthly (§7) | — | — | fault-run availability reported | prod |
| K1 | Estimator accuracy: tokens ≤ 2% (incl. reasoning), cost ≤ 5% (§8) | `tests/live/test_estimator.py` over every live call | `TokenEstimator` vs Bedrock `usage` | per-model token error reported (n small); cost needs real prices and billing | live-n (cost P0) |
| SE1 | AuthZ matrix: zero leaks (§9) | SEC-AZ-01…04 (`tests/security/test_authz_matrix.py`, spec-09 §3, §10); authn hygiene SEC-AUTH-01…06 | generated from spec-09 §3 × openapi | every cell equals the independent oracle (404, 403 or an empty page where declared) | now |
| SE2 | RLS with a wrong `app.tenant_id` (§9) | spec-03 §6 db tests | per-session DB | 0 rows | now |
| SE3 | Cache isolation (§9, §6) | SEC-ISO-01 = spec-06 T-L2-2 (`tests/integration/decision/test_l2_isolation.py`); SEC-ISO-02 (pub/sub tenant check) | two tenants | 0 cross-tenant L2 hits | now; Bedrock-side `cacheReadInputTokens`/timing test P0 |
| SE4 | Cross-account access (§9) | SEC-STS-01…17 (ADR-001; 05…10 deny cases, 15…17 credential-chain and refresh windows) | moto + `trust_enforcer` | denied before any mock request | now; AWS-side and CloudTrail P0 |
| SE5 | Labeler poisoning (§9) | — | adversarial-v1 | — | P0 |
| SE6 | `<decision_context>` forgery (§9) | SEC-INJ-01, -02, -05 (`tests/security/injection/`, spec-09 §5), spec-04 T-COND-3 | wire-mock request capture | 0 unescaped reserved tags in any captured request | now |
| SE7 | Media injection per input view (§9) | SEC-INJ-02…04, -06 (datamarked document and OCR blocks in generation and decision prompts, IMPL-086): the same corpus in images and PDF text layers | capture | 0 policy violations (no route outside the allowlist, no constraint loosened); `injection_suspected` recall reported | now (recall P0) |
| SE8 | Filenames and SSRF (§9) | SEC-INJ-03, MED-11, `tests/integration/decision/test_no_filenames.py`, facade `image_url` http(s) tests | capture | 0 filename substrings; remote URLs 400 `schema_violation` | now |
| SE9 | Upload fuzz: no escape, no egress (§9) | SEC-SBX-02/03/05; SEC-SBX-08 nightly | media image | no crash outside the jail; egress-deny logs empty | now (SEC-SBX-08 nightly, reported) |
| SE10 | Prompt injection vs decisions: flip rate (§9) | — | adversarial-v1 | — | P0 |
| SE11 | Output handling: nothing active; CSP clean (§9) | SEC-UI-01…03 (spec-09 §8, Playwright), MED-04, MED-05 | `@playwright/test` | 0 active elements; 0 CSP reports | now |
| SE12 | Secrets in telemetry; 401s never echo (§9) | SEC-RED-1/2, SEC-STS-12, SEC-AUTH-01, `tests/unit/api/test_errors.py` | pattern scan of logs, traces, Redis, DB | 0 matches | now |
| SE15 | (supporting) config integrity and edge caps (§9) | SEC-CFG-01, SEC-EDGE-01 (spec-09 §10; owners foundation, media-pipeline) | pytest | as spec-09 §10 | now |
| SE13 | Dependencies: SCA and container scans (§9) | `make qa-security` | pip-audit, npm audit, trivy (docker images) | no unaddressed critical CVE | now |
| SE14 | Pre-GA penetration test (§9) | — | — | — | not run (pre-GA) |
| M1 | Negotiation matrix 100% (§10) | MED-08 (spec-07 §7) | wire-mock capture | 100% for 7 models × the image/PDF cells; 0 `ValidationException` | now |
| M2 | Degradation reporting (§10) | MED-08, MED-09, spec-04 G-fixtures with media | capture + SSE | each reason on the right event and `attempt_id` | now |
| M3 | OCR CER (§10) | `tests/integration/media/test_ocr_cer.py` on the probe fixtures | Tesseract | CER reported (Tesseract only; no engine comparison) | meas |
| M4 | Decision accuracy on media turns within 3 pts (§10) | — | seed image/PDF turns | — | P0 |
| M5 | > 20 images / > 5 PDFs / 600-page PDFs → no Bedrock limit errors (§10) | MED-09 | enforcing wire mock (§2.2) | 0 `ValidationException`; `media_budget` degradations as planned | now |
| X1 | Contract tests green; check_docs; OpenAPI ↔ 07 drift (§11) | `make contracts-check`; `tests/contract_internal/test_guide_examples.py` | `check_docs.sh` | green | now |

### 3.1 Module test ids that feed the gates (owners write them in C1; QA re-runs them in E)

| Ids | Spec | Owner | Gate rows |
|---|---|---|---|
| G01–G12, P-SSE-1, T-FSM-1, T-BIND-1…5, T-COND-1…4, T-SESS-1…6, T-TIME-1…4, T-HIER-T1, T-ING-1 | spec-04 §13 | orchestrator, generation | Q1, Q8, Q9, L2, L3, R5, S1, SE6, F1–F12 |
| T-SIG-1, T-LP-1, T-VB-1, T-POOL-1/2, T-CLS-1, T-OD-1, T-PRED-1, T-CAL-1…4, T-ESC-1…3, T-RUN-1/2, T-JEV-2/3, T-FAC-1/2, T-L2-1…3, T-HIER-1…10 | spec-06 §17–§19 | decision | Q1–Q3, Q6, Q9, L4, L5, SE3, SE8 |
| MED-01…07 / MED-08…11 | spec-07 §7 | media-pipeline / media-negotiation | M1, M2, M5, SE8, SE9, SE11 |
| T-WG-1…5, T-GCRA-1/2, T-ADM-1…3 | spec-11 §8 | ops-admission | C1, C5, F10, Q9 |
| T-COST-1/2, T-USAGE-1, T-OBX-1/2, T-IDEM-1/2, T-JOB-1/2, T-RET-1 | spec-11 §8 | ops-ledger | Q1, F11, K1 (machinery), spec-04 G12 |
| spec-03 §6 db tests | spec-03 §6 | db | SE2 |
| SEC-*, MED-04/05 | spec-09 §10, ADR-001, ADR-002 | as spec-09 §10 | SE1–SE15 |

Not Phase 1 gates (06 §11 lists them elsewhere): budget enforcement (§8; `budget_exhausted` is unreachable, IMPL-036), labeling cost ≤ 5% (§8), cache effectiveness (§6), routing quality (§3), generation ensembles (§3.2).

## 4. Fault suite (`tests/fault/test_fNN_*.py`, 06 §7)

Every row runs ≥ 200 turns (2 tenants, `Aghoo-Deadline-Ms: 10000`) through 2 API processes, with the S1 assertions of §3 on all of them.

| # | Fault | Injection (§2.4 `fault.kind`, toxiproxy) | Pass criterion |
|---|---|---|---|
| F1 | Throttling | `throttling` on Opus in `opus-deep`, tenant A only | ≥ 99% of A's turns commit on the next member with no backoff sleep (spec-04 §6); tenant B's TTFT p95 within 10% of a clean run |
| F2 | AssumeRole failure / AccessDenied | `trust_enforcer` denies (SEC-STS-05/06/07/09); `access_denied` on a generation member; `access_denied` on one decision member | ingress 503 `tenant_access_misconfigured`, `retryable: false`, 0 mock requests; generation: `error{tenant_access_misconfigured}`, no fallback; decision member: degraded (IMPL-057), turn completes; 100% classified |
| F3 | 5xx burst | `http_5xx{503}`, `window_ms: [0, 30000]`, `rate: 1.0` on Sol | breaker open within the 30 s window once ≥ 20 requests saw > 50% errors; half-open 15 s later (5% probes); closed after the window; time-to-open and time-to-recover reported |
| F4 | Hang before the first byte | `hang_before_first_byte` on GLM (`glm-reason`, first-event timeout 10 s) with `Aghoo-Deadline-Ms: 30000`; and on Sol under `sol-fast` (no member left) | GLM: fallback to Sol at 10 s (spec-04 T10); Sol: `error{all_routes_unavailable}` at 5 s; every turn's `e2e_ms ≤ deadline + 1000` |
| F5 | Mid-stream stall | `stall_after{n: 3}` pre-commit and post-commit | pre-commit: fallback; post-commit: `error{upstream_stalled, retryable: true}`, partial text stored (spec-04 G10) |
| F6 | Exception event in ConverseStream | `exception_event{modelStreamErrorException, after: 0 \| 5}` | pre-commit: fallback; post-commit: `error{all_routes_unavailable, retryable: true}` (G10b); 100% classified |
| F7 | Reasoning-only output | `reasoning_only` on GLM in `glm-reason`; `empty_text` | nothing committed from the failed attempt; fallback to Sol; 0 `generation.completed` with empty text |
| F8 | Malformed SSE / invalid decision JSON | `malformed{bad_crc}` (converse-stream), `invalid_verbalized{…}`, `truncated_json` (Mantle) | attempt → fallback; member → `error` with its parse code; 0 HTTP 500 and 0 `internal_error` events |
| F9 | All decision members down | `http_5xx` on every member model | 100% of turns `completed`, `routing.reason = "decision unavailable: all_members_failed"` (G09) |
| F10 | Redis down | toxiproxy disables valkey mid-run | ≥ 99.9% of turns complete; local GCRA buckets; L2 bypass (`aghoo_cache_misses_total{reason=error\|bypass}`); same-pod SSE unaffected; cross-pod attach waits then 410; a cancel on another process (incl. the sibling process of the pod) 500 retryable |
| F11 | Postgres down | toxiproxy disables postgres for 60 s | `store: true` → 500 `internal_error` (retryable) at the lock; `store: false` unaffected; in-flight finalizes → outbox + `session_pending`; after recovery every finalized turn and usage line exists exactly once (by id) |
| F12 | Retry storm | `http_5xx{503}`, `rate: 0.5` on every generation model, at 5× the C1 load (and `rate: 0.25`, breakers closed) | per failing upstream X: requests to X at the mock ÷ first sends to X (turns that started an attempt on X) ≤ 1.1 over a 60 s window, in both rows (R8: RetryBudget 10% of first sends, no floor, on same-model re-sends; a fallback to another model is a first send, not a retry, and is bounded by the attempt cap, admission and the breakers: IMPL-016 amendments, R6, R8). Requests per turn are recorded, not gated |

## 5. Load tooling (`tests/load/`)

- **Driver**: a Python anyio + httpx SSE client (`tests/load/driver.py`), run as N processes (50 streams each at 200 streams). k6 isn't used, because its SSE support is limited (06 §5 allows k6 or Locust; this is the substitute). A run is **invalid** (repeated, not reported) if any driver or mock process has event-loop lag p99 > 2 ms.
- **Placement** (48-core host): API process (uvicorn + uvloop, 1 worker = 1 "pod") on cores 0–1; Postgres/Valkey containers on 2–7; wire mock (4 processes) on 8–15; drivers on 16–23 (`taskset`). All clocks are `time.monotonic_ns()` (`CLOCK_MONOTONIC`, shared by every process on the host).
- **Overhead scenario** (L1): each stream loops turns on its own session (`store: true`) with every pre-commit key pinned (`generation: {profile: sol-fast, effort: low, max_output_tokens: 600}`, `conditioning: "off"`), so the decided spec always equals spec0. The default router stays on (20 decision calls per turn), so the overhead includes the decision fan-out; "gate-free" (06 §4) means the commit never waits, which the next condition enforces. Mock: decision members `latency_ms: 50`; Sol `first_text_ms: 300`, 400 tokens at 60 tokens/s, `stamp: true`. S1 (≈ 56 ms) therefore always precedes the first text: the run is invalid unless ≥ 99.9% of turns have `holdback_ms = 0` and `generation.committed.reason = spec_agrees`.
- **Stamps**: with `stamp: true` every mock text chunk starts with `«ts:<monotonic_ns>»`; the driver strips the markers (a coalesced frame can carry several). The user text carries `«nonce:<uuid>»`, which the mock records with `t_req_recv` and `t_first_text_sent` for that request (`GET /_mock/requests`).
- **Gated quantity** (06 §4 "client TTFT − upstream TTFT"), per turn: `overhead = (t_client_first_delta_recv − t_client_send) − (t_mock_first_text_sent − t_mock_req_recv)`. Reported decomposition (not gated): ingress `t_mock_req_recv − t_client_send`; first-delta egress `t_client_first_delta_recv − stamp₀`; inter-delta egress `t_client_recv − stampᵢ`.
- **Statistic**: 60 s warm-up, then ≥ 10,000 turns at 200 concurrent streams; p50/p95/p99 with bootstrap 95% CIs (1,000 resamples). Pass: CI upper bound of p50 ≤ 10 ms and of p95 ≤ 25 ms.
- uvloop, orjson and pre-serialized frames in the API (PLAN). Event-loop lag comes from `aghoo_event_loop_lag_seconds`.
- Reports go to `reports/load/*.json` + `.md` (config hash, commit, n, CIs, core placement, validity checks).

## 6. Live smoke (`tests/live/`, `NOEMA_LIVE=1`, hard stop at 60 calls; 87 in total from Wave E3)

*Owner decision 2026-10-12 (IMPL-080 amendment):* +30 calls for re-verification raise the ledger's hard stop to **87 entries in total** (the Wave E smoke used 57).

A "call" = one `Adapter.complete()` or `Adapter.stream()` entry, which includes backups, fallbacks and restarts (an in-adapter connect retry sends no model request). Worst case = every backup, restart and failure attempt the code may issue.

| # | Item | Expected | Worst |
|---|---|---|---|
| L1 | Non-streaming Converse generation: GLM (effort `none`) | 1 | 1 |
| L2 | ConverseStream: Opus (effort `low`, reasoning first), GLM (`none`); Sol streaming is covered by L7/L8 | 2 | 2 |
| L3 | Mantle auth probes (ADR-001 §6), DeepSeek: short-term token from the ambient credentials; SigV4 | 2 | 2 |
| L4 | Sonnet media (IMPL-043): one image, one PDF | 2 | 2 |
| L5 | `/v1/decisions` on `lightweight-model-decide`, 3 questions, `output_format: "jev"`, `escalation: "off"` | 3 | 3 |
| L6 | `/v1/decisions` on `router-default`, 1 question, `escalation: "off"` (3 members + ≤ 1 DeepSeek backup) | 3 | 4 |
| L7 | One turn, `router-default`, first turn of a new session, `verify: true`: decision 15 (+ ≤ 5 backups); A1 (+ ≤ 1 restart); ≤ 2 failure attempts; verification 2 questions × 2 members | 20 | 28 |
| L8 | Pinned e2e (gate L6): 5 turns with `generation: {profile: sol-fast, effort: low, max_output_tokens: 512}` under the smoke router (1 commit-critical question, 1 member Luna), 2 calls each, interleaved with 5 direct `Adapter.stream()` calls on the same prompts (~300-token answers) | 15 | 15 |
| L9 | labeler@1 smoke (`tests/live/test_labelers_live.py`): one predicate, both shipped labelers (gpt-6.1-sol, claude-sonnet-5-5), `max_tokens` 16 + the labeler reasoning reserve | 2 | 2 |
| | **Total** | **50** | **59** (1 spare) |

- Config `tests/live/config/`: the shipped config plus `labeling.sample` rates 0 (no labeler calls) and, for L8 only, a second activation with `routing.router_profile: smoke-router`. Region `us-east-1` passed explicitly; `AmbientBroker` (its one `GetCallerIdentity` is not a model call).
- The API runs **in-process** (uvicorn on port 0 in the test's event loop), so one counter sees every call: `tests/live/conftest.py` wraps the composition root's `AdapterRegistry` in `CountingAdapterRegistry` (`budget.LIMIT`: 60 in Wave E, 87 from Wave E3, counted over the persistent journal `.orchestration/reports/live-calls.jsonl`). The call past the limit raises `LiveBudgetExceeded` before any byte and aborts the session; the count is written to `reports/live/calls.json`.
- Model allowlist = the 7 config models; `max_tokens` cap 256 (generation 512; decision and verification members 1,024 since Wave E3, so the config's own budgets reach Bedrock uncut, e.g. Haiku's verify-dist@1 at 600 tokens, CR #26; labeling 16 + the labeler reasoning reserve = 1,040, L9). Constants in `tests/live/budget.py`.
- Redacted cassettes (§2.3) are recorded from every call; K1 (estimator) uses the same calls.

## 7. Dependencies B1 pre-adds (IMPL-080; agents never add dependencies)

- Python dev: `pytest`, `pytest-asyncio` (or anyio's plugin), `pytest-xdist`, `pytest-timeout`, `pytest-socket`, `hypothesis`, `hypothesis-jsonschema`, `schemathesis`, `respx`, `moto[server]`, `openai`, `jsonschema>=4.23`, `datamodel-code-generator==0.83.0`, and `atheris` in the media image's test layer only (ADR-002 SEC-SBX-08, nightly).
- npm devDependencies: `openai`, `@playwright/test`, `msw`, `@redocly/cli@2.62.1`, `openapi-typescript@7.13.0`, `typescript@5`, `vitest`.
- The CI `api.openai.com` grep is scoped to `src/`, `tests/`, `sdk/`, `web/src/`, and allowlists the openai package's own default in `node_modules`/`site-packages` (never scanned).

## 8. Decision-subset additions (critic 54)

- `test_l2_isolation.py`: tenants A and B, identical input and questions → B misses; keys differ.
- `test_no_filenames.py`: for every turn and decision in the integration suite, the recorded wire-mock requests contain no uploaded filename substring.
- `test_jev_facade_censored.py`: the K = 3 censored case of IMPL-049 → Jev confidence 0.40 and facade 0.4; validators pass.
- `test_jev_k16.py`: Jev score and confidence from the 2-dp map pass `jev_invariants` for 10,000 random Dirichlet(0.5) distributions.
- `test_facade_non_pattern_name.py`: an explicit `"Severity Level"` name → `x_aghoo` keyed by `"Severity Level"`; the stored result uses `q0`; both validators pass.
