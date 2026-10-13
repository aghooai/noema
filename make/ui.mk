# Web UI targets (owner: ui). Offline: packages resolve from the root node_modules; Playwright uses the system Chrome.
.PHONY: ui-check ui-build ui-serve ui-e2e
UI_E2E_RECORD ?= test-results/api-recording.jsonl

ui-check: ## web/: tsc --strict (app + node), vitest + msw, vite build, ruff/mypy on the e2e helpers
	cd web && npm run check
	$(RUN) ruff check web/e2e
	$(RUN) ruff format --check web/e2e
	$(RUN) mypy --strict web/e2e/validate_recording.py web/e2e/make_media.py web/e2e/stack_env.py

ui-build: ## web/dist (vite build)
	cd web && npm run build

ui-serve: ui-build ## Dev cookie proxy + SPA (NOEMA_ENV=dev, NOEMA_UI_API_URL, NOEMA_UI_API_KEYS[_FILE]; port 5180)
	cd web && node server/main.ts

# Against a bootstrapped stack: UI_STACK_STATE=~/.noema/stacks/<project>.json [UI_STACK_TENANT=t_acme] (else the local
# fixture API). NOEMA_UI_API_URL / NOEMA_UI_API_KEYS[_FILE] set by hand work too. UI_STACK_MOCK=1 (your own stack
# only: the spec loads a scenario into the stack's mockbedrock) also runs the "Refining…" spec.
UI_STACK_STATE ?=
UI_STACK_TENANT ?= t_acme
UI_STACK_MOCK ?=
UI_STACK_ENV = $(if $(UI_STACK_STATE),NOEMA_UI_API_URL="$$($(RUN) python web/e2e/stack_env.py $(UI_STACK_STATE) url)" \
	NOEMA_UI_API_KEYS="$$($(RUN) python web/e2e/stack_env.py $(UI_STACK_STATE) keys $(UI_STACK_TENANT))" \
	$(if $(filter 1,$(UI_STACK_MOCK)),NOEMA_UI_E2E_MOCK_URL="$$($(RUN) python web/e2e/stack_env.py $(UI_STACK_STATE) mock)",),)

ui-e2e: ui-build ## Playwright (system Chrome): W1/W2/W4/W7 + SEC-UI-01..03; then every proxied response vs the contract
	$(UI_STACK_ENV) NOEMA_UI_RECORD=$(UI_E2E_RECORD) bash -c 'cd web && npx playwright test $(PW_ARGS)'
	$(RUN) python web/e2e/validate_recording.py web/$(UI_E2E_RECORD)
