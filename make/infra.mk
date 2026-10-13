# Infra (Wave B2): the dev compose stack with profiles, the images and the media test runner (docs/modules/infra.md).
# Images: noema-api (api), noema-worker (jobs, media-worker; derives from noema-api), noema-media (media-sandbox) and
# noema-media-test (media-sandbox + dev/fuzz groups + tests). Docker builds are the only networked step.
IMAGE_TAG ?= dev
API_IMAGE ?= noema-api:$(IMAGE_TAG)
WORKER_IMAGE ?= noema-worker:$(IMAGE_TAG)
MEDIA_IMAGE ?= noema-media:$(IMAGE_TAG)
MEDIA_TEST_IMAGE ?= noema-media-test:$(IMAGE_TAG)
DOCKER_BUILD ?= docker build
# `make up` profiles (deploy/compose/docker-compose.yml): toxiproxy, media, app, fault.
PROFILES ?= toxiproxy
# Paths that `make test-media` runs inside the media image (those that exist when the test image is built).
MEDIA_TESTS ?= $(wildcard tests/unit/media_pipeline tests/integration/media_pipeline tests/integration/infra/media_image)
# Mounted read-only at the same path, so spec-07 MED-02 fixtures resolve identically on the host and in the image.
PROBE_FIXTURES ?= /codes/aghooai/probes/fixtures

INFRA_ENV_FILE := $(if $(wildcard .env),--env-file .env,)
media_compose = docker compose -p $(1) -f $(COMPOSE_FILE) $(INFRA_ENV_FILE) --profile media

# Image scan (defect 29; SE13, spec-09 §9 SEC-SC-01): per image a trivy JSON report (waived findings listed under
# ExperimentalModifiedFindings), a CycloneDX SBOM in $(IMAGE_SCAN_OUT)/sbom/, and the gate: a CRITICAL finding not
# waived in $(IMAGE_WAIVERS) (one statement and review date per CVE; an expired entry no longer waives) fails it.
IMAGE_SCAN_OUT ?= $(HOME)/.noema/image-scan
IMAGE_SCAN_CACHE ?= $(HOME)/.noema/image-scan-cache
IMAGE_SCAN_TRIVY ?= aquasec/trivy:0.75.0
IMAGE_WAIVERS := deploy/docker/.trivyignore.yaml
image_trivy = docker run --rm --user $$(id -u):$$(id -g) --group-add $$(stat -c %g /var/run/docker.sock) \
	-v /var/run/docker.sock:/var/run/docker.sock -e TRIVY_CACHE_DIR=/cache -v $(IMAGE_SCAN_CACHE):/cache \
	-v $(abspath $(IMAGE_SCAN_OUT)):/out -v $(abspath $(IMAGE_WAIVERS)):/waivers.yaml:ro $(IMAGE_SCAN_TRIVY) image --quiet

.PHONY: up down images image-api image-worker image-media image-media-test test-media sandbox-selftest image-scan

up: ## Start the dev stack: base services + PROFILES (default toxiproxy; e.g. PROFILES="toxiproxy media")
	$(COMPOSE) $(foreach p,$(PROFILES),--profile $(p)) up -d --wait

down: ## Stop every dev-stack service of every profile (volumes kept)
	$(COMPOSE) --profile '*' down

images: image-api image-worker image-media ## Build noema-api, noema-worker and noema-media (IMAGE_TAG=dev)
	@docker images --format '{{.Repository}}:{{.Tag}}  {{.Size}}' | grep -E '^noema-(api|worker|media):$(IMAGE_TAG) ' || true

image-api: ## Build noema-api (no media group)
	time $(DOCKER_BUILD) -f deploy/docker/api.Dockerfile -t $(API_IMAGE) .

image-worker: image-api ## Build noema-worker (FROM noema-api)
	time $(DOCKER_BUILD) -f deploy/docker/worker.Dockerfile --build-arg NOEMA_API_IMAGE=$(API_IMAGE) -t $(WORKER_IMAGE) .

image-media: ## Build noema-media (nsjail, tesseract, qpdf, libmagic, media group, Docling models)
	time $(DOCKER_BUILD) -f deploy/docker/media-sandbox.Dockerfile --target media-sandbox -t $(MEDIA_IMAGE) .

image-media-test: ## Build noema-media-test (noema-media + dev/fuzz groups + current src and tests)
	time $(DOCKER_BUILD) -f deploy/docker/media-sandbox.Dockerfile --target media-test -t $(MEDIA_TEST_IMAGE) .

# The tests run under the media-sandbox service's own compose settings (network_mode none, the ADR-002 capabilities,
# the outer seccomp profile, read-only root) in a throwaway project, so concurrent runs never share a spool.
test-media: image-media-test ## Media tests inside the media image, network-less (MEDIA_TESTS=..., PYTEST_ARGS=...)
	@paths="$(strip $(MEDIA_TESTS))"; \
	if [ -z "$$paths" ]; then echo "no media test paths: skipped"; exit 0; fi; \
	project="noema-media-test-$$(od -An -N4 -tx4 /dev/urandom | tr -d ' \n')"; \
	trap '$(call media_compose,"$$project") down -v --remove-orphans >/dev/null 2>&1' EXIT; \
	set +e; \
	NOEMA_MEDIA_IMAGE=$(MEDIA_TEST_IMAGE) $(call media_compose,"$$project") run --rm --no-deps -T \
		$(if $(wildcard $(PROBE_FIXTURES)),-v $(PROBE_FIXTURES):$(PROBE_FIXTURES):ro,) \
		media-sandbox pytest -p no:cacheprovider $$paths $(XDIST_ARG) -m "not live" $(PYTEST_ARGS); \
	rc=$$?; set -e; \
	if [ $$rc -eq 5 ]; then echo "no tests collected in $$paths"; exit 0; fi; exit $$rc

sandbox-selftest: ## ADR-002 §2 checks 1-10 (SEC-SBX-01) in the media image under the compose privileges
	@project="noema-media-selftest-$$(od -An -N4 -tx4 /dev/urandom | tr -d ' \n')"; \
	trap '$(call media_compose,"$$project") down -v --remove-orphans >/dev/null 2>&1' EXIT; \
	NOEMA_MEDIA_IMAGE=$(MEDIA_IMAGE) $(call media_compose,"$$project") run --rm --no-deps -T \
		media-sandbox /etc/noema-sandbox/selftest.sh

image-scan: ## trivy report + CycloneDX SBOM per image (IMAGE_TAG); fails on a CRITICAL not waived in IMAGE_WAIVERS
	@mkdir -p $(IMAGE_SCAN_OUT)/sbom $(IMAGE_SCAN_CACHE)
	@status=0; for img in $(API_IMAGE) $(WORKER_IMAGE) $(MEDIA_IMAGE); do name=$${img%%:*}; \
		$(image_trivy) --scanners vuln --ignorefile /waivers.yaml --show-suppressed --format json \
			--output /out/trivy-$$name.json $$img; \
		$(image_trivy) --format cyclonedx --output /out/sbom/$$name.cdx.json $$img; \
		echo "== $$img: CRITICAL not waived"; \
		$(image_trivy) --scanners vuln --ignorefile /waivers.yaml --severity CRITICAL --exit-code 1 \
			--table-mode detailed $$img || status=1; \
	done; \
	if [ $$status -ne 0 ]; then echo "image-scan FAILED: a CRITICAL finding is not waived in $(IMAGE_WAIVERS)"; fi; \
	exit $$status
