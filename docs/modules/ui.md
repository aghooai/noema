# Web UI: `web/` (owner: ui; Wave C2b/D)

Spec: 03 §10 (W1 chat + inspector, W2 uploads, W4 calibration, W7 cost; Phase 1 scope), spec-09 §2 (dev login), §6.8
(serving derivatives), §8 (UI security: CSP, sanitized Markdown, CSRF, SEC-UI-01…03), spec-07 §6, IMPL-042/065/083.
The SPA calls the Noema API only, through `@aghoo/sdk` (sdk/typescript); it never calls Bedrock or any model API.

## Composition and wiring
Nothing in `composition.py`. `vite build` writes the SPA to `web/dist`; the **dev cookie proxy** (`web/server`, plain
`node:http`, run as `node server/main.ts` with Node's type stripping, Node >= 22.18) serves it and forwards `/v1/*` to
one API process. Phase 1 has no BFF or edge (spec-09 §1): the proxy is the dev login of spec-09 §2 and refuses to start
unless `NOEMA_ENV=dev`.

## Layout
| Path | Contents |
|---|---|
| `web/src/api/` | `protocol.ts` (cookie and header names, `/auth/*` paths, shared with the server), `client.ts` (`createApi`: `AghooClient` with `baseURL` = the proxy origin, no API key, `csrfFetch` adding `x-csrf-token` on mutating requests and dropping any `authorization`) |
| `web/src/chat/` | `turnState.ts` (pure reducer `applyEvent(TurnView, TurnEvent)`, `fromTurnObject`), `chatStore.ts` (sessions, history, `turns.stream`, cancel, inspector selection), `ChatPage.tsx` (three panes), `Conversation.tsx` |
| `web/src/inspector/` | `AnswerView.tsx` (candidate bars as `<meter>`, calibration badge, heuristic confidence + band, quorum, per-member vote / latency / input view, hierarchical groups), `Inspector.tsx` (decision, escalation stages, route + reason + matched bindings + basis, effort + conditioning summary, attempts + spec restarts, timings, degradations, errors, verification badge) |
| `web/src/files/` | `uploadStore.ts` (upload, `files.watch`, derivative previews, turn notices), `FileCard.tsx` (status, typed rejection/failure, active-content notice, previews), `FilesPage.tsx` (drop zone) |
| `web/src/calibration/` | `calibrationStore.ts`, `CalibrationPage.tsx` (W4: calibrators with holdout vs LLM consensus, CIs, coverage, reliability table; labeling runs; dropped items with both labelers' answers) |
| `web/src/usage/` | `usageStore.ts` (`pivot`: one row per key, one cell per line), `UsagePage.tsx` (W7: group_by + dates; session -> turns -> turn drill-down; open a turn by id) |
| `web/src/markdown/SafeMarkdown.tsx` | the only Markdown renderer (model output and Markdown derivatives) |
| `web/src/lib/` | `store.ts` (tiny observable store + `useSyncExternalStore`), `format.ts` |
| `web/server/` | `config.ts` (env), `security.ts` (CSP, cookies, CSRF, in-memory sessions), `app.ts` (auth endpoints, `/v1` proxy, response recorder), `static.ts`, `main.ts` |
| `web/test/` | vitest (Node environment) + msw; components rendered with `react-dom/server` |
| `web/e2e/` | Playwright specs `01`…`05`, `support.ts` (guards), `mock-scenario.ts` (the restart scenario for a stack's mockbedrock), `fixture-api.ts` + `fixture-data.ts` (local stand-in API), `global-setup.ts`, `make_media.py`, `stack_env.py`, `validate_recording.py` |
| `make/ui.mk` | `ui-check`, `ui-build`, `ui-serve`, `ui-e2e` (owner ui in OWNERS.yaml) |

## Commands
- `make ui-check`: `cd web && npm run check` (= `tsc -p tsconfig.app.json` (DOM, no Node types) and
  `tsc -p tsconfig.node.json` (server, tests, e2e), `vitest run`, `vite build`), then ruff + `mypy --strict` on the
  e2e Python helpers. No ESLint is installed (not in the root toolchain), so "lint" is tsc strict plus the
  source-policy test.
- `make ui-build` (web/dist), `make ui-serve` (build, then the proxy on 127.0.0.1:5180).
- `make ui-e2e [PW_ARGS=...] [UI_STACK_STATE=~/.noema/stacks/<project>.json UI_STACK_TENANT=t_acme UI_STACK_MOCK=1]`:
  build, run Playwright on the system Chrome, then validate every proxied API response against the contract
  (`validate_recording.py`, ContractValidator). Without `UI_STACK_STATE` (or `NOEMA_UI_API_URL`), Playwright starts
  the local fixture API instead of using a stack.
- Packages resolve from the root `node_modules` (react 19.3, react-dom, react-markdown 10.1, vite 8.3,
  @vitejs/plugin-react 6.1, vitest 5.0, msw 3.0, @playwright/test 1.64, typescript 5.9); `web/package.json` has no
  dependencies of its own. `@aghoo/sdk` is aliased to `sdk/typescript/src/index.ts` (tsconfig `paths`, vite alias).

## Dev cookie proxy (spec-09 §2, §8)
| Variable | Meaning |
|---|---|
| `NOEMA_ENV` | must be `dev`, else exit 2 ("the dev cookie proxy runs only with NOEMA_ENV=dev") |
| `NOEMA_UI_API_URL` | upstream API origin (default `http://127.0.0.1:8000`; any path is dropped) |
| `NOEMA_UI_API_KEYS` | `alias=key` pairs (comma or newline separated); aliases `^[a-z][a-z0-9_-]{0,31}$`, keys must match the spec-09 §2 format with the test prefix, `ak_test_<ulid>.<43>` (else exit 2; an `ak_live_` key is refused by this dev-only proxy) |
| `NOEMA_UI_API_KEYS_FILE` | a file with the same format, or a JSON object `{alias: key}`; merged after the variable |
| `NOEMA_UI_HOST`, `NOEMA_UI_PORT` | default `127.0.0.1`, `5180`. The host must be loopback (`localhost`, `127.0.0.0/8`, `::1`), else exit 2 |
| `NOEMA_UI_ALLOW_NON_LOOPBACK=1` | allows a non-loopback `NOEMA_UI_HOST` (e.g. `0.0.0.0` or a LAN address) |
| `NOEMA_UI_DIST`, `NOEMA_UI_SECURE_COOKIES=1`, `NOEMA_UI_RECORD` | built SPA dir; add `Secure` to cookies; JSONL file receiving every proxied API response (e2e only) |

- **Host guard (DNS rebinding)**: every request (SPA files, `/auth/*`, `/v1/*`) whose `Host` is not
  `<name>:<port>` -> 403 `permission_denied`, not forwarded. `<name>` is the configured `NOEMA_UI_HOST`, `localhost`,
  `127.0.0.1` or `[::1]` (case-insensitive); `<port>` is the port the request arrived on (`NOEMA_UI_PORT`, or the
  ephemeral port when it is 0).
- **Origin check**: `POST /auth/login`, `POST /auth/logout` and every mutating `/v1` request (not GET/HEAD/OPTIONS)
  need `Origin` = `http(s)://` + the request's `Host`; a missing, `null` or other Origin -> 403 `permission_denied`,
  checked before the CSRF token and not forwarded. Browsers send Origin on every same-origin `fetch` POST/DELETE.
- `GET /auth/session` -> `{authenticated, alias, aliases}`; sets the readable `noema_csrf` cookie (`SameSite=Strict;
  Path=/`) when the browser has none. `POST /auth/login {alias}` and `POST /auth/logout` need `x-csrf-token` equal to
  that cookie (403 otherwise). Login sets `noema_session=<256-bit id>; HttpOnly; SameSite=Strict; Path=/` and rotates
  the CSRF token, bound to the session (8 h idle TTL, in memory). The key never leaves the proxy: responses carry
  only the alias.
- `/v1/*`: no session -> 401 (`invalid_api_key` envelope); a non-GET/HEAD/OPTIONS request whose `x-csrf-token` does
  not equal both the cookie and the session's token -> 403 `permission_denied` (SEC-UI-03), not forwarded. Otherwise
  forwarded with `Authorization: Bearer <alias key>` and an allowlist of request headers (accept, content-type,
  content-length, transfer-encoding, idempotency-key, aghoo-version, aghoo-deadline-ms, last-event-id, x-request-id,
  traceparent, tracestate, x-aghoo-end-user, x-aghoo-include): cookies, the CSRF header and any browser
  `Authorization` are dropped. Bodies stream both ways (uploads, SSE) unbuffered; `set-cookie` and hop-by-hop
  response headers are dropped; a browser disconnect closes the upstream request. Unreachable API -> 502
  `internal_error` (retryable).
- Every response (SPA files, auth, proxied API) gets the spec-09 §8 CSP verbatim (`default-src 'self'; img-src 'self'
  blob:; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri
  'none'; form-action 'self'`), `Referrer-Policy: no-referrer` and `X-Content-Type-Options: nosniff` (replacing any
  upstream value of these headers). Static files:
  `/` = index.html (`no-store`), known extensions only (no SVG), no traversal, GET/HEAD only.

## The SPA (03 §10)
Hash routes `#/chat`, `#/files`, `#/calibration`, `#/usage`, behind the dev-login gate. Stores are rebuilt on every
login, so no data outlives its alias. Views are pure functions of store state (testable with `react-dom/server`).
- **W1** (three panes: sessions | conversation | inspector). `turns.stream` with `session_id` (and `input_file` /
  `input_image` parts plus `wait_for_files: true` for attachments). The text is the committed attempt's deltas only
  (the SDK's reconciliation, including `generation.rerouted` with `discard_from_seq`). **"Refining…"** shows from a
  `generation.cancelled{spec_changed}` before any commit (the invisible restart) until the next attempt commits; a
  failed attempt (fallback) or a lost race does not show it. The inspector follows the live turn from `turn.created`;
  older answers open through `GET /v1/turns/{id}` (`fromTurnObject`, same view). Calibration badge: `calibrated` ->
  "calibrated vs LLM consensus", `provider_reported` -> "provider reported", anything else -> "uncalibrated" (OpenEnum
  rule). Confidence is labeled heuristic (method, margin, entropy, agreement in the tooltip). Member rows: model, status,
  own vote (argmax of its calibrated or raw distribution), latency, input view ("saw OCR text"), elicitation, skip
  reason. Verification badge: each verification predicate passes when `P(true) >= 0.5`. Stop = `TurnStream.abort()`
  (POST cancel). Model Markdown goes through `SafeMarkdown`; the user's own text is plain text.
- **W2**: drop zone or file picker -> `files.upload` (purpose, session_id, then file) -> `files.watch` snapshots ->
  derivatives with preview buttons (`page_images`: pages 1…8). Previews by blob content type: PNG/JPEG as blob-URL
  `<img>`, `text/markdown` through `SafeMarkdown`, `text/plain` in `<pre>`, `application/pdf` as a download link only
  (never embedded), anything else (SVG included) not rendered. Typed rejections (all 12 `FileRejectionCode`s) and
  failures; typed upload errors (413 `file_too_large`, 415 `unsupported_media_type`, ...). The "active content removed"
  notice comes from turn degradations `active_content_removed` naming the file (spec-07 §4.3: on
  `preflight.completed`).
- **W4** (read-only): the three `/v1/admin` lists load independently (an auditor gets 403 on dropped items only),
  newest first, 100 rows; calibrator status filter and dropped-reason filter. Activation stays in the CLI.
- **W7**: `usage.list({group_by, from, to})`, one column per line (decision, generation, labeling, verification, waste,
  cache savings) plus billed = every line except cache_savings (spec-11 §3); group_by session -> the session's
  assistant turns -> one turn (decision, each attempt as generation or waste, verification, total, and the inspector).

## Security map (spec-09 §8)
| Requirement | Implementation | Test |
|---|---|---|
| No API key in the browser | proxy-held keys, HttpOnly session cookie, SPA sets no Authorization | `proxy.test.ts`, `stores.test.ts`, e2e guard + `05-security` (localStorage, sessionStorage, IndexedDB, document.cookie, context cookies) |
| CSP, Referrer-Policy, nosniff | `server/security.ts` on every response; build has no inline script/style, `assetsInlineLimit: 0` | `proxy.test.ts`, SEC-UI-01 guard in every e2e test (0 `securitypolicyviolation`, 0 requests outside 'self', the three header values on every same-origin response: SPA files, `/auth`, proxied JSON, SSE and derivatives), `05-security` |
| Sanitized Markdown | react-markdown, `skipHtml`, `urlTransform` (absolute http/https/mailto only), `<img>` replaced by a text marker, links `target=_blank rel="noopener noreferrer nofollow"` + external marker + confirm | `markdown.test.tsx` (18-entry corpus), SEC-UI-02 in Chrome (`01-w1-chat`) |
| No SVG / PDF embedding | preview allowlist PNG/JPEG; PDF download link | `uploadStore.test.ts`, `components.test.tsx`, `02-w2-uploads` |
| CSRF | double submit + session binding on every mutating `/v1` and `/auth` call | `proxy.test.ts`, SEC-UI-03 in Chrome |
| DNS rebinding / foreign pages | Host guard on every request, Origin check on `/auth` POSTs and mutating `/v1`, loopback-only `NOEMA_UI_HOST` by default, `ak_test_` keys only | `proxy.test.ts` (4 cases) |
| Source policy | no storage APIs, no `innerHTML`/`dangerouslySetInnerHTML`, no `eval`, no inline styles, no SVG/iframe/embed, no `api.openai.com` in `web/src` | `markdown.test.tsx` "source policy" |

## Tests
- vitest, 75: `turnState` 25 (all 17 golden SSE fixtures: committed text = stored `output_text` or the deltas, final
  decision, timings, attempts; "Refining…" exactly over G02's restart window; G05/G06 never; reroute; degradations;
  the getTurn fixture), `chatStore` 6 (sessions + history pagination fixtures, G02 stream with CSRF and no
  Authorization, attachment parts, the 409 fixture, stored-turn inspect, cancel), `uploadStore` 7 (watchFile fixture,
  encrypted rejection, 413, partial-with-pending polling, preview typing, page fetch, notices), `components` 12
  (inspector on G01/G02/G08/G09/G10 and the valid, escalated and hierarchical contract examples; conversation;
  FileCard; W4 cards on the admin fixtures; usage pivot), `markdown` 5, `proxy` 14 (real sockets: env gate, key
  format, cookies, CSRF, header allowlist, SSE pass-through, early-close recording, static hardening, 502, Host guard,
  Origin check, loopback-only host, `ak_live_` refusal), `stores` 6.
- Playwright (`web/e2e`, system Chrome, 1 worker, 12 tests): W1 stream + inspect (live and stored), "Refining…",
  SEC-UI-02; W2 upload + previews, encrypted + .docx rejections, active-content notice; W4 dashboard and the
  permission error; W7 grouping + drill-down; CSP/headers, SEC-UI-03, cookie flags and storage. All 12 pass against
  the local fixture API (55 recorded responses) and, with none skipped, against the compose stack (`noema-e2e-ui`,
  `UI_STACK_MOCK=1`: 56 responses per run; `--repeat-each=2`: 24/24 and 111 responses); 0 contract violations.

## Running the e2e against the stack (e2e phase)
1. Bring the stack up and bootstrap it (integrator: `make e2e-up e2e-bootstrap`, state file
   `~/.noema/stacks/<project>.json`).
2. `make ui-e2e UI_STACK_STATE=~/.noema/stacks/<project>.json` (tenant `t_acme`, project `main`; aliases = principal
   names: developer, tenant_admin, reviewer, ...). Or set `NOEMA_UI_API_URL` and `NOEMA_UI_API_KEYS[_FILE]` yourself;
   override the aliases with `NOEMA_UI_E2E_ALIAS_{CHAT,ADMIN,REVIEWER}`; `NOEMA_UI_PORT` (default 5180; the fixture API
   uses port+1).
   Use your own compose project when another agent holds the default one (`make e2e-up E2E_PROJECT=noema-e2e-ui
   E2E_PORT_BASE=28200 E2E_BUILD=0`, then `UI_STACK_STATE=~/.noema/stacks/noema-e2e-ui.json`).
3. "Refining…" against a stack: `UI_STACK_MOCK=1` (sets `NOEMA_UI_E2E_MOCK_URL` = `endpoints.mockbedrock`; your own
   stack only, since it replaces the mock's scenario for the test). `mock-scenario.ts` loads the stack's default
   scenario (deploy/compose/mockbedrock/default.yaml) with three rules in front for a unique marker: the router answers
   intent `coding` (binding 0 routes opus-deep instead of the speculative sol-fast, a `spec_changed` restart) and the
   opus-deep attempt's first text (its commit) comes after 2.5 s, so "Refining…" stays visible. The spec also checks
   the confirmed route is opus-deep, then reloads the default scenario. Without `UI_STACK_MOCK=1` it needs
   `NOEMA_UI_E2E_RESTART_PROMPT` (a prompt that restarts under the stack's current scenario), else it is skipped.
   The active-content notice runs on every stack (`DerivativeRef.active_content` landed in C2b; the orchestrator
   emits `active_content_removed` on `preflight.completed`). W1 and W7 prompts get a per-run suffix on a stack
   (`unique()`), so the L2 decision cache never answers them. W4 accepts empty lists on a fresh stack; W7 polls the
   session row for 15 s (usage lines are applied through the outbox), then opens the turn by id.
4. `global-setup.ts` writes the upload files with `uv run --frozen --no-sync python web/e2e/make_media.py` (the media
   pipeline's generated fixtures: pikepdf/Pillow from the project venv).

## Decisions and deviations
- The dev "server" is a small Node proxy serving the built bundle, not the Vite dev server: Vite's dev mode injects
  inline scripts/styles that the spec-09 §8 CSP forbids. No HMR.
- The UI keeps its own turn reducer instead of the SDK's internal `TurnAccumulator` (not exported): the inspector needs
  attempt states, routing, restarts and errors as they stream. Same reconciliation rules, checked on every golden.
- Component tests render with `react-dom/server` (no jsdom/testing-library in the pinned toolchain); interaction is
  covered by Playwright. `noUnusedLocals`/`noUnusedParameters` are off because tsc would also apply them to the SDK
  sources compiled through the `@aghoo/sdk` path alias (one unused import there).
- Proxy response recording (`NOEMA_UI_RECORD`) exists so the UI e2e meets "every API response validated against the
  contract": the Python ContractValidator checks the JSONL afterwards.
- Model output links are allowed for http(s)/mailto with a confirm dialog and an external-host marker ("link warning");
  relative and other schemes render as plain text.
- C2b/D e2e (stack): the UI e2e ran on its own compose project (`noema-e2e-ui`, port base 28200), not the
  integrator's `noema-e2e`, because the specs create sessions, turns and files and the restart spec loads a mock
  scenario. Loading a scenario is opt-in (`UI_STACK_MOCK=1`) so a run against a shared stack never changes its mock.
- The restart spec's scenario is built from the stack's default.yaml (js-yaml `load`/`dump`; `web/test/js-yaml.d.ts`
  declares both) instead of a separate scenario file, so it follows changes to the default scenario.
- C2b/D review fix (DNS rebinding): the proxy checks `Host` on every request and `Origin` on every mutating call,
  refuses a non-loopback `NOEMA_UI_HOST` unless `NOEMA_UI_ALLOW_NON_LOOPBACK=1`, and holds `ak_test_` keys only
  (spec-09 §2 refuses test keys only in prod, so dev stacks work with them; `tools/bootstrap_dev.py` mints
  `--env test`). Both refusals are 403 `permission_denied` envelopes, like the CSRF refusal. Origin must equal
  `http(s)://` + Host (not just any allowed name), so `localhost` and `127.0.0.1` pages are distinct origins.
- The guard's header check skips `blob:` responses (preview images are local blobs; Chrome reports the page origin for
  them).

## Known limits
- The SDK's `files.watch` ends at `partial` even with OCR pending; the UI then polls `GET /v1/files/{id}` every 2 s
  (at most 150 times). Request: `.orchestration/requests/ui.md` item 3.
- Sessions live in proxy memory: restarting the proxy logs every browser out (the next API call is 401; reload to log
  in again). No session expiry UI.
- The conversation re-renders the whole Markdown of the live answer on each delta (fine for chat-sized answers).
- W4 and W7 show at most 100 / 1000 rows (no paging UI). W2 previews at most 8 page images per file.
- The Host guard compares against the port the proxy listens on and the configured or loopback names. A port
  forward that changes the port number, or an https front (`NOEMA_UI_SECURE_COOKIES=1`) that rewrites `Host` or
  listens on another port, is refused with 403, and so is port 80 (browsers omit the port from Host). With
  `NOEMA_UI_HOST=0.0.0.0` only the loopback spellings pass; bind the address the browser uses instead.
- The attach button's label is the 📎 emoji (with a title): on a host without an emoji font, such as the e2e host's
  headless Chrome, it renders as an empty box.
