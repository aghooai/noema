# API targets (owner: api). App factory: noema.api.app:create_app (uvicorn --factory --loop uvloop).
API_HOST ?= 127.0.0.1
API_PORT ?= 8080

.PHONY: api-test api-smoke api-serve
api-test: ## API unit + integration tests (middleware, SSE framing, stubs, ingress order, uvicorn over sockets)
	$(call run_pytest,tests/unit/api tests/integration/api,-m "not live and not e2e and not perf")

api-smoke: ## Schemathesis smoke over every bundled OpenAPI operation against the 501 stubs
	$(call run_pytest,tests/integration/api/test_schemathesis_smoke.py,)

api-serve: ## Serve the API on API_HOST:API_PORT (uvloop, httptools; needs noema.composition)
	NOEMA_ENV=$${NOEMA_ENV:-dev} $(RUN) uvicorn noema.api.app:create_app --factory --loop uvloop --http httptools \
		--host $(API_HOST) --port $(API_PORT)
