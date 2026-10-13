# Noema developer entry points. Every fragment in make/*.mk is included (each role may add its own, e.g. make/db.mk).
# `make help` lists the targets. Tools run inside the project venv without re-syncing it (uv run --frozen --no-sync):
# agents never add dependencies, and a plain `uv sync` would drop the media group from the dev venv.
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help
MAKEFLAGS += --no-print-directory

include $(sort $(wildcard make/*.mk))

.PHONY: check
CHECK_STEPS := lint typecheck typecheck-tests importlint forbidden-scan ownership contracts-check test-unit \
	test-conformance test-integration sdk-check
check: $(CHECK_STEPS) ## Everything the wave gates run (CHECK_STEPS, in order)
	@echo "make check: all green"
