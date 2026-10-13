# The full compose stack for e2e, UI and perf runs (integrator; docs/modules/integration.md). Its own compose project
# (E2E_PROJECT, default noema-e2e) with its own postgres/valkey/moto, so it never touches the shared noema-dev
# services. Fixed host ports from E2E_PORT_BASE (default 28100; 0 = ephemeral ports, read them from the state file):
#   api-1 +1, api-2 +2, api-1 metrics +11, api-2 metrics +12 (second worker: +13, +14), postgres +32, moto +50,
#   toxiproxy API +74, fault proxies postgres +75 / valkey +76 / moto +77, valkey +79, mockbedrock +80.
# The bootstrap state (API keys, operator keys, endpoints; mode 0600) is E2E_STATE, outside the repository.
E2E_PROJECT ?= noema-e2e
E2E_ENV ?= dev
E2E_PORT_BASE ?= 28100
E2E_BUILD ?= 1
E2E_VALIDATE ?=
E2E_BREAKER_TTFT ?= off
E2E_DB_HOST ?= postgres:5432
E2E_STATE ?= $(HOME)/.noema/stacks/$(E2E_PROJECT).json
E2E_TESTS ?= tests/e2e/smoke

e2e_port = $(if $(filter 0,$(E2E_PORT_BASE)),0,$(shell expr $(E2E_PORT_BASE) + $(1)))
E2E_VARIABLES = NOEMA_STACK_ENV=$(E2E_ENV) NOEMA_CONTRACT_VALIDATE=$(E2E_VALIDATE) \
	NOEMA_STACK_BREAKER_TTFT_RULE=$(E2E_BREAKER_TTFT) \
	NOEMA_API1_PORT=$(call e2e_port,1) NOEMA_API2_PORT=$(call e2e_port,2) \
	NOEMA_API1_METRICS_PORT=$(call e2e_port,11) NOEMA_API2_METRICS_PORT=$(call e2e_port,12) \
	NOEMA_API1_METRICS2_PORT=$(call e2e_port,13) NOEMA_API2_METRICS2_PORT=$(call e2e_port,14) \
	NOEMA_PG_PORT=$(call e2e_port,32) NOEMA_MOTO_PORT=$(call e2e_port,50) NOEMA_VALKEY_PORT=$(call e2e_port,79) \
	NOEMA_MOCKBEDROCK_PORT=$(call e2e_port,80) NOEMA_FAULT_TOXIPROXY_PORT=$(call e2e_port,74) \
	NOEMA_FAULT_PG_PORT=$(call e2e_port,75) NOEMA_FAULT_VALKEY_PORT=$(call e2e_port,76) \
	NOEMA_FAULT_MOTO_PORT=$(call e2e_port,77)
E2E_COMPOSE = $(E2E_VARIABLES) docker compose -p $(E2E_PROJECT) -f $(COMPOSE_FILE)
E2E_BOOTSTRAP = $(RUN) python -m tools.bootstrap_dev --compose-project $(E2E_PROJECT) --env $(E2E_ENV) \
	--db-host $(E2E_DB_HOST) --state $(E2E_STATE)

.PHONY: e2e e2e-up e2e-down e2e-bootstrap e2e-smoke e2e-ps e2e-logs e2e-images

e2e-images: image-api image-worker image-media ## Rebuild the three images the e2e stack runs

e2e-up: $(if $(filter 1,$(E2E_BUILD)),e2e-images,) ## Start the full stack (E2E_PROJECT), bootstrap it, wait until healthy
	$(E2E_COMPOSE) up -d --wait postgres valkey moto toxiproxy mockbedrock
	$(E2E_BOOTSTRAP) >/dev/null
	$(E2E_COMPOSE) --profile app up -d --wait
	$(E2E_BOOTSTRAP)

e2e-bootstrap: ## Seed (or re-check) the running stack: secrets, DB, config, tenants, keys -> E2E_STATE (0600)
	$(E2E_BOOTSTRAP)

e2e-smoke: e2e-up ## The smoke e2e suite (tests/e2e/smoke) against the stack, every response contract-validated
	NOEMA_E2E_STATE=$(E2E_STATE) NOEMA_E2E_PROJECT=$(E2E_PROJECT) NOEMA_E2E_REQUIRE=1 \
		$(PYTEST) $(E2E_TESTS) -m e2e -p no:cacheprovider $(PYTEST_ARGS)

# The whole e2e tree (smoke + tests/e2e/suite, about 2 min, serial: the tests share one stack and one mock).
e2e: e2e-up ## The full e2e suite (tests/e2e: smoke + suite) against the stack, every response contract-validated
	NOEMA_E2E_STATE=$(E2E_STATE) NOEMA_E2E_PROJECT=$(E2E_PROJECT) NOEMA_E2E_REQUIRE=1 \
		$(PYTEST) tests/e2e -m e2e -p no:cacheprovider $(PYTEST_ARGS)

e2e-down: ## Stop the e2e stack and delete its volumes (database, spool)
	$(E2E_COMPOSE) --profile '*' down -v --remove-orphans

e2e-ps: ## Show the e2e stack's services
	$(E2E_COMPOSE) --profile '*' ps

e2e-logs: ## Tail the e2e stack's logs (SERVICE=api-1 for one service)
	$(E2E_COMPOSE) --profile '*' logs --tail=200 $(SERVICE)
