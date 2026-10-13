# Wave E3 live smoke: re-verification on the final tree (spec-10 §6; owner decision 2026-10-12)

Role: live-smoke, the only role allowed to call Bedrock. Date: 2026-10-12, 11:11-11:13 UTC. Region us-east-1, passed
explicitly. Code under test: HEAD `1a28933` (tag wave-e2) plus the final E3 working tree. The sprint phase was
finished: no file under `src/` or `tools/` changed after 08:36 local, and the tree fingerprint was `c7ab5a60c98fb974`
(sha256 over every tracked, modified and untracked file of `src/` and `tools/`). The only edits are my own, in
`tests/live/**`, `tests/unit/adapters/test_live_harness.py` and `docs/modules/adapters.md`.

## 0. Call budget (exact)

| | Value |
|---|---|
| Hard cap | **87 ledger entries in total**. Owner approval of 2026-10-12: +30 calls for re-verification (IMPL-080 amendment, spec-10 §6). The cap was 60. It is written in `tests/live/budget.py` (`LIMIT = 87`, with the approval comment) and in the `tests/live/conftest.py` docstring |
| Before this role | 57 entries (Wave E) |
| **Used by this role** | **28 calls**: 12 Converse `complete` and 16 ConverseStream |
| **After** | **85 of 87; 2 calls left, unused** |
| By run | `20261012T111104` (items 1 and 2): 3. `20261012T111143` (items 3-6): 25 |
| By item | (1) L2 Opus low and GLM low: 2. (2) `:` model id: 1. (3)+(4) the verified turn on the GLM race: 7. (5) L6, 6 pairs: 18 (6 direct streams, 6 pinned turns of 2 calls each). (6) K1: 0 (it reads the cassettes) |
| By model | sol-6.1 13, luna-6 10, glm-5.3 2, haiku-5.5 2, opus-5.5 1 |
| Rehearsal | 2 runs against the wire mock (`NOEMA_LIVE_MOCK=1`) under a no-egress guard: 0 egress attempts, journal unchanged. The first rehearsal hit the default 60 s pytest timeout in L8 (now `timeout(1800)`); its cassettes were removed. The second (`mock-20261012T110809`) passed 6 of 6 with 28 mock calls, the same count the live run then used |
| Not model calls | `aws sts get-caller-identity` (pre-flight), `aws bedrock list-inference-profiles` (the `:` id check: control plane, no inference), and the brokers' one `GetCallerIdentity` per session |

Pre-flight before any call:
- `aws sts get-caller-identity --region us-east-1` returned account 976939723775 (IAM user `mltest`).
- `AWS_BEARER_TOKEN_BEDROCK` is set (length checked only; the value was never printed).
- Host quiet: 1-minute loadavg 2.4-3.1 for every live run (< 8). Session B was pinned to cores 8-11 (`taskset`).

## 1. Harness changes (tests/live/**; before → after)

| Change | Before | After | Why |
|---|---|---|---|
| Global cap | `LIMIT = 60` | `LIMIT = 87` (owner approval comment) | owner decision 2026-10-12 |
| `max_tokens` cap of decision and verification members | 256 | 1,024 (`MAX_TOKENS_DECISION`) | CR #26 gives Haiku's verify-dist@1 at low 600 tokens and its router intent call 696. A 256 cap would test a budget the config does not issue. Live, one Haiku verifier used **270** output tokens, so the old cap would have cut it |
| Cassette and journal model id | the alias's configured id | the id the request resolves to (`model_id(req)`: its snapshot's) | the `:` id call is journaled as its ARN |
| K1 fields per cassette | `estimated_input_tokens` (chars/4 in Wave E) | `input_chars` (code points of the system strings and text blocks), `estimated_input_tokens` (the session's estimator: the shipped per-model table) and `estimated_input_tokens_chars4` (the V1 rule, i.e. the before) | item 6: input chars and Bedrock tokens on every call |
| Signer shadow (`wiretap.py`) | none | every lean Converse request: `signed_headers` (the new `_sigv4.sign_post`) is compared with `botocore.auth.SigV4Auth` on the same request at the same `X-Amz-Date`. Only booleans are stored: equal, `%3A` in the path, session token present | live evidence for item 1's signer, at 0 extra calls |
| Decoder shadow (`wiretap.py`) | none | every ConverseStream: botocore's `EventStreamBuffer` gets the same chunks as the new `EventStreamDecoder`, and each message (headers, payload) is compared. Only counts are stored: chunks, bytes, messages, equal, botocore extra, botocore errors, fallback used | live evidence for item 1's decoder, at 0 extra calls |
| `:` model id test | none | `test_colon_model_id_through_lean_sigv4`: Luna through its inference-profile ARN | item 2 |
| Verified race turn | none (L7 costs 20-28 calls) | `test_l7_verified_turn_on_the_glm_race`: the smoke router (1 call), pinned `race-sol-glm`, `verify: true`, conditioning off, 7 calls | items 3 and 4 in one turn |
| L8 (gate L6) | 5 direct streams first (adapter test), then 5 pinned turns | pairs interleaved as spec-10 §6 says, the order alternating per pair; the number of pairs comes from the budget (`remaining // 3`, at most `NOEMA_LIVE_L8_PAIRS`); the report is rewritten after every pair; per-pair ratios and output tokens | item 5 |
| K1 report | chars/4 vs Bedrock over all cassettes | the shipped table out of sample (the E3 calls) next to chars/4; every live call with the shipped table; a refit on every live call (in sample and leave-one-out) | item 6 |

Offline tests (`tests/unit/adapters/test_live_harness.py`, 8 → 11): the journal limit tests moved to 87. The caps test
now expects 1,024 for decision members. New: `test_cassettes_keep_input_chars_and_the_resolved_model_id`,
`test_the_signer_shadow_compares_with_sigv4auth` (equal on an ARN URL; a forged `Authorization` is caught) and
`test_the_decoder_shadow_compares_with_eventstreambuffer` (7-byte chunks, all equal; a corrupting decoder is caught).

## 2. Results, in the priority order of the brief

| # | Item | Command (all: `NOEMA_LIVE=1 UV_NO_SYNC=1 uv run --frozen --no-sync pytest -p tests.live.bearer tests/live -m live -p no:cacheprovider -x`) | Before | After (this run) | Result |
|---|---|---|---|---|---|
| 1 | Lean ConverseStream, **Opus 5.5 low** | `-k "l2 or colon"` (run A) | Wave E (botocore `SigV4Auth`, aiobotocore's event-stream reader): first text 1,078 ms, no reasoning | first text **1,117 ms**, e2e 1,158 ms; MessageStart, Text, Stop, Usage; `end_turn`, "391"; 31 in / 3 out tokens; no reasoning deltas. Decoder: 5 of 5 messages equal to `EventStreamBuffer` (4 chunks), no fallback. Signer: equal to `SigV4Auth` | **PASS** |
| 1 | Lean ConverseStream, **GLM-5.3 low** | same | Wave E: first text 358 ms, 0 reasoning deltas | first text **397 ms**; **6 reasoning deltas before the text** (new at low, see §5 item 2); `end_turn`, "391"; 41 in / 10 out tokens (3 reasoning, estimated). Decoder: 15 of 15 equal (7 chunks). Signer: equal | **PASS** |
| 1 | New decoder and signer, **every E3 call** | runs A and B (wire-tap shadows) | offline only: goldens, the wire mock, fuzzing | signer: **28 of 28 requests** byte-identical to `SigV4Auth` (signed headers, order, values); decoder: **4,304 of 4,304 messages** identical over 16 live streams (3,856 chunks, 815,688 bytes), 0 botocore extra messages, 0 botocore errors, 0 fallbacks. Bedrock accepted every signature (no 403) | **PASS** |
| 2 | **`:` model id** through the lean SigV4 path | `-k "l2 or colon"` (run A) | never live; the configured ids have no `:` (no `-v1:0` id among the 7 models, `list-inference-profiles`) | `arn:aws:bedrock:us-east-1:976939723775:inference-profile/us.openai.gpt-6-luna`: path `model/arn%3Aaws%3Abedrock%3A...%3Ainference-profile%2Fus.openai.gpt-6-luna/converse`, signature equal to `SigV4Auth`, Bedrock answered "391" in **375 ms**, `end_turn`, region us-east-1; journal and cassette carry the ARN | **PASS** |
| 3 | **Verification quorum, CR #26 config** (L7 with verify on) | `-k "verified or l8 or zz"` (run B) | Wave E L7: Haiku `max_tokens` 88/88, no text (`reasoning_only`); both questions `no_quorum`, status `partial` | `turn.verified`: status **completed**; `answers_question` and `unsupported_claims` both **quorum 2/2** (required 2, refused 0). Haiku at low: `max_tokens` **600**, `end_turn`, **85 and 270** output tokens (80 and 265 of them hidden reasoning, estimated), text in both, 1,120 / 1,994 ms. Luna (effort none): 88, 13 / 17 tokens, 427 / 726 ms | **PASS** (R5 machinery live) |
| 4 | **GLM race at low** (`race-sol-glm`) | same turn | Wave E: GLM rejected at `none` (ValidationException), race = Sol alone (bug 1) | both started (Sol low, GLM low); **GLM won**: first text 497 ms into its stream, 335 output tokens (32 reasoning, estimated), `end_turn`; Sol `generation.cancelled{lost_race}` at 549 ms (no event received yet); no error on either. Turn: first delta 575 ms, e2e 3,420 ms, `turn.completed`; **7 calls** (router 1, race 2, verification 4), contract-valid | **PASS** |
| 5 | **L6**: pinned single-model e2e vs direct, p50 | same (run B), `NOEMA_LIVE_L8_PAIRS=8` (the budget allowed 6) | Wave E, n = 5, not interleaved: e2e ratio **1.093**, first delta 1.36 | n = **6 interleaved pairs**: p50 e2e pinned 5,946.5 ms vs direct 6,614 ms, **ratio 0.899**; p50 first delta 856.5 vs first text 854 ms, **1.003** (details in §3) | **PASS** at live-n (the gate proper stays P0) |
| 6 | **K1**: input chars and Bedrock tokens on every call; estimator error per model with the new table | `test_zz_estimator.py` (run B, 0 calls) | Wave E (chars/4): input error 2.1% (Luna) to 61% (GLM) | 25 answered E3 calls, out of sample for `TOKEN_ESTIMATOR_V1`: **14 of 25** within 2% (chars/4 on the same calls: 5 of 25); per model in §4 | **FAIL** on the 2% gate (much improved); cost vs billing NOT-RUN (P0) |
| Q1 | Contract validity on the live api | every API test (`Checked` + the server's test-mode validator) | 0 violations | 0 violations on every `/readyz` and `/v1/turns` (SSE) response | PASS |

## 3. L6 in detail (`reports/live/e2e-pairs.json`)

Each pair: one direct `Adapter.stream()` (sol-6.1, effort low, 512 tokens, in-process, measured at the adapter) and
one pinned turn (`/v1/turns`, `generation: {profile: sol-fast, effort: low, max_output_tokens: 512}`, conditioning off,
the smoke router with 1 Luna member). Both run on the same user text. The pinned turn is measured at the HTTP client
(first `generation.delta` frame, end of stream). The order alternates per pair. Sol first text is the pinned generation
call's `t_first_TextDelta_ms` in its cassette.

| Pair | Order | Direct first text / e2e (ms) | Pinned first delta / e2e (ms) | Sol first text in the pinned turn (ms) | Router (Luna) | Output tokens direct / pinned |
|---|---|---|---|---|---|---|
| 1 | direct first | 726 / 6,661 | 715 / 6,185 | 706 | 599 ms | 337 / 337 |
| 2 | pinned first | 982 / 6,929 | 1,383 / 5,995 | 1,378 | 490 ms | 329 / 289 |
| 3 | direct first | 691 / 7,941 | 1,009 / 5,898 | 727 | **cut at 998 ms** (`timeout:deadline`) | 364 / 325 |
| 4 | pinned first | 1,042 / 6,567 | 998 / 5,564 | 591 | **cut at 997 ms** | 335 / 309 |
| 5 | direct first | 605 / 6,440 | 674 / 5,741 | 652 | 410 ms | 347 / 315 |
| 6 | pinned first | 1,192 / 6,208 | 614 / 6,271 | 608 | 448 ms | 330 / 327 |
| **p50** | | **854 / 6,614** | **856.5 / 5,946.5** | | | 336 / 320 |

- **Ratios.** p50 e2e 0.899 and p50 first delta 1.003. The per-pair e2e ratio has median 0.878 (min 0.743, max
  1.010) and the per-pair first-delta ratio median 1.049 (0.515-1.46). Normalized by output tokens, the p50 e2e ms
  per token is 18.29 pinned vs 19.68 direct (ratio 0.929; per-pair 0.83-1.02), so shorter pinned answers explain
  only part of the e2e gap.
- **Gateway overhead on the first delta.** When the router answers before Sol's first text, the client first delta
  comes **5-22 ms** after Sol's first text (pairs 1, 2, 5, 6: 9, 5, 22 and 6 ms).
- **Holdback.** In pairs 3 and 4 the Luna router member hit the smoke router's 1,000 ms deadline. The commit-critical
  `intent` question holds the first delta until the decision ends (spec-04 holdback, `holdback_ms` 1,000; a pinned
  profile does not lift it, since tightening still applies), so the first delta came at 1,009 / 998 ms although Sol
  had text at 727 / 591 ms. This is spec behaviour: the pinned TTFT floor is the router decision's latency, capped
  at its deadline.
- **Method change vs Wave E.** Wave E ran its 5 direct streams first, then its 5 pinned turns (not interleaved), with
  the botocore signer and the aiobotocore stream reader. Its 1.093 and today's 0.899 therefore differ in method as
  well as in code. With n = 6 and Bedrock's TTFT spread (direct first text 605-1,192 ms), the ratio is a live-n
  reading. The gate proper (real n) stays P0.

## 4. K1: estimator error per model with the new table (`reports/live/estimator.json`)

The new table is `TOKEN_ESTIMATOR_V1` (`noema.config.models`; E2 defect 32). Each model has (chars per token,
per-request overhead): DeepSeek (4.12, 7), GLM (4.0, 25), the three Claude models (3.06, 10), Luna (3.99, 0) and
Sol (4.61, 11). It was fitted on the 48 Wave E calls, so the **25 answered E3 calls are out of sample**. Every E3
cassette records `input_chars`, the table estimate (`estimated_input_tokens`, what admission reserves), the chars/4
estimate (`estimated_input_tokens_chars4`, the before) and Bedrock's usage.

**Input tokens, E3 calls (out of sample).** Errors are mean / max absolute relative error, with the signed mean in
parentheses. Cost uses the fake dev prices.

| Model | n | Table: error | Table: within 2% | chars/4: error | chars/4: within 2% | Output, signed mean: chars/4 / table cpt | Cost within 5% |
|---|---|---|---|---|---|---|---|
| glm-5.3 | 2 | 6.2% / 12.3% (+6.2%) | 1 | 32.4% / 61.0% | 0 | -43.8% / -43.8% (n 2, reasoning-dominated) | 2 of 2 |
| haiku-5.5 | 2 | **1.1%** / 1.2% (+1.1%) | 2 | 23.9% / 24.0% | 0 | — | 2 of 2 |
| luna-6 | 8 | 8.5% / 42.9% (-3.0%) | 5 | 8.6% / 42.9% | 5 | — | 5 of 8 |
| opus-5.5 | 1 | 3.2% (-3.2%) | 0 | 48.4% | 0 | -66.7% (n 1: 1 vs 3 tokens) | 1 of 1 |
| sol-6.1 | 12 | 6.9% / 18.9% (-6.5%) | 6 | 20.8% / 43.2% | 0 | **+25.1% / +8.6%** (n 12 streams) | 12 of 12 |
| **all** | **25** | | **14 of 25** | | **5 of 25** | | 22 of 25 |

Where the table still misses:
- **Short prompts** (59-95 chars, the adapter-level calls). Bedrock's fixed overhead is above the table's:
  - Luna: 28 actual tokens for 59 chars, against a table overhead of 0 (-42.9%).
  - Sol: 36-37 actual tokens for 82-95 chars, against an overhead of 11 (-8% to -19%).
- **Luna verification prompts** (2,253-2,285 chars): overestimated by +10.7% and +11.0% (512 and 518 actual
  tokens).
- **GLM turn prompt** (589 chars): +12.3% (154 actual tokens).
- **The gateway's Sol generation prompts** (563-576 chars): exact to 1.5% (135-136 actual tokens). The Luna router
  prompts (827-845 chars) are within 0.9%.

**Every live call (71 text-only: Wave E + E3).** `estimator_fit.report`, mean absolute error:

| Model | n | chars/4 | Shipped table (within 2%) | Refit on all 71: (cpt, overhead) | Refit, in sample (within 2%) |
|---|---|---|---|---|---|
| deepseek-3.2 | 8 | 10.2% | 2.4% (5) | (4.12, 7) | 2.4% (5) |
| glm-5.3 | 4 | 47.3% | 3.7% (2) | (4.64, 27) | 0.6% (3) |
| haiku-5.5 | 10 | 25.8% | 1.6% (7) | (3.06, 10) | 1.6% (7) |
| luna-6 | 21 | 4.9% | 4.9% (14) | (4.24, 14) | 2.3% (12) |
| opus-5.5 | 2 | 50.0% | 1.6% (1) | (3.06, 10) | 1.6% (1) |
| sol-6.1 | 24 | 18.8% | 5.1% (15) | (4.73, 14) | 4.2% (12) |
| sonnet-5.5 | 2 | 37.4% | 0.0% (2) | (3.06, 10) | 0.0% (2) |

Leave-one-out error of the refit: Claude 2.1% (n 14), DeepSeek 3.2%, GLM 2.9%, Luna 5.8% (max 67.9%), Sol 5.1%.

**K1 verdict.**
- Input tokens <= 2%: **FAIL** at live-n. The table cuts the mean error 3-22x for Claude, GLM and Sol, but Luna and
  Sol mix prompt types that one (cpt, overhead) line per model cannot fit to 2% (leave-one-out 5-6%).
- Cost: fake prices only. Cost against AWS billing is NOT-RUN (P0).
- For the Phase 0 refit: per-call-kind fits (short adapter prompts vs templates), and more data per model. Opus,
  Sonnet and GLM have n <= 4.
- For the waste line of a cancelled Sol stream (`generation/run.py` still uses chars/4, misc-fixes §9): the table
  cpt (4.61) would cut the output estimate's error from +25.1% to +8.6% on these 12 streams.

## 5. Mock fidelity: E3 live cassettes vs the rehearsal (`reports/live/fidelity-e3.json`)

Command: `UV_NO_SYNC=1 uv run --frozen --no-sync python -m tests.live.fidelity --live "live-20261012T*" --mock
"mock-20261012T110809" --out reports/live/fidelity-e3.json`. It ran on its own output path, so the E2 diff in
`reports/live/fidelity.json` is untouched. Result: **8 pairs, 4 identical**, 1 live-only and 1 mock-only call (the
race winner: GLM live, Sol on the mock; timing). The differences, for qa-tools (`tools/mockbedrock`):

1. **The `p` member.** Live ConverseStream payloads carry an unknown `"p"` member on `messageStart`,
   `contentBlockDelta`, `contentBlockStop` and `messageStop`, but **not on `metadata`** (Opus, GLM and Sol alike).
   - Wave E taps never showed it, because aiobotocore's shape-driven parser drops members outside the model's shapes.
     `p` is in none of them (checked with botocore's `bedrock-runtime` model).
   - The lean `parse_event` returns the JSON payload itself on its fast path, so the tap now sees `p`.
   - `_ConverseRaw.feed` reads only the modeled keys, so the StreamEvents are unchanged. The decoder shadow shows the
     messages themselves are byte-identical.
   - The mock has the quirk (`stream.pad: n`), but it pads every event, `metadata` included, and the rehearsal
     scenario leaves it off.
2. **GLM-5.3 at low reasoned this time.**
   - L2, the same "17 * 23" prompt: 6 reasoning deltas before the text, 3 reasoning tokens. On 2026-10-11 it streamed
     none.
   - The race turn: 32 reasoning tokens.
   - The mock's E2 default (no reasoning for GLM at low, from n = 1) therefore holds only sometimes. GLM's reasoning at
     low is nondeterministic.
3. **Luna through an inference-profile ARN.** The mock answers `ValidationException` "The provided model identifier is
   invalid". It knows no ARN ids (by design of its catalog), so the rehearsal checks only the signer for this call.

## 6. Findings and notes

1. **minor (live smoke config / decision).** The Luna member hit its 1,000 ms deadline in **2 of 7** live router
   calls of this session (`timeout:deadline` at 997-998 ms, pairs 3 and 4). These were warm connections; the other
   calls took 410-599 ms. Wave E saw one cut, on a cold first connection.
   - For pinned turns this shows up as first-delta holdback (§3). For `router-default`, a missed Luna member lowers
     the quorum (2 of 3 still holds).
   - Luna's live tail is near the router deadline. Spec-06 latency tables and the mock's Luna row (350-610 ms) do not
     have this tail.
2. **note.** Haiku 5.5's hidden reasoning on verify-dist@1 at low ranges up to **265 tokens** (270 output) live.
   - CR #26's 600-token budget covers it with margin.
   - The old live-harness cap of 256 would not have, so the cap change in §1 was necessary for a valid CR #26 check.
   - The spec-10 §6 bullet "`max_tokens` cap 256 (generation 512; labeling ...)" should name the 1,024
     decision/verification cap (spec owner; not edited here).
3. **note (not verified live).** The signer's `X-Amz-Security-Token` branch.
   - Every live call used the ambient IAM-user long-term keys, so no request carried a session token (`session_token:
     false` in all 28 signer records).
   - Tenant AssumeRole credentials always carry one. That branch is verified offline only: `test_sigv4.py` goldens
     and fuzzing with tokens.
   - I tried to spend the 2 spare calls on it, with STS session credentials in a private temp file. The environment's
     permission policy denied writing credentials to a file, so nothing was written and the calls stay unused.
4. **No product bug found.** Every item behaved as specified: CR item 1 (GLM at low) and CR #26 hold on live.

## 7. Checks run

| Command | Result |
|---|---|
| `aws sts get-caller-identity --region us-east-1`; `AWS_BEARER_TOKEN_BEDROCK` presence | account 976939723775 (`mltest`); token set (length only) |
| rehearsal: `env -u AWS_* AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null NOEMA_LIVE=1 NOEMA_LIVE_MOCK=1 NOEMA_LIVE_STACK=noema-e3live NOEMA_LIVE_L8_PAIRS=6 NOEMA_TEST_MOTO_URL=http://127.0.0.1:36650 PYTHONPATH=<guard> uv run --frozen --no-sync pytest -p noegress tests/live -m live -k "l2 or colon or verified or l8 or zz"` | 6 passed, 28 mock calls, **0 egress attempts**, journal unchanged (57) |
| live run A: `... -k "l2 or colon" -x` | 3 passed, 3 calls (57 → 60) |
| live run B: `NOEMA_LIVE_STACK=noema-e3live NOEMA_TEST_MOTO_URL=... NOEMA_LIVE_L8_PAIRS=8 taskset -c 8-11 ... -k "verified or l8 or zz" -x` | 3 passed, 25 calls (60 → 85) |
| redaction grep over the new cassettes, `reports/live/**` and the journal (prompt fragments "17 * 23", "You are terse", "In about 300 tokens", "My package says", "TCP slow start"; "AKIA", "ASIA", "Bearer ", "AWS4-HMAC", "Signature=", "aws_secret", "ABSK") | 0 hits. The L8 report first carried the topic names; it now stores the pair index (test changed, both report files rewritten mechanically) |
| `python -m tests.live.fidelity --live "live-20261012T*" --mock "mock-20261012T110809" --out reports/live/fidelity-e3.json` | 8 pairs, 4 identical (§5) |
| `ruff check` / `ruff format --check tests/live tests/unit/adapters/test_live_harness.py` | clean |
| `mypy --strict tests/live tests/unit/adapters/test_live_harness.py` | clean (13 files) |
| `pytest tests/unit/adapters/test_live_harness.py` | 11 passed |
| `make check` (final tree + this role's edits) | **`make check: all green`**: lint, typecheck 327 + 590 files, importlint 27 kept, unit 3,447 passed, conformance 757 passed / 8 skipped, integration 573 passed / 21 skipped, web 86 passed, sdk 192 passed (5 min 26 s) |

## 8. Files

- Harness (changed): `tests/live/{budget,conftest,wiretap,test_adapters_live,test_api_live,test_zz_estimator}.py`.
  `test_l8_direct_streams` was removed; its direct streams now run interleaved inside `test_l8_pinned_turns`.
- Offline tests: `tests/unit/adapters/test_live_harness.py` (3 new, 3 updated to the 87 cap and the 1,024 cap).
- Docs: `docs/modules/adapters.md` (the tests/live entry: cap 87, caps, K1 fields, shadows, test list).
- Cassettes: `tests/cassettes/live-20261012T111104/` (3) and `live-20261012T111143/` (25); the rehearsal
  `tests/cassettes/mock-20261012T110809/` (28).
- Reports:
  - New: `reports/live/{lean-stream,colon-model-id,l7-verified-race,e2e-pairs,fidelity-e3}.json`.
  - Rewritten: `reports/live/estimator.json` (now the E3 K1 over every live call) and `reports/live/calls.json`
    (run B's 25 calls; run A's 3 are in their cassettes).
  - The Wave E `l7-turn.json`, `e2e-direct.json`, `e2e-pinned.json` and `fidelity.json` are untouched.
  - `reports/live/mock/*` holds the E3 rehearsal.
- Journal: `.orchestration/reports/live-calls.jsonl`, now 85 lines.

## 9. Teardown

- Stack `noema-e3live` (port base 36600; images `noema-{api,worker,media}:e3gc`, with api-1, api-2 and jobs stopped
  during the runs) removed with `make e2e-down E2E_PROJECT=noema-e3live E2E_PORT_BASE=36600`: 0 containers, 0
  volumes left.
- Its state files `~/.noema/stacks/noema-e3live.{json,env}`, the rehearsal guard and the inference-profile listing in
  `/tmp` were deleted.
- **2 live calls remain in the global budget (85 of 87)**, unused.
