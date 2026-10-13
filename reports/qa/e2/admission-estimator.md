# Wave E2: admission-estimator fixer (QL-2 / C1, QL-4, defect 32 / K1)

Role: admission-estimator fixer, 2026-10-11. Edits: `src/noema/ops/admission/**`, the TokenEstimator
(`src/noema/ops/cost/estimator.py`, new `src/noema/ops/cost/estimator_fit.py`), one constant in
`src/noema/config/models.py`, and their tests. I made no live Bedrock call and ran no git write command. My stacks:
`noema-e2adm` (postgres/valkey/moto on ports 294xx, plus the load api on 29490 and the mock on 29481) and
`noema-e2adm-e2e` (E2E_PORT_BASE 29600, images `noema-*:e2adm`). I tore both down at the end (section 7).

## 1. QL-2 (C1): the model-quota burst

**Cause.** In spec-11 §1.2 every bucket is a GCRA with `T = 60 000 / L` and `tau = 60 000` ms. A GCRA admits
`N` units in a window `W` only if `N * T < W + tau`. With `T = W / L` that bound is `L + tau / T`. So a window
that starts on a full bucket admits up to `2L - 1`. The QA lead measured 915 in C1, and the new regression test
counts 1,199 for one call every 10 ms.

Keeping `T = 60/L` while also requiring "every 60 s window <= L" forces `tau <= T`. A bucket with `tau = T`
can never accept a debit of more than one unit. That rules it out: a turn reserves 4 Sol calls, and a TPM debit
is thousands of tokens. A GCRA can only meet the window bound by lowering its rate to `L - b + 1` for a burst of
`b`, with `b` at least the largest debit. That is unusable for TPM. So no GCRA meets the brief's wording ("T =
60/rpm, burst chosen so the first minute <= rpm") for multi-unit debits. I implemented the bound itself instead.

**Fix (sliding-window quota buckets).** `mdl:{tenant}:{alias}:rpm|tpm` buckets are now built by
`gcra.quota_bucket` (`plan.py`). Key and project buckets and `authfail` stay GCRA, unchanged.
- Each bucket counts its units per 1 s slice (`QUOTA_SLICE_MS`). A debit of `n` is admitted iff the units in the
  current slice and the 61 before it (`QUOTA_SLICES = 62`), plus `n`, are <= `cap`. `cap` is the limit, or
  `floor(0.8 L)` for labeling (`LABELING_HEADROOM`, same semantics as before). A debit with `n > L` never passes
  and gets retry-after 60, as before.
- Units leave the count 61-62 s after their debit. Every 60 s window of admissions, and every 61 s window, holds
  at most `L`. The extra second (`QUOTA_MARGIN_MS`) covers calls sent up to 1 s after their admission (the
  router decision runs first), so the mock and Bedrock, which count call times, stay within the quota.
- A refund of `r` units takes them off the slice of their debit (`Debit.at_ms`, carried to both stores and to
  the marker undo). If that slice has already left the window, the refund does nothing.
- 429s: `retry-after` is the time until the oldest slices free enough room (Lua computes `waits`).
  `x-ratelimit-remaining` is `L - counted`. `x-ratelimit-reset` is the time until the newest counted slice
  leaves; after a debit, until the current slice leaves.
- Redis: a hash of slice index -> units under `RedisKeys.gcra("win:" + name)`. That key cannot collide with a
  GCRA string of the same bucket name during a rolling deploy (tested: no WRONGTYPE). TTL is 63 s. The
  `RESERVE` and `REFUND` scripts take 4 arguments per bucket (`slices` = 0 means GCRA) and return 4 values per
  bucket (before, after, wait, reset). `LocalStore` implements the same semantics (the Redis-down fallback
  divides `L` and `cap` by the pod count, IMPL-078). The all-or-nothing debit, markers, check-only mode and slot
  handling are unchanged.
- Files: `ops/admission/{gcra,lua,stores,admission,plan}.py`.

**Regression tests** (each fails with the old buckets: I swapped `plan.py` back to `admission_bucket`, ran the
tests, then restored it):

| Test | Before the fix | After |
|---|---|---|
| `tests/unit/ops_admission/test_quota_window.py::test_c1_the_first_minute_admits_at_most_the_rpm` (1 call every 10 ms for 130 s, rpm 600) | `assert 1199 == 600` | 600 in the first minute; busiest 60 s and 61 s windows 600; >= 1,200 in 130 s |
| `::test_c1_tpm_is_bounded_the_same_way` (tpm 100,000) | `assert 199000 == 100000` | busiest window 100,000 |
| `::test_no_60s_window_admits_more_than_the_quota` (hypothesis, 150 examples: arbitrary gaps 0 ms to 61 s, 0-12 calls, 0-300 tokens, consumed <= reserved, reconcile 0-70 s later; rpm in {1, 7, 10, 60}, tpm in {50, 1000}) | falsified (`51 <= 50` tokens) | no 60 s or 61 s window holds more kept calls or tokens than the quota |
| `::test_a_full_quota_says_when_it_frees_and_frees_then` | reset 60 (GCRA) vs 62 | headers 0 / 62; retry-after 32 at +30 s; still refused at +61.999 s, the full quota again at +62 s |
| `tests/unit/ops_admission/test_gcra.py::test_quota_buckets_are_windows_whose_cap_divides_per_pod` | new API | cap and per-pod division; wait 1,001 ms |
| `tests/integration/ops_admission/test_lua_quota_window.py` (T-GCRA-3, hypothesis on Valkey: script == `LocalStore` verdicts incl. waits/resets, identical stored slices, window property on what Redis admitted; waits/resets/TTL; marker undo exactly once; no WRONGTYPE next to a GCRA string) | new | 4 passed. A deliberate off-by-one in the Lua window was caught (then reverted) |

I also updated existing tests for the new interfaces: `test_failover_store.py` (the `at_ms` refund keyword) and
`test_lua_gcra.py` (the raw `RESERVE` arguments now have 4 values per bucket).

**C1 gate on my stack** (`tests.load.stack activate --quota t_acme:sol-6.1:600:100000000000`; `python -m
tests.load.run --state ~/.noema/stacks/noema-e2adm.json --api-port 29490 --mock-port 29481 --api-cpus 20-21
--stack-cpus 22-23 --mock-cpus 24,25 --driver-cpus 10-13,34-37 --plan <streams>:240 --warmup-s 60 --mock-quotas
'[{"model": "us.openai.gpt-6.1-sol", "rpm": 600 [, "akid": "*"]}]'`). One Converse mock process, so every Sol
call meets the same window. Counts come from the mock records (`/tmp/e2adm/c1_mock.py`, sliding 60 s windows
over `t_req_recv_ns`).

| Run | Sol calls at the mock | busiest 60 s (all AKIDs / per AKID) | mock throttles | per minute from the first call | admission 429s | overrun counter |
|---|---|---|---|---|---|---|
| QA lead e5-c1-1proc (GCRA, before) | 3,855 | 995 / 995 | **874** | - | 16,160 rpm + 1,239 queue | 0 |
| QA lead e5-c1 2 procs (GCRA, before) | 4,005 | **915** / 464, 451 | 0 (per-AKID mock) | 915, 686, 605, 600, 600, 599 | 37,908 rpm | 0 |
| e2adm c1-1proc (150 streams, 1 proc) | 2,316 | **546** / 546 | **0** | 546, 472, 421, 434, 443 | 16,602 rpm + 45 queue (server) | absent (0) |
| e2adm c1-2proc (200 streams, 2 procs, per-AKID mock) | 2,319 | **546** / 235, 311 | **0** | 546, 483, 416, 430, 444 | 21,439 rpm + 290 queue (server) | absent (0) on both |
| e2adm c1-2proc-shared (200 streams, 2 procs, `akid: "*"`: one window for the account, QL-4) | 2,335 | **546** / 296, 250 | **0** | 546, 499, 422, 424, 444 | 20,525 rpm + 284 queue (server) | absent (0) on both |

- C1's throttle-before-Bedrock half now holds in every run: no 60 s window exceeds 600 Sol calls, and the mock
  refused nothing. `aghoo_admission_degraded` stayed 0 and there was no server-side ERROR log.
- The drivers saw 1, 2 and 2 `transport:ReadError`s (no response headers, nothing logged by the api) among about
  23,000 requests per run. These are probably keep-alive races: a stream waits out its `retry-after` (p50 1 s, p90
  3-4 s, max 46 s with the window) and then reuses a pooled connection the server may close after 5 s idle. I did
  not prove this, and I do not attribute the errors to admission.
- These windows are invalid by spec-10 §5 latency validity (mock lag p99 2.8 ms; the host's loadavg was 8-18 from
  the other fixers' stacks). The C1 criteria are counts, so validity does not affect them.
- **Throughput cost (measured).** Sustained admission is about 430-445 Sol calls per minute here, against 600 for
  the GCRA. The cause is the worst-case reservation, not the window: a turn holds 4 Sol units (A1, restart, 2
  failure attempts) for its whole life of about 7 s, and refunds 3 only at reconcile. Held units count against
  the window, so the sustained rate is `L / (61.5 s + 3 D)`. A GCRA's rate does not depend on held units, which
  is exactly why it could not bound the window. To recover most of it, see not_fixed item 6.
- **Known limit.** A call sent more than 1 s after its admission (a fallback or restart attempt after a timeout)
  is counted in its admission slice. A Bedrock window around its send time can then overlap up to `d - 1` s of
  later admissions. The airtight variant counts consumed units at their send time (or moves them to the
  reconcile slice). That needs `consume` timestamps carried to reconcile, so it is a design item for the spec
  agent (section 2).

## 2. Rulings for the spec agent

**QL-2 (spec-11 §1.2, replaces the model-quota line of the GCRA block).**
- `mdl:{tenant}:{alias}:rpm|tpm` are sliding-window buckets, not GCRA. Units are counted per 1 s slice. A debit
  of `n` is allowed iff `sum(units of the current slice and the 61 before) + n <= cap`. `cap = L` for user
  traffic, `floor(0.8 L)` for labeling. `n > L` never passes (retry-after 60). Debited units leave 61-62 s after
  their debit, so no 60 s window admits more than `L`, including calls sent up to 1 s after admission.
- `refund r` takes `r` units off the debit's own slice, if that slice is still counted.
- `retry_after_s = ceil(ms until enough of the oldest slices leave / 1000)`, at least 1.
  `remaining = L - counted`. `reset_s` = until the newest counted slice leaves.
- Redis key `RedisKeys.gcra("win:" + bucket)` (a hash). Platform could add a `RedisKeys.quota_window` method;
  the docstring of `RedisKeys.gcra` says "String".
- The Redis-down fallback divides `L` (and `cap`) by `NOEMA_EXPECTED_PODS`. Key, project and `authfail` buckets
  keep `T = 60 000 / L`, `tau = 60 000` unchanged.
- Rationale for the record: for `n > 1` debits, no GCRA with `T = 60/L` keeps a 60 s window at `<= L` (section 1).
- Accepted cost: with worst-case reservations, the sustained rate is `L / (61.5 s + held units x hold time)`.
- Open design item: count consumed units at their send time (`consume` timestamps carried to reconcile), so
  fallback attempts sent more than 1 s after admission are also airtight.

**Defect 32 (spec-11 §2 and IMPL-029c, replaces "4.0 for every Phase 1 model").**
- `TokenEstimator.estimate(alias, ...) = overhead(alias) + sum ceil(chars / cpt(alias))` over the system strings
  and text blocks, plus the unchanged image and document terms.
- `(cpt, overhead)` per alias comes from `TOKEN_ESTIMATOR_V1` (noema.config.models), fitted by
  `noema.ops.cost.estimator_fit` on the Wave E live calls. An alias missing from the table counts 4.0 and no
  overhead.
- A Phase 0 refit replaces the table without a code change (section 4).
- The shared conformance vectors (`tests/contract_internal/test_token_estimator.py`, fakes-telemetry) pin the V1
  arithmetic. The real estimator is now registered for them with the empty table
  (`tests/unit/ops_ledger/conformance_impls.py`). The fitted table is tested in
  `tests/unit/ops_ledger/test_estimator.py`.
- The "exact for the vectors" sentence of spec-11 §2 should name both.

## 3. QL-4: per-account quota windows in the mock

QL-4 lives in `tools/mockbedrock`, which is not mine. During this wave the mock gained `akid: "*"`: one window
shared by every AKID (`tools/mockbedrock/state.py`, `SHARED`; the contracts-config fixer's edit). That is the
account-scoped window QL-4 asked for. I used it as-is for the C1 run `c1-2proc-shared` (section 1); I did not
edit the mock.
- In the deployment shape (2 api processes, 2 STS sessions, one account window) the Sol calls stayed at <= 546 in
  every 60 s window, with 0 mock throttles. The C1 overshoot (915 / 874 throttles) is gone.
- The QA lead's C1 table and the gate's command line should use `akid: "*"` from now on.

## 4. Defect 32 (K1): per-model chars/token and per-request overhead

**What the cassettes hold.** The 48 answered live calls of `tests/cassettes/live-20261011T07*` do not keep the
request text. They keep `estimated_input_tokens`, recorded with the chars/4 rule (rounded up per item), so the fit
takes `chars = 4 x estimated_input_tokens`. That is exact up to 3 chars per text item. `estimator_fit` already
reads an `input_chars` field when a cassette has one (see not_fixed item 3). I fitted the 46 text-only calls; the
2 Sonnet media calls (image, PDF) would confound the text and image terms (n = 1 each), so the image and document
terms are not refit.

**Method.** One fit per group. The 3 Claude models share a tokenizer and form one group (Sonnet and Opus both
answer 31 tokens for the same 16-token probe). The model is `input = overhead + ceil(chars / cpt)`, with cpt on a
0.01 grid in [2, 8] and an integer overhead in [0, 100]. The fit minimizes the mean absolute relative error and
breaks ties on the max error. I compared this with a minimax fit: the mean objective put more calls within 2%
(33 vs 17 of 46) and had the lower leave-one-out error (except Luna). GLM has a single prompt length (16 -> 41
twice), so its chars/token cannot be fitted: it keeps 4.0 and fits only the overhead (25).

**Table and fitted errors** (input tokens vs Bedrock `usage.input_tokens`; `python -m noema.ops.cost.estimator_fit
tests/cassettes/live-20261011T073404/*.json tests/cassettes/live-20261011T073549/*.json
tests/cassettes/live-20261011T073610/*.json`):

| Model | n | chars/token | overhead | chars/4: mean / max / within 2% / bias | fitted: mean / max / within 2% / bias |
|---|---|---|---|---|---|
| deepseek-3.2 | 8 | 4.12 | 7 | 10.2% / 30.4% / 3 / -10.2% | 2.4% / 9.9% / 5 / -1.9% |
| glm-5.3 | 2 | 4.00 | 25 | 61.0% / 61.0% / 0 / -61.0% | 0.0% / 0.0% / 2 / +0.0% (one point: overfit) |
| haiku-5.5 | 8 | 3.06 | 10 | 26.3% / 29.2% / 0 / -26.3% | 1.8% / 5.3% / 5 / -0.6% |
| luna-6 | 13 | 3.99 | 0 | 2.1% / 5.6% / 7 / -0.1% | 2.1% / 5.3% / 9 / +0.4% |
| opus-5.5 | 1 | 3.06 | 10 | 48.4% / 48.4% / 0 / -48.4% | 0.0% / 0.0% / 1 / +0.0% |
| sol-6.1 | 12 | 4.61 | 11 | 16.0% / 37.9% / 1 / -10.1% | 2.1% / 7.8% / 9 / -0.6% |
| sonnet-5.5 | 2 | 3.06 | 10 | 37.4% / 48.4% / 0 / -37.4% | 0.0% / 0.0% / 2 / +0.0% |

- Leave-one-out (held-out call vs the fit of the others): Claude 1.5% mean / 5.3% max (8 of 11 within 2%);
  DeepSeek 3.2% / 9.9% (2 of 8); Luna 2.9% / 7.9% (5 of 13); Sol 2.5% / 9.3% (8 of 12).
- Within 2%: 33 of 46 calls fitted vs 11 of 46 with chars/4. The systematic under-reservation is gone: Claude was
  26-48% under, now at most 0.6% under on average.
- The per-call 2% gate is still not met everywhere: the max errors are 5-10%. Within one model the ratio of
  chars to tokens depends on content (DeepSeek decision prompts: 174 -> 175 tokens but 292 -> 323), so two
  parameters per model cannot reach 2% per call. K1 stays a Phase 0 item with more data per model. n is tiny for
  Opus (1), GLM (2, one length) and Sonnet (2).
- Output side (reported, not in the table: the TokenEstimator only estimates input). For Sol streams at effort
  low (n = 10), the text runs 4.97 chars per output token. `ceil(chars / 4.97)` gives a mean error of 4.6% (max
  9.0%) against 24.2% for chars/4. The generation waste estimate uses `CHARS_PER_TOKEN_V1` (not_fixed item 4).

**Override for Phase 0 refits.**
- `HeuristicTokenEstimator(table=...)` takes any alias -> `TokenFit` mapping; `{}` gives the V1 rule.
- `load_table(path)` reads the JSON that `estimator_fit` writes (`{"models": {alias: {"chars_per_token",
  "request_overhead_tokens"}}}`) and rejects invalid fits with `ValueError`.
- The default is `TOKEN_ESTIMATOR_V1` in `noema/config/models.py`.
- I did not do the deployment wiring (a setting or config field that passes the path): see not_fixed item 2.

**Regression tests** (`tests/unit/ops_ledger/test_estimator.py`):
- `test_k1_input_errors_on_the_wave_e_live_calls`: every text-only live call is replayed through the estimator;
  per model the mean error must be <= 2.5%, the max <= 10% and the bias >= -2%. Before the fix (the estimator
  with the V1 rule, a temporary mutation that I reverted) it fails: `assert (0.3737 <= 0.025)` (Sonnet). After:
  passes.
- `test_the_shipped_table_is_the_fit_of_the_wave_e_live_calls`: the CLI's fit of the 48 calls equals
  `TOKEN_ESTIMATOR_V1`, round-trips through `load_table`, and invalid files raise.
- `test_each_alias_counts_with_its_fit_and_unknown_ones_with_chars_over_four`.
- `test_items_round_separately_and_add_up` now checks both the V1 arithmetic and the fitted Opus value.
- I removed the old `test_every_phase1_alias_counts_alike`: it asserted the old rule, which is the defect.

## 5. Checks (commands and results)

| Command | Result |
|---|---|
| `uv run pytest tests/unit/ops_admission/test_quota_window.py` with `plan.py` temporarily back on `admission_bucket` (pre-fix) | 3 deterministic tests fail (1199 != 600, 199000 != 100000, reset 60 != 62); the hypothesis test is falsified (`51 <= 50`) |
| `uv run pytest tests/unit/ops_admission` / `tests/integration/ops_admission` (Valkey) | 71 passed / 22 passed (incl. T-GCRA-3) |
| `uv run pytest tests/contract_internal/test_admission.py tests/contract_internal/test_token_estimator.py` | 31 passed (fake and real implementations) |
| `uv run pytest tests/unit/ops_ledger` | 105 passed. With the V1 rule as a temporary mutation, `test_k1_input_errors_on_the_wave_e_live_calls` fails (`0.3737 <= 0.025`) |
| `make lint` / `mypy --strict` on my paths | clean |
| C1 load gate, my stack, 3 runs (section 1 table) | 0 mock throttles, busiest 60 s <= 546 (quota 600), overrun 0 |
| `make e2e E2E_PROJECT=noema-e2adm-e2e E2E_PORT_BASE=29600` (images `noema-*:e2adm` from the working tree) | **52 passed** (163 s) |
| `make check XDIST=4` (full, final tree) | **all green**: lint, typecheck(s), importlint, forbidden-scan, ownership, contracts-check, unit 3,284, conformance 757 (+8 skipped), integration 561 (+21 skipped), sdk-check |

I did not run qa-fault, qa-security or qa-contract. My changes touch neither the fallback path (F1-F8, R6 is
another fixer's) nor the authz or contract surfaces. No fault, security or contract config sets `model_quotas`,
so the window buckets only run under C1. The estimator change reaches every stack, and `make e2e` covered it.
K1 itself is live-only: no live calls in this wave, so I evaluated it offline on the cassettes (section 4).

## 6. Not fixed / handed over

1. **Spec and docs** (spec agent, docs owners): spec-11 §1.2 and §2, IMPL-029c, `docs/modules/ops_admission.md`
   and `docs/modules/ops-ledger.md` per section 2. I may not edit those files.
2. **Override wiring for Phase 0** (platform + integrator). The table can be overridden in code
   (`HeuristicTokenEstimator(table=load_table(path))`), but nothing passes a path in a deployment. Either a
   `ProcessSettings` field (e.g. `NOEMA_TOKEN_ESTIMATOR_TABLE`, a file path) that `composition._wire_platform`
   passes to the estimator, or a config schema field `models.<alias>.estimator {chars_per_token,
   request_overhead_tokens}` (contracts-config; `config.schema.json` has `additionalProperties: false`).
3. **Refit input** (qa / live-smoke, `tests/live/budget.py`). Record `input_chars` (the sum of system string and
   text block lengths) in every cassette. `tests/live/conftest.py` now records the fitted estimate, so 4x
   `estimated_input_tokens` would no longer be the text length; `estimator_fit` already prefers `input_chars`.
4. **Output side** (generation): the waste estimate of cancelled streams (`generation/run.py`, `policy.py`) uses
   `CHARS_PER_TOKEN_V1`. Sol at effort low measures 4.97 chars per output token (24% -> 4.6% error). Owners:
   generation and adapters (`ModelCapabilities.chars_per_token`).
5. **Fake fidelity** (fakes-telemetry): `tests/fakes/ops_admission.py` still runs a GCRA for `mdl:` buckets.
6. **Quota throughput** (orchestrator / generation + spec): give back the attempt units that can no longer be used
   once the turn commits (restart and failure attempts), instead of at reconcile. That would recover most of the
   measured 600 -> about 440 Sol calls per minute under C1. It needs a partial-refund call on `Reservation` and a
   caller at commit.
7. **Delayed calls** (spec agent design item, section 2): send-time accounting for calls made more than 1 s after
   admission.
8. **K1 per call <= 2%** is not reachable with two parameters per model (content-dependent ratios; max errors
   5-10%): a Phase 0 refit with more data. Cost accuracy needs real prices and billing (P0). The image and
   document terms were not refit (n = 1 each).
9. **RedisKeys** (platform): a `quota_window(bucket)` key method; the window hashes now sit under
   `gcra:win:<bucket>`.

## 7. Stacks and teardown

- `noema-e2adm`: compose postgres/valkey/moto (`NOEMA_PG_PORT=29432 NOEMA_MOTO_PORT=29450
  NOEMA_VALKEY_PORT=29479`), bootstrapped and load-activated. Load api on 29490/29491, mocks on 29481/29482,
  CPUs 20-25 and 10-13/34-37. Raw data is in `/tmp/e2adm/runs/{c1-1proc,c1-2proc,c1-2proc-shared}` (not in the
  repo).
- `noema-e2adm-e2e`: `make e2e` stack on E2E_PORT_BASE 29600.
- Teardown: both stacks are down with their volumes (`docker compose -p noema-e2adm ... down -v`, `make e2e-down
  E2E_PROJECT=noema-e2adm-e2e`); the image tags `noema-*:e2adm` and the state files `~/.noema/stacks/noema-e2adm*`
  are removed. No container or port of mine is left.
