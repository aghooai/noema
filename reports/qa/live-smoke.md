# Wave E QA: live smoke against Amazon Bedrock (spec-10 §6, §2.3; 06 §4, §8, §10)

Track: live-smoke (the only track allowed to call Bedrock). Date: 2026-10-11, 07:34-07:37 UTC. Region us-east-1,
passed explicitly. Code under test: HEAD `b85f318` (tag wave-c2b) plus the uncommitted working tree at 07:34 UTC.
That tree already held other Wave E tracks' edits, notably the perf sprint's lean unary Converse path
(`src/noema/adapters/{converse,_converse_wire,_unary}.py`, last written 07:22-07:31 UTC), so the live run checked
that path against real Bedrock too.

## 0. Call budget (exact)

| | Value |
|---|---|
| Global budget | 60 calls over all runs |
| Spent | **57 calls** (42 Converse/Mantle `complete`, 15 ConverseStream). 3 calls are left and unused |
| Journal | `.orchestration/reports/live-calls.jsonl`: 57 lines `{ts, model, alias, operation, test, run}`. Each line is written before any byte is sent |
| By model | gpt-6-luna 16, gpt-6.1-sol 14, deepseek.v3.2 9, claude-haiku-5-5 9, glm-5.3 4, claude-sonnet-5-5 4, claude-opus-5-5 1 |
| By run | `20261011T073404` (phase 1, everything except L8) 40; `20261011T073549` (re-run of the 2 failed GLM cases) 2; `20261011T073610` (L8) 15 |
| By item | L1 1+1 (re-run), L2 2+1 (re-run), L3 2, L4 2, capability probes 3, L5 3, L6 3, L7 22, L8 5 direct + 10 pinned, L9 2 |
| Rehearsal | 3 full runs against the wire mock (`NOEMA_LIVE_MOCK=1`) before and after the live runs. They made 0 Bedrock calls and are not in the journal |

Pre-flight before any call:
- `aws sts get-caller-identity --region us-east-1` gave account 976939723775 (IAM user `mltest`).
- `AWS_BEARER_TOKEN_BEDROCK` is set: only its presence and length were checked, the value was never printed.

## 1. Harness (what was built, tests/live/**)

- **Global journal and refusal.**
  - `Ledger(journal=...)` counts the calls of every run.
  - The live conftest exits with code 3 at configure time when the journal holds >= 60 calls, or when the run was
    started without `-p tests.live.bearer` (the early plugin keeps the shell's bearer token past the root conftest's
    `AWS_*` scrub).
  - `ledger.need(n)` skips a test before any call when its worst case does not fit in the budget that is left.
  - Offline tests: `tests/unit/adapters/test_live_harness.py`, 8 tests including the 3 new ones (journal across
    runs, `need`, refusal).
- **Cassettes.**
  - Every call is recorded, including cancelled ones: backup losers, members cut at a deadline, the cancelled
    speculative attempt. Before this change a cancelled `complete` was counted but never recorded.
  - Each cassette keeps the node id at `take()`, the Bedrock model id, the normalized events and the usage.
  - `tests/live/wiretap.py` adds a value-free wire-shape tap: Converse/Mantle JSON key paths, ConverseStream/SSE event
    signatures, the raw error code, HTTP status and redacted message.
  - No prompt or answer text is stored. A grep for the prompts, fixtures, "AKIA", "ASIA", "Bearer " and API-key
    patterns over `tests/cassettes`, `reports/live` and the journal found nothing.
- **API rig.** `tests/live/apistack.py` runs the in-process api on `composition.build("dev", wrap_adapters=...)` with
  `NOEMA_CREDENTIALS_BROKER=ambient` (identity's `AmbientBroker`).
  - Infrastructure: Postgres, Valkey and moto of my own stack, `make e2e-up E2E_PROJECT=noema-live
    E2E_PORT_BASE=31400 E2E_BUILD=0`. Its api-1, api-2 and jobs containers were stopped, so the in-process api was
    the only process on the stack.
  - Endpoints: only secretsmanager and s3 are overridden (moto). Bedrock, Mantle and STS are the real us-east-1
    endpoints.
  - Tenant: t_acme is mapped to the ambient account by config versions proposed by operator A and approved by
    operator B through noema-admin (labeling sample rates 0; L8 adds the smoke router).
  - Contract validation: every response is checked client side (`ContractValidator`: JSON, SSE frame by frame,
    decision invariants) and by the server's test-mode validator. `contract_violations == []` is asserted after
    each test and at teardown.
  - Inputs carry a per-run tag, so no run hits the L2 decision cache of an earlier one. The rehearsal found that
    hazard: the second rehearsal's L5/L6 made 0 calls.
- **Mock rehearsal.** `NOEMA_LIVE_MOCK=1` runs the same tests against an in-process `tools/mockbedrock`
  (`tests/live/mock_scenario.yaml`, probe latencies, fake STS-shaped credentials, moto STS). It predicted the call
  counts (59 mock calls vs 57 live) and is the mock side of the fidelity diff.

## 2. Gate and item results

Gate ids are from spec-10 §3 and 06 §11. The live items L1-L9 are from spec-10 §6.

| Gate / item | Command | Pass criterion | Measured | Result |
|---|---|---|---|---|
| Live L1 (non-streaming Converse, GLM) | `NOEMA_LIVE=1 UV_NO_SYNC=1 uv run --frozen --no-sync pytest -p tests.live.bearer tests/live -m live -p no:cacheprovider -k "not l8"`, then the re-run `-k "l1 or (l2 and glm)"` | answer contains 391, `end_turn`, usage > 0 | effort `none`: **ValidationException** (see bug 1). Re-run at effort `low`: 391, `end_turn`, 428 ms | FAIL as specified (`none`); PASS at `low` |
| Live L2 (ConverseStream: Opus low, GLM) | same | well-formed stream (MessageStart … Stop, Usage), 391 in the answer, any reasoning before the text | Opus low: first text at 1,078 ms, no reasoning deltas. GLM `none`: rejected. GLM `low`: first text at 358 ms, no reasoning deltas | Opus PASS; GLM FAIL as specified, PASS at `low` |
| Live L3 (Mantle auth, DeepSeek; ADR-001 §6) | same | the shipped `mantle_auth` (bearer_short_term) works; SigV4 reported | short-term bearer presigned from IAM-user keys: ok, 323 ms. SigV4 with signing name `bedrock`: ok, 299 ms. Both answered 391 | PASS (both modes verified; tokens presigned from assumed roles stay unverified) |
| Live L4 (Sonnet 5.5 media, gap 43 / IMPL-043) | same | the config's image and document modalities are accepted, and the model reads 4471 from both fixtures | PNG and PDF (`DocumentBlock`) both accepted, 4471 read, 729 / 901 ms | PASS (gap 43 media closed) |
| Capability matrix: pre-wire refusals (spec-10 §2.2; 06 §10 "re-run the probes on every model change") | same (`test_capability_matrix_prewire_refusals`, 0 calls) | for each of the 7 models, every effort and temperature that `PROBED` rejects is refused before any byte (`invalid_request`, no provider code); `probe_discrepancies(config) == []` | all 7 models conform | PASS |
| Capability matrix: provider probes | same (`test_capability_provider_probes`, 3 calls with relaxed controls) | Sol effort `none` and Haiku `temperature` are still rejected (ValidationException → `invalid_request`); Sonnet effort `low` is reported | Sol `none`: rejected, and the list of supported values now includes `xhigh`. Haiku temperature: rejected ("`temperature` is deprecated"). Sonnet `low`: **accepted** (gap 43 effort: Sonnet takes `outputConfig.effort`, but the config lists none) | PASS. The matrix is stale for GLM `none` (bug 1) |
| Live L5 (`/v1/decisions`, lightweight-model-decide, jev, 3 questions) | same | exactly 3 calls (Luna, verbalized); Jev answers for the 3 questions; contract-valid | 3 calls (353-448 ms); department, urgency and refund_requested answered | PASS |
| Live L6 (`/v1/decisions`, router-default, 1 question; elicitation shapes) | same | 3-4 calls; quorum >= 2; DeepSeek carries logprobs; Luna and Haiku verbalized, without logprobs | 3 calls (DeepSeek 362 ms beat the 400 ms backup); members `token_logprobs` ok, `verbalized` ok, `verbalized` ok | PASS |
| Live L7 (one turn, router-default, new session, `verify: true`) | same | `turn.completed`, at most 28 calls, contract-valid | `turn.completed` after 22 calls (details in §3); TTFT 6,475 ms, e2e 9,881 ms; **verification `no_quorum`** on both questions (bug 2) | PASS on the item criterion; R5 machinery on live: FAIL (bug 2) |
| Live L8 / gate **L6** (pinned e2e within 5% of direct at p50) | `NOEMA_LIVE=1 NOEMA_LIVE_L8_PAIRS=5 … -k "l8 or zz"` | exactly 2 calls per pinned turn; p50 ratio <= 1.05 (live-n, gate P0) | 5 pairs, 2 calls per turn. p50 e2e: pinned 7,561 ms vs direct 6,919 ms → **1.093**. p50 first delta: 1,031 ms vs first text 758 ms → **1.36** | item PASS; gate L6 **FAIL** at n = 5 (reported; the gate itself is P0) |
| Live L9 (labeler@1, Sol and Sonnet) | phase-1 command | one parseable letter from each labeler, not refused | both parsed; Sol (effort medium) 1,055 ms, Sonnet 766 ms | PASS |
| **K1** (06 §8: tokens <= 2%, cost <= 5%) | `test_zz_estimator.py`, over every live cassette (`reports/live/estimator.json`) | every answered call has an estimate; thresholds per 06 §8 | 48 answered calls. Input-token error > 2% for 6 of the 7 models (§4) | **FAIL** (tokens); cost FAIL with the fake dev prices; cost against billing **NOT-RUN** (P0: no real prices, no billing data) |
| **Q1** (100% schema-valid responses) on the live api | every API test (`Checked` + server validator) | 0 contract violations | 0 violations on every `/readyz`, `/v1/decisions` and `/v1/turns` (SSE) response | PASS |
| **M1** (negotiation matrix), live part | L4 | the Sonnet 5.5 image and PDF cells are native, with no ValidationException | 2/2 cells native | PASS for the Sonnet cells; the other cells are mock-only (MED-08) |
| **L7** (first answer per route, Opus and GLM) | L2 | — | single samples: Opus low 1,078 ms, GLM low 358 ms (Sol low 681-841 ms, n = 5) | NOT-RUN as a gate (P0; single samples recorded) |
| Mock fidelity (spec-10 §2.3) | `UV_NO_SYNC=1 uv run --frozen --no-sync python -m tests.live.fidelity` (`reports/live/fidelity.json`) | event types and order, usage field names and error strings equal for the same scenario | 21 pairs, 14 identical, 7 differ (§5) | PARTIAL (differences reported; spec-10 sets no threshold) |

## 3. The live turn (L7) in detail

The stored turn and its decisions were read from the noema-live database.

- **Calls: 22.**
  - Router decision: 16 = 15 + 1 DeepSeek backup. Every member answered ok: DeepSeek 357-560 ms, Luna 391-602 ms,
    Haiku 654-944 ms, all inside the 1,000 ms deadline.
  - Generation: 2. The speculative spec0 (sol-fast, effort low) was cancelled at 955 ms. The decision answered
    complexity = hard (p 0.437), so `bindings[2]` set effort high, and Sol restarted at effort high (first text
    5,511 ms; `spec_restarts` 1, `wasted_output_tokens` 64).
  - Verification: 4 (answer-verifier, 2 questions × 2 members).
- **Routing.**
  - Intent: shipping, p 0.484 (DeepSeek 0.98 shipping, Luna 0.70 billing, Haiku 0.42 billing). No binding set the
    profile, so `default_profile` sol-fast was used.
- **Verification (decision status `partial`).**
  - Both questions: `no_quorum`.
  - Haiku stopped with `max_tokens` on 88/88 output tokens and no text: error `reasoning_only` ("The model spent
    its output budget on reasoning.").
  - Luna was cancelled at 1.07 s once quorum (2 of 2) could not be met. Status `late` (c) per spec-06 §9, which is
    correct.

## 4. K1: estimator accuracy (06 §8; n is small, so the results are reported)

The estimator is `HeuristicTokenEstimator` (chars/4, images, pages). It is compared with Bedrock's reported usage on
48 answered calls.

| Model | n | input error mean / max | within 2% | cost error with fake uniform prices, mean | output (chars/4 vs output tokens, streams) |
|---|---|---|---|---|---|
| luna-6 | 13 | 2.1% / 5.6% | 7 | 1.4% | — |
| deepseek-3.2 | 8 | 10.2% / 30.4% | 3 | 8.2% | — |
| sol-6.1 | 12 | 16.0% / 37.9% | 1 | 0.6% | 27% mean over 11 streams |
| haiku-5.5 | 8 | 26.3% / 29.2% (underestimate) | 0 | 18.3% | — |
| sonnet-5.5 | 4 | 24.9% / 48.4% | 0 | 20.9% | — |
| opus-5.5 | 1 | 48.4% | 0 | 34.9% | 67% (n = 1, 3 tokens) |
| glm-5.3 | 2 | 61.0% | 0 | 43.9% | 75% (n = 1, 4 tokens) |

The table is from `reports/live/estimator.json` (per call: estimate, input, output, reasoning, cached).

- **Short prompts.** Bedrock adds a fixed per-request overhead the estimator does not count: Opus and Sonnet 31 vs
  16 estimated, GLM 41 vs 16.
- **Claude tokenizers.** Haiku's decision prompts count about 1.36× chars/4.
- **Sol output.** At effort low, chars/4 overestimates the streamed text tokens by about 25% (English prose runs
  about 5 chars per token). At effort high, Sol bills hidden reasoning: 393 output tokens for 166 tokens of text
  (bug 3).
- **What this affects.** Completed calls are billed from the reported usage, so their usage lines are exact. The
  estimate is used for admission reservations and for the waste lines of cancelled calls. On live, admission
  would therefore reserve about 26% too little input for Haiku. C1 ("reservation >= issued") holds on the mock
  only because the mock's usage is chars/4 by construction.
- **Cost.** Real prices and billing data are missing, so cost accuracy against AWS billing is NOT-RUN (P0, 05 task
  0.10). The "cost" column only weights the token errors with the fake dev catalog (1.5 / 6 USD per million).

## 5. Mock fidelity: live cassettes vs the wire mock for the same tests

The diff pairs calls by (test, model, operation, call kind) and skips cancelled calls, whose shape depends on timing.
Of 21 pairs, 14 are identical, including:
- the 3 capability-probe error strings, which match the mock byte for byte (Haiku temperature, Sol effort `none`),
  and Sonnet `low`;
- GLM `complete`;
- Sonnet media;
- Luna decision members;
- Sol at effort low;
- the labelers.

The differences, in priority order for qa-tools (`tools/mockbedrock`):

1. **Haiku verification (verify-dist@1, effort low).** Live answers with `reasoningContent{reasoningText{text: "",
   signature}}` and `stopReason: max_tokens`, with no text block. The mock answers with a text block and
   `end_turn`. Claude's adaptive reasoning is not modelled, which is why bug 2 is invisible on the mock.
2. **GLM-5.3 effort `none`.** Live rejects it with ValidationException "Unsupported value: 'none' is not supported
   with this model. Supported values are: 'low', 'medium', 'high', 'xhigh', and 'max'." (param `reasoning_effort`).
   The mock accepts it. At effort low, live GLM streams no reasoning deltas, while the mock streams them and
   reports `reasoning_tokens`.
3. **Opus effort low.** Live streams no `reasoningContent`: messageStart, text, stop, metadata. The mock sends 100
   reasoning deltas and a signature. The mock's usage also reports `outputTokens` 401 for `maxTokens` 256, which
   Bedrock cannot return.
4. **Sol at effort high.** Live streams `contentBlockDelta.delta.reasoningContent.redactedContent` deltas. The mock
   never sends `redactedContent`.
5. **Mantle chat JSON.** Live has a top-level `service_tier`; the mock does not.
6. **Mantle auth.** Under a `bearer_short_term` scenario the mock refuses SigV4 with 401. Live accepts both modes.
   This difference is by design (one mode per scenario).
7. **Converse auth.** The mock requires `X-Amz-Security-Token` and refuses long-term IAM-user keys, which Bedrock
   accepts (the ambient dev path). That is why the rehearsal uses fake STS-shaped keys.
8. **Latency profile** (from the cassettes, not from the diff). Live Haiku took 647-942 ms against the probe table's
   890-1,800 ms, and live DeepSeek 299-553 ms against 470-590 ms. The mock therefore over-predicts DeepSeek backups
   (L7: 5 on the mock, 1 live) and Haiku deadline misses (every Haiku router call on the mock was cut at
   1,000 ms; none live).
9. **Cold first call.** On live, the first Luna call of the L8 session (a new connection) took 990 ms and was cut by
   the smoke router's 1,000 ms deadline. Later calls took 431-473 ms.

## 6. Bugs found (module code and config; for the fixers)

1. **major. GLM-5.3 no longer accepts effort `none` (capability drift since the 2026-10-09 probes).**
   - Stale claims:
     - `PROBED["us.zai.glm-5.3"]` (`src/noema/adapters/capabilities.py`) lists `none` as accepted.
     - The shipped config `models.glm-5.3.controls.effort` lists `none` (canonical `docs/noema/contracts/examples/platform.example.yaml`).
     - `generation_profiles.race-sol-glm` runs GLM at `none`.
     - spec-10 §6 L1/L2 and 03-env assume it.
   - Effect: every GLM attempt at `none` fails with `invalid_request` (`ValidationException`). The race profile
     degrades to Sol alone, and `probe_discrepancies` cannot see the problem.
   - Repro: cassettes `tests/cassettes/live-20261011T073404/01-glm-5.3-complete.json` and `03-glm-5.3-stream.json`.
     Or `ConverseAdapter.complete(ModelRequest("glm-5.3", ..., effort="none"))` against us-east-1.
   - Fix: owner decision on the config (GLM effort list without `none`, race member at `low`; `xhigh` is now
     accepted), `PROBED`, and the mock's `_GLM` effort set.
2. **major. Live verification never reaches quorum when Haiku reasons.**
   - `answer-verifier` member Haiku 5.5 (verify-dist@1, effort low) gets `max_tokens` 88, because
     `models.haiku-5.5.controls.reasoning: false` means no `EFFORT_RESERVE` is added (spec-06 §5).
   - Live Haiku 5.5 reasons adaptively at effort low. It spent 88/88 tokens on hidden reasoning (empty
     `reasoningText` plus a signature) and returned no text, so the member error is `reasoning_only`. With
     `min_quorum` 2 of 2, both verification questions end `no_quorum` and the turn's verification is `partial`.
   - The 6 live Haiku router calls (L6 and L7, 88-184 tokens) did not reason, so this is prompt-dependent. Any
     Haiku member can hit it.
   - Repro: L7 (`tests/live/test_api_live.py::test_l7_one_routed_turn_with_verification`); cassettes
     `live-20261011T073404/35-haiku-5.5-complete.json` and `37-...` (stop `max_tokens`, `text_chars` 0, output 88).
   - Owners: decision and platform config. Possible fixes: Haiku `reasoning: true` (a template-contract change, so a
     new version), or a reasoning reserve for Claude members. The mock should model it (§5 item 1).
3. **minor. `reasoning_tokens` is 0 when Bedrock hides the reasoning** (adapters, `wire.reasoning_estimate`).
   - Haiku returns `reasoningContent` with empty text, and Sol streams `reasoningContent.redactedContent`. In both
     cases the adapter counts 0 reasoning characters, so `UsageRecord.reasoning_tokens` is 0.
   - Example: Haiku 88 output tokens, all reasoning, reported as 0; Sol high 393 output tokens, about 227 of them
     hidden by a chars/4 estimate, reported as 0.
   - Effect: the usage breakdown and K1 "incl. reasoning" are wrong, and the waste line of a cancelled high-effort
     Sol stream (chars/4) undercounts.
   - Repro: the cassettes above and `live-20261011T073404/34-sol-6.1-stream.json` (wire events include
     `redactedContent`).
4. **major. K1 gate fails on live** (ops-ledger `HeuristicTokenEstimator`, which ops-admission uses for reservations).
   - Input error is above 2% for 6 of the 7 models. Claude is underestimated by about 26% (Haiku) and up to 48%
     (short Sonnet/Opus prompts), and the fixed per-request overhead is not counted.
   - Effect: on live, reservations under-reserve Claude input, so the C1 "reservation >= issued" property holds on
     the mock only.
   - Repro: `reports/live/estimator.json`.
   - Fix: per-model `chars_per_token` and per-request overhead (a Phase 0 calibration item).
5. **minor. Wire-mock fidelity gaps** (qa-tools; §5 items 1-5, 7 and 8).
   - The concrete mock bug: a reasoning stream whose `outputTokens` exceeds `maxTokens` (Opus: 401 > 256).
   - Repro: `python -m tests.live.fidelity` (`reports/live/fidelity.json`) and
     `tests/cassettes/mock-20261011T073845/02-opus-5.5-stream.json`.

Two harness issues were fixed in my own area, so they are changes, not bugs for the fixers:
- The CountingAdapter counted cancelled calls but did not record them.
- The old `test_api_live.py` activated the L8 config in memory, and the 5 s config watcher would have reverted it.
  It now goes through noema-admin.

## 7. Files (this track)

- Harness: `tests/live/{budget,conftest,livecfg,test_adapters_live,test_api_live,test_labelers_live,test_zz_estimator}.py`
  (changed); `tests/live/{bearer,wiretap,apistack,fidelity}.py` and `tests/live/mock_scenario.yaml` (new).
- Offline tests: `tests/unit/adapters/test_live_harness.py` (+3 tests).
- Docs: `docs/modules/adapters.md` (the tests/live description and the live-smoke known limits).
- Cassettes: `tests/cassettes/live-20261011T073404/` (40), `live-20261011T073549/` (2), `live-20261011T073610/`
  (15), and `mock-20261011T073845/` (the final rehearsal, 59).
- Reports: `reports/live/{calls,capability-prewire,capability-probes,mantle-auth,sonnet-media,decision-shapes,
  l7-turn,e2e-direct,e2e-pinned,estimator,fidelity}.json` and `reports/live/mock/*` (the rehearsal).
- Journal: `.orchestration/reports/live-calls.jsonl`.

## 8. Checks run, and what is not covered

- Lint and types on my paths:
  - `ruff check` / `ruff format --check tests/live tests/unit/adapters/test_live_harness.py`: clean.
  - `mypy --strict tests/live tests/unit/adapters/test_live_harness.py`: clean.
- `make test-unit`: 3,000 passed (8 of them in test_live_harness).
- `make forbidden-scan`: clean.
- **`make check` is not green at this moment, but not because of this track.** Two in-progress files of other Wave
  E tracks fail:
  - `make lint`: PT011 in the untracked `tests/unit/composition/test_workers.py`.
  - `make typecheck-tests`: 1 error in the modified `tests/integration/streaming/test_redis_mirror.py:193`.
  - The later steps did not run after the lint stop.
- Not run:
  - Short-term bearer tokens from assumed roles. The live smoke uses ambient IAM-user credentials, with no
    AssumeRole (D2 tenant accounts are P0).
  - Mantle streaming: no configured model streams over Mantle.
  - Cost against AWS billing (P0).
  - L7 per-route p50/p95 as a gate (P0; single samples only).
- The 3 calls left in the global budget are kept in reserve.
- The noema-live stack was torn down after the report (`make e2e-down E2E_PROJECT=noema-live E2E_PORT_BASE=31400`).
