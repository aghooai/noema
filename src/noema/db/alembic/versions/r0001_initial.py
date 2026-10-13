"""0001_initial: the Phase 1 schema of spec-03.

Schemas and the database ACL, every table, index, CHECK and FK, ENABLE + FORCE RLS with the spec-03 §3 policies, the
grants of §1 and the SECURITY DEFINER functions of §4 (owned by noema_definer, search_path pg_catalog, pg_temp,
EXECUTE only for their callers). SQL: `sql/0001_initial/*.sql`.
"""

from noema.db.alembic.runner import drop_noema_schemas, run_sql_scripts

revision = "0001_initial"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    run_sql_scripts("0001_initial")


def downgrade() -> None:
    drop_noema_schemas()  # CI/test only (spec-03 §6); refuses outside NOEMA_ENV dev/test
