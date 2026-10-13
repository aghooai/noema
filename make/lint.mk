# Static checks, ownership and contract synchronisation.
LINT_PATHS := src tests tools conftest.py
CONTRACTS_SRC ?= /codes/aghooai/docs/noema/contracts

.PHONY: lint format typecheck typecheck-tests importlint forbidden-scan ownership contracts-check contracts-sync \
	contracts-generate
lint: ## ruff check + ruff format --check (MOD=<name> for one module)
ifeq ($(MOD),)
	$(RUN) ruff check $(LINT_PATHS)
	$(RUN) ruff format --check $(LINT_PATHS)
else
	$(call require_mod,$(mod_src)$(mod_tests))
	$(RUN) ruff check $(mod_src) $(mod_tests)
	$(RUN) ruff format --check $(mod_src) $(mod_tests)
endif

format: ## ruff format + safe autofixes (MOD=<name> for one module; only on paths you own)
ifeq ($(MOD),)
	$(RUN) ruff format $(LINT_PATHS)
	$(RUN) ruff check --fix $(LINT_PATHS)
else
	$(call require_mod,$(mod_src)$(mod_tests))
	$(RUN) ruff format $(mod_src) $(mod_tests)
	$(RUN) ruff check --fix $(mod_src) $(mod_tests)
endif

typecheck: ## mypy --strict on src/noema (MOD=<name> for one package)
ifeq ($(MOD),)
	$(RUN) mypy --strict
else
	$(call require_mod,$(mod_src))
	$(RUN) mypy --strict $(mod_src)
endif

typecheck-tests: ## mypy --strict on the root conftest, tests/ and tools/ (every role's test paths)
	$(RUN) mypy --strict conftest.py tests tools

importlint: ## import-linter contracts of spec-01 §2 (.importlinter)
	$(RUN) lint-imports --no-cache

# spec-01 §2 rule 8 and spec-10 §7, beyond import-linter (which sees neither TYPE_CHECKING imports nor
# importlib/__import__): no api.openai.com anywhere we ship or test, no dynamic import of a forbidden client, and no
# import of one in src/ at all. The vendored openai-compat.ts names the OpenAI endpoint in a doc comment.
FORBIDDEN_CLIENTS := openai|anthropic|requests|boto3
FORBIDDEN_SCAN_PATHS = $(wildcard src tests tools sdk web/src conftest.py)
FORBIDDEN_ALLOW := ^src/noema/contracts/_vendor/openai-compat\.ts:
forbidden-scan: ## No api.openai.com, no dynamic/TYPE_CHECKING imports of openai, anthropic, requests or boto3
	@hits="$$( { grep -rnE --exclude-dir=node_modules --exclude-dir=__pycache__ 'api\.openai\.com' \
		$(FORBIDDEN_SCAN_PATHS) || true; \
		grep -rnE --include='*.py' --exclude-dir=__pycache__ \
		"(import_module|__import__)\(\s*['\"]($(FORBIDDEN_CLIENTS))(['\".]|$$)" $(FORBIDDEN_SCAN_PATHS) || true; \
		grep -rnE --include='*.py' --exclude-dir=__pycache__ \
		'^\s*(import|from)\s+($(FORBIDDEN_CLIENTS))(\s|\.|,|$$)' src || true; } \
		| grep -vE '$(FORBIDDEN_ALLOW)' || true)"; \
	if [ -n "$$hits" ]; then echo "forbidden endpoint or client import:"; echo "$$hits"; exit 1; fi; \
	echo "forbidden-scan: clean"

ownership: ## Changed paths -> owners (OWNERS.yaml); ROLE=<role> fails on paths that role does not own
	$(RUN) python tools/check_ownership.py $(if $(ROLE),--role $(ROLE),)

contracts-check: ## Vendored contracts identical to the canonical tree; bundle and generated models up to date
	NOEMA_CONTRACTS_SRC=$(CONTRACTS_SRC) $(RUN) python tools/sync_contracts.py --check
	$(RUN) python tools/sync_contracts.py --check-generated

contracts-sync: ## Re-copy the canonical contracts into src/noema/contracts/_vendor and the SDKs (platform/orchestrator only)
	NOEMA_CONTRACTS_SRC=$(CONTRACTS_SRC) $(RUN) python tools/sync_contracts.py
	$(MAKE) sdk-sync

contracts-generate: ## Re-bundle openapi.yaml (Redocly, npx), regenerate the strict server models and the SDK wire types
	$(RUN) python tools/sync_contracts.py --generate
	$(MAKE) sdk-sync
