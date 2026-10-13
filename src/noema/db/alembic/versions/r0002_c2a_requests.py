"""0002_c2a_requests: the Wave C2a change requests (spec-03 §2, §4).

- labeling 9: `labeling_run_open` keys a running run by question definition (name and signature);
- labeling 4: indexes for `LabelRepo.kept_items` (`label_item_by_decision` already serves `items_for_decision`);
- media-pipeline 2: `noema.derivative.facts` (the pdf.sanitize active-content facts, closed per kind);
- ops-ledger 1: `jobs.finish` returns the job's new state; ops-ledger 5: `ops.retention_page` (keyset cursor);
- identity 1 and 5: `admin.config_body`, `admin.rehash_operator` (noema_cli).

SQL: `sql/0002_c2a_requests/*.sql`; the CI/test-only downgrade runs `sql/0002_c2a_requests/down/*.sql`.
"""

from noema.db.alembic.runner import run_downgrade_scripts, run_sql_scripts

revision = "0002_c2a_requests"
down_revision: str | None = "0001_initial"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    run_sql_scripts("0002_c2a_requests")


def downgrade() -> None:
    run_downgrade_scripts("0002_c2a_requests")  # CI/test only; refuses outside NOEMA_ENV dev/test
