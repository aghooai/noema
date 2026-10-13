# SDK targets (owner: sdk). Both SDKs run offline: TS from the root node_modules, Python in the root venv.
.PHONY: sdk-check sdk-ts-check sdk-py-check sdk-sync
sdk-check: sdk-ts-check sdk-py-check ## Both SDKs: contract sync, typecheck, lint, tests (no network)

sdk-ts-check: ## @aghoo/sdk: vendored contract sync, tsc --strict (type-level checks), vitest + msw
	cd sdk/typescript && npm run check

sdk-py-check: ## aghoo: ruff, mypy --strict, pytest + respx
	$(RUN) ruff check sdk/python
	$(RUN) ruff format --check sdk/python
	cd sdk/python && $(UV) run --project ../.. --frozen --no-sync mypy
	$(call run_pytest,sdk/python,)

sdk-sync: ## Refresh the SDK contract copies from src/noema/contracts (run by contracts-sync and contracts-generate)
	cp src/noema/contracts/_vendor/python_sdk_types.py sdk/python/src/aghoo/_decision_types.py
	cd sdk/typescript && npm run sync
