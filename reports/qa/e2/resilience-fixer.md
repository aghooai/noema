# E2 resilience fixer (Wave E2, 2026-10-11)

Role: resilience fixer. Edits limited to `src/noema/adapters/**`, `src/noema/generation/**` and their tests. Bedrock-
only: no live Bedrock call, no non-Bedrock model API, no AWS resource touched, no git write command. Own stack
`noema-resil` (port base 32400), torn down at the end; the shared `noema-dev` was not touched.

**Revision measured.** The working tree is shared with six fixers mid-edit (e.g. `deploy/docker/api.Dockerfile`,
`make/infra.mk`, `src/noema/core/text.py`). So that the gate numbers measure this role's change only, the stack ran
images built from a frozen tree: `git archive HEAD` (4bc26a7, Wave E) into `/tmp/noema-resil-tree` plus this role's
five source files (`cmp` equal for all five after every rebuild; `diff -rq` against the live `src` shows only
other roles' in-flight files); images `noema-{api,worker,media}:resil`. The A/B control image `noema-{api,worker}:resilbase` is HEAD
alone (`/tmp/noema-resil-base`). Every unit, integration and check run below used the live working tree.

## 1. Items

| Item | Fix | Regression test | Before the fix | After |
|---|---|---|---|---|
| **R6** (owner ruling; fault B1 / CR item 5): a fallback to a different model consumed the retry budget, so F1, F4-F8 refused 20-60 % of pre-commit fallbacks | `generation/walk.py` `_start`: a failure-driven attempt spends `RetryBudget.try_spend` only when it is a same-model re-send (`any(a.model_id == model.id for a in self._attempts)`: the default profile's member after the route's, a race member re-run at the default; the throttle retry stays prepaid). A fallback to another model spends no budget of either model; it is bounded by the turn's `AttemptBudget` (the profile attempt cap, checked as before) and by admission (the reservation already counts every attempt). Docstrings of `walk.py` and `adapters/resilience/retry_budget.py` state the rule | `tests/unit/generation/test_walk.py::test_a_fallback_to_another_model_is_not_a_retry` (503 and throttle, both models' budgets exhausted: served, 0 units spent), `::test_a_re_send_to_a_model_that_already_failed_is_a_retry` (default-profile Sol after opus-deep's Sol: 1 unit; refused with Sol's budget spent); `test_attempt_fsm.py::test_t11_the_walk_starts_the_next_member` now expects 0 units. Property: `tests/unit/generation/test_fallback_slo.py::test_pre_commit_fallback_success_is_at_least_99_percent[7 rows]` (real `SlidingRetryBudget` + `BreakerRegistry`, real engine, 200 turns per row, one account) | walk tests 3/3 FAIL (`[(Opus, failed)]` only; `2 == 1`; `1 == 0`); property 7/7 FAIL: served/failed-first F1 11/22, F4 11/61, F5 10/140, F6 11/22, F7 reasoning-only 10/200, F7 empty 10/200, F8 11/22 (the budget's floor of 10 per minute) | PASS (every row 100 %) |
| **R6, F12 side** (the storm bound must hold) | none needed beyond R6: same-model re-sends are the only retries and stay budgeted | `test_fallback_slo.py::test_a_retry_storm_amplifies_no_failing_upstream_by_more_than_1_1[5xx-0.25, 5xx-0.5, throttled-0.25]` (750 turns at 12.5/s for 60 s, all three models failing at `rate`): per model re-sends <= max(10, 10 % of its first sends), amplification <= 1.1 against every model with >= 100 first sends, upstream requests per turn <= 1.1 at 0.5, <= 3 attempts per turn | guard (passed before too: the old rule was stricter) | PASS; simulated: Sol 399 req / 27 re-sends (1.073) at 5xx 0.25, Sol 409 / 36 (1.097) at throttles 0.25; 0.201 requests per turn at 0.5 |
| **Io #10 rest** (e4-engines-fix row 10): one tenant's slow calls could open the shared service breaker through the p95-TTFT rule | `adapters/resilience/breakers.py`: TTFT samples carry their tenant account; the rule opens only when the window p95 > `ttft_factor` x baseline **and** samples above that limit came from >= `BreakerPolicy.ttft_min_accounts` distinct accounts (default 2, configurable like every `BreakerPolicy` field). The quorum spans processes (section 3 shows why a local one fails F3b): a process whose window breaches without the quorum publishes `{state: "ttft_breach", accounts, limit_ms, until_ms}` on `breaker_events`; every process counts fresh remote accounts and answers with its own accounts above that limit (an unarmed process included). A breaching window never feeds the baseline (unchanged invariant), so one tenant cannot drift it up either | `tests/unit/adapters/test_breakers.py::test_one_accounts_slow_calls_never_open_the_shared_breaker_by_the_ttft_rule` (40 s of account A at 4x: closed; one breaching sample of B: open), `::test_the_ttft_rule_account_quorum_is_configurable`, `::test_breach_evidence_of_other_processes_completes_the_account_quorum[both-armed, second-unarmed]` (two registries, events exchanged each second: A slow on one, B slow on the other -> both open), `::test_one_account_slow_on_every_process_still_never_opens` (payload checked, no answer without a new account), 6 new malformed `ttft_breach` cases; `test_p95_ttft_above_three_times_the_baseline_opens` now slows both accounts | quorum tests 2/2 FAIL (`'half_open' == 'closed'`: A alone opened it; `TypeError`: no field); cross-process tests FAIL on the local-only quorum (`'closed' == 'open'` second-unarmed) | PASS (`test_breakers.py` 41, `test_resilience_redis.py`, health-registry conformance) |
| **F3b stale re-open** (pre-existing, found while verifying io #10; blocks F3b on 2 processes per pod, section 3) | `breakers.py` `_check_ttft`: the check now runs over the window **plus** the incoming sample, and the TTFT rule needs that latest sample above the limit too. Before, the check ran before the incoming sample joined the window, so the first fast samples after a recovery were judged on a window refilled with slow samples after a half-open close, and the breaker re-opened, refusing healthy traffic for another `open_s` (15 s). A sustained slowdown still opens on its next slow sample. The answering process also answers at once when an ask arrives, and reports each account at most once per window (`told`), so an evidence round trip does not wait for the next sample | `test_breakers.py::test_the_ttft_rule_never_reopens_on_samples_from_before_a_restore` (probe-close while slow, 19 slow samples, restore: stays closed), `::test_a_pending_breach_opens_as_the_answer_arrives_without_new_traffic[in-time, late]`, `::test_an_ask_is_answered_at_once_from_the_current_window` | stale test FAIL (`'open' == 'closed'` at the 2nd fast sample); pending and immediate-answer tests FAIL on the previous version (`'closed' == 'open'`; no answer event; a repeated ask) | PASS |
| **Fault B1** (text-less stops never opened the breaker, so every F7 turn needed a fallback) | `generation/run.py` `_record_health`: `reasoning_only` / `empty_text` record `error` for the service breaker. Exception: a reasoning-only stop under a cap the request lowered (`Attempt.client_capped`, set in `walk._start` when `max_tokens < attempt_max_tokens(None, member, model)`) is tenant-shaped and not recorded, so a tenant cannot open the shared breaker by pinning a tiny `max_output_tokens` (assumption of this role, in the spirit of io #10; see section 5) | `tests/unit/generation/test_usage_and_telemetry.py::test_text_less_stops_are_service_breaker_failures` (GLM reasoning-only and empty: `error`; `max_output_tokens: 64`: not recorded) | FAIL (`('us.zai.glm-5.3', 'ok', 200)` recorded) | PASS; fault F7: 50 of 100 turns reached GLM (was 100/100), GLM skipped once its breaker opened |
| **SigV4 `:` model ids** (io finding 1, verify the lean path against botocore) | test only: the byte-compare against aiobotocore now covers `-v1:0` ids (plain and `us.` profile), an inference-profile ARN, an application-inference-profile ARN and a foundation-model ARN, on Converse **and** ConverseStream (URL, body, every SigV4 header equal); a second test asserts the transport's `URL(url, encoded=True)` request-line path is the signed one (`%3A` kept) and that a str URL would re-quote it | `tests/unit/adapters/test_lean_converse_wire.py::test_model_ids_are_percent_encoded_as_botocore_does[12 cases]`, `::test_a_colon_model_id_goes_on_the_wire_as_signed[10 cases]` | guards (the lean signing was already right; the io fix is in the transport, covered by `tests/integration/adapters/test_unary_transport.py`, which passes) | PASS (60 in the file). Not verified against live Bedrock (no live budget in this wave) |

## 2. R6 and the F12 measure (needs a ruling on how F12 is measured)

R6 makes a cross-model fallback a first send of its model, not a retry. `tests/fault/test_f12_retry_storm.py` (QA,
not this role's file) asserts `generation requests at the mock / turns <= 1.1` for **both** rows; its docstring calls
the `rate: 0.25` row informational (breakers closed, "the retry budget alone bounds the amplification"). Under R6
that measure counts every fallback: at 25 % 503s on Sol, Opus and GLM, an opus-deep or glm-reason turn whose first
member fails goes on to Sol (+0.25 x 2/3 requests per turn), so the row reads about 1.2 by construction. Measured
on this role's stack (section 4):

| Row | Test measure (requests / turns) | Per failing upstream: requests / first sends (re-sends) | Retry-budget bound per (account, model) |
|---|---|---|---|
| F12 rate 0.5 (gated; breakers open) | **0.429** (322 / 750); run 1: 0.395 | Opus 44 / 44 = 1.000; GLM 228 / 228 = 1.000; Sol 50 / 42 = 1.190 (8 re-sends); run 1: Sol 137 / 123 = 1.114 | floor regime: < 100 first sends per account and minute; 8 <= 2 accounts x 10 |
| F12 rate 0.25 (informational) | **1.221** (916 / 750): FAIL as asserted; run 1: 1.219 | Opus 250 / 250 = 1.000; GLM 250 / 250 = 1.000; Sol 416 / 382 = **1.089** (34 re-sends); run 1: Sol 1.098 | ratio regime: 34 <= 10 % of 382 |

Final run: `reports/qa/e2/resilience/fault-results-final.json`; run 1: `fault-results.json`.
Re-sends = the 3-attempt turns (Opus or GLM, Sol, then the default profile's Sol); no other path re-sends a model.
Against each failing upstream the re-send amplification stays <= 1.1 wherever the budget's ratio applies (>= 100
first sends a minute); below that the pre-existing floor (`min_retries` 10 per (account, model) and minute, IMPL-016
as built) allows up to 10 extra requests a minute, which is what Sol's 1.19 / 1.114 at rate 0.5 are. The same holds in the
unit property (`test_fallback_slo.py`). **Request:** the QA fault owner (or the spec agent recording R6) should
restate F12 as same-model amplification per failing upstream (requests to X / first sends to X), keeping the
requests-per-turn form for the breaker-open row; this role does not edit `tests/fault`. Keeping requests per turn
<= 1.1 at rate 0.25 would require charging cross-model fallbacks again, which R6 forbids.

## 3. F3b (p95-TTFT rule) on two api processes per pod: why the quorum is cross-process, and the stale re-open

Since perf sprint B every pod runs 2 uvicorn processes, and each keeps its own breaker window and baseline. F3b's
clients hold one keep-alive pool per (tenant, replica) at 2.5 turns/s, so a process often sees one tenant, about 18
samples per 30 s window (below `min_requests` 20). In the slow phase the longer streams open new connections, which
land on the idle process. That process has no baseline, learns the slow latency as its own (2,403-2,414 ms) and never
breaches. Diagnostic images (`/tmp/noema-resil-diag`, logging every check with its pid; never in the repo) showed:

- a local-only quorum (v1) and evidence from breaching processes only (v2): the armed process breached on
  `444455556666` alone, while `111122223333`'s slow calls had moved to the unarmed process, so it never opened.
  HEAD opens on that single account, which is exactly what io #10 forbids;
- v3 sent the limit, and any process answered, but only on its next sample; by then the asker received no samples
  to re-check on (runs opened 48-57 s late). v4 added the pending breach (5 s) that opens as an answer arrives; v5
  added the immediate answer and the `told` set (no echo loop);
- the remaining failures, **HEAD included**, were the recovery assertion. A half-open probe closes the breaker while
  the model is still slow (fault.md §5 flapping), and the cleared window refills with slow samples. The first fast
  samples after the restore were then checked against that stale window (the incoming sample was not in it), and
  the breaker re-opened for 15 s of healthy traffic. With 2 processes per pod the refill takes about 2x longer, so
  that re-open lands after the restore (`closed`, then `open` at 125-132 s; the restore is at about 123 s).

| Image (fresh api processes before each run unless noted) | Runs | F3b passed | Time to open after the slowdown | Failure |
|---|---|---|---|---|
| HEAD 4bc26a7 (`resilbase`), containers kept between runs | 2 | 2 | 11.16 s, 4.56 s | - |
| HEAD 4bc26a7 (`resilbase`), fresh processes | 3 | **0** | 6.37, 4.38, 6.82 s | re-open after the restore (132.4 s, ..., 125.0 s): recovery turns refused (36-38 of 112) |
| v1 local-only quorum (kept containers; the first in the full suite) | 3 | 1 | 23.74 s, never, never | no armed process saw both accounts' slow samples |
| v2 evidence from breaching processes only (kept) | 3 | 0 | never x3 | the unarmed process never contributed |
| v3 + limit, any process answers on its next sample (kept) | 3 | 0 | 5.15, 50.74, 57.21 s | late opens; a re-open after the restore |
| v4 + pending breach 5 s (kept) | 3 | 0 | 7.28, 48.21, 12.18 s | late open; re-opens after the restore |
| v5 + immediate answer, `told` (diagnostic image, fresh) | 3 | 2 | 7.62, 4.49, 12.15 s | 1 re-open after the restore (127.6 s) |
| **final** (+ the latest sample above the limit), fresh | 3 | **3** | **7.55, 5.23, 4.07 s** | none: closed 0.10-0.23 s after the restore, recovery 112/112 completed, 41-42 slow-phase turns refused without a model call |

Result files: `reports/qa/e2/resilience/f3b-*.json` (the `fair-*` and `ab-*` runs are in `/tmp/resil`). One
harness limit stays: with 2 processes per pod at 2.5 turns/s no process may reach 20 samples during the 80 s baseline
(one diagnostic run: 18 per window on every process). Then nothing arms, the rule cannot open, and HEAD fails F3b
too. Request for QA: pace F3b at >= 1.4 turns/s per process (e.g. `PACE_S` 0.2) or run it on one process per pod.

## 4. Gates and checks

Fault stack: `make qa-fault FAULT_PROJECT=noema-resil FAULT_PORT_BASE=32400 FAULT_IMAGE_TAG=resil
COMPOSE_FILE=/tmp/noema-resil-tree/deploy/compose/docker-compose.yml FAULT_RESULTS=reports/qa/e2/resilience/<file>
NOEMA_FAULT_TURNS=200` (api processes restarted first for fresh breaker state; F3b alone with
`FAULT_TESTS=tests/fault/test_f03_ttft_breaker.py`). Final full suite: **18 passed, 4 failed** in 2,176 s
(`fault-results-final.json`). The 4 failures are the same as run 1 (`fault-results.json`, before the section 3 work).

| Gate (06 §11) | Pass criterion | Wave E (fault.md / summary) | This role, final run | Result |
|---|---|---|---|---|
| F1 throttling | >= 99 % of failed-first turns served, no backoff, B's TTFT p95 <= 1.1x clean | fallback 15/40, FAIL | fallback **50/50** (run 1: 77/77); A 100/100 on Sol; 429 -> Sol request p95 21.8 ms; B TTFT p95 676.1 vs 648.7 ms clean (+4.2 %) | **PASS** |
| F4 hang (GLM) | fallback >= 99 %, e2e <= deadline + 1 s | 40/50, FAIL | **100/100**; F4b (Sol, no member left) 100/100 typed, e2e max 5.36 s | **PASS** |
| F5 stall pre-commit / post-commit | fallback >= 99 % / typed, partial kept | 40/53, FAIL / PASS | **100/100** / 100/100 `upstream_stalled`, partial stored | **PASS** |
| F6 stream exception pre / post | fallback >= 99 % / typed | 24/53, FAIL / PASS | **60/60** / 5 types typed | **PASS** |
| F7 reasoning-only / empty text | fallback >= 99 %, 0 empty answers, nothing committed from GLM | 40/100, FAIL | **25/25 + 25/25**; the other 50 turns skipped GLM (its breaker opens now: B1); 0 empty, 0 from GLM | **PASS** |
| F8 malformed stream | fallback >= 99 %, 0 x 500 | 27/40, FAIL | **30/30 + 30/30**, 0 x 500; members row PASS | **PASS** |
| S1b pre-commit fallback >= 99 % | every fallback row | 37.5-80 %, FAIL | 100 % in every row | **PASS** |
| S1a zero duplicated text | every row | PASS | 0 problems (asserted in every row) | **PASS** |
| F12 rate 0.5 (gated) | requests / turns <= 1.1 | 0.372 | **0.429** | **PASS** |
| F12 rate 0.25 (informational, asserted) | requests / turns <= 1.1 | 1.045 | 1.221; per failing upstream <= 1.1 (Sol 1.089) | **FAIL as asserted**: R6 measure conflict, section 2 |
| F3 5xx burst | opens in the window, recovers | 3.93 s / 5.09 s | 3.84 s / 5.02 s | **PASS** |
| F3b p95-TTFT rule | opens on latency alone, closes after the restore | 4.4 s / 14.8 s | 3.14 s / 13.99 s in the suite; 3/3 standalone (7.55, 5.23, 4.07 s); HEAD 0/3 with fresh processes (section 3) | **PASS** |
| F2 access, F9 members down, F11 Postgres down | as fault.md | PASS | PASS (F2b: 0 Sol requests: tenant access still never falls back) | **PASS** |
| C3 slow clients; F10 Redis down / hung | as fault.md | FAIL (edge #24, #25) | FAIL: the edge fixes are not in this role's frozen tree | not this role's (edge) |

`make check` (live working tree, every role's in-flight work included; `make -k check XDIST=4`): **all green**:
lint, typecheck (323 files), typecheck-tests (573), importlint (27 kept), forbidden-scan, ownership, contracts-check,
test-unit 3,294 passed, test-conformance 757 passed / 8 skipped, test-integration 561 passed / 21 skipped,
sdk-check (TS 86, Python 192). Narrow runs while editing: `pytest tests/unit/generation tests/unit/adapters
tests/integration/adapters tests/contract_internal/test_health_registry.py` 772 passed; the six changed test files 3
consecutive runs 167 passed each (deterministic: FakeClock, seeded scripts). No live Bedrock call: the lean SigV4 path
with `:` ids is verified against botocore only. qa-security and qa-contract were not run: this role changed no API
surface, response shape or authorization path (generation and adapters internals only).

## 5. Requests for other owners, assumptions and residual risks

Docs and specs this role may not edit (the behaviour is in the code docstrings):

| Where | Change needed | Owner |
|---|---|---|
| `docs/modules/generation.md:121-125` | the failure-walk guards: `RetryBudget.try_spend` only for a same-model re-send (R6); a cross-model fallback is bounded by `AttemptBudget` and admission | generation docs |
| `docs/modules/generation.md:159` | breaker outcomes: text-less stops are `error`; a reasoning-only stop under a cap the request lowered (`Attempt.client_capped`) is not recorded | generation docs |
| `docs/modules/adapters.md:217-220, 370-371` | TTFT rule: `ttft_min_accounts` (2), the latest sample above the limit, `ttft_breach` evidence across processes (ask, immediate answer, `told`, 5 s pending breach) | adapters docs |
| `docs/modules/adapters.md:240-243, 363` | the retry budget is spent only for same-model re-sends | adapters docs |
| `docs/impl-spec/02-interfaces.md:1021` (`breaker_events`, owner adapters) | add the payload `{model, region, state: "ttft_breach", accounts, limit_ms, until_ms, origin}` | spec agent |
| IMPL-016 / spec-04 §6 | record R6, B1 (text-less stops count as breaker failures) and the io #10 quorum | spec agent |
| `tests/fault/test_f12_retry_storm.py` | measure F12 per failing upstream (section 2) | QA fault |
| `tests/fault/test_f03_ttft_breaker.py` | pace for >= 20 samples per process and window on 2 processes per pod (section 3) | QA fault |

Assumptions and residual risks:

- **Tenant-shaped text-less stops (assumption).** B1 alone would let a tenant open a shared breaker by pinning a tiny
  `max_output_tokens`, so a reasoning-only stop under a cap the request lowered below the member's default is not
  recorded. A tenant-pinned higher effort is not excluded, since the reasoning reserve is meant to cover it.
- **Quorum semantics as specified:** one sample above the limit from a second account completes it. With
  `ttft_min_accounts` 2 a single tenant can no longer open the breaker, but any second tenant's jitter above 3x the
  baseline p95 can.
- **Retry-budget floor (pre-existing):** below 100 first sends per (account, model) and minute, the floor of 10
  re-sends allows more than 1.1x against that model (Sol 1.114 at F12 rate 0.5).
- **Per-process baselines (pre-existing):** a process that first sees traffic during a slowdown learns it as its
  baseline. The answer path lets such a process still contribute to the quorum, but its own rule stays blind
  until the baseline decays.
- **Rolling deploys:** pods on older code log "malformed breaker event dropped" for each `ttft_breach` until all pods
  run this code. The events are otherwise ignored, so the behaviour is that of a local-only quorum meanwhile.
- **F4b `refused_by_open_breaker` 0** (35 in Wave E's 1-process run): each of the 4 processes needs 20 failures of its
  own, and all 100 turns start before the 5 s timeouts land. The row's criteria pass; this is not caused by this change.

## 6. Files

Code: `src/noema/generation/{walk.py,run.py,attempt.py}`, `src/noema/adapters/resilience/{breakers.py,retry_budget.py}`
(docstring only). Tests: `tests/unit/generation/{test_fallback_slo.py (new),test_walk.py,test_attempt_fsm.py,
test_usage_and_telemetry.py}`, `tests/unit/adapters/{test_breakers.py,test_lean_converse_wire.py}`. Report and result
files: this file, `reports/qa/e2/resilience/*.json`. No other path was edited. (`tests/unit/adapters/
test_wire_and_registry.py` and `tests/integration/adapters/test_against_mockbedrock.py` also show changes in the tree:
those are another role's GLM-effort config change, io CR item 1, not this role's.)

## 7. Stack hygiene

`noema-resil` (ports 32401-32480) torn down with `make qa-fault-down ...` (containers, volumes `noema-pg` and
`media-spool`, network removed); its state files `~/.noema/stacks/noema-resil.{json,env}` deleted; the
`resildiag` / `resilbase` image tags removed. Kept for a re-run: images `noema-{api,worker,media}:resil` and the
frozen tree `/tmp/noema-resil-tree` (HEAD 4bc26a7 + this role's five source files). `noema-dev` was not touched.
