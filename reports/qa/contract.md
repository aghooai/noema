# Contract QA track (Wave E, role qa: contract; 2026-10-11)

Scope: gates Q1 (100% schema-valid responses, schemathesis on every operation), Q2 (OpenAI facade conformance with
the official `openai` Python and npm clients, 100%), Q3 (Jev-shaped conformance, 100%) and X1 (contract tests,
check_docs, OpenAPI <-> 07 drift) of spec-10 §3 / 06 §11 Phase 1, plus SDK compatibility (spec-08 §10.3/§10.4).
Bedrock-only: no live Bedrock call (the stack runs on the wire mock), no OpenAI call (both SDKs point at
`http://127.0.0.1:<port>/openai/v1`; the npm script refuses any other URL), no AWS resource touched (moto only).

## 1. Setup

- **Stack**: my own compose project `noema-qa-contract`, `E2E_PORT_BASE=29600` (api-1 29601, api-2 29602, mock
  29680), bootstrapped by `make e2e-up`, server-side ContractValidator on (`E2E_VALIDATE=1`, verified
  `NOEMA_CONTRACT_VALIDATE=1` in the api containers). Images: the wave-c2b images (`noema-api|worker|media:c2bfix`,
  built 04:54 from the tagged tree: no file under src/, deploy/, tools/ is newer) re-tagged `:contract`, so the
  perf sprints' concurrent rebuilds of `:dev` could not change my stack mid-run.
- **Compose file**: the tagged one (`git show HEAD:deploy/compose/docker-compose.yml`, run with
  `--project-directory deploy/compose`). The working-tree compose file is being changed by perf sprint B
  (`WEB_CONCURRENCY=2` per api container); with the wave-c2b image it crash-loops api-1/api-2 (`OSError: [Errno 98]
  Address already in use` on the metrics port 9464, "Child process failed to start"). Not a finding against the
  tagged code, but the compose change and the images must ship together (see §4).
- **Command** (both measured runs): `NOEMA_{API,WORKER,MEDIA}_IMAGE=noema-*:contract make qa-contract
  QA_CONTRACT_BUILD=0 COMPOSE_FILE="<tagged compose> --project-directory deploy/compose"`. Plain `make qa-contract`
  (QA lead) rebuilds the images and uses the working-tree compose file.
- **New files** (all test code): `tests/qa/contract/**`, `make/qa-contract.mk` (targets `qa-contract`, `docs-drift`,
  `sdk-compat`, `qa-contract-up`, `qa-contract-down`), `.orchestration/requests/qa.md` (OWNERS rule, §4). Ruff,
  ruff format and `mypy --strict` are clean on them; forbidden-scan clean.
- **Every response is contract-validated**: schemathesis responses by `fuzzkit.GATED` (incl. the
  `ContractValidator`), the stack tests through the e2e `CheckedClient`, the Python openai client through an httpx
  response hook, the npm client by recording each response in its `fetch` and validating it in Python.

## 2. Results

Run 1 and run 2 are full `make qa-contract` runs (run 2 after fixing one over-strict assertion of mine, §2 Q2).
Raw: `reports/tmp/qa-contract/run{1,2}.{log,xml}`, schemathesis tallies `run2-stateless.txt`, `run2-stateful.txt`.

| Gate | Check | Command | Pass criterion | Measured | Result |
|---|---|---|---|---|---|
| Q1 | schemathesis, stateless, every operation (25 = 23 authenticated + healthz/readyz), examples + coverage + fuzzing phases, positive and negative data, real auth per operation (developer, or tenant_admin for the admin views), each case with path ids or body ids sent again with real ids | `pytest tests/qa/contract/test_schemathesis_stack.py` (in `make qa-contract`), 50 examples/op, derandomized | per response: no 5xx except resumeTurnEvents' 501; declared status, content type, required headers; schema-valid (schemathesis + ContractValidator) | run 1: 4,978 calls, **7 x 500** (createTurn, U+0000 in a `file_id`: B1), everything else conformant. Run 2: 6,175 calls (200 x787, 201 x15, 202 x159, 400 x3,033, 401 x222, 403 x50, 404 x1,295, 409 x261, 415 x157, 422 x131, 501 x65), 0 failures: the generated inputs changed with my test edits and drew no U+0000 into an id | **FAIL** (B1; the deterministic reproducers below fail in both runs) |
| Q1 | schemathesis, stateful: the state machine over the 60 links schemathesis infers (createSession -> get/update/delete/listSessionTurns/createTurn, createTurn -> getTurn/cancelTurn, createDecision -> getDecision, uploadFile -> getFile, listLabelingRuns -> listLabelingDropped, ...) | `test_schemathesis_stateful.py`, 150 scenarios x 10 steps | GATED plus `use_after_free` and `ensure_resource_availability` (a `store: false` turn's 404 excepted, G11) | run 1: 1,812 calls, **3 x 500** (createSession metadata / createTurn with U+0000: B1). Run 2: 1,926 calls, 0 failures; no use-after-free, no missing created resource | **FAIL** (B1) |
| Q1 | deterministic reproducers of B1 and B2 | `test_input_robustness.py::test_nul_*` | ids that cannot exist -> 400/404; a 200 create is readable within 10 s | 16 of 18 id cases answer **500** (the 2 turn-id cases answer 404); 3 of 3 text cases answer 200 and the row is **never stored** (404) | **FAIL** (B1, B2) |
| Q1 | per-operation header rules (23 authenticated operations) | `test_input_robustness.py` | `x-aghoo-end-user` -> 422 feature_not_available `param: x-aghoo-end-user`; no credentials -> 401 invalid_api_key + `WWW-Authenticate: Bearer` | 23/23 and 23/23 (bodies of the declared media type: the 413/415 pre-checks run before authentication) | PASS |
| Q1 | server-side ContractValidator on every response the api served during both runs | `docker compose logs api-1 api-2 \| grep -c -E 'contract violation\|ContractViolation'` | 0 | 0 | PASS |
| Q2 | openai Python client 3.28 (unmodified): acceptance and parsing of both facade request examples, answers equal to the contract response examples, `x-aghoo-include: ensemble` (`x_aghoo` calibration, decision_id readable natively on the other pod), unavailable -> refusal with and without the header (`mapped_unavailable`), model ids (`aghoo/lightweight-model-decide` echoes `@1`, pinned `@1`), input as text and as messages with a data-URL image, typed errors (400 model_not_available, 403 profile_not_allowed, 400 schema_violation for remote image URL / 21 images / 256 choices / bool-string collision, 401 AuthenticationError), no retry on 4xx | `test_facade_python.py` | 100% of cases | 14/14 in both runs | PASS |
| Q2 | Python client retries (06 §2.2) | `test_facade_retries.py` | 503 (every member down) -> exactly 3 attempts (max_retries 2), each reaching the members on the mock, then InternalServerError `all_routes_unavailable`; 503 then healed -> [503, 200]; 429 rpm_exceeded (t_beta `rpm_per_key: 30`, restored after) -> [429, 200] after `retry-after` | run 2: 3/3. Run 1: 2/3, the failing one was my assertion: it required every mock record of the attempts to be `fault`, but a call the engine gives up on is logged `disconnected` (integration.md §10); fixed to `fault` >= 3 and nothing else but `disconnected` | PASS |
| Q2 | openai npm client 7.32 (unmodified, root node_modules) on api-2: the same 11 cases (incl. 503 x3 retries) + 429 -> 200 | `test_facade_typescript.py`, `facade_conformance.mjs` | 100% of cases | 12/12 in both runs | PASS |
| Q3 | Jev: K in {2, 3, 5, 16} choice and score questions plus `noul` / `predicate`, on lightweight-model-decide and router-default (escalation off); the IMPL-049 K = 5 vector; a choice that ties after rounding | `test_jev_conformance.py` | every response validates `JevDecisionResponse` + `jev_invariants` + the tolerant models; every Jev value equals the IMPL-049 map of the native result of the same decision_id (GET on the other pod) | 8/8 tests, 26/26 answers in both runs (e.g. native score 2.0848 -> Jev 2.07 from the 2-dp map; native tie winner `b` 0.4449 -> Jev `a`, declared first) | PASS |
| X1 | docs drift: every JSON example of 07-api-user-guide.md (4 json fences, 4 curl bodies, the openai python call, 16 inline spans; 8 abbreviated with `…`; 2 pseudo-syntax spans listed) validates against the bundle as a request or a 200 response of its operation, plus the request invariants and the Jev response's `jev_invariants` | `make docs-drift` | 0 errors; 6 planted mutations (renamed field, unknown enum value or answer type) all caught | 8/8 tests in both runs | PASS |
| X1 | vendored contracts and generated files current | `make contracts-check` | identical / up to date | "_vendor is identical ... (70 files)", "generated contract files are up to date" | PASS |
| X1 | design docs check | `cd /codes/aghooai/docs/noema && ./check_docs.sh` | exit 0 | exit 0: mermaid 11/11, tsc, schema, pydantic, config, audit, openapi, bundle, prose, links PASS | PASS |
| SDK (TS) | fresh openapi-typescript 7.13.0 output vs the SDK's committed wire types, and assignability to types.ts / sdk.ts / openai-compat.ts | `make sdk-compat` (`test_sdk_wire_types.py`) | identical body; responses wire -> declared, requests declared -> wire, enums both ways; the documented exception only (allOf answer refinements, wire-compat.check.ts) | identical; 61 assertions: 54 hold as is, the 7 answer-carrying types hold without `answers`/answer values; a planted wrong assertion is reported | PASS |
| SDK (Py) | strict server models vs schema vs tolerant client models on 56 contract examples | `make sdk-compat` (`test_sdk_models.py`) | strict accepts iff the schema does (except the documented calibrated => holdout looseness, IMPL-CON-10); every strict-accepted example is tolerant-accepted with its values unchanged; unknown fields / answer types break only the strict models | 63/63 in both runs | PASS |

## 3. Bugs found (module code; not fixed here: my role fixes test code only)

**B1 (major): U+0000 in an id or a stored map is a 500 `internal_error`.** Postgres text cannot hold NUL; the
value reaches `asyncpg` (`CharacterNotInRepertoireError: invalid byte sequence for encoding "UTF8": 0x00`, or
`UntranslatableCharacterError: \u0000 cannot be converted to text` for jsonb) and the route answers 500 (retryable)
instead of the 404 of an id that cannot exist or a 400. Found by schemathesis (createTurn with
`"file_id": "file_\u0000..."`, createSession with NUL in `metadata`), then mapped by hand. The api logs one ERROR
with a traceback per request (api-1 logged 57 + 7 of them during my first fuzz runs), so any client can flood the error logs.
Affected (16, all reproduced by `test_nul_in_ids_and_strings_is_a_client_error`): POST /v1/turns with NUL in an
`input_file.file_id`, `session_id` or `retry_of`; POST /v1/decisions with NUL in a `file_id` or `session_id`;
POST /v1/sessions with NUL in a metadata key or value; GET/DELETE /v1/files/file_%00, GET .../events,
GET .../derivatives/raster; GET/PATCH/DELETE /v1/sessions/ses_%00, GET .../turns; GET /v1/decisions/dec_%00.
(GET /v1/turns/turn_%00 and POST .../cancel answer 404: those lookups never reach Postgres with the raw id.)
Reproducer: `curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:29601/v1/sessions/ses_%00` ->
`500 {"error":{"type":"server_error","code":"internal_error",...,"retryable":true}}`; traceback ends in
`noema/db/repos/files.py:45 get` for the createTurn case (via `turns/ingress.py:106 authorize` ->
`media/pipeline.py:771 get`). Expected: reject U+0000 in ids and stored strings at the ingress (400
`schema_violation` with `param`) or treat such ids as not found (404), before any SQL.

**B2 (major): U+0000 in turn or decision text is answered 200, then the row is lost and its outbox entry never
drains.** POST /v1/turns (`stream: false`, store default true) with `input_text` "hi \u0000 there" or an
`input_json` `{"a": "\u0000"}`, and POST /v1/decisions (also the facade) with input "a \u0000 b", run the models
and answer 200 with an id, but the finalize INSERT fails (`turn finalize failed; queued in the outbox ...
UntranslatableCharacterError`). The outbox then rejects the entry on every drain ("rejected
(apply_failed:UntranslatableCharacterError); retried from Redis") and cannot dead-letter it either ("outbox
drainer: dead-lettering turn_finalize|decision_row ... failed (UntranslatableCharacterError)": 32 lines in 10 min
on api-2 after the runs; Valkey stream `noema:outbox:4` holds 20 such entries). GET of the returned id stays 404
(G11 says only `store: false` turns may be unreadable; spec-11 §3 says the outbox persists, and dead-letters what
it cannot apply). Reproduced by `test_nul_in_stored_text_is_rejected_or_stored` (3 cases): "/v1/turns answered
200 but /v1/turns/turn_... is 404 10 s later". Expected: reject or strip U+0000 at the ingress (400), or encode it so
the row can be stored; and the dead-letter path must not fail on the payload it is dead-lettering.

**B3 (minor, contract): uploadFile declares the file part as `contentMediaType: application/octet-stream`, which the
server answers 415.** spec-07 §2 accepts a file only when the part's Content-Type is a row's declared type (e.g.
`image/png`) and the filename has a matching extension, so a client that sends what the OpenAPI declares
(`curl -F "file=@x.png;type=application/octet-stream" -F purpose=input .../v1/files`) gets 415
`unsupported_media_type`; with `type=image/png` it is 202. Schemathesis' generated uploads are all 415 (115 of 163
calls in run 2). Owner: platform / contracts (the canonical openapi.yaml): describe the part's Content-Type and
extension rule (or list the accepted types) in the request body.

**B4 (minor, contract tooling): SSE bodies are declared with `schema:` (OpenAPI 3.1).** schemathesis 4.30 (OpenAPI
3.2 semantics) reads a `text/event-stream` schema as the event envelope `{data, event, id}` and so reports every
valid watchFile frame as "`filename` is a required property". The ContractValidator reads the schema as the `data`
payload and validates every frame (no finding). For third-party tooling, consider `itemSchema` with
`contentMediaType`/`contentSchema` for `data` when the bundle moves to 3.2. My check skips schemathesis' schema
check on SSE only (`fuzzkit.json_schema_conformance`).

Not bugs (by design, recorded so nobody re-reports them): an undeclared method is 404 `not_found`, not 405
(IMPL-064; schemathesis CLI `unsupported_method` flags it 10x); the 413/415 pre-checks come before authentication
(a POST without a body is 415 even without a key); the per-IP auth-failure bucket (burst 50, 10/s) also answers
429 to valid keys from that IP once drained (spec-09 §2), so the fuzz tests pause 0.15 s after each 401; generated
ids such as `ses_` fail semantic validation (schemathesis' "schema validation mismatch" warning for 18 operations:
cursors, ranges, unique names, groups are stricter than the JSON Schema, documented in the operation descriptions).
An exploratory schemathesis CLI run with `negative_data_rejection` and `missing_required_header` (1,545 cases)
found nothing else.

## 4. Notes for the QA lead and the fixers

- **make check**: my files pass `ruff check`, `ruff format --check` and `mypy --strict` (`make typecheck-tests`
  reports one error, in `tests/integration/streaming/test_redis_mirror.py:193`, and `make lint` one, in
  `tests/unit/composition/test_workers.py:60`: both files are other agents' work in progress, not mine).
  `make ownership` fails on every new path under `tests/qa/**` and on `make/qa-contract.mk` (and the fault track's
  `make/qa-fault.mk`): OWNERS.yaml has no rule for them. Requested in `.orchestration/requests/qa.md`:
  `{ pattern: "{tests/qa/**,make/qa-*.mk}", owners: [qa] }`. Until it lands, `make check` is red on `ownership`.
  None of the new tests is collected by a `make check` step (they need a stack or the design docs);
  `make docs-drift` and `make sdk-compat` are cheap and stack-free and could join CHECK_STEPS (platform's call;
  docs-drift needs `DOCS_GUIDE`, default `/codes/aghooai/docs/noema/07-api-user-guide.md`).
- **Compose and images move together**: the working-tree compose file (perf sprint B: `WEB_CONCURRENCY=2`, metrics
  on 9464 and 9465) with an image built before sprint B's code changes crash-loops both api containers on
  EADDRINUSE 9464. `make qa-contract` rebuilds the images by default (`QA_CONTRACT_BUILD=1`); keep that for the
  final run.
- **Determinism**: the hypothesis profile is derandomized (`ci`), so for one version of the test code the stateless
  inputs are fixed; the stateful machine's path also depends on the stack's answers (created ids, statuses), so
  which inputs it draws can differ between runs. The pass criteria are invariants (no 5xx, declared, schema-valid),
  and B1/B2 have deterministic reproducers that fail on every run. Once B1/B2 are fixed the fuzz tests should be
  stable; a 500 they find later is a new bug, with its curl in the failure message.
- **Run time**: about 4 min for `make qa-contract` on a running stack (docs-drift 0.4 s, sdk-compat 3.7 s, stack
  tests 3.5 min: stateless schemathesis 80 s, stateful 60 s). Knobs: `NOEMA_QA_FUZZ_EXAMPLES` (50),
  `NOEMA_QA_STATEFUL_SCENARIOS` (150), `NOEMA_QA_STATEFUL_STEPS` (10).
- The retry tests change t_beta's `rpm_per_key` through noema-admin (propose A, approve B) and roll it back in
  `finally`, as the e2e GCRA test does; they mint a t_beta key and revoke it.
- B2 leaves poison entries in the stack's outbox stream; `make qa-contract-down` (volumes deleted) clears them.

## 5. Not run

- Coverage of uploadFile's 202 path by schemathesis-generated bodies: none reach it (B3); the upload path is
  exercised with real PNGs by the real-id pass (`resources.py`), the stateful getFile/watchFile/deleteFile steps
  and the e2e suite.
- resumeTurnEvents beyond its Phase 1 501 (IMPL-036, Phase 2).
- The full `make check`, `make e2e` and the UI gates: other tracks / the QA lead's final regression.
