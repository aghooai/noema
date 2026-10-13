# Testing

Strategy: spec-10 ([impl-spec/10-qa-test-strategy.md](../impl-spec/10-qa-test-strategy.md)). Gate results:
[reports/qa/wave-e-summary.md](../../reports/qa/wave-e-summary.md). Counts below are from the final Wave E3 tree
(that summary, section 4) unless marked otherwise.

## 1. Test layers

| Layer | Path | Target | Needs | Network |
|---|---|---|---|---|
| unit | `tests/unit/<module>/` | `make test-unit [MOD=<name>]` | nothing | none (pytest-socket) |
| conformance | `tests/contract_internal/` | `make test-conformance` | `make up` | loopback: one Postgres clone, one Redis namespace per session |
| integration | `tests/integration/<module>/` | `make test-integration [MOD=<name>]` | `make up` | loopback: uvicorn on port 0, the in-process wire mock, moto |
| one module | unit + integration of a module | `make test MOD=<name>` | `make up` | as above |
| database | `tests/integration/db/` | `make test-db` | `make up` | loopback |
| api | `tests/unit/api`, `tests/integration/api` | `make api-test`, `make api-smoke` (schemathesis) | `make up` | loopback |
| media | `tests/{unit,integration}/media_pipeline`, `tests/integration/infra/media_image` | `make test-media` | the media test image | none (`network_mode: none`) |
| SDKs | `sdk/typescript`, `sdk/python` | `make sdk-check` (`sdk-ts-check`, `sdk-py-check`), `make sdk-compat` | `make npm-ci` | none |
| UI | `web/test`, `web/e2e` | `make ui-check`, `make ui-e2e` | `make npm-ci`, system Chrome for Playwright | loopback / a stack |
| e2e | `tests/e2e/smoke`, `tests/e2e/suite` | `make e2e-smoke`, `make e2e` | Docker | the e2e compose stack |
| contract QA | `tests/qa/contract` | `make qa-contract`, `make docs-drift`, `make sdk-compat` | Docker (the stack part) | its own stack |
| security | `tests/security/` | `make qa-security` and its parts | Docker | its own stack |
| fault | `tests/fault/` | `make qa-fault` | Docker | its own stack, through toxiproxy |
| stack gates and load | `tests/load/` | `make qa-gates` and its parts; `python -m tests.load.run` | Docker, a quiet host | its own stack |
| live | `tests/live/` | no make target (section 6) | AWS credentials, `NOEMA_LIVE=1` | Amazon Bedrock |

Pytest markers (`pyproject.toml`, `--strict-markers`): `unit`, `integration`, `e2e`, `live`, `slow`, `perf`.
`make test-integration` deselects `live`, `e2e` and `perf`; `live` tests are skipped unless `NOEMA_LIVE=1`. The
default timeout is 60 s per test. `MOD=<name>` narrows lint, typecheck and tests to one module (e.g. `MOD=decision`,
`MOD=ops/admission`, `MOD=media_pipeline`); `XDIST=<n>` runs n xdist workers (at most 4 per agent).

## 2. `make check`

Everything the wave gates run, in this order (`CHECK_STEPS` in the `Makefile`); it needs the dev services
(`make up`) for the conformance and integration steps:

| Step | What | Final tree |
|---|---|---|
| `lint` | ruff check + ruff format --check | 916 files |
| `typecheck`, `typecheck-tests` | mypy --strict on `src/noema`; on the root conftest, `tests/` and `tools/` | 327 / 590 files |
| `importlint` | the import-linter contracts of spec-01 §2 | 27 kept, 0 broken |
| `forbidden-scan` | no `api.openai.com`, no dynamic or `TYPE_CHECKING` imports of `openai`, `anthropic`, `requests`, `boto3` | clean |
| `ownership` | every changed path has an owner in `OWNERS.yaml` | 0 BAD |
| `contracts-check` | the vendored contracts equal the design repo's; bundle and generated models up to date | pass |
| `test-unit`, `test-conformance`, `test-integration` | the three layers above | 3,447 / 757 / 573 passed |
| `sdk-check` | both SDKs | TS 86 / Python 192 |

`make check` ends with `make check: all green`. A full-tree ownership check of every tracked path (not only the
changed ones) is `make qa-ownership-all` (run while writing this page: "tracked paths: 1912, unowned: 0"; new
untracked files are not counted).

## 3. QA targets and their stacks

Each stack target runs in a compose project and port base of its own, so tracks never share containers, ports or
images. The `*-up` targets start or re-check the stack; the `*-down` targets stop it and delete its volumes.

| Target | Gates (spec-10 §3) | Stack (defaults) | Final tree |
|---|---|---|---|
| `qa-contract` (= `docs-drift`, `sdk-compat`, `qa-contract-up`, then the stack tests), `qa-contract-up`, `qa-contract-down` | Q1-Q3, X1 | `QA_CONTRACT_PROJECT=noema-qa-contract`, `QA_CONTRACT_PORT_BASE=29600`, `QA_CONTRACT_BUILD=1`; validator on | 131/131; schemathesis 6,160 stateless + 1,664 stateful calls, 0 failures |
| `docs-drift` | X1 | none: every JSON example of the design's `07-api-user-guide.md` (`DOCS_GUIDE`) against the bundled OpenAPI | 8/8 (also run here: 8 passed) |
| `sdk-compat` | X1 | none: openapi-typescript output vs the contract types; strict server vs tolerant client models | 65/65 (also run here: 65 passed) |
| `qa-security` (= `qa-security-local`, `-stack`, `-fuzz`, `-sca`; every part runs, then the failed ones are listed), `qa-security-down` | SE1-SE13 | `QA_SEC_PROJECT=noema-sec`, `QA_SEC_PORT_BASE=28900`, `QA_SEC_TAG=sec`, `QA_SEC_UI_PORT=28990`; artifacts in `~/.noema/qa-security`, reports in `reports/qa/security` | all green |
| `qa-security-local` | SE1 oracle, SE4 (STS on moto), scanner patterns | stackless | 72/72 |
| `qa-security-stack` | SE1-SE3, SE6-SE8, SE11, SE12 | e2e traffic, SEC-UI Playwright, `tests/security/stack`, gitleaks on the capture | 31/31 stack tests (356 authz cells, 0 leaks) |
| `qa-security-fuzz` | SE9 | inside the media image, network none: mutation corpus through nsjail, atheris | 6/6 (12 min 22 s) |
| `qa-security-sca` | SE13 | pip-audit (`uv.lock`), npm audit, trivy + CycloneDX SBOM per image, gitleaks on the repo; waivers `deploy/docker/.trivyignore.yaml` | 0 unwaived |
| `qa-fault`, `qa-fault-up`, `qa-fault-down` | F1-F12, S1, the p95-TTFT breaker rule, C3 slow clients | `FAULT_PROJECT=noema-fault`, `FAULT_PORT_BASE=29300`, `FAULT_BUILD=0`, `FAULT_IMAGE_TAG=dev`, `FAULT_TESTS=tests/fault`, results `FAULT_RESULTS=reports/qa/fault/results.json`; postgres, valkey and moto behind toxiproxy | 22/22 |
| `qa-gates` (every part below, then the failed ones are listed), `qa-gates-up`, `qa-gates-down` | L2, L3, Q8, L5, R4, Q9, SE7, M3, ownership | `QA_GATES_PROJECT=noema-qa-gates`, `QA_GATES_PORT_BASE=31800`, `QA_GATES_TAG=qa-gates`, `QA_GATES_BUILD=1`, `QA_GATES_MOCK_CPUS` empty (the mock unpinned; pin it only to isolated CPUs, [reports/perf/sizing.md](../../reports/perf/sizing.md) §10), results `QA_GATES_RESULTS=reports/qa/e2/gates-results.json` | all green |
| `qa-gates-selftest` | the gates' pure parts | stackless | run here: passed |
| `qa-load-holdback` | L2, L3, Q8 | the gate stack | every cell within its bound |
| `qa-load-backup-tail` | L5 | the gate stack | p99 929.5 ms (n 5,000) |
| `qa-replay` | R4 (measured; thresholds P0) | the gate stack | restarts = script, replay 60/60 |
| `qa-hier-deadline` | Q9 | the gate stack | share 1.0 at 1,000 ms, 0.0 at 3,000 ms |
| `qa-media-injection` | SE7 | the gate stack (also in `qa-security-stack`) | 0 policy violations |
| `qa-ocr-cer` | M3 | inside the media image | CER 0.0 |
| `qa-ownership-all` | supporting | stackless, read-only `git ls-files` | 0 unowned |

Other gates and checks: `make observability-check` (promtool check and test of the rules, dashboard JSON, every
referenced metric in the registry; run while writing this page: 139 passed, "observability-check: all green"),
`make image-scan` (SE13, [operations/deployment.md](../operations/deployment.md) section 1),
`make sandbox-selftest` (ADR-002 §2 checks 1-10 in the media image), `make test-media` (307 passed, 8 skipped). The
load gates L1, L4, C1-C5 run with the load harness (`python -m tests.load.run`, [tests/load/README.md](../../tests/load/README.md));
their runs and results are in [reports/qa/e3/qa-lead.md](../../reports/qa/e3/qa-lead.md) section 3. Every QA target
above except `qa-gates-selftest`, `qa-ownership-all`, `docs-drift` and `sdk-compat` was run by the QA lead on the final tree, not while
writing this page.

## 4. Isolation and determinism rules

Up to 12 agents and their xdist workers run tests on one host at the same time (spec-10 §1.1). Every test must hold
under that:

| Rule | How it is enforced |
|---|---|
| No ambient credentials | the root `conftest.py` removes every `AWS_*` variable (including `AWS_BEARER_TOKEN_BEDROCK`), points the AWS config and credentials files at `/dev/null`, and sets `NOEMA_ENV=test`, at import time and again per test |
| No network beyond loopback | pytest-socket `--allow-hosts=127.0.0.1,::1` (stack suites add the compose network hosts) |
| Own resources per session | `noema_run_id` = `<NOEMA_AGENT_ID or local>-<xdist worker>-<uuid8>`: a Postgres clone `noema_t_<run>` of the migrated template (named by the migration hash), a Redis namespace `ns:<run>:`, one moto server and one wire-mock instance per session or test, servers on port 0 |
| No order dependence | `pytest-xdist --dist load` spreads tests arbitrarily; a test may not rely on another's state |
| Reproducible property tests | Hypothesis profile `ci` (default): derandomized, no example database; `NOEMA_HYPOTHESIS_PROFILE=nightly` explores with random seeds (add a failure's blob as an `@example`) |
| Deterministic upstream | the wire-level Bedrock mock (`tools/mockbedrock`) with scripted latencies, quirks and faults from the scenario DSL (`tests/fakes/scenario.py`); never a real model in `make check` |
| Timing asserted as order | e2e assertions are orderings and reasons, never exact milliseconds; scenarios keep >= 200 ms margins between first text, S1, the hold cap and the router deadline |
| Valid load windows only | a load window counts only if every driver and mock process has lag p99 <= 2 ms and >= 99.9% of turns have `holdback_ms = 0` with `spec_agrees`; record the 1-minute loadavg with every run (spec-10 §5) |
| Contract validation everywhere | every response in every layer except load and live passes the test-mode `ContractValidator` |

Fakes and real implementations share conformance suites (`tests/contract_internal`): a fake that passes them is a
valid stand-in, and every real implementation must pass the same suite. Golden SSE fixtures live in
`tests/golden/sse/`. Clean up stale test databases with `make db-reap-test` (drops `noema_t_*` older than 6 h or
of dead processes).

## 5. The e2e suite

`make e2e` = `make e2e-up`, then `pytest tests/e2e -m e2e` (smoke + suite, 52 tests, about 2.5 minutes, serial: the
tests share one stack and one mock). `make e2e-smoke` runs the 8 smoke tests only (run while writing these docs on
a stack of its own: 8 passed in 2.17 s). Against another stack: `make e2e E2E_PROJECT=<p> E2E_PORT_BASE=<base>
[E2E_BUILD=0]`. What each file covers: [modules/integration.md](../modules/integration.md) section 10.

## 6. The live smoke and its ledger

The only tests that call Amazon Bedrock. Never part of `make check` or any QA target.

```bash
NOEMA_LIVE=1 UV_NO_SYNC=1 uv run --frozen --no-sync pytest -p tests.live.bearer tests/live -m live -p no:cacheprovider -x
```

(The command of the Wave E3 live run, [reports/qa/e3/live-smoke.md](../../reports/qa/e3/live-smoke.md); **not
run** in Wave F: no live calls in this wave.)

| Rule | Value |
|---|---|
| Hard stop | **87 ledger entries in total** across every run (60 until the owner decision of 2026-10-12 added 30 for re-verification); `tests/live/budget.py` `LIMIT` |
| Ledger | `.orchestration/reports/live-calls.jsonl`, one JSON line per call (`ts`, `model`, `alias`, `operation`, `test`) written before any byte is sent; **85 lines** now |
| A call | one `Adapter.complete()` or `Adapter.stream()` entry, including backups, fallbacks and restarts (an in-adapter connect retry is not a call) |
| Refusals | the session does not start when the ledger already holds 87 entries or the bearer token is missing; each test declares its worst case (`ledger.need(n)`) and is skipped if fewer calls remain; call 88 raises `LiveBudgetExceeded` before any byte and stops the session |
| Limits per call | the 7 configured models only; `max_tokens` capped at 256 (generation 512, decision and verification members 1,024, labeling 16 plus the labeler reasoning reserve) |
| Credentials | `AmbientBroker` (dev/test only) with the developer's own credentials, region us-east-1 passed explicitly; `-p tests.live.bearer` keeps `AWS_BEARER_TOKEN_BEDROCK` for Mantle |
| Records | redacted cassettes under `tests/cassettes/live-<run>/` (shapes, counts, timings, usage; never prompt or answer text); `reports/live/calls.json` |
| Rehearsal | `NOEMA_LIVE=1 NOEMA_LIVE_MOCK=1` runs the same tests against an in-process wire mock: no Bedrock call, no ledger entry, cassettes under `tests/cassettes/mock-<run>/` for the mock-fidelity diff |

The live results (L1-L9, K1 estimator, mock fidelity) are in [reports/qa/e3/live-smoke.md](../../reports/qa/e3/live-smoke.md)
and gates L6, R5 and K1 of [reports/qa/wave-e-summary.md](../../reports/qa/wave-e-summary.md).

## 7. Where a new test goes

| Change | Test |
|---|---|
| a module's logic | `tests/unit/<module>/` (no network), owned by the module's role |
| a Protocol implementation | the shared suite in `tests/contract_internal/` must pass for it (the fakes are frozen; changes go through a change request) |
| SQL, RLS, a definer function | `tests/integration/db/` or `tests/integration/db_repos/` (db roles, frozen: change request) |
| behavior across processes or the compose stack | `tests/e2e/suite/` (integrator) |
| a gate | its QA track (`tests/security`, `tests/fault`, `tests/load`, `tests/qa`) |
