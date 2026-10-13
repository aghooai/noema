# Stack gates of the QA harness (Wave E2; reports/qa/e2/qa-harness.md): L2/L3/Q8 holdback, L5 DeepSeek backup tail,
# R4 replay set + idempotent replay, Q9 hierarchical deadline share, SE7 media injection on an e2e stack of their own
# (QA_GATES_PROJECT on QA_GATES_PORT_BASE: never noema-e2e or noema-dev), M3 OCR CER inside the media image, and the
# full-tree ownership check. All on the wire mock; never part of make check. Numbers -> QA_GATES_RESULTS (JSON).
#   make qa-gates [QA_GATES_BUILD=0] [QA_GATES_TAG=<images>]     every gate, then the failed ones are listed
#   make qa-load-holdback | qa-load-backup-tail | qa-replay | qa-hier-deadline | qa-media-injection | qa-ocr-cer
#   make qa-gates-down
# The timed drivers read the api containers directly on the compose network (NOEMA_QA_GATES_DIRECT, as the fault
# suite's slow clients do): docker-proxy's userland copy is not part of Noema.
QA_GATES_PROJECT ?= noema-qa-gates
QA_GATES_PORT_BASE ?= 31800
QA_GATES_TAG ?= qa-gates
QA_GATES_BUILD ?= 1
QA_GATES_RESULTS ?= reports/qa/e2/gates-results.json
QA_GATES_OUT ?= $(HOME)/.noema/qa-gates
QA_GATES_STATE = $(HOME)/.noema/stacks/$(QA_GATES_PROJECT).json
# QA_GATES_MOCK_CPUS=<list> pins the compose mock (tools/mockbedrock --cpus); default unpinned. Measured on a shared
# host (Wave F, reports/perf/sizing.md section 10): pinned to 44-45, 10 and 6 of 13 holdback cells invalid by mock
# lateness; unpinned, 3 of 13 on the same stack. Pin only to CPUs nothing else runs on.
QA_GATES_MOCK_CPUS ?=
QA_GATES_IMAGES = NOEMA_API_IMAGE=noema-api:$(QA_GATES_TAG) NOEMA_WORKER_IMAGE=noema-worker:$(QA_GATES_TAG) \
	NOEMA_MEDIA_IMAGE=noema-media:$(QA_GATES_TAG) NOEMA_STACK_MOCK_CPUS=$(QA_GATES_MOCK_CPUS)
QA_GATES_STACK = E2E_PROJECT=$(QA_GATES_PROJECT) E2E_PORT_BASE=$(QA_GATES_PORT_BASE) IMAGE_TAG=$(QA_GATES_TAG)
QA_GATES_PARTS = qa-gates-selftest qa-load-holdback qa-load-backup-tail qa-replay qa-hier-deadline qa-media-injection qa-ocr-cer \
	qa-ownership-all

# pytest on the gate stack: $(1) = test paths, $(2) = extra environment
define qa_gate
	@ips="$$(docker compose -p $(QA_GATES_PROJECT) -f $(COMPOSE_FILE) ps -q api-1 api-2 \
		| xargs docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' | paste -sd, -)"; \
	echo "api containers: $$ips"; mkdir -p $(QA_GATES_OUT); \
	NOEMA_QA_GATES=1 NOEMA_QA_GATES_DIRECT="$$ips" NOEMA_QA_GATES_RESULTS=$(QA_GATES_RESULTS) \
		NOEMA_E2E_STATE=$(QA_GATES_STATE) NOEMA_E2E_PROJECT=$(QA_GATES_PROJECT) NOEMA_E2E_REQUIRE=1 $(2) \
		$(PYTEST) $(1) -p no:cacheprovider -s --allow-hosts=127.0.0.1,::1,$$ips $(PYTEST_ARGS)
endef

.PHONY: qa-gates qa-gates-up qa-gates-down $(QA_GATES_PARTS)

qa-gates-selftest: ## The gates' pure parts (timed driver, validity, lateness, replay script, SE7 checks, CER arithmetic)
	$(PYTEST) tests/load/test_gatekit.py tests/security/test_media_capture_checks.py \
		"tests/qa/media/test_ocr_cer.py::test_cer_arithmetic" -p no:cacheprovider $(PYTEST_ARGS)

qa-gates: qa-gates-up ## Every QA-harness gate (L2/L3/Q8, L5, R4, Q9, SE7, M3, ownership), then the failed ones are listed
	@failed=""; for part in $(QA_GATES_PARTS); do $(MAKE) $$part QA_GATES_BUILD=0 || failed="$$failed $$part"; done; \
	if [ -n "$$failed" ]; then echo "qa-gates FAILED:$$failed"; exit 1; fi; echo "qa-gates: all green"

qa-gates-up: ## Start (or re-check) the gate stack QA_GATES_PROJECT (QA_GATES_BUILD=1 builds the images first)
	$(QA_GATES_IMAGES) $(MAKE) e2e-up $(QA_GATES_STACK) E2E_BUILD=$(QA_GATES_BUILD)

qa-load-holdback: qa-gates-up ## L2/L3 holdback TTFT bounds per (T_d, TTFA) cell, Q8 holdback_deadline/served_spec_differs
	$(call qa_gate,tests/load/test_holdback.py)

qa-load-backup-tail: qa-gates-up ## L5: DeepSeek-only decisions on the probe tail, p99 <= 1.5 s with backups, n >= 5,000
	$(call qa_gate,tests/load/test_backup_tail.py)

qa-replay: qa-gates-up ## R4 replay set (spec restarts, waste; P0 thresholds reported) + idempotent replay at load
	$(call qa_gate,tests/load/test_replay.py)

qa-hier-deadline: qa-gates-up ## Q9: hierarchical deadline_exceeded share at deadline_ms 1,000 vs 3,000 (probe latencies)
	$(call qa_gate,tests/load/test_hier_deadline.py)

qa-media-injection: qa-gates-up ## SE7: one injection corpus in every media view, 0 policy violations (also in qa-security)
	$(call qa_gate,tests/security/stack/test_media_injection.py -m e2e,NOEMA_SEC_ARTIFACTS=$(QA_GATES_OUT))

qa-ocr-cer: ## M3: OCR CER (Tesseract) on the probe fixtures and generated renders, inside the media image
	@mkdir -p $(QA_GATES_OUT)
	$(MAKE) test-media IMAGE_TAG=$(QA_GATES_TAG) MEDIA_TESTS=tests/qa/media PYTEST_ARGS="-s -p no:cacheprovider" \
		| tee $(QA_GATES_OUT)/ocr-cer.log
	@grep -q '^\[gate\] M3 ' $(QA_GATES_OUT)/ocr-cer.log || { echo "M3 did not run (skipped?)"; exit 1; }

qa-ownership-all: ## Every tracked path (git ls-files, read-only) has an OWNERS.yaml owner, not only the changed ones
	@set +e; out="$$($(RUN) python tools/check_ownership.py --paths $$(git ls-files) 2>&1)"; rc=$$?; set -e; \
	echo "$$out" | grep -v '^ok ' || true; \
	echo "tracked paths: $$(git ls-files | wc -l), unowned: $$(echo "$$out" | grep -c UNOWNED || true)"; exit $$rc

qa-gates-down: ## Stop the gate stack and delete its volumes
	$(QA_GATES_IMAGES) $(MAKE) e2e-down $(QA_GATES_STACK)
