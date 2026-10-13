# The fault suite (QA, Wave E; spec-10 §4, 06 §7): F1-F12, the p95-TTFT breaker rule and slow clients against an e2e
# stack of its own (FAULT_PROJECT) whose app containers reach postgres, valkey and moto through toxiproxy
# (docs/modules/integration.md §5 "Fault runs"), with the production breaker policy (p95-TTFT rule armed).
#   make qa-fault [FAULT_BUILD=1] [FAULT_IMAGE_TAG=dev] [FAULT_TESTS=tests/fault/test_f01_throttling.py]
#   make qa-fault-down
# Numbers of every row go to FAULT_RESULTS (JSON) for reports/qa/fault.md. The slow-client test reads the api
# containers directly on the compose network (no docker-proxy buffering), so their IPs join pytest-socket's hosts.
# To pin the stack to a tag while deploy/ changes in the working tree, extract it (git archive <tag> deploy tools
# tests/fakes) outside the repo and pass COMPOSE_FILE=<tree>/deploy/compose/docker-compose.yml (reports/qa/fault.md).
FAULT_PROJECT ?= noema-fault
FAULT_PORT_BASE ?= 29300
FAULT_BUILD ?= 0
FAULT_IMAGE_TAG ?= dev
FAULT_TESTS ?= tests/fault
FAULT_RESULTS ?= reports/qa/fault/results.json
FAULT_STATE = $(HOME)/.noema/stacks/$(FAULT_PROJECT).json
FAULT_ENV = NOEMA_STACK_REDIS_URL=redis://toxiproxy:16379/0 NOEMA_STACK_MOTO_URL=http://toxiproxy:15000 \
	NOEMA_API_IMAGE=noema-api:$(FAULT_IMAGE_TAG) NOEMA_WORKER_IMAGE=noema-worker:$(FAULT_IMAGE_TAG) \
	NOEMA_MEDIA_IMAGE=noema-media:$(FAULT_IMAGE_TAG)
FAULT_STACK_ARGS = E2E_PROJECT=$(FAULT_PROJECT) E2E_PORT_BASE=$(FAULT_PORT_BASE) E2E_DB_HOST=toxiproxy:15432 \
	E2E_BREAKER_TTFT=on

.PHONY: qa-fault qa-fault-up qa-fault-down

qa-fault-up: ## Start (or re-check) the fault-routed stack FAULT_PROJECT (toxiproxy in front of postgres/valkey/moto)
	$(FAULT_ENV) $(MAKE) e2e-up $(FAULT_STACK_ARGS) E2E_BUILD=$(FAULT_BUILD)

qa-fault: qa-fault-up ## The fault suite F1-F12 (+ TTFT breaker rule, slow clients) on the fault stack; numbers -> FAULT_RESULTS
	@ips="$$(docker compose -p $(FAULT_PROJECT) -f $(COMPOSE_FILE) ps -q api-1 api-2 2>/dev/null \
		| xargs docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' | paste -sd, -)"; \
	echo "api containers: $$ips"; \
	NOEMA_FAULT=1 NOEMA_FAULT_DIRECT="$$ips" NOEMA_FAULT_RESULTS=$(FAULT_RESULTS) NOEMA_E2E_STATE=$(FAULT_STATE) \
		NOEMA_E2E_PROJECT=$(FAULT_PROJECT) NOEMA_E2E_REQUIRE=1 \
		$(PYTEST) $(FAULT_TESTS) -p no:cacheprovider -s --allow-hosts=127.0.0.1,::1,$$ips $(PYTEST_ARGS)

qa-fault-down: ## Stop the fault stack and delete its volumes
	$(FAULT_ENV) $(MAKE) e2e-down $(FAULT_STACK_ARGS)
