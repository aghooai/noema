# Wave E2: contracts-config-mock fixer (CR item 1, CR #26, findings 6/18/25, R7 media cap, mock fidelity, QL-4)

Date 2026-10-11, 20:00-21:50 UTC. No live Bedrock call was made (the live journal
`.orchestration/reports/live-calls.jsonl` still holds 57 lines). The mock rehearsal ran under a no-egress guard
(`-p noegress`, a pytest plugin that refuses every non-loopback connect and every AWS DNS lookup: 0 attempts).
My own stacks, all torn down at the end:
- `noema-e2ccm-live` (port base 35000, the rehearsal);
- `noema-e2ccm-sec` (34600, qa-security-stack and e2e);
- `noema-e2ccm-contract` (34800, qa-contract);
- `noema-e2ccm-load` (35200, L4).

Images `noema-{api,worker,media}:e2ccm` were built from the shared tree at 20:58 UTC.

"Before" means the same test run in an isolated copy of the tree (`/tmp/e2ccm/before`) with this role's product
files restored from a baseline snapshot taken before any edit (core/text.py, tools/mockbedrock, tests/fakes, the
vendored config and bundle). Tests that already passed before are marked as guards.

## 1. Items

| Item | Fix | Regression test | Before | After |
|---|---|---|---|---|
| **CR item 1**: GLM-5.3 effort `none` (live ValidationException) | Canonical `contracts/examples/platform.example.yaml`: `glm-5.3.controls.effort: [low, medium, high, xhigh, max]`; `race-sol-glm` runs GLM at `low` (live: first text 358 ms, no reasoning). Mock `_GLM` = `EFFORTS_NO_NONE`, with the live rejection text (`zai_effort_error`, "... with this model ...", `param` `reasoning_effort`). `KNOWN_DRIFT = []`. Design docs 01, 02, 05 updated | `tests/unit/mockbedrock/test_fidelity.py::test_effort_table[GLM-none-False]`, `::test_effort_rejection_texts` (the mock text == the live cassette `01-glm-5.3-complete.json` message, byte for byte); `tests/unit/adapters/test_wire_and_registry.py::test_the_example_config_agrees_with_the_probes` (`probe_discrepancies == []`), `::test_capabilities_come_from_config` | 4 FAIL (`none` accepted; drift `['models.glm-5.3 ... rejects']`; `'xhigh' in {..none..}`) | PASS |
| **CR #26**: answer-verifier never reaches quorum on live (L7 `no_quorum`) | `models.haiku-5.5.controls.reasoning: true`: Haiku's decision calls get the effort reserve (spec-06 §5.5). verify-dist@1 at `low` is now 64 + 12·2 + 512 = 600 tokens (was 88), the router's intent call 696. No calibrator exists, so spec-06 §5 (line 285) needs no template or profile version bump | `tests/unit/decision/test_verifier_reasoning_budget.py` (real engine, fake world, example config; Haiku hides 200 reasoning tokens via the new DSL key): `::test_the_verifier_reaches_quorum_when_haiku_hides_its_reasoning`; `::test_without_the_reserve_...` reproduces live L7 with the old config | new DSL + old config: FAIL, both questions `unavailable/no_quorum` (exactly L7) | PASS: both predicates answered, Haiku `max_tokens` 600 |
| **Finding 18**: uploadFile declared `application/octet-stream`, which the server answers 415 | openapi: the `file` part is `{type: string, format: binary}`; `encoding.file.contentType` lists the 9 accepted types (= `media.intake.TYPE_ROWS`); the request description states the type + extension rule and the 415; 07 guide §6 says the same, with a curl example. The first form (no `format: binary`) made schemathesis fail to serialize negative cases (`Cannot serialize to 'image/gif'`, qa-contract run 1), hence `format: binary` | `tests/unit/contracts/test_upload_and_media_limits.py::test_upload_part_types_are_the_allowlist`, `::test_upload_description_names_every_accepted_extension` | 2 FAIL (`KeyError: 'encoding'`; `.pdf` not named) | PASS; qa-contract `POST /v1/files` passes |
| **R7**: 64-media-part cap, a typed 400 | It was undeclared. Now declared on `TurnCreateParams.input` (openapi) and on the array form of `DecisionCreateParams.input` (decision.schema.json): "At most 64 media parts ... 400 `schema_violation` (`param` `input`, `details.max_media_parts` 64)". Also a row in the guide §11 Limits table. The facade keeps its stricter 20 `input_image` | `test_upload_and_media_limits.py::test_the_media_part_cap_is_declared_with_the_code_value` (the contract text and both code constants are 64) | FAIL (`KeyError: 'description'`) | PASS |
| **Finding 6**: `core/text.py` escaping vs the datamark | `_GAP` includes U+02C6. The tag name ends at `(?![0-9A-Za-z_])` instead of `\b`: `\b` failed before `ˆ`, which is category Lm. `datamark()` removes a literal U+02C6 before marking. `<ˆ/untrusted_input>`, `</untrusted_inputˆ>`, `<ˆ/ˆdecision_context...` are escaped; the SEC-INJ-01 vectors are unchanged | `tests/unit/core/test_text_datamark_escape.py` (7 forged forms, datamark forgery, 9 spec vectors and word-boundary guards, datamark vectors) | 8 FAIL (every forged form passed unescaped; `datamark("aˆb c") == "aˆbˆc"`) | 18 PASS. SE6 on the stack: `test_injection.py` 11/11 |
| **CR item 2 / finding 25 (b)**: Opus/GLM reasoning at `low` | `scenario._default_reasoning`: under probe defaults, Opus, GLM and Sol reason at every effort except `none` and `low`. Before: Opus at every effort, GLM except none/unset, Sol never. Live: none of the three reasoned at `low`; Sol `high` sent one `redactedContent` delta | `test_timing.py::test_probe_defaults_stream_no_reasoning_at_low[Opus, GLM]`, `::test_probe_defaults_sol_high_hides_its_reasoning`; guards `[Sol]` and `::test_probe_defaults_reasoning_cadence` | 3 FAIL (reasoning deltas at low; no Sol redacted delta) | PASS |
| **CR item 2 / (g)**: the probe latency table | The call rows follow the live cassettes: DeepSeek 300-560 ms (2% 4-12 s tail kept), Luna 350-610, Haiku 640-950. Low-effort first-text rows: Sol low 630-1,250, Sol above low 2,000-8,000 (live high 5,511, n 1), Opus low 1,000-2,200, GLM low 350-1,000. Per-row n is in the code comments | `test_timing.py::test_deepseek_tail_and_haiku_shares` (new bands); `tests/contract_internal/test_fake_scenario.py::test_probe_latency_defaults` (Luna median) | FAIL (DeepSeek p5 472 > 330) | PASS; L4 below |
| **CR item 2 / (a)**: Claude hidden JSON reasoning | New DSL key `stream.reasoning.hidden_tokens` (schema, `StreamSpec`, `Completion.hidden_reasoning`). `RequestInfo.max_tokens` comes from the fakes (`ModelRequest.max_tokens`) and the mock (`maxTokens`). The hidden tokens count toward `max_tokens`: at or above it the answer has no text and stops `max_tokens`, else the text follows (cut at the budget). The mock renders Claude's block as an empty `reasoningText` + signature (also for the `reasoning_only` fault, as live), the OpenAI family's as `redactedContent`. A stream plan's reasoning never bills more than `max_tokens` | `tests/unit/mockbedrock/test_hidden_reasoning.py` (at the live budget 88 the mock's response key paths == live cassette 35's `wire.response`, stop and `outputTokens` 88; text after it at 600; cut text; Sol redacted; none by default = guard); `test_converse.py::test_reasoning_only_returns_a_signed_reasoning_block` (empty text) | 5 FAIL (`hidden_tokens` unknown; reasoning text non-empty) | PASS |
| **Mock fidelity** (7 of 21 live/mock pairs differed) | The items above, plus the rehearsal scenario `tests/live/mock_scenario.yaml`. Haiku hides 200 tokens on verify-dist@1: live showed only the lower bound 88, so 200 is an assumption, documented. Complexity answers `hard`, as live L7 did, so the Sol restart at `high` is replayed. Rehearsal re-run and diff re-run: section 2 | `python -m tests.live.fidelity` | 21 pairs, 14 identical | **23 pairs, 20 identical**; 3 justified |
| **QL-4**: per-account quota windows | `POST /_mock/quotas` accepts `akid: "*"`: one window shared by every AKID, since a Bedrock quota is per account (`state.SHARED`). Precedence: exact AKID, then `*`, then the per-AKID default. The admission fixer used it for its C1 run (`reports/qa/e2/admission-estimator.md` §3) | `tests/unit/mockbedrock/test_control.py::test_a_shared_quota_is_one_window_for_every_akid` | FAIL (DID NOT RAISE: 2 AKIDs, 2 windows) | PASS |

Mock and fakes docs: `docs/modules/qa-tools.md` (quotas, GLM text, latency defaults, reasoning defaults, hidden
reasoning, `reasoning_only` shapes, test list), `docs/modules/fakes.md` ("Changes (Wave E2)"), and
`docs/modules/core.md` (finding 6).

## 2. Mock fidelity re-run (spec-10 §2.3)

Rehearsal: `NOEMA_LIVE=1 NOEMA_LIVE_MOCK=1 NOEMA_LIVE_STACK=noema-e2ccm-live NOEMA_TEST_MOTO_URL=http://127.0.0.1:35050
PYTHONPATH=/tmp/e2ccm/guard uv run --frozen --no-sync pytest -p noegress tests/live -m live` with the AWS variables
unset and the credential files set to `/dev/null`. Result: 15 passed, 56 mock calls, 0 egress attempts, journal
unchanged. Cassettes are in `tests/cassettes/mock-20261011T210838/`. Diff: `uv run --frozen --no-sync python -m
tests.live.fidelity`, written to `reports/live/fidelity.json`: **23 pairs, 20 identical**. Before: 21 pairs, 14
identical.
- The 2 new pairs are the Haiku router members of L6 and L7. With the old Haiku latency row (890-1,800 ms), the mock
  cut every one at the 1,000 ms deadline.
- Now identical: Opus low and GLM low (no reasoning); the Mantle `service_tier` pairs and Sol's `redactedContent`
  (io-fixer's mock fixes, now in a rehearsal); L7 Sol at high (the restart is replayed).

The remaining differences:

| Pair or call | Difference | Why it remains |
|---|---|---|
| L3 Mantle auth, DeepSeek | the mock answers SigV4 with 401 under a `bearer_short_term` scenario; live accepts both modes | By design: spec-10 §2.2 makes the mock accept only the scenario's `mantle_auth`, so tests can assert the adapter's configured mode (ADR-001 §6) |
| L7 Haiku `verification_member` | live: reasoning only, `max_tokens`; mock: reasoning + text, `end_turn`; mock usage adds `reasoning_tokens` | **CR #26**: the live call ran with the old budget (88), the rehearsal with the new one (600). At 88 the same plan reproduces the live wire shape key for key (`test_hidden_reasoning.py::test_at_the_live_budget...`). `reasoning_tokens` is io-fixer finding 24 (the adapter now estimates hidden reasoning); the live cassette predates that fix |
| L7 Sol stream (high) | `usage_nonzero`: the mock run adds `reasoning_tokens` | Adapter fix 24 again; the wire events are identical (`redactedContent` then text) |
| live-only: L1 and L2 GLM `none` | no mock counterpart | The live harness renamed the tests to GLM `low` after the live run. The mock's `none` rejection equals cassette 01's message (`test_fidelity.py::test_effort_rejection_texts`) |
| mock-only: L7 Luna `verification_member` | live Luna was cancelled (`late`) | Live Haiku had failed, so a 2-of-2 quorum was impossible. With CR #26 Haiku answers, and Luna completes |

Not modelled (live-smoke §5 item 9): a cold first connection taking about 990 ms. That is client and TLS
behavior; the mock is plain HTTP.

## 3. Edits outside the listed paths (each a consequence of the config change)

Minimal, single-purpose edits; each is named in its file's comment.
- `tests/unit/adapters/test_wire_and_registry.py`: `KNOWN_DRIFT = []`, as the item asks. GLM capabilities: `xhigh`
  in, `none` out.
- `tests/unit/adapters/test_lean_converse_wire.py` (`glm_controls`) and
  `tests/integration/adapters/test_against_mockbedrock.py` (stream faults): GLM effort `none` -> `low`.
- `tests/golden/wire.py`: the GLM golden at `low`; Haiku member budgets 612, 600 and 612. Regenerated with
  `PYTHONPATH=. python -m tests.golden.regenerate`; only 4 wire goldens changed, no SSE fixture.
- `tests/unit/decision/test_render.py` and `tests/unit/ops_admission/test_planner.py`: the Haiku reserve. TWG-1
  Haiku output gains 5 × 512.
- `tests/contract_internal/test_fake_scenario.py`: the Luna median band.
- New tests: `tests/unit/core/test_text_datamark_escape.py`, `tests/unit/decision/test_verifier_reasoning_budget.py`,
  `tests/unit/contracts/test_upload_and_media_limits.py`.
- Generated, by `make contracts-sync contracts-generate`: `src/noema/contracts/{_vendor/*, build/openapi.bundled.yaml,
  _generated/models.py}` and `sdk/typescript/src/generated/openapi.d.ts` (descriptions only; `file` stays `string`).

## 4. Gates and checks

| Gate / check | Command | Result |
|---|---|---|
| design docs | `cd /codes/aghooai/docs/noema && ./check_docs.sh` | exit 0 (11 mermaid, tsc, schema, pydantic, config, audit, openapi lint, bundle, prose, links) |
| contracts | `make contracts-sync contracts-generate contracts-check` | `_vendor` identical (70 files); generated up to date |
| regression tests, before | the tests of §1 in `/tmp/e2ccm/before` | 15 + 1 (CR #26, config alone) + 1 (QL-4) + 3 (contract) FAIL; the 8 finding-6 cases FAIL |
| **make check** (full, shared tree, final run 21:5x UTC) | `make -k check` | **all green**: lint, typecheck (323 files), typecheck-tests (573), importlint 27 kept, forbidden-scan, ownership, contracts-check, test-unit 3,290 passed, conformance 757 passed / 8 skipped, integration 561 passed / 21 skipped, sdk-check TS 86 / Python 192 |
| e2e (G06 race-sol-glm, G08 verification on the new config) and SE6 / SE8 / SE11 / SE12 | `make qa-security-stack QA_SEC_PROJECT=noema-e2ccm-sec QA_SEC_PORT_BASE=34600 QA_SEC_TAG=e2ccm QA_SEC_BUILD=0 QA_SEC_UI_PORT=34690` | exit 0: e2e **52/52**, ui-e2e 12/12 (55 responses contract-valid), `tests/security/stack` **31/31** (`test_injection.py` 11/11). The gitleaks note is the triaged mock-AKID class |
| Q1 / X1 contract | `make qa-contract QA_CONTRACT_PROJECT=noema-e2ccm-contract QA_CONTRACT_PORT_BASE=34800 QA_CONTRACT_BUILD=0` | run 1: 129 passed, 2 FAILED (uploadFile serialization, then the stateful machine): both caused by my first openapi form. Run 2 after `format: binary`: docs-drift 8/8, sdk-compat 65/65, stack **131/131**, server-side violations **0** |
| mock fidelity | section 2 | 23 pairs, 20 identical, 3 justified |
| L4 decision quorum on the probe latencies (measured, re-gate is the QA lead's) | `python -m tests.load.run --state .../noema-e2ccm-load.json --plan 50:330 --warmup-s 60 --scenario tests/load/scenarios/decision.yaml --api-port 35290 --mock-port 35281` | quorum reached **2,357** for 2,356 completed decisions (one more was in flight at the scrape; Wave E: 96.9%), all within 1.0 s, 97.5% within 0.75 s. Haiku `timeout` 267 / 11,615 (Wave E: 12,139 / 13,870), DeepSeek 8. `decision_ms` p95 995. Caveats: 1,914 timed turns (3,116 turns 429 `rpm_exceeded` by the dev stack's key limit), and the window is invalid by design of the scenario (holdback), as in Wave E |

## 5. Not fixed here / for other owners

- **Live verification (P0 budget): unverified.** CR #26 (Haiku verify at 600 tokens reaching quorum on live L7) and
  the GLM race at `low` were not run against Bedrock; GLM `low` itself was live-verified (358 ms).
- **Spec agent (docs/impl-spec):**
  - spec-10 §2.2's probe latency table and its "Haiku ≥ 5% above 1 s" line, and the "reasoning deltas for Opus,
    GLM ≠ none" rule (now: Opus, GLM and Sol, except `none`/`low`);
  - spec-10 §2.4: the DSL key `reasoning.hidden_tokens` and `RequestInfo.max_tokens`;
  - spec-10 §6 L1/L2: GLM at `low`;
  - spec-06 §5.2 and spec-09 §5: `_GAP` with U+02C6, `(?![0-9A-Za-z_])` instead of `\b`, `datamark` strips a
    literal U+02C6; the new vectors;
  - spec-06 §5.5: Haiku is a reasoning model (no version bump, no calibrator yet).
- **qa-harness:** SEC-INJ-02 corpus cases with the datamark forms in `tests/security/stack/test_injection.py`; the
  C1 runner's `--mock-quotas` can now pass `"akid": "*"`.
- **QA lead:** L4 depends on the probe table, so re-gate it. The schemathesis upload cases now send the declared part
  types, but their filenames have no accepted extension, so most still get 415 by the documented rule (B3's
  coverage gap stays).
