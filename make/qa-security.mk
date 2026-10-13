# QA security track (Wave E, role qa-security): tests/security on the track's OWN compose stack (spec-09 §10, spec-10
# §3 SE1-SE13; report reports/qa/security.md). The stack has its own project, ports and image tag, so it shares no
# container, port or image with another agent. Non-dependency tools run from docker images (trivy, gitleaks) or uvx
# (pip-audit); nothing enters pyproject.toml or package.json. Artifacts (captured logs, dumps, findings: they hold
# test data) go to QA_SEC_OUT outside the repository; scanner reports and SBOMs to QA_SEC_REPORTS.
QA_SEC_PROJECT ?= noema-sec
QA_SEC_PORT_BASE ?= 28900
QA_SEC_TAG ?= sec
QA_SEC_UI_PORT ?= 28990
QA_SEC_OUT ?= $(HOME)/.noema/qa-security
QA_SEC_REPORTS ?= reports/qa/security
QA_SEC_BUILD ?= 1
# uvicorn workers per api pod (compose NOEMA_STACK_API_WORKERS; empty = the compose default). Images built before the
# E sprint-B multi-worker change need 1 (their second worker cannot bind the metrics port).
QA_SEC_API_WORKERS ?=
TRIVY_IMAGE ?= aquasec/trivy:0.75.0
GITLEAKS_IMAGE ?= zricethezav/gitleaks:v8.30.1
PIP_AUDIT ?= pip-audit==2.10.1
# SE13 waivers (infra, defect 29): the trivy ignore file, one id + review date per line; read by the sca_gate.
QA_SEC_WAIVERS ?= deploy/docker/.trivyignore.yaml

QA_SEC_STATE = $(HOME)/.noema/stacks/$(QA_SEC_PROJECT).json
QA_SEC_IMAGES = noema-api noema-worker noema-media
QA_SEC_STACK = NOEMA_API_IMAGE=noema-api:$(QA_SEC_TAG) NOEMA_WORKER_IMAGE=noema-worker:$(QA_SEC_TAG) \
	NOEMA_MEDIA_IMAGE=noema-media:$(QA_SEC_TAG) $(if $(QA_SEC_API_WORKERS),NOEMA_STACK_API_WORKERS=$(QA_SEC_API_WORKERS),)
QA_SEC_E2E = E2E_PROJECT=$(QA_SEC_PROJECT) E2E_PORT_BASE=$(QA_SEC_PORT_BASE) IMAGE_TAG=$(QA_SEC_TAG) \
	E2E_BUILD=$(QA_SEC_BUILD)
QA_SEC_PYTEST_ENV = NOEMA_E2E_STATE=$(QA_SEC_STATE) NOEMA_E2E_PROJECT=$(QA_SEC_PROJECT) NOEMA_E2E_REQUIRE=1 \
	NOEMA_SEC_ARTIFACTS=$(QA_SEC_OUT)
QA_SEC_UI_ENV = NOEMA_ENV=dev NOEMA_UI_PORT=$(QA_SEC_UI_PORT) \
	NOEMA_UI_API_URL="$$($(RUN) python $(CURDIR)/web/e2e/stack_env.py $(QA_SEC_STATE) url)" \
	NOEMA_UI_API_KEYS="$$($(RUN) python $(CURDIR)/web/e2e/stack_env.py $(QA_SEC_STATE) keys)"
# Both scanners run as the calling user (no root-owned files in the tree); trivy reaches the docker socket by group.
DOCKER_AS_ME = docker run --rm --user $$(id -u):$$(id -g)
TRIVY = $(DOCKER_AS_ME) --group-add $$(stat -c %g /var/run/docker.sock) -v /var/run/docker.sock:/var/run/docker.sock \
	-e TRIVY_CACHE_DIR=/cache -v $(QA_SEC_OUT)/trivy-cache:/cache -v $(abspath $(QA_SEC_REPORTS)):/out $(TRIVY_IMAGE)

.PHONY: qa-security qa-security-local qa-security-stack qa-security-fuzz qa-security-sca qa-security-down

qa-security: ## Security gates SE1-SE13 (spec-09 §10): every part runs, then the failed parts are listed
	@failed=""; for part in qa-security-local qa-security-stack qa-security-fuzz qa-security-sca; do \
		$(MAKE) $$part || failed="$$failed $$part"; done; \
	if [ -n "$$failed" ]; then echo "qa-security FAILED:$$failed"; exit 1; fi; echo "qa-security: all green"

qa-security-local: ## Stackless: the authz oracle and its golden cells, x-noema-roles, scanner patterns, STS + adapters on moto/mock
	$(QA_SEC_PYTEST_ENV) $(PYTEST) tests/security --ignore=tests/security/stack --ignore=tests/security/fuzz \
		-p no:cacheprovider $(PYTEST_ARGS)

# e2e traffic first (the log scan covers it), then the UI flows (Playwright SEC-UI-01..03) and the stack tests with the
# dev proxy up (test_ui_csp); test_zz_secrets_scan captures and scans everything last; gitleaks re-scans the capture.
qa-security-stack: ## On the security stack: e2e traffic, SEC-UI Playwright, tests/security/stack, gitleaks on the capture
	$(QA_SEC_STACK) $(MAKE) e2e $(QA_SEC_E2E)
	$(MAKE) ui-build
	NOEMA_UI_PORT=$$(( $(QA_SEC_UI_PORT) + 5 )) $(MAKE) ui-e2e UI_STACK_STATE=$(QA_SEC_STATE) UI_STACK_MOCK=1
	@mkdir -p $(QA_SEC_OUT); \
	(cd web && $(QA_SEC_UI_ENV) exec node server/main.ts > $(QA_SEC_OUT)/ui-proxy.log 2>&1) & proxy=$$!; \
	trap 'kill $$proxy 2>/dev/null || true' EXIT; sleep 2; status=0; \
	$(QA_SEC_PYTEST_ENV) NOEMA_SEC_UI_URL=http://127.0.0.1:$(QA_SEC_UI_PORT) \
		$(PYTEST) tests/security/stack -m e2e -p no:cacheprovider $(PYTEST_ARGS) || status=$$?; \
	$(DOCKER_AS_ME) -v $(QA_SEC_OUT):/out $(GITLEAKS_IMAGE) dir /out/capture --redact --no-banner \
		--report-format json --report-path /out/gitleaks-capture.json >/dev/null 2>&1 || \
		echo "gitleaks: findings in the capture (triage in reports/qa/security.md: the mock's AKID records)"; \
	exit $$status

qa-security-fuzz: ## SE9 inside the media image (network none): mutation corpus through nsjail + coverage-guided atheris
	$(MAKE) test-media IMAGE_TAG=$(QA_SEC_TAG) MEDIA_TESTS=tests/security/fuzz PYTEST_ARGS="-s -p no:cacheprovider"

qa-security-sca: ## SE13: pip-audit (uv.lock), npm audit (2 lockfiles), trivy + CycloneDX SBOM per image, gitleaks repo
	@mkdir -p $(QA_SEC_REPORTS)/sbom $(QA_SEC_OUT)/sca $(QA_SEC_OUT)/trivy-cache
	$(UV) export --frozen --format requirements-txt --all-groups --no-emit-project --no-hashes \
		-o $(QA_SEC_OUT)/sca/requirements-all.txt >/dev/null
	rm -f $(QA_SEC_REPORTS)/pip-audit.json
	uvx $(PIP_AUDIT) -r $(QA_SEC_OUT)/sca/requirements-all.txt --no-deps --disable-pip --format json \
		-o $(QA_SEC_REPORTS)/pip-audit.json \
		|| test -s $(QA_SEC_REPORTS)/pip-audit.json \
		|| { echo 'qa-security-sca: pip-audit produced no report (network or install failure)' >&2; exit 2; }
	npm audit --json > $(QA_SEC_REPORTS)/npm-audit-root.json || true
	cd sdk/typescript && npm audit --package-lock-only --json > $(abspath $(QA_SEC_REPORTS))/npm-audit-sdk.json || true
	for img in $(QA_SEC_IMAGES); do \
		$(TRIVY) image --quiet --scanners vuln --format json --output /out/trivy-$$img.json $$img:$(QA_SEC_TAG); \
		$(TRIVY) image --quiet --format cyclonedx --output /out/sbom/$$img.cdx.json $$img:$(QA_SEC_TAG); done
	$(DOCKER_AS_ME) -v $(CURDIR):/repo:ro -v $(abspath $(QA_SEC_REPORTS)):/out \
		$(GITLEAKS_IMAGE) git /repo --redact --no-banner --report-format json --report-path /out/gitleaks-repo.json \
		>/dev/null 2>&1 || true
	$(RUN) python -m tests.security.sca_gate $(QA_SEC_REPORTS) $(QA_SEC_WAIVERS)

qa-security-down: ## Stop the security stack and delete its volumes
	$(QA_SEC_STACK) $(MAKE) e2e-down E2E_PROJECT=$(QA_SEC_PROJECT) E2E_PORT_BASE=$(QA_SEC_PORT_BASE)
