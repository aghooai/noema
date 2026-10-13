# Database targets (db-schema role; spec-03 §7, spec-10 §1.1). Defaults target the compose Postgres with dev-only
# passwords; override DB_NAME, DB_ADMIN_DSN, DB_OWNER_DSN (or the NOEMA_DB_*_PASSWORD variables) for other databases.
DB_NAME ?= noema
DB_PG_PORT ?= $(or $(NOEMA_PG_PORT),25432)
DB_ADMIN_DSN ?= $(or $(NOEMA_PG_ADMIN_DSN),postgresql://postgres:noema-dev-only@127.0.0.1:$(DB_PG_PORT)/postgres)
DB_OWNER_DSN ?= $(or $(NOEMA_DB_OWNER_DSN),postgresql://noema_owner:$(or $(NOEMA_DB_OWNER_PASSWORD),noema-owner-dev-only)@127.0.0.1:$(DB_PG_PORT)/$(DB_NAME))

.PHONY: db-bootstrap db-migrate db-hash db-reap-test test-db
db-bootstrap: ## Create/repair the four noema roles and the database DB_NAME (superuser DSN; dev passwords in dev/test)
	NOEMA_ENV=$${NOEMA_ENV:-dev} NOEMA_PG_ADMIN_DSN='$(DB_ADMIN_DSN)' NOEMA_DB_NAME='$(DB_NAME)' \
		$(RUN) python -m noema.db.alembic.runner bootstrap

db-migrate: ## alembic upgrade head as noema_owner (never as the app; production is forward-only)
	NOEMA_DB_OWNER_DSN='$(DB_OWNER_DSN)' $(RUN) python -m noema.db.alembic.runner migrate

db-hash: ## Print the migration hash (the test template is noema_tpl_<first 16 hex>)
	@$(RUN) python -m noema.db.alembic.runner hash

db-reap-test: ## Drop stale test databases (noema_t_* older than 6 h or of dead processes; old templates)
	$(RUN) python -m tools.reap_test_dbs

test-db: ## The schema/RLS/definer-function tests (tests/integration/db) against the compose Postgres
	$(call run_pytest,tests/integration/db,-m "not live")
