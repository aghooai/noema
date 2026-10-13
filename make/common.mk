# Shared variables, environment, compose services and test targets.
UV ?= uv
RUN := $(UV) run --frozen --no-sync
PYTEST := $(RUN) pytest
# MOD=<name> narrows lint/typecheck/test to src/noema/<name> and tests/{unit,integration}/<name> (whichever exist).
# A path-style name also finds the underscore test directories (MOD=ops/admission -> tests/*/ops_admission), and the
# role names of the split packages map to their source files (MOD=media_negotiation, media_pipeline, ops_admission,
# ops_ledger), so every role uses one name for lint, typecheck and test: e.g. MOD=core, MOD=db, MOD=ops/admission.
MOD ?=
# XDIST=<n> runs pytest with n xdist workers (at most 4 per agent, spec-10 §1.1); 0 = no xdist.
XDIST ?= 0
PYTEST_ARGS ?=
XDIST_ARG := $(if $(filter-out 0,$(XDIST)),-n $(XDIST) --dist load,)

COMPOSE_FILE := deploy/compose/docker-compose.yml
COMPOSE := docker compose -p noema-dev -f $(COMPOSE_FILE) $(if $(wildcard .env),--env-file .env,)

mod_key = $(subst /,_,$(MOD))
MOD_SRC_media_negotiation := $(addprefix src/noema/media/,negotiation.py budget.py views.py)
MOD_SRC_media_pipeline := $(foreach m,intake quarantine pipeline sandbox extractors worker,src/noema/media/$(m) \
	src/noema/media/$(m).py)
MOD_SRC_ops_admission := src/noema/ops/admission
MOD_SRC_ops_ledger := $(foreach m,cost prices usage jobs outbox idempotency retention reapers notify_bridge,\
	src/noema/ops/$(m) src/noema/ops/$(m).py)
mod_src = $(sort $(wildcard src/noema/$(MOD) src/noema/$(MOD).py $(MOD_SRC_$(mod_key))))
mod_unit = $(sort $(wildcard tests/unit/$(MOD) tests/unit/$(mod_key)))
mod_integration = $(sort $(wildcard tests/integration/$(MOD) tests/integration/$(mod_key)))
mod_tests = $(mod_unit) $(mod_integration)
require_mod = $(if $(strip $(1)),,$(error MOD=$(MOD) matches nothing under src/noema or tests))

# Runs pytest on the given paths; "no tests collected" (exit 5) counts as success, a missing path is skipped.
define run_pytest
	@paths="$(strip $(wildcard $(1)))"; \
	if [ -z "$$paths" ]; then echo "no tests under $(1): skipped"; exit 0; fi; \
	set +e; $(PYTEST) $$paths $(XDIST_ARG) $(2) $(PYTEST_ARGS); rc=$$?; set -e; \
	if [ $$rc -eq 5 ]; then echo "no tests collected in $$paths"; exit 0; fi; exit $$rc
endef

.PHONY: help sync sync-dev lock npm-ci services-up services-down services-ps services-logs \
	test-unit test-integration test-conformance test
help: ## List the targets
	@grep -hE '^[a-zA-Z0-9_.-]+:.*## ' $(MAKEFILE_LIST) | sort -u | \
		awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

sync: ## Install the locked dev + media dependency groups into .venv (exact)
	$(UV) sync --frozen --group media

sync-dev: ## Install only the locked dev group (drops the media parsers)
	$(UV) sync --frozen

lock: ## Re-resolve uv.lock (platform/orchestrator only; agents never change dependencies)
	$(UV) lock

npm-ci: ## Install the locked JS toolchain (package-lock.json) into node_modules: offline, no install scripts
	npm ci --offline --ignore-scripts --no-audit --no-fund

services-up: ## Start the base compose services (postgres, valkey, moto) and wait until healthy
	$(COMPOSE) up -d --wait

services-down: ## Stop the base compose services (volumes kept)
	$(COMPOSE) down

services-ps: ## Show the base compose services
	$(COMPOSE) ps

services-logs: ## Tail the base compose service logs
	$(COMPOSE) logs --tail=100

test-unit: ## Unit tests (no network): tests/unit, or MOD=<name>
ifeq ($(MOD),)
	$(call run_pytest,tests/unit,-m "not live")
else
	$(call run_pytest,$(or $(mod_unit),tests/unit/$(MOD)),-m "not live")
endif

test-conformance: ## Conformance suites shared by fakes and real implementations (tests/contract_internal)
	$(call run_pytest,tests/contract_internal,-m "not live")

test-integration: ## Integration tests against the compose services: tests/integration, or MOD=<name>
ifeq ($(MOD),)
	$(call run_pytest,tests/integration,-m "not live and not e2e and not perf")
else
	$(call run_pytest,$(or $(mod_integration),tests/integration/$(MOD)),-m "not live and not e2e and not perf")
endif

test: ## Unit + integration tests of one module: make test MOD=<name>
	$(call require_mod,$(mod_tests))
	$(call run_pytest,$(mod_tests),-m "not live and not e2e and not perf")
