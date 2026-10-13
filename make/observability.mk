# Observability as code (Wave F): Prometheus rules and their promtool tests, the example scrape configuration and
# the Grafana dashboards under deploy/observability (docs/operations/observability.md). promtool runs from the
# pinned Prometheus image (pulled once; the only networked step).
PROMETHEUS_IMAGE ?= prom/prometheus:v3.5.0
OBS_DIR := deploy/observability
OBS_RULES := $(sort $(wildcard $(OBS_DIR)/prometheus/rules/*.rules.yml))
OBS_RULE_TESTS := $(sort $(wildcard $(OBS_DIR)/prometheus/tests/*.test.yml))
OBS_DASHBOARD_BUILDER := $(OBS_DIR)/grafana/build_dashboards.py
promtool = docker run --rm --user $$(id -u):$$(id -g) --entrypoint promtool -v $(CURDIR):/work:ro -w /work \
	$(1) $(PROMETHEUS_IMAGE)

.PHONY: observability-check observability-dashboards
observability-check: ## promtool check/test of the rules, config check, dashboard queries and JSON, metric refs
	$(call promtool) check rules --lint=all $(OBS_RULES)
	$(call promtool) test rules $(OBS_RULE_TESTS)
	$(call promtool) check config --syntax-only $(OBS_DIR)/prometheus/prometheus.example.yml
	@tmp="$$(mktemp -d)"; trap 'rm -rf "$$tmp"' EXIT; chmod 755 "$$tmp"; \
	$(RUN) python $(OBS_DASHBOARD_BUILDER) --promql-rules "$$tmp/dashboard-queries.rules.yml"; \
	echo "promtool check rules <every dashboard query>"; \
	$(call promtool,-v $$tmp:/dashboard-queries:ro) check rules /dashboard-queries/dashboard-queries.rules.yml
	NOEMA_OBS_RUNBOOKS=1 $(PYTEST) tests/unit/observability -p no:cacheprovider $(PYTEST_ARGS)
	@echo "observability-check: all green"

observability-dashboards: ## Regenerate the Grafana dashboard JSON from deploy/observability/grafana/build_dashboards.py
	$(RUN) python $(OBS_DASHBOARD_BUILDER)
