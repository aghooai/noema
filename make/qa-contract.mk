# Contract QA track (Wave E, role qa; reports/qa/contract.md). Gates Q1-Q3 and X1 of spec-10 §3 on a compose stack of
# its own (QA_CONTRACT_PROJECT on QA_CONTRACT_PORT_BASE, never the shared noema-e2e or noema-dev), with the
# server-side ContractValidator on (E2E_VALIDATE=1). QA_CONTRACT_BUILD=0 reuses the current images.
QA_CONTRACT_PROJECT ?= noema-qa-contract
QA_CONTRACT_PORT_BASE ?= 29600
QA_CONTRACT_BUILD ?= 1
QA_CONTRACT_STATE = $(HOME)/.noema/stacks/$(QA_CONTRACT_PROJECT).json
DOCS_GUIDE ?= /codes/aghooai/docs/noema/07-api-user-guide.md
QA_CONTRACT_STACK = E2E_PROJECT=$(QA_CONTRACT_PROJECT) E2E_PORT_BASE=$(QA_CONTRACT_PORT_BASE)

.PHONY: qa-contract qa-contract-up qa-contract-down docs-drift sdk-compat

docs-drift: ## Every JSON example of 07-api-user-guide.md validates against the bundled OpenAPI (DOCS_GUIDE=...)
	NOEMA_DOCS_GUIDE=$(DOCS_GUIDE) NOEMA_DOCS_REQUIRE=1 $(PYTEST) tests/qa/contract/test_docs_drift.py \
		-p no:cacheprovider $(PYTEST_ARGS)

sdk-compat: ## openapi-typescript output vs the contract TS types; strict server vs tolerant client models
	$(PYTEST) tests/qa/contract/test_sdk_models.py tests/qa/contract/test_sdk_wire_types.py -p no:cacheprovider \
		$(PYTEST_ARGS)

qa-contract-up: ## Start (or re-check) the contract track's own stack
	$(MAKE) e2e-up $(QA_CONTRACT_STACK) E2E_BUILD=$(QA_CONTRACT_BUILD) E2E_VALIDATE=1

qa-contract: docs-drift sdk-compat qa-contract-up ## The contract track: drift, SDK compat, schemathesis + facade/Jev on the stack
	NOEMA_E2E_STATE=$(QA_CONTRACT_STATE) NOEMA_E2E_PROJECT=$(QA_CONTRACT_PROJECT) NOEMA_E2E_REQUIRE=1 \
		$(PYTEST) tests/qa/contract -m e2e -p no:cacheprovider $(PYTEST_ARGS)
	@n=$$(docker compose -p $(QA_CONTRACT_PROJECT) -f $(COMPOSE_FILE) logs api-1 api-2 2>&1 | grep -c -E 'contract violation|ContractViolation' || true); \
		echo "server-side ContractValidator violations (api-1, api-2): $$n"; test "$$n" = 0

qa-contract-down: ## Stop the contract track's stack and delete its volumes
	$(MAKE) e2e-down $(QA_CONTRACT_STACK)
